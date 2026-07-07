from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import date
from pathlib import Path

from expenses_tracker.bucket_matcher import BucketMatcher
from expenses_tracker.config import NotifyEmailPolicy, Settings
from expenses_tracker.db import Database
from expenses_tracker.email_import import (
    build_context,
    explain_match_failure,
    explain_unparsed,
    preview_email_query,
    query_matches,
)
from expenses_tracker.models import ExpenseStatus
from expenses_tracker.sync import ExpenseSyncService


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
        app_base_url=None,
        app_timezone="America/New_York",
        cron_secret=None,
        smtp_host=None,
        smtp_port=587,
        smtp_user=None,
        smtp_password=None,
        smtp_from=None,
        smtp_use_tls=True,
    )


def _gmail_message(*, message_id: str, subject: str, body: str, sender: str = "alerts@bank.example.com") -> dict:
    encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    return {
        "id": message_id,
        "internalDate": "1751500800000",
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": "Thu, 3 Jul 2025 08:00:00 -0700"},
            ],
            "body": {"data": encoded},
            "mimeType": "text/plain",
        },
    }


LBS_WITHDRAWAL_BODY = (
    "Greetings from LBS Financial Credit Union!\n\n"
    "A transaction just posted to your account.\n\n"
    "Date: 6/2/2025\n"
    "Description: Withdrawal-ACH-A-CMG MORTGAGE INC WEBCMG MORTGAGE INC (CMG MORTGA)\n"
    "Amount: $3,145.20\n"
    "Balance: $9,000.00."
)

LBS_DEPOSIT_BODY = (
    "Greetings from LBS Financial Credit Union!\n\n"
    "A transaction just posted to your account.\n\n"
    "Date: 6/3/2025\n"
    "Description: Deposit-ACH-A-ACME PAYROLL\n"
    "Amount: $2,512.34\n"
    "Balance: $11,512.34."
)


class StubGmail:
    def __init__(self, by_query: dict[str, list[dict]]) -> None:
        self.by_query = by_query
        self.fetched_queries: list[str] = []

    def has_token(self) -> bool:
        return True

    def authenticate(self) -> None:
        pass

    def fetch_messages(self, query: str, max_results: int | None = None) -> list[dict]:
        self.fetched_queries.append(query)
        messages = self.by_query.get(query, [])
        if max_results is not None:
            return messages[:max_results]
        return messages


class EmailQueryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "test.db"
        global_db = Database(db_path)
        self.tenant_id = global_db.get_default_tenant_id()
        self.tenant = global_db.get_tenant(self.tenant_id)
        self.db = Database(db_path, tenant_id=self.tenant_id)
        self.settings = _settings(db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _service(self, by_query: dict[str, list[dict]]):
        return ExpenseSyncService(
            self.settings,
            self.db,
            StubGmail(by_query),
            BucketMatcher(self.db),
            tenant=self.tenant,
        )


class EmailQueryCrudTests(EmailQueryTestCase):
    def test_create_and_update_query(self) -> None:
        bucket = self.db.create_bucket("Mortgage")
        income_bucket = self.db.create_income_bucket("Salary")
        created = self.db.create_email_query(
            name="LBS withdrawals",
            query="from:lbs",
            kind="withdrawal",
            match_text="Description: Withdrawal",
            merchant_label="Description",
            amount_label="Amount",
            expense_bucket_id=bucket.id,
        )
        self.assertEqual(created.kind, "withdrawal")
        self.assertEqual(created.expense_bucket_id, bucket.id)

        updated = self.db.update_email_query(
            created.id,
            name="LBS deposits",
            query="from:lbs",
            enabled=True,
            kind="income",
            match_text="Description: Deposit",
            merchant_label="Description",
            amount_label="Amount",
            income_bucket_id=income_bucket.id,
        )
        self.assertEqual(updated.kind, "income")
        self.assertIsNone(updated.expense_bucket_id)
        self.assertEqual(updated.income_bucket_id, income_bucket.id)


class EmailQuerySyncTests(EmailQueryTestCase):
    def test_withdrawal_imports_as_expense(self) -> None:
        bucket = self.db.create_bucket("Mortgage")
        self.db.create_email_query(
            name="LBS withdrawals",
            query="from:lbs",
            kind="withdrawal",
            match_text="Description: Withdrawal",
            merchant_label="Description",
            amount_label="Amount",
            expense_bucket_id=bucket.id,
        )
        message = _gmail_message(
            message_id="m1",
            subject="Alert",
            body=LBS_WITHDRAWAL_BODY,
            sender="alerts@lbsfcu.org",
        )
        result = self._service({"from:lbs": [message]}).sync(record_notification=False)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["withdrawals_imported"], 1)
        expenses = self.db.list_expenses()
        self.assertEqual(len(expenses), 1)
        self.assertEqual(expenses[0].amount, 3145.20)
        self.assertEqual(expenses[0].bucket_id, bucket.id)

    def test_income_import(self) -> None:
        bucket = self.db.create_income_bucket("Salary")
        self.db.create_email_query(
            name="LBS deposits",
            query="from:lbs",
            kind="income",
            match_text="Description: Deposit",
            merchant_label="Description",
            amount_label="Amount",
            income_bucket_id=bucket.id,
        )
        message = _gmail_message(
            message_id="m2",
            subject="Alert",
            body=LBS_DEPOSIT_BODY,
            sender="alerts@lbsfcu.org",
        )
        result = self._service({"from:lbs": [message]}).sync(record_notification=False)
        self.assertEqual(result["income_imported"], 1)
        incomes = self.db.list_incomes()
        self.assertEqual(len(incomes), 1)
        self.assertEqual(incomes[0].amount, 2512.34)
        self.assertEqual(incomes[0].bucket_id, bucket.id)

    def test_dedup_skips_existing_message(self) -> None:
        bucket = self.db.create_bucket("Mortgage")
        self.db.create_email_query(
            name="LBS withdrawals",
            query="from:lbs",
            kind="withdrawal",
            match_text="Description: Withdrawal",
            merchant_label="Description",
            amount_label="Amount",
            expense_bucket_id=bucket.id,
        )
        message = _gmail_message(
            message_id="m-dup",
            subject="Alert",
            body=LBS_WITHDRAWAL_BODY,
            sender="alerts@lbsfcu.org",
        )
        service = self._service({"from:lbs": [message, message]})
        first = service.sync(record_notification=False)
        second = service.sync(record_notification=False)
        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(len(self.db.list_expenses()), 1)

    def test_non_matching_message_skipped(self) -> None:
        self.db.create_email_query(
            name="Deposits only",
            query="from:lbs",
            kind="income",
            match_text="Description: Deposit",
            merchant_label="Description",
            amount_label="Amount",
        )
        message = _gmail_message(
            message_id="m3",
            subject="Alert",
            body=LBS_WITHDRAWAL_BODY,
            sender="alerts@lbsfcu.org",
        )
        result = self._service({"from:lbs": [message]}).sync(record_notification=False)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["income_imported"], 0)
        self.assertEqual(len(self.db.list_expenses()), 0)
        self.assertEqual(len(self.db.list_incomes()), 0)


class EmailQueryPreviewTests(EmailQueryTestCase):
    def test_preview_extracts_labeled_fields(self) -> None:
        email_query = self.db.create_email_query(
            name="LBS deposits",
            query="from:lbs",
            kind="income",
            match_text="Description: Deposit",
            merchant_label="Description",
            amount_label="Amount",
        )
        context = build_context(
            _gmail_message(
                message_id="preview-1",
                subject="Alert",
                body=LBS_DEPOSIT_BODY,
                sender="alerts@lbsfcu.org",
            )
        )
        self.assertTrue(query_matches(context, email_query))
        preview = preview_email_query(context, email_query)
        self.assertTrue(preview.matched)
        self.assertEqual(preview.amount, 2512.34)
        self.assertIn("Deposit", preview.merchant or "")


CITI_LABELED_BODY = (
    "Amount: $42.15 Card Ending In 4149 Merchant COSTCO WHSE #1234 "
    "Location SILVERDALE US Date 06/15/2026"
)


