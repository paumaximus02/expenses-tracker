from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    gmail_credentials_path: Path
    gmail_token_path: Path
    gmail_search_query: str
    database_path: Path
    card_holders: dict[str, str]
    secret_key: str
    auth_disabled: bool
    allow_signup: bool
    session_cookie_secure: bool
    sync_stale_hours: int


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


def get_settings() -> Settings:
    return Settings(
        gmail_credentials_path=Path(os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")),
        gmail_token_path=Path(os.getenv("GMAIL_TOKEN_PATH", "token.json")),
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
    )
