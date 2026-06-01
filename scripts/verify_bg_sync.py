from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from expenses_tracker.background_sync import try_start_background_sync
from expenses_tracker.config import NotifyEmailPolicy, Settings
from expenses_tracker.services import build_services
from expenses_tracker.web import create_app


def main() -> None:
    settings = Settings(
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
        app_timezone="America/New_York",
        cron_secret=None,
        smtp_host=None,
        smtp_port=587,
        smtp_user=None,
        smtp_password=None,
        smtp_from=None,
        smtp_use_tls=True,
    )
    app = create_app(settings)
    client = app.test_client()

    with app.app_context():
        db, _ = build_services(settings)
        tenant_id = db.tenant_id

        response = client.get("/sync/status")
        assert response.status_code == 200, response.data
        payload = response.get_json()
        assert payload is not None
        assert "in_progress" in payload and "unread_count" in payload
        print("GET /sync/status OK")

        db.set_sync_value("last_sync_at", datetime.now().isoformat())
        db.set_sync_in_progress(False)
        response = client.post("/sync/start?only_if_stale=1")
        assert response.status_code == 200
        body = response.get_json()
        assert body is not None
        assert body["started"] is False and body["reason"] == "fresh"
        print("Stale skip OK")

        db.set_sync_in_progress(True)
        response = client.post("/sync/start")
        body = response.get_json()
        assert body is not None
        assert body["reason"] == "already_running"
        db.set_sync_in_progress(False)
        print("In-progress guard OK")

        db.set_sync_value("last_sync_at", (datetime.now() - timedelta(hours=7)).isoformat())
        result = try_start_background_sync(settings, tenant_id, only_if_stale=False)
        if result.get("started"):
            print("Background start OK (thread spawned)")
            db.set_sync_in_progress(False)
        elif result.get("reason") == "not_connected":
            response = client.post("/sync/start")
            body = response.get_json()
            assert body is not None
            assert body["reason"] == "not_connected"
            print("Not connected handling OK")
        else:
            raise AssertionError(f"Unexpected start result: {result}")

    print("All checks passed")


if __name__ == "__main__":
    main()
