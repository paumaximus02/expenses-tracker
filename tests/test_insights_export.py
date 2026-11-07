from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from expenses_tracker.config import NotifyEmailPolicy, Settings
from expenses_tracker.db import Database
from expenses_tracker.export_csv import build_export_rows, render_csv
from expenses_tracker.gmail_client import GmailClient
from expenses_tracker.insights import (
    InsightsError,
    build_insight_payload,
    clean_api_key,
    request_insights,
)
from expenses_tracker.models import ExpenseStatus, Tenant
from expenses_tracker.web import create_app

API_KEY = "sk-test-secret-key-9876"
EMAIL_SECRET = "SECRET-EMAIL-SUBJECT-DO-NOT-SEND"


def _settings(db_path: Path) -> Settings:
    return Settings(
        gmail_credentials_path=Path("credentials.json"),
        gmail_credentials_json=None,
        gmail_token_path=Path("token.json"),
        gmail_token_json=None,
        gmail_search_query="test",
        database_path=db_path,
        card_holders={"4149": "Juan"},
        secret_key="test",
        auth_disabled=True,
        allow_signup=True,
        session_cookie_secure=False,
        sync_stale_hours=6,
        sync_interval_seconds=3600,
        notifications_enabled=False,
        notify_email_enabled=False,
        notify_email_debug=False,
        notify_email_policy=NotifyEmailPolicy.ON_IMPORT,
        app_base_url="http://127.0.0.1:5000",
        app_timezone="America/New_York",
        cron_secret="secret",
        smtp_host=None,
        smtp_port=587,
        smtp_user=None,
        smtp_password=None,
        smtp_from=None,
        smtp_use_tls=True,
    )


def _tenant(**overrides) -> Tenant:
    values = dict(
        id=1,
        name="Home",
        invite_code="ABCD1234",
        card_holders={"4149": "Juan"},
        gmail_search_query="",
        gmail_token_json=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        insights_api_base="https://api.openai.com/v1",
        insights_model="gpt-4o-mini",
        insights_api_key=API_KEY,
    )
    values.update(overrides)
    return Tenant(**values)


class ExportAndInsightDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "test.db"
        global_db = Database(db_path)
        self.db = Database(db_path, tenant_id=global_db.get_default_tenant_id())
        self.groceries = self.db.create_bucket("Groceries")
        self.salary = self.db.create_income_bucket("Salary")
        self._seed()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _expense(
        self,
        message_id: str,
        when: date,
        amount: float,
        *,
        holder: str = "Juan",
        merchant: str = "Store",
        exclude: bool = False,
    ) -> None:
        expense_id = self.db.insert_expense(
            gmail_message_id=message_id,
            transaction_date=when,
            merchant=merchant,
            merchant_normalized=merchant.lower(),
            amount=amount,
            currency="USD",
            bucket_id=self.groceries.id,
            suggested_bucket_id=None,
            status=ExpenseStatus.CONFIRMED,
            email_subject=EMAIL_SECRET,
            email_from="bank@example.com",
            card_holder=holder,
        )
        if exclude:
            self.db.update_expense(expense_id, exclude_from_report=True)

    def _seed(self) -> None:
        self._expense("e-jan", date(2026, 1, 10), 40.0)
        self._expense("e-mar", date(2026, 3, 5), 10.0, merchant="Reimbursed Hotel", exclude=True)
        self._expense("e-aug", date(2026, 8, 1), 100.0, merchant="August Store")
        self._expense("e-debora", date(2026, 1, 12), 7.0, holder="Debora", merchant="Debora Store")
        self.db.insert_income(
            gmail_message_id="i-feb",
            received_date=date(2026, 2, 1),
            allocated_month="2026-02",
            source="Pay",
            amount=1000.0,
            bucket_id=self.salary.id,
            person="Juan",
            email_subject=EMAIL_SECRET,
            email_from="payroll@example.com",
        )
        self.db.insert_income(
            gmail_message_id="i-jul",
            received_date=date(2026, 7, 1),
            allocated_month="2026-07",
            source="Bonus",
            amount=500.0,
            bucket_id=self.salary.id,
            person="Juan",
            email_subject=EMAIL_SECRET,
        )

    def test_csv_respects_person_ytd_window_and_keeps_excluded_rows(self) -> None:
        rows = build_export_rows(
            self.db,
            month="2026-03",
            view="ytd",
            person="Juan",
            scope="period",
        )
        rendered = render_csv(rows)
        self.assertTrue(rendered.startswith(
            "type,date,month,person,name,amount,currency,bucket,excluded\n"
        ))
        self.assertEqual(
            [(row["type"], row["date"], row["name"], row["amount"], row["excluded"]) for row in rows],
            [
                ("expense", "2026-01-10", "Store", "40.00", "no"),
                ("income", "2026-02-01", "Pay", "1000.00", "no"),
                ("expense", "2026-03-05", "Reimbursed Hotel", "10.00", "yes"),
            ],
        )
        self.assertEqual(rows[0]["person"], "Juan")
        self.assertEqual(rows[0]["bucket"], "Groceries")
        self.assertEqual(rows[1]["month"], "2026-02")
        self.assertEqual(rows[1]["bucket"], "Salary")
        self.assertNotIn("Debora Store", rendered)
        self.assertNotIn("August Store", rendered)
        self.assertNotIn("Bonus", rendered)
        self.assertNotIn(EMAIL_SECRET, rendered)

    def test_all_history_drops_the_date_window_and_keeps_person(self) -> None:
        rows = build_export_rows(
            self.db,
            month="2026-03",
            view="month",
            person="Juan",
            scope="all",
        )
        names = [row["name"] for row in rows]
        self.assertIn("August Store", names)
        self.assertIn("Bonus", names)
        self.assertNotIn("Debora Store", names)

    def test_insight_payload_omits_email_fields_and_excluded_lines(self) -> None:
        payload = build_insight_payload(
            self.db,
            month="2026-03",
            view="ytd",
            person="Juan",
        )
        encoded = json.dumps(payload)
        self.assertNotIn(EMAIL_SECRET, encoded)
        self.assertNotIn("email_subject", encoded)
        self.assertNotIn("email_from", encoded)
        self.assertNotIn("gmail", encoded)
        names = [line["name"] for line in payload["lines"]]
        self.assertEqual(names, ["Store", "Pay"])
        self.assertNotIn("Reimbursed Hotel", names)
        self.assertEqual(payload["expense_total"], 40.0)
        self.assertEqual(payload["income_total"], 1000.0)
        self.assertEqual(payload["net"], 960.0)
        self.assertEqual(payload["excluded_expense_total"], 10.0)
        self.assertEqual(payload["omitted_line_count"], 0)
        self.assertEqual(payload["person"], "Juan")
        self.assertEqual(payload["lines"][0]["bucket"], "Groceries")
        self.assertEqual(payload["lines"][0]["person"], "Juan")

    def test_insight_payload_keeps_largest_lines_when_over_cap(self) -> None:
        payload = build_insight_payload(
            self.db,
            month="2026-03",
            view="ytd",
            person="Juan",
            line_cap=1,
        )
        self.assertEqual(payload["omitted_line_count"], 1)
        self.assertEqual(len(payload["lines"]), 1)
        self.assertEqual(payload["lines"][0]["name"], "Pay")
        self.assertEqual(payload["lines"][0]["amount"], 1000.0)
        self.assertEqual(payload["expense_total"], 40.0)


