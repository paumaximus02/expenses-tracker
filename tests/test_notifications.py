from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from expenses_tracker.config import NotifyEmailPolicy, Settings
from expenses_tracker.db import Database
from expenses_tracker.delivery.events import SyncCompletedEvent, SyncTransactionSummary
from expenses_tracker.delivery.policy import should_send_sync_email
from expenses_tracker.models import ExpenseStatus, User
from expenses_tracker.notifications import format_sync_email_subject, format_sync_email_text
from expenses_tracker.scheduled_sync import run_scheduled_sync_for_tenant


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
        notifications_enabled=True,
        notify_email_enabled=True,
        notify_email_debug=False,
        notify_email_policy=NotifyEmailPolicy.ON_IMPORT,
        app_base_url="http://127.0.0.1:5000",
        app_timezone="America/New_York",
        cron_secret="secret",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user",
        smtp_password="pass",
        smtp_from="expenses@example.com",
        smtp_use_tls=True,
    )


class NotificationPolicyTests(unittest.TestCase):
    def test_on_import_requires_new_transactions(self) -> None:
        settings = _settings(Path("data/expenses.db"))
        event = SyncCompletedEvent(
            tenant_id=1,
            tenant_name="Home",
            synced_at=datetime.now(timezone.utc),
            messages_checked=1,
            imported=0,
            auto_assigned=0,
            pending=0,
            skipped=1,
        )
        self.assertFalse(should_send_sync_email(settings, event))

        event.imported = 2
        self.assertTrue(should_send_sync_email(settings, event))

    def test_debug_sends_on_empty_sync(self) -> None:
        base = _settings(Path("data/expenses.db"))
        settings = Settings(
            gmail_credentials_path=base.gmail_credentials_path,
            gmail_credentials_json=base.gmail_credentials_json,
            gmail_token_path=base.gmail_token_path,
            gmail_token_json=base.gmail_token_json,
            gmail_search_query=base.gmail_search_query,
            database_path=base.database_path,
            card_holders=base.card_holders,
            secret_key=base.secret_key,
            auth_disabled=base.auth_disabled,
            allow_signup=base.allow_signup,
            session_cookie_secure=base.session_cookie_secure,
            sync_stale_hours=base.sync_stale_hours,
            sync_interval_seconds=base.sync_interval_seconds,
            notifications_enabled=base.notifications_enabled,
            notify_email_enabled=base.notify_email_enabled,
            notify_email_debug=True,
            notify_email_policy=NotifyEmailPolicy.ON_IMPORT,
            app_base_url=base.app_base_url,
            app_timezone=base.app_timezone,
            cron_secret=base.cron_secret,
            smtp_host=base.smtp_host,
            smtp_port=base.smtp_port,
            smtp_user=base.smtp_user,
            smtp_password=base.smtp_password,
            smtp_from=base.smtp_from,
            smtp_use_tls=base.smtp_use_tls,
        )
        event = SyncCompletedEvent(
            tenant_id=1,
            tenant_name="Home",
            synced_at=datetime.now(timezone.utc),
            messages_checked=5,
            imported=0,
            auto_assigned=0,
            pending=0,
            skipped=5,
        )
        self.assertTrue(should_send_sync_email(settings, event))

    def test_on_error_only_when_failed(self) -> None:
        base = _settings(Path("data/expenses.db"))
        settings = Settings(
            gmail_credentials_path=base.gmail_credentials_path,
            gmail_credentials_json=base.gmail_credentials_json,
            gmail_token_path=base.gmail_token_path,
            gmail_token_json=base.gmail_token_json,
            gmail_search_query=base.gmail_search_query,
            database_path=base.database_path,
            card_holders=base.card_holders,
            secret_key=base.secret_key,
            auth_disabled=base.auth_disabled,
            allow_signup=base.allow_signup,
            session_cookie_secure=base.session_cookie_secure,
            sync_stale_hours=base.sync_stale_hours,
            sync_interval_seconds=base.sync_interval_seconds,
            notifications_enabled=base.notifications_enabled,
            notify_email_enabled=base.notify_email_enabled,
            notify_email_debug=False,
            notify_email_policy=NotifyEmailPolicy.ON_ERROR,
            app_base_url=base.app_base_url,
            app_timezone=base.app_timezone,
            cron_secret=base.cron_secret,
            smtp_host=base.smtp_host,
            smtp_port=base.smtp_port,
            smtp_user=base.smtp_user,
            smtp_password=base.smtp_password,
            smtp_from=base.smtp_from,
            smtp_use_tls=base.smtp_use_tls,
        )
        success = SyncCompletedEvent(
            tenant_id=1,
            tenant_name="Home",
            synced_at=datetime.now(timezone.utc),
            messages_checked=1,
            imported=1,
            auto_assigned=1,
            pending=0,
            skipped=0,
        )
        failure = SyncCompletedEvent(
            tenant_id=1,
            tenant_name="Home",
            synced_at=datetime.now(timezone.utc),
            messages_checked=0,
            imported=0,
            auto_assigned=0,
            pending=0,
            skipped=0,
            error="boom",
        )
        self.assertFalse(should_send_sync_email(settings, success))
        self.assertTrue(should_send_sync_email(settings, failure))