class EmailQueryPersonModeTests(EmailQueryTestCase):
    def test_from_card_assigns_holder_with_labeled_fields(self) -> None:
        self.db.create_email_query(
            name="Citi Costco",
            query="from:citi",
            kind="expense",
            match_text="COSTCO",
            merchant_label="Merchant",
            amount_label="Amount",
            person_mode="from_card",
        )
        message = _gmail_message(
            message_id="citi-1",
            subject="Transaction alert",
            body=CITI_LABELED_BODY,
            sender="alerts@citibank.com",
        )
        result = self._service({"from:citi": [message]}).sync(record_notification=False)
        self.assertEqual(result["imported"], 1)
        expense = self.db.list_expenses()[0]
        self.assertEqual(expense.card_holder, "Juan")
        self.assertEqual(expense.card_last_four, "4149")
        self.assertEqual(expense.amount, 42.15)

    def test_fixed_person_overrides_card_mapping(self) -> None:
        self.db.create_email_query(
            name="Citi fixed",
            query="from:citi",
            kind="expense",
            match_text="COSTCO",
            merchant_label="Merchant",
            amount_label="Amount",
            person="Debora",
            person_mode="fixed",
        )
        message = _gmail_message(
            message_id="citi-2",
            subject="Transaction alert",
            body=CITI_LABELED_BODY,
            sender="alerts@citibank.com",
        )
        self._service({"from:citi": [message]}).sync(record_notification=False)
        expense = self.db.list_expenses()[0]
        self.assertEqual(expense.card_holder, "Debora")
        self.assertIsNone(expense.card_last_four)

    def test_preview_reports_unmapped_card(self) -> None:
        email_query = self.db.create_email_query(
            name="Citi",
            query="from:citi",
            kind="expense",
            match_text="COSTCO",
            merchant_label="Merchant",
            amount_label="Amount",
            person_mode="from_card",
        )
        context = build_context(
            _gmail_message(
                message_id="citi-3",
                subject="Transaction alert",
                body=CITI_LABELED_BODY.replace("4149", "9999"),
                sender="alerts@citibank.com",
            )
        )
        preview = preview_email_query(context, email_query, card_holders={"4149": "Juan"})
        self.assertIsNone(preview.card_holder)
        self.assertEqual(preview.card_last_four, "9999")
        self.assertIn("not mapped", preview.note or "")


class EmailQueryDebugTests(EmailQueryTestCase):
    def test_explain_match_failure(self) -> None:
        email_query = self.db.create_email_query(
            name="Deposits",
            query="from:lbs",
            kind="income",
            match_text="ACH transaction with amount larger than",
        )
        context = build_context(
            _gmail_message(
                message_id="dbg-1",
                subject="Alert",
                body=LBS_WITHDRAWAL_BODY,
                sender="alerts@lbsfcu.org",
            )
        )
        reason = explain_match_failure(context, email_query)
        self.assertIn("match text", reason)

    def test_explain_unparsed_missing_amount(self) -> None:
        email_query = self.db.create_email_query(
            name="Broken labels",
            query="from:lbs",
            kind="income",
            match_text="No amount here",
            merchant_label="Description",
            amount_label="NotARealLabel",
        )
        context = build_context(
            _gmail_message(
                message_id="dbg-2",
                subject="No amount here",
                body="Description: Deposit-ACH-A-ACME PAYROLL\nBalance: $11,512.34.",
                sender="alerts@lbsfcu.org",
            )
        )
        reason = explain_unparsed(context, email_query)
        self.assertIn("amount", reason)

    def test_debug_logs_include_skip_reasons(self) -> None:
        from dataclasses import replace

        debug_settings = replace(self.settings, email_query_debug=True)
        self.db.create_email_query(
            name="Deposits",
            query="from:lbs",
            kind="income",
            match_text="Description: Deposit",
            merchant_label="Description",
            amount_label="Amount",
        )
        withdrawal = _gmail_message(
            message_id="dbg-3",
            subject="Alert",
            body=LBS_WITHDRAWAL_BODY,
            sender="alerts@lbsfcu.org",
        )
        service = ExpenseSyncService(
            debug_settings,
            self.db,
            StubGmail({"from:lbs": [withdrawal]}),
            BucketMatcher(self.db),
            tenant=self.tenant,
        )
        with self.assertLogs("expenses_tracker.sync", level="INFO") as logs:
            result = service.sync(record_notification=False)
        self.assertEqual(result["imported"], 0)
        output = "\n".join(logs.output)
        self.assertIn("[email-query]", output)
        self.assertIn("Running query", output)
        self.assertIn("match text", output)


if __name__ == "__main__":
    unittest.main()
