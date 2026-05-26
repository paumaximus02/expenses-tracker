from __future__ import annotations

from werkzeug.security import check_password_hash, generate_password_hash

from expenses_tracker.config import Settings
from expenses_tracker.db import Database
from expenses_tracker.models import User

MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


def validate_password(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


def login_user(user_id: int) -> None:
    from flask import session

    session.clear()
    session["user_id"] = user_id
    session.permanent = True


def logout_user() -> None:
    from flask import session

    session.clear()


def current_user(db: Database) -> User | None:
    from flask import session

    user_id = session.get("user_id")
    if user_id is None:
        return None
    try:
        return db.get_user_by_id(int(user_id))
    except (TypeError, ValueError):
        return None


def is_auth_enabled(settings: Settings) -> bool:
    return not settings.auth_disabled
