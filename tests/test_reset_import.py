from __future__ import annotations

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
        gmail_search_query="from:citi.example.com",
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


class ResetImportStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "test.db"
        self.global_db = Database(db_path)
        self.tenant_id = self.global_db.get_default_tenant_id()
        self.db = Database(db_path, tenant_id=self.tenant_id)
        self.global_db.update_tenant_settings(
            self.tenant_id,
            gmail_search_query="from:legacy.example.com",
            income_gmail_search_query="from:bank.example.com",
        )
        self.bucket = self.db.create_bucket("Mortgage")
        self.global_db.create_user("test@example.com", "hash", self.tenant_id)
        self.db.insert_expense(
            gmail_message_id="msg-1",
            transaction_date=date(2025, 6, 1),
            merchant="Shop",
            merchant_normalized="shop",
            amount=10.0,
            currency="USD",
            bucket_id=self.bucket.id,
            suggested_bucket_id=self.bucket.id,
            status=ExpenseStatus.AUTO,
            email_subject="Alert",
            email_from="bank@example.com",
        )
        self.db.upsert_merchant_rule(
            merchant_pattern="Shop",
            bucket_id=self.bucket.id,
            match_type=MatchType.EXACT,
            confirmed_by_user=True,
        )
        self.db.create_email_query(name="Bank", query="from:bank.example.com")
        self.db.create_income_rule(match_text="PAYROLL", source_name="Payroll")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reset_clears_import_data_and_rules_keeps_users_and_buckets(self) -> None:
        counts = self.global_db.reset_import_state(self.tenant_id)
        self.assertGreater(counts["expenses"], 0)
        self.assertEqual(len(self.db.list_expenses()), 0)
        self.assertTrue(any(query.name == "Bank" for query in self.db.list_email_queries()))
        self.assertEqual(len(self.db.list_merchant_rules()), 0)
        self.assertEqual(len(self.db.list_income_rules()), 0)
        self.assertEqual(self.global_db.count_users(), 1)
        self.assertEqual(len(self.db.list_buckets()), 1)
        tenant = self.global_db.get_tenant(self.tenant_id)
        self.assertEqual(tenant.gmail_search_query, "")
        self.assertEqual(tenant.income_gmail_search_query, "")


class EmailQuerySyncSkipTests(unittest.TestCase):
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

    def test_no_configured_queries_skips_gmail_fetch(self) -> None:
        class StubGmail:
            fetch_calls: list[str] = []

            def has_token(self) -> bool:
                return True

            def authenticate(self) -> None:
                pass

            def fetch_messages(self, query: str) -> list[dict]:
                StubGmail.fetch_calls.append(query)
                return []

        stub = StubGmail()
        StubGmail.fetch_calls = []
        sync = ExpenseSyncService(
            self.settings,
            self.db,
            stub,
            BucketMatcher(self.db),
            tenant=self.tenant,
        )
        result = sync.sync(record_notification=False)

        self.assertEqual(result["messages_checked"], 0)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(StubGmail.fetch_calls, [])

    def test_query_without_match_text_skips_fetch(self) -> None:
        class StubGmail:
            fetch_calls: list[str] = []

            def has_token(self) -> bool:
                return True

            def authenticate(self) -> None:
                pass

            def fetch_messages(self, query: str) -> list[dict]:
                StubGmail.fetch_calls.append(query)
                return []

        self.db.create_email_query(
            name="Incomplete",
            query="from:bank.example.com",
            match_text="",
        )
        stub = StubGmail()
        StubGmail.fetch_calls = []
        sync = ExpenseSyncService(
            self.settings,
            self.db,
            stub,
            BucketMatcher(self.db),
            tenant=self.tenant,
        )
        result = sync.sync(record_notification=False)

        self.assertEqual(result["messages_checked"], 0)
        self.assertEqual(StubGmail.fetch_calls, [])


if __name__ == "__main__":
    unittest.main()
