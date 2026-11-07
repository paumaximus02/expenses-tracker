from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from expenses_tracker.config import NotifyEmailPolicy, Settings
from expenses_tracker.db import Database
from expenses_tracker.gmail_client import GmailClient
from expenses_tracker.web import create_app


def _settings(db_path: Path) -> Settings:
    return Settings(
        gmail_credentials_path=db_path.parent / "credentials.json",
        gmail_credentials_json=None,
        gmail_token_path=db_path.parent / "missing-token.json",
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


class SettingsGmailStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        app = create_app(_settings(self.db_path))
        app.config["TESTING"] = True
        self.client = app.test_client()
        global_db = Database(self.db_path)
        self.tenant_id = global_db.get_default_tenant_id()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_settings_shows_disconnected_without_token(self) -> None:
        global_db = Database(self.db_path)
        with global_db.connection() as conn:
            conn.execute(
                "UPDATE tenants SET gmail_token_json = NULL WHERE id = ?",
                (self.tenant_id,),
            )
        with patch.object(
            GmailClient,
            "verify_connection",
            return_value=("disconnected", None),
        ):
            response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Gmail is not connected yet.", response.data)
        self.assertIn(b"Connect Gmail", response.data)

    def test_settings_shows_needs_reconnect_when_token_invalid(self) -> None:
        global_db = Database(self.db_path)
        global_db.update_tenant_gmail_token(self.tenant_id, '{"token":"stale"}')

        with patch.object(
            GmailClient,
            "verify_connection",
            return_value=("needs_reconnect", "token revoked"),
        ):
            response = self.client.get("/settings")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Gmail connection was lost", response.data)
        self.assertIn(b"token revoked", response.data)
        self.assertIn(b"Reconnect Gmail", response.data)
        self.assertNotIn(b"Gmail is connected for this household.", response.data)

    def test_settings_shows_connected_when_verification_succeeds(self) -> None:
        global_db = Database(self.db_path)
        global_db.update_tenant_gmail_token(self.tenant_id, '{"token":"ok"}')

        with patch.object(
            GmailClient,
            "verify_connection",
            return_value=("connected", None),
        ):
            response = self.client.get("/settings")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Gmail is connected for this household.", response.data)
        self.assertIn(b"Reconnect Gmail", response.data)


if __name__ == "__main__":
    unittest.main()
