from __future__ import annotations

import re
from calendar import month_name
from datetime import date, datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from expenses_tracker.config import Settings, get_settings

BODY_TRANSACTION_DATE_PATTERNS = [
    re.compile(
        r"Date\s+(\d{1,2}/\d{1,2}/\d{4})"
        r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?(?:\s+(?:ET|EST|EDT))?)?",
        re.I,
    ),
    re.compile(r"\bon\s+(\d{1,2}/\d{1,2}/\d{2,4})\b", re.I),
    re.compile(r"(?:transaction date|date)\s*[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", re.I),
    re.compile(r"(\d{4}-\d{2}-\d{2})"),
]

DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y-%m-%d")


def app_zoneinfo(settings: Settings | None = None) -> ZoneInfo:
    settings = settings or get_settings()
    try:
        return ZoneInfo(settings.app_timezone)
    except Exception:
        return ZoneInfo("UTC")


def app_today(settings: Settings | None = None) -> date:
    return datetime.now(app_zoneinfo(settings)).date()


def app_month(settings: Settings | None = None) -> str:
    return app_today(settings).strftime("%Y-%m")


def parse_month(month: str) -> date:
    """Parse YYYY-MM into the first day of that month."""
    return datetime.strptime(month, "%Y-%m").date().replace(day=1)


def shift_month(month: str, delta: int) -> str:
    """Return YYYY-MM shifted by delta calendar months."""
    start = parse_month(month)
    year = start.year + (start.month - 1 + delta) // 12
    month_num = (start.month - 1 + delta) % 12 + 1
    return f"{year:04d}-{month_num:02d}"


def format_month_label(month: str) -> str:
    """Format YYYY-MM as a readable label, e.g. July 2026."""
    start = parse_month(month)
    return f"{month_name[start.month]} {start.year}"


def ytd_start_month(end_month: str) -> str:
    """Return January of the calendar year for end_month (YYYY-MM)."""
    start = parse_month(end_month)
    return f"{start.year:04d}-01"


def months_in_ytd(end_month: str) -> list[str]:
    """Return YYYY-MM values from January through end_month inclusive."""
    start = parse_month(end_month)
    return [f"{start.year:04d}-{month:02d}" for month in range(1, start.month + 1)]


def format_ytd_range_label(end_month: str) -> str:
    """Format a YTD range label, e.g. Jan–Jul 2026."""
    start = parse_month(end_month)
    start_abbr = month_name[1][:3]
    end_abbr = month_name[start.month][:3]
    if start.month == 1:
        return f"{start_abbr} {start.year}"
    return f"{start_abbr}–{end_abbr} {start.year}"


def format_month_short_label(month: str) -> str:
    """Format YYYY-MM as a short month label, e.g. Jan."""
    start = parse_month(month)
    return month_name[start.month][:3]


def email_header_to_app_date(email_date_header: str, settings: Settings | None = None) -> date:
    from email.utils import parsedate_to_datetime

    parsed = parsedate_to_datetime(email_date_header)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=app_zoneinfo(settings))
    return parsed.astimezone(app_zoneinfo(settings)).date()


def gmail_internal_date_to_app_date(
    internal_date_ms: str | int | None,
    settings: Settings | None = None,
) -> date | None:
    if internal_date_ms is None:
        return None
    timestamp = datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=app_zoneinfo(settings))
    return timestamp.date()


def parse_date_from_text(text: str) -> date | None:
    for pattern in BODY_TRANSACTION_DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1)
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return None


def resolve_transaction_date(
    text: str,
    *,
    email_date_header: str | None = None,
    gmail_internal_date_ms: str | int | None = None,
    settings: Settings | None = None,
) -> date:
    """Pick transaction date from email body, Date header, or Gmail received time."""
    body_date = parse_date_from_text(text)
    if body_date is not None:
        return body_date

    if email_date_header:
        try:
            return email_header_to_app_date(email_date_header, settings)
        except (TypeError, ValueError, IndexError, OverflowError):
            pass

    internal_date = gmail_internal_date_to_app_date(gmail_internal_date_ms, settings)
    if internal_date is not None:
        return internal_date

    return app_today(settings)