class EmailFormattingTests(unittest.TestCase):
    def test_email_body_groups_auto_and_pending(self) -> None:
        event = SyncCompletedEvent(
            tenant_id=1,
            tenant_name="Home",
            synced_at=datetime.now(timezone.utc),
            messages_checked=2,
            imported=2,
            auto_assigned=1,
            pending=1,
            skipped=0,
            review_url="http://127.0.0.1:5000/review/sync/5",
            auto_assigned_transactions=[
                SyncTransactionSummary(
                    merchant="Costco",
                    amount=50.0,
                    currency="USD",
                    transaction_date=date(2026, 5, 1),
                    card_holder="Juan",
                    bucket_name="Groceries",
                    suggested_bucket_name="Groceries",
                    status=ExpenseStatus.AUTO,
                )
            ],
            pending_transactions=[
                SyncTransactionSummary(
                    merchant="Unknown Shop",
                    amount=12.5,
                    currency="USD",
                    transaction_date=date(2026, 5, 2),
                    card_holder="Debora",
                    bucket_name=None,
                    suggested_bucket_name="Shopping",
                    status=ExpenseStatus.PENDING,
                )
            ],
        )
        subject = format_sync_email_subject(event)
        body = format_sync_email_text(event)
        self.assertIn("2 new transactions", subject)
        self.assertIn("Costco", body)
        self.assertIn("Unknown Shop", body)
        self.assertIn("review/sync/5", body)


class ScheduledSyncTests(unittest.TestCase):
    def test_skips_fresh_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            settings = _settings(db_path)
            global_db = Database(db_path)
            tenant_id = global_db.get_default_tenant_id()
            global_db.update_tenant_gmail_token(tenant_id, '{"token":"test"}')
            tenant_db = Database(db_path, tenant_id=tenant_id)
            tenant_db.set_sync_value("last_sync_at", datetime.now(timezone.utc).isoformat())

            result = run_scheduled_sync_for_tenant(settings, tenant_id, only_if_stale=True)
            self.assertFalse(result["started"])
            self.assertEqual(result["reason"], "fresh")

    def test_user_notify_email_defaults_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            global_db = Database(db_path)
            tenant_id = global_db.get_default_tenant_id()
            user = global_db.create_user("test@example.com", "hash", tenant_id)
            self.assertFalse(user.notify_email)


class EmailChannelTests(unittest.TestCase):
    def test_skips_users_without_opt_in(self) -> None:
        from expenses_tracker.delivery.channels.email import EmailChannel

        settings = _settings(Path("data/expenses.db"))
        channel = EmailChannel(settings)
        event = SyncCompletedEvent(
            tenant_id=1,
            tenant_name="Home",
            synced_at=datetime.now(timezone.utc),
            messages_checked=1,
            imported=1,
            auto_assigned=1,
            pending=0,
            skipped=0,
        )
        recipients = [
            User(
                id=1,
                email="test@example.com",
                tenant_id=1,
                created_at=datetime.now(timezone.utc),
                notify_email=False,
            )
        ]
        result = channel.send_sync_completed(event, recipients)
        self.assertEqual(result.sent, 0)
        self.assertEqual(result.skipped, 1)

    @patch("expenses_tracker.delivery.channels.email.smtplib.SMTP")
    def test_sends_to_opted_in_users(self, smtp_mock) -> None:
        from expenses_tracker.delivery.channels.email import EmailChannel

        settings = _settings(Path("data/expenses.db"))
        channel = EmailChannel(settings)
        event = SyncCompletedEvent(
            tenant_id=1,
            tenant_name="Home",
            synced_at=datetime.now(timezone.utc),
            messages_checked=1,
            imported=1,
            auto_assigned=1,
            pending=0,
            skipped=0,
        )
        recipients = [
            User(
                id=1,
                email="test@example.com",
                tenant_id=1,
                created_at=datetime.now(timezone.utc),
                notify_email=True,
            )
        ]
        smtp_instance = smtp_mock.return_value.__enter__.return_value
        result = channel.send_sync_completed(event, recipients)
        self.assertEqual(result.sent, 1)
        smtp_instance.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
