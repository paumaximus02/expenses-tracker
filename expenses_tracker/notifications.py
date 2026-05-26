from __future__ import annotations

from datetime import datetime

from expenses_tracker.models import NotificationLevel, NotificationType


def format_sync_result(result: dict[str, int]) -> tuple[str, str]:
    title = "Gmail sync completed"
    message = (
        f"{result['messages_checked']} messages checked, "
        f"{result['imported']} imported, "
        f"{result['auto_assigned']} auto-assigned, "
        f"{result['pending']} pending review, "
        f"{result['skipped']} skipped."
    )
    return title, message


def format_sync_error(error: Exception | str) -> tuple[str, str]:
    title = "Gmail sync failed"
    message = str(error)
    return title, message


def format_notification_time(value: datetime) -> str:
    return value.strftime("%b %d, %Y %I:%M %p")
