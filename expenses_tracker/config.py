from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class NotifyEmailPolicy(str, Enum):
    NEVER = "never"
    ON_IMPORT = "on_import"
    ON_ERROR = "on_error"
    ALWAYS = "always"


@dataclass(frozen=True)
class Settings:
    gmail_credentials_path: Path
    gmail_credentials_json: str | None
    gmail_token_path: Path
    gmail_token_json: str | None
    gmail_search_query: str
    database_path: Path
    card_holders: dict[str, str]
    secret_key: str
    auth_disabled: bool
    allow_signup: bool
    session_cookie_secure: bool
    sync_stale_hours: int
    sync_interval_seconds: int
    notifications_enabled: bool
    notify_email_enabled: bool
    notify_email_debug: bool
    notify_email_policy: NotifyEmailPolicy
    app_base_url: str | None
    app_timezone: str
    cron_secret: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_from: str | None
    smtp_use_tls: bool


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_card_holders(raw: str) -> dict[str, str]:
    holders: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        last_four, name = item.split(":", 1)
        last_four = last_four.strip()
        name = name.strip()
        if len(last_four) == 4 and last_four.isdigit() and name:
            holders[last_four] = name
    return holders


def _parse_notify_email_policy(raw: str | None) -> NotifyEmailPolicy:
    if raw is None:
        return NotifyEmailPolicy.ON_IMPORT
    cleaned = raw.strip().lower()
    try:
        return NotifyEmailPolicy(cleaned)
    except ValueError:
        return NotifyEmailPolicy.ON_IMPORT


def resolve_gmail_token_json(settings: Settings, tenant_token_json: str | None) -> str | None:
    if tenant_token_json and tenant_token_json.strip():
        return tenant_token_json
    if settings.gmail_token_json:
        return settings.gmail_token_json
    if settings.gmail_token_path.exists():
        return settings.gmail_token_path.read_text(encoding="utf-8")
    return None


def resolve_gmail_credentials_path(settings: Settings) -> Path:
    if settings.gmail_credentials_json:
        path = settings.database_path.parent / ".gmail_credentials.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != settings.gmail_credentials_json:
            json.loads(settings.gmail_credentials_json)
            path.write_text(settings.gmail_credentials_json, encoding="utf-8")
        return path
    return settings.gmail_credentials_path


def get_settings() -> Settings:
    app_base_url = os.getenv("APP_BASE_URL", "").strip() or None
    return Settings(
        gmail_credentials_path=Path(os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")),
        gmail_credentials_json=os.getenv("GMAIL_CREDENTIALS_JSON"),
        gmail_token_path=Path(os.getenv("GMAIL_TOKEN_PATH", "token.json")),
        gmail_token_json=os.getenv("GMAIL_TOKEN_JSON"),
        gmail_search_query=os.getenv(
            "GMAIL_SEARCH_QUERY",
            'from:(citibank.com OR citi.com) after:2026/04/01 subject:transaction',
        ),
        database_path=Path(os.getenv("DATABASE_PATH", "data/expenses.db")),
        card_holders=_parse_card_holders(
            os.getenv("CARD_HOLDERS", "4149:Juan,1201:Debora"),
        ),
        secret_key=os.getenv("SECRET_KEY", "expenses-tracker-local-dev"),
        auth_disabled=_env_bool("AUTH_DISABLED", False),
        allow_signup=_env_bool("ALLOW_SIGNUP", True),
        session_cookie_secure=_env_bool("SESSION_COOKIE_SECURE", False),
        sync_stale_hours=int(os.getenv("SYNC_STALE_HOURS", "6")),
        sync_interval_seconds=int(os.getenv("SYNC_INTERVAL_SECONDS", "3600")),
        notifications_enabled=_env_bool("NOTIFICATIONS_ENABLED", True),
        notify_email_enabled=_env_bool("NOTIFY_EMAIL_ENABLED", False),
        notify_email_debug=_env_bool("NOTIFY_EMAIL_DEBUG", False),
        notify_email_policy=_parse_notify_email_policy(os.getenv("NOTIFY_EMAIL_POLICY")),
        app_base_url=app_base_url,
        app_timezone=os.getenv("APP_TIMEZONE", "America/New_York"),
        cron_secret=os.getenv("CRON_SECRET"),
        smtp_host=os.getenv("SMTP_HOST") or None,
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USER") or None,
        smtp_password=os.getenv("SMTP_PASSWORD") or None,
        smtp_from=os.getenv("SMTP_FROM") or None,
        smtp_use_tls=_env_bool("SMTP_USE_TLS", True),
    )
