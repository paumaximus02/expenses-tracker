from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from expenses_tracker.config import NotifyEmailPolicy, Settings
from expenses_tracker.db import Database
from expenses_tracker.models import ExpenseStatus
from expenses_tracker.web import create_app


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


class YtdDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "test.db"
        global_db = Database(db_path)
        tenant_id = global_db.get_default_tenant_id()
        self.db = Database(db_path, tenant_id=tenant_id)
        self.bucket = self.db.create_bucket("Groceries")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _insert_expense(
        self,
        *,
        message_id: str,
        transaction_date: date,
        amount: float,
        card_holder: str | None = "Juan",
        bucket_id: int | None = None,
    ) -> int:
        return self.db.insert_expense(
            gmail_message_id=message_id,
            transaction_date=transaction_date,
            merchant="Store",
            merchant_normalized="store",
            amount=amount,
            currency="USD",
            bucket_id=bucket_id if bucket_id is not None else self.bucket.id,
            suggested_bucket_id=None,
            status=ExpenseStatus.CONFIRMED,
            email_subject="Alert",
            email_from="bank@example.com",
            card_holder=card_holder,
        )

    def test_ytd_expense_and_income_range_totals(self) -> None:
        self._insert_expense(
            message_id="e-jan",
            transaction_date=date(2026, 1, 10),
            amount=40.0,
        )
        self._insert_expense(
            message_id="e-mar",
            transaction_date=date(2026, 3, 5),
            amount=60.0,
        )
        self._insert_expense(
            message_id="e-aug",
            transaction_date=date(2026, 8, 1),
            amount=100.0,
        )
        self._insert_expense(
            message_id="e-prev-year",
            transaction_date=date(2025, 12, 20),
            amount=25.0,
        )

        income_bucket = self.db.create_income_bucket("Salary")
        self.db.insert_income(
            gmail_message_id="i-feb",
            received_date=date(2026, 2, 1),
            allocated_month="2026-02",
            source="Pay",
            amount=1000.0,
            bucket_id=income_bucket.id,
            person="Juan",
        )
        self.db.insert_income(
            gmail_message_id="i-jul",
            received_date=date(2026, 7, 1),
            allocated_month="2026-07",
            source="Pay",
            amount=500.0,
            bucket_id=income_bucket.id,
            person="Juan",
        )

        expense_totals = self.db.ytd_expense_totals("2026-03")
        self.assertEqual(sum(row[2] for row in expense_totals), 100.0)

        income_totals = self.db.ytd_income_totals("2026-03")
        self.assertEqual(sum(row[2] for row in income_totals), 1000.0)

        juan_expenses = self.db.ytd_expense_totals("2026-03", card_holder="Juan")
        self.assertEqual(sum(row[2] for row in juan_expenses), 100.0)

    def test_monthly_nets_for_ytd_zero_fills_and_year_boundary(self) -> None:
        self._insert_expense(
            message_id="e-jan",
            transaction_date=date(2026, 1, 15),
            amount=50.0,
        )
        self._insert_expense(
            message_id="e-prev",
            transaction_date=date(2025, 12, 15),
            amount=999.0,
        )
        self.db.insert_income(
            gmail_message_id="i-mar",
            received_date=date(2026, 3, 1),
            allocated_month="2026-03",
            source="Pay",
            amount=200.0,
            person="Juan",
        )

        nets = self.db.monthly_nets_for_ytd("2026-03")
        self.assertEqual([row["month"] for row in nets], ["2026-01", "2026-02", "2026-03"])
        self.assertEqual(nets[0]["label"], "Jan")
        self.assertEqual(nets[0]["expenses"], 50.0)
        self.assertEqual(nets[0]["income"], 0.0)
        self.assertEqual(nets[0]["net"], -50.0)
        self.assertEqual(nets[1]["expenses"], 0.0)
        self.assertEqual(nets[1]["income"], 0.0)
        self.assertEqual(nets[1]["net"], 0.0)
        self.assertEqual(nets[2]["income"], 200.0)
        self.assertEqual(nets[2]["expenses"], 0.0)
        self.assertEqual(nets[2]["net"], 200.0)


    def test_list_expenses_month_from_range(self) -> None:
        self._insert_expense(
            message_id="e-jan",
            transaction_date=date(2026, 1, 10),
            amount=40.0,
        )
        self._insert_expense(
            message_id="e-mar",
            transaction_date=date(2026, 3, 5),
            amount=60.0,
        )
        self._insert_expense(
            message_id="e-aug",
            transaction_date=date(2026, 8, 1),
            amount=100.0,
        )
        ranged = self.db.list_expenses(month="2026-03", month_from="2026-01")
        self.assertEqual(len(ranged), 2)
        self.assertEqual(sum(e.amount for e in ranged), 100.0)


class YtdReportRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "test.db"
        global_db = Database(db_path)
        tenant_id = global_db.get_default_tenant_id()
        self.db = Database(db_path, tenant_id=tenant_id)
        self.bucket = self.db.create_bucket("Groceries")
        self.db.insert_expense(
            gmail_message_id="e1",
            transaction_date=date(2026, 2, 10),
            merchant="Store",
            merchant_normalized="store",
            amount=25.0,
            currency="USD",
            bucket_id=self.bucket.id,
            suggested_bucket_id=None,
            status=ExpenseStatus.CONFIRMED,
            email_subject="Alert",
            email_from="bank@example.com",
            card_holder="Juan",
        )
        app = create_app(_settings(db_path))
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_report_ytd_view_smoke(self) -> None:
        response = self.client.get("/report?view=ytd&month=2026-07")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Year-to-date report", response.data)
        self.assertIn(b"Jan\xe2\x80\x93Jul 2026", response.data)
        self.assertIn(b'id="report-ytd-net-chart"', response.data)
        self.assertIn(b"Year to date", response.data)
        self.assertIn(b"month_from=2026-01", response.data)

    def test_report_month_view_has_no_ytd_chart(self) -> None:
        response = self.client.get("/report?view=month&month=2026-07")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Monthly report", response.data)
        self.assertNotIn(b'id="report-ytd-net-chart"', response.data)
        self.assertNotIn(b"month_from=2026-01", response.data)

    def test_expenses_ytd_range_lists_across_months(self) -> None:
        self.db.insert_expense(
            gmail_message_id="e-jan",
            transaction_date=date(2026, 1, 5),
            merchant="Store",
            merchant_normalized="store",
            amount=10.0,
            currency="USD",
            bucket_id=self.bucket.id,
            suggested_bucket_id=None,
            status=ExpenseStatus.CONFIRMED,
            email_subject="Alert",
            email_from="bank@example.com",
            card_holder="Juan",
        )
        response = self.client.get(
            f"/expenses?month=2026-07&month_from=2026-01&bucket_id={self.bucket.id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Jan\xe2\x80\x93Jul 2026", response.data)
        self.assertIn(b"$25.00", response.data)
        self.assertIn(b"$10.00", response.data)


if __name__ == "__main__":
    unittest.main()
