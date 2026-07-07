from __future__ import annotations

import unittest
from datetime import datetime, timezone

from expenses_tracker.citi_parser import parse_citi_message
from expenses_tracker.dates import (
    gmail_internal_date_to_app_date,
    parse_date_from_text,
    resolve_transaction_date,
)
from expenses_tracker.config import Settings, NotifyEmailPolicy
from pathlib import Path


def _settings() -> Settings:
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
        app_timezone="America/New_York",
        cron_secret=None,
        smtp_host=None,
        smtp_port=587,
        smtp_user=None,
        smtp_password=None,
        smtp_from=None,
        smtp_use_tls=True,
    )


class CitiDateParsingTests(unittest.TestCase):
    def test_parse_citi_body_date_with_eastern_time(self) -> None:
        body = (
            "Amount: $12.34 Card Ending In 4149 Merchant COSTCO "
            "Date 05/31/2026 11:44 PM ET"
        )
        parsed = parse_date_from_text(body)
        self.assertEqual(parsed.isoformat(), "2026-05-31")

    def test_parse_citi_on_date(self) -> None:
        body = "A $10.00 transaction was made at WALMART on 5/31/2026 on your card ending in 4149."
        parsed = parse_date_from_text(body)
        self.assertEqual(parsed.isoformat(), "2026-05-31")

    def test_resolve_prefers_body_over_received_header(self) -> None:
        body = "Date 05/31/2026 11:44 PM ET"
        header = "Sun, 01 Jun 2026 03:30:00 +0000"
        resolved = resolve_transaction_date(body, email_date_header=header, settings=_settings())
        self.assertEqual(resolved.isoformat(), "2026-05-31")

    def test_resolve_uses_gmail_internal_date_when_body_missing(self) -> None:
        settings = _settings()
        # 2026-06-01 03:30 UTC -> still May 31 in New York
        ms = int(datetime(2026, 6, 1, 3, 30, tzinfo=timezone.utc).timestamp() * 1000)
        resolved = resolve_transaction_date(
            "no date here",
            gmail_internal_date_ms=ms,
            settings=settings,
        )
        self.assertEqual(resolved.isoformat(), "2026-05-31")
        self.assertEqual(gmail_internal_date_to_app_date(ms, settings).isoformat(), "2026-05-31")

    def test_parse_citi_message_uses_body_date(self) -> None:
        body = (
            "Amount: $25.00 Card Ending In 4149 Merchant TARGET "
            "Date 05/31/2026 11:44 PM ET"
        )
        parsed = parse_citi_message(
            gmail_message_id="abc",
            subject="A $25.00 transaction was made",
            sender="alerts@citibank.com",
            body_text=body,
            email_date_header="Sun, 01 Jun 2026 03:30:00 +0000",
            gmail_internal_date_ms=int(
                datetime(2026, 6, 1, 3, 30, tzinfo=timezone.utc).timestamp() * 1000
            ),
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.transaction_date.isoformat(), "2026-05-31")

    def test_parse_citi_link_only_subject_not_present(self) -> None:
        from expenses_tracker.citi_parser import (
            format_citi_display_text,
            is_citi_link_only_body,
        )

        body = (
            "Please visit the following link to view your message:\n"
            "http://fm.info6.citi.com/ats/msg.aspx?sg1=abc"
        )
        subject = "A card on your account was not present for a $5.00 transaction"
        self.assertTrue(is_citi_link_only_body(body))
        display = format_citi_display_text(subject=subject, body=body)
        self.assertIn(subject, display)
        self.assertNotIn("Please visit the following link", display)

        parsed = parse_citi_message(
            gmail_message_id="citi1",
            subject=subject,
            sender="alerts@info6.citi.com",
            body_text=body,
            email_date_header="Thu, 3 Jul 2026 08:00:00 -0700",
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.amount, 5.00)
        self.assertEqual(parsed.merchant, "Card not present")

    def test_parse_citi_merchant_from_html_body(self) -> None:
        body = (
            "Amount: $5.00 Merchant Grok Xai +18002698161 Us Location United States "
            "Date 07/03/2026 Time 03:55 PM ET"
        )
        parsed = parse_citi_message(
            gmail_message_id="citi-html",
            subject="A card on your account was not present for a $5.00 transaction",
            sender="alerts@info6.citi.com",
            body_text=body,
            email_date_header="Thu, 3 Jul 2026 08:00:00 -0700",
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.amount, 5.00)
        self.assertEqual(parsed.merchant, "Grok Xai +18002698161 Us")

    def test_parse_citi_merchant_from_costco_subject(self) -> None:
        subject = (
            "A transaction was made on your Costco Anywhere Visa Card by "
            "Card ending in 4149 for a $42.15 at COSTCO WHSE #1234."
        )
        body = "Please visit the following link to view your message: http://fm.info6.citi.com/x"
        parsed = parse_citi_message(
            gmail_message_id="citi2",
            subject=subject,
            sender="alerts@info6.citi.com",
            body_text=body,
            email_date_header="Thu, 3 Jul 2026 08:00:00 -0700",
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.amount, 42.15)
        self.assertIn("COSTCO", parsed.merchant)


if __name__ == "__main__":
    unittest.main()
