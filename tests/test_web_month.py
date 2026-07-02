from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from expenses_tracker.config import NotifyEmailPolicy, Settings
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


class SelectedMonthPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "test.db"
        app = create_app(_settings(db_path))
        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_month_selected_on_report_carries_to_other_pages(self) -> None:
        response = self.client.get("/report?month=2025-11")
        self.assertEqual(response.status_code, 200)

        for path in ("/expenses", "/income", "/report"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200)
            self.assertIn(b'value="2025-11"', page.data, path)

    def test_invalid_month_falls_back_to_default(self) -> None:
        response = self.client.get("/expenses?month=not-a-month")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'value="not-a-month"', response.data)

    def test_explicit_empty_month_is_not_persisted(self) -> None:
        self.client.get("/expenses?month=2025-10")
        page = self.client.get("/expenses?month=")
        self.assertNotIn(b'value="2025-10"', page.data)

        # The remembered month is untouched by the "all months" view.
        page = self.client.get("/income")
        self.assertIn(b'value="2025-10"', page.data)


if __name__ == "__main__":
    unittest.main()
