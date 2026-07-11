from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from expenses_tracker.config import NotifyEmailPolicy, Settings
from expenses_tracker.dates import (
    app_today,
    format_month_label,
    format_month_short_label,
    format_ytd_range_label,
    months_in_ytd,
    resolve_transaction_date,
    shift_month,
    ytd_start_month,
)


def _settings(timezone_name: str = "America/New_York") -> Settings:
    return Settings(
        gmail_credentials_path=Path("credentials.json"),
        gmail_credentials_json=None,
        gmail_token_path=Path("token.json"),
        gmail_token_json=None,
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


class MonthHelperTests(unittest.TestCase):
    def test_shift_month_across_year_boundary(self) -> None:
        self.assertEqual(shift_month("2026-01", -1), "2025-12")
        self.assertEqual(shift_month("2025-12", 1), "2026-01")
        self.assertEqual(shift_month("2026-07", 1), "2026-08")

    def test_format_month_label(self) -> None:
        self.assertEqual(format_month_label("2026-07"), "July 2026")

    def test_ytd_start_and_months(self) -> None:
        self.assertEqual(ytd_start_month("2026-07"), "2026-01")
        self.assertEqual(
            months_in_ytd("2026-03"),
            ["2026-01", "2026-02", "2026-03"],
        )
        self.assertEqual(months_in_ytd("2026-01"), ["2026-01"])

    def test_format_ytd_range_label(self) -> None:
        self.assertEqual(format_ytd_range_label("2026-07"), "Jan–Jul 2026")
        self.assertEqual(format_ytd_range_label("2026-01"), "Jan 2026")

    def test_format_month_short_label(self) -> None:
        self.assertEqual(format_month_short_label("2026-07"), "Jul")


if __name__ == "__main__":
    unittest.main()
