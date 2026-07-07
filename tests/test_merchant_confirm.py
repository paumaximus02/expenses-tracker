from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import date
from pathlib import Path

from expenses_tracker.bucket_matcher import BucketMatcher
from expenses_tracker.config import NotifyEmailPolicy, Settings
from expenses_tracker.db import Database
from expenses_tracker.models import ExpenseStatus, MatchType
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


class MerchantConfirmTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "test.db"
        global_db = Database(db_path)
        self.tenant_id = global_db.get_default_tenant_id()
        self.tenant = global_db.get_tenant(self.tenant_id)
        self.db = Database(db_path, tenant_id=self.tenant_id)
        self.settings = _settings(db_path)
        self.bucket = self.db.create_bucket("Groceries")
        self.sync = ExpenseSyncService(
            self.settings,
            self.db,
            _StubGmail({}),
            BucketMatcher(self.db),
            tenant=self.tenant,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _insert_pending(self, *, message_id: str, merchant: str) -> int:
        return self.db.insert_expense(
            gmail_message_id=message_id,
            transaction_date=date(2025, 6, 1),
            merchant=merchant,
            merchant_normalized=merchant.lower(),
            amount=10.0,
            currency="USD",
            bucket_id=None,
            suggested_bucket_id=None,
            status=ExpenseStatus.PENDING,
            email_subject="Alert",
            email_from="bank@example.com",
        )


class ConfirmBatchTests(MerchantConfirmTestCase):
    def test_confirm_with_rule_updates_matching_pending(self) -> None:
        first_id = self._insert_pending(message_id="m1", merchant="Costco")
        self._insert_pending(message_id="m2", merchant="Costco")
        self._insert_pending(message_id="m3", merchant="Target")

        batch_updated = self.sync.confirm_expense_by_id(
            first_id,
            self.bucket.id,
            create_rule=True,
        )

        self.assertEqual(batch_updated, 1)
        expenses = self.db.list_expenses()
        confirmed = [expense for expense in expenses if expense.status == ExpenseStatus.CONFIRMED]
        self.assertEqual(len(confirmed), 2)
        self.assertEqual(confirmed[0].bucket_id, self.bucket.id)
        self.assertEqual(len(self.db.list_merchant_rules()), 1)

    def test_confirm_without_rule_only_updates_one(self) -> None:
        first_id = self._insert_pending(message_id="m1", merchant="Costco")
        self._insert_pending(message_id="m2", merchant="Costco")

        batch_updated = self.sync.confirm_expense_by_id(
            first_id,
            self.bucket.id,
            create_rule=False,
        )

        self.assertEqual(batch_updated, 0)
        pending = self.db.list_expenses(status=ExpenseStatus.PENDING)
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(self.db.list_merchant_rules()), 0)


class SyncMerchantRuleTests(MerchantConfirmTestCase):
    def test_sync_applies_existing_merchant_rule(self) -> None:
        self.db.upsert_merchant_rule(
            merchant_pattern="Costco",
            bucket_id=self.bucket.id,
            match_type=MatchType.EXACT,
            confirmed_by_user=True,
        )
        self.db.create_email_query(
            name="Bank",
            query="from:bank",
            kind="expense",
            match_text="Costco",
            merchant_label="Merchant",
            amount_label="Amount",
        )
        body = "Merchant: Costco\nAmount: $12.34"
        message = _gmail_message(message_id="sync-1", subject="Alert", body=body)
        self.sync.gmail = _StubGmail({"from:bank": [message]})

        result = self.sync.sync(record_notification=False)

        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["auto_assigned"], 1)
        self.assertEqual(result["pending"], 0)
        expense = self.db.list_expenses()[0]
        self.assertEqual(expense.bucket_id, self.bucket.id)
        self.assertEqual(expense.status, ExpenseStatus.AUTO)

    def test_query_bucket_overrides_merchant_rule(self) -> None:
        other_bucket = self.db.create_bucket("Gas")
        self.db.upsert_merchant_rule(
            merchant_pattern="Costco",
            bucket_id=self.bucket.id,
            match_type=MatchType.EXACT,
            confirmed_by_user=True,
        )
        self.db.create_email_query(
            name="Bank",
            query="from:bank",
            kind="expense",
            match_text="Costco",
            merchant_label="Merchant",
            amount_label="Amount",
            expense_bucket_id=other_bucket.id,
        )
        body = "Merchant: Costco\nAmount: $12.34"
        message = _gmail_message(message_id="sync-2", subject="Alert", body=body)
        self.sync.gmail = _StubGmail({"from:bank": [message]})

        self.sync.sync(record_notification=False)

        expense = self.db.list_expenses()[0]
        self.assertEqual(expense.bucket_id, other_bucket.id)


class _StubGmail:
    def __init__(self, by_query: dict[str, list[dict]]) -> None:
        self.by_query = by_query

    def has_token(self) -> bool:
        return True

    def authenticate(self) -> None:
        pass

    def fetch_messages(self, query: str, max_results: int | None = None) -> list[dict]:
        messages = self.by_query.get(query, [])
        if max_results is not None:
            return messages[:max_results]
        return messages


if __name__ == "__main__":
    unittest.main()