class InsightsClientTests(unittest.TestCase):
    def test_clean_api_key_removes_wrapping_and_whitespace(self) -> None:
        self.assertEqual(clean_api_key('  Bearer "xai-abc def" \n'), "xai-abcdef")

    def test_xai_base_rejects_a_key_that_does_not_start_with_xai(self) -> None:
        with patch("expenses_tracker.insights.urllib.request.urlopen") as urlopen:
            with self.assertRaises(ValueError) as caught:
                request_insights(
                    _tenant(
                        insights_api_base="https://api.x.ai/v1",
                        insights_api_key="sk-test-secret-key-9876",
                    ),
                    {"period": "March 2026"},
                )
        urlopen.assert_not_called()
        self.assertIn("xai-", str(caught.exception))

    def test_missing_key_does_not_call_the_service(self) -> None:
        with patch("expenses_tracker.insights.urllib.request.urlopen") as urlopen:
            with self.assertRaises(InsightsError) as caught:
                request_insights(_tenant(insights_api_key=None), {"period": "March 2026"})
        urlopen.assert_not_called()
        self.assertIn("API key", str(caught.exception))

    def test_request_returns_briefing_text(self) -> None:
        captured: dict[str, object] = {}

        class _Response:
            def read(self) -> bytes:
                return json.dumps(
                    {"choices": [{"message": {"content": "  Groceries led the month.  "}}]}
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> bool:
                return False

        def _urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["body"] = request.data.decode()
            return _Response()

        with patch("expenses_tracker.insights.urllib.request.urlopen", side_effect=_urlopen):
            text = request_insights(_tenant(), {"period": "March 2026", "lines": []})

        self.assertEqual(text, "Groceries led the month.")
        self.assertEqual(captured["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(captured["timeout"], 30)
        self.assertNotIn(API_KEY, str(captured["body"]))
        self.assertNotIn("temperature", str(captured["body"]))

    def test_http_error_does_not_echo_the_key(self) -> None:
        def _urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                Message(),
                io.BytesIO(f"rejected {API_KEY}".encode()),
            )

        with patch("expenses_tracker.insights.urllib.request.urlopen", side_effect=_urlopen):
            with self.assertRaises(InsightsError) as caught:
                request_insights(_tenant(), {"period": "March 2026"})
        message = str(caught.exception)
        self.assertIn("HTTP 401", message)
        self.assertNotIn(API_KEY, message)

    def test_http_error_includes_a_safe_provider_message(self) -> None:
        def _urlopen(request, timeout=None):
            body = json.dumps({"error": {"message": "temperature is not supported"}}).encode()
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "Bad Request",
                Message(),
                io.BytesIO(body),
            )

        with patch("expenses_tracker.insights.urllib.request.urlopen", side_effect=_urlopen):
            with self.assertRaises(InsightsError) as caught:
                request_insights(_tenant(), {"period": "March 2026"})
        self.assertIn("temperature is not supported", str(caught.exception))


class InsightsAndExportRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        app = create_app(_settings(self.db_path))
        app.config["TESTING"] = True
        self.client = app.test_client()
        global_db = Database(self.db_path)
        self.tenant_id = global_db.get_default_tenant_id()
        self.db = Database(self.db_path, tenant_id=self.tenant_id)
        bucket = self.db.create_bucket("Groceries")
        self.db.insert_expense(
            gmail_message_id="e1",
            transaction_date=date(2026, 3, 2),
            merchant="Market",
            merchant_normalized="market",
            amount=12.5,
            currency="USD",
            bucket_id=bucket.id,
            suggested_bucket_id=None,
            status=ExpenseStatus.CONFIRMED,
            email_subject=EMAIL_SECRET,
            email_from="bank@example.com",
            card_holder="Juan",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_export_route_downloads_the_current_month(self) -> None:
        response = self.client.get("/report/export.csv?month=2026-03&view=month&scope=period&preview=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.content_type)
        self.assertIn('filename="expenses-2026-03.csv"', response.headers["Content-Disposition"])
        body = response.data.decode()
        self.assertIn("expense,2026-03-02,2026-03,Juan,Market,12.50,USD,Groceries,no", body)
        self.assertNotIn(EMAIL_SECRET, body)

        ytd = self.client.get("/report/export.csv?month=2026-03&view=ytd&scope=period")
        self.assertIn('filename="expenses-2026-ytd.csv"', ytd.headers["Content-Disposition"])
        history = self.client.get("/report/export.csv?scope=all")
        self.assertIn('filename="expenses-all.csv"', history.headers["Content-Disposition"])

    def test_report_links_to_settings_until_a_key_is_saved(self) -> None:
        page = self.client.get("/report?month=2026-03&preview=1")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Download CSV", page.data)
        self.assertIn(b"All history", page.data)
        self.assertIn(b"Add an API key in Settings", page.data)
        self.assertNotIn(b'id="report-insights-button"', page.data)

    def test_settings_save_stores_the_key_and_shows_only_the_mask(self) -> None:
        with patch.object(GmailClient, "verify_connection", return_value=("disconnected", None)):
            response = self.client.post(
                "/settings/insights?preview=1",
                data={
                    "insights_api_base": "https://example.test/v1/",
                    "insights_model": "gpt-4o-mini",
                    "insights_api_key": API_KEY,
                },
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(API_KEY.encode(), response.data)
        self.assertIn("••••9876".encode(), response.data)
        tenant = Database(self.db_path).get_tenant(self.tenant_id)
        assert tenant is not None
        self.assertEqual(tenant.insights_api_key, API_KEY)
        self.assertEqual(tenant.insights_api_base, "https://example.test/v1")
        self.assertEqual(tenant.insights_model, "gpt-4o-mini")

        page = self.client.get("/report?month=2026-03")
        self.assertIn(b'id="report-insights-button"', page.data)
        self.assertNotIn(API_KEY.encode(), page.data)

    def test_insights_route_requires_a_key(self) -> None:
        response = self.client.post(
            "/report/insights?preview=1",
            json={"month": "2026-03", "view": "month", "person": ""},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"API key", response.data)

    def test_invalid_api_base_is_rejected(self) -> None:
        with patch.object(GmailClient, "verify_connection", return_value=("disconnected", None)):
            response = self.client.post(
                "/settings/insights?preview=1",
                data={
                    "insights_api_base": "ftp://files.example",
                    "insights_model": "gpt-4o-mini",
                    "insights_api_key": API_KEY,
                },
                follow_redirects=True,
            )
        self.assertIn(b"API base URL", response.data)
        tenant = Database(self.db_path).get_tenant(self.tenant_id)
        assert tenant is not None
        self.assertIsNone(tenant.insights_api_key)

    def test_xai_settings_reject_a_non_xai_key(self) -> None:
        with patch.object(GmailClient, "verify_connection", return_value=("disconnected", None)):
            response = self.client.post(
                "/settings/insights?preview=1",
                data={
                    "insights_api_base": "https://api.x.ai/v1",
                    "insights_model": "grok-4.7",
                    "insights_api_key": "sk-test-secret-key-9876",
                },
                follow_redirects=True,
            )
        self.assertIn(b"starts with xai-", response.data)
        tenant = Database(self.db_path).get_tenant(self.tenant_id)
        assert tenant is not None
        self.assertIsNone(tenant.insights_api_key)

    def test_preview_flag_gates_the_features(self) -> None:
        hidden = self.client.get("/report?month=2026-03")
        self.assertNotIn(b"Download CSV", hidden.data)
        self.assertNotIn(b"Get insights", hidden.data)
        self.assertEqual(self.client.get("/report/export.csv?month=2026-03").status_code, 404)
        self.assertEqual(self.client.post("/report/insights", json={"month": "2026-03"}).status_code, 404)

        shown = self.client.get("/report?month=2026-03&preview=1")
        self.assertIn(b"Download CSV", shown.data)
        self.assertIn(b"Get insights", shown.data)
        self.assertEqual(
            self.client.get("/report/export.csv?month=2026-03&view=month&scope=period").status_code,
            200,
        )

        with patch.object(GmailClient, "verify_connection", return_value=("disconnected", None)):
            settings_page = self.client.get("/settings")
        self.assertIn(b"AI insights", settings_page.data)

        turned_off = self.client.get("/report?month=2026-03&preview=0")
        self.assertNotIn(b"Download CSV", turned_off.data)
        self.assertEqual(self.client.get("/report/export.csv?month=2026-03").status_code, 404)


if __name__ == "__main__":
    unittest.main()
