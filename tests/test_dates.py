from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from expenses_tracker.config import NotifyEmailPolicy, Settings
from expenses_tracker.dates import app_today, resolve_transaction_date


def _settings(timezone_name: str = "America/New_York") -> Settings:
    return Settings(
        gmail_credentials_path=Path("credentials.json"),
        gmail_credentials_json=None,
        gmail_token_path=Path("token.json"),
        gmail_search_query="test",
        database_path=Path("data/expenses.db"),
        card_holders={"4149": "Juan"},
        secret_key="test",
        auth_disabled=True,
        allow_signup=True,
        session_cookie_secure=False,
        sync_stale_hours=6,
        sync_interval_seconds=3600,
        notifications_enabled=True,
        notify_email_enabled=False,
        notify_email_debug=False,
        notify_email_policy=NotifyEmailPolicy.ON_IMPORT,
        app_base_url=None,
        app_timezone=timezone_name,
        cron_secret=None,
        smtp_host=None,
        smtp_port=587,
        smtp_user=None,
        smtp_password=None,
        smtp_from=None,
        smtp_use_tls=True,
    )


class AppTimezoneTests(unittest.TestCase):
    def test_app_today_uses_configured_timezone(self) -> None:
        settings = _settings("America/New_York")

        def fake_now(tz):
            return datetime(2026, 5, 31, 23, 44, tzinfo=tz)

        with patch("expenses_tracker.dates.datetime") as mock_datetime:
            mock_datetime.now.side_effect = fake_now
            mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            today = app_today(settings)

        self.assertEqual(today.isoformat(), "2026-05-31")

    def test_email_header_converts_to_app_timezone(self) -> None:
        settings = _settings("America/New_York")
        header = "Sun, 01 Jun 2026 03:30:00 +0000"
        parsed = resolve_transaction_date("no date in body", email_date_header=header, settings=settings)
        self.assertEqual(parsed.isoformat(), "2026-05-31")


if __name__ == "__main__":
    unittest.main()
