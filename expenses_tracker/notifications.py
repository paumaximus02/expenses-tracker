from __future__ import annotations

from datetime import datetime

from expenses_tracker.models import Notification, NotificationLevel, NotificationType


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


def notification_to_dict(
    notification: Notification,
    *,
    review_url: str | None = None,
) -> dict[str, object]:
    return {
        "id": notification.id,
        "type": notification.type.value,
        "level": notification.level.value,
        "title": notification.title,
        "message": notification.message,
        "created_at": notification.created_at.isoformat(),
        "is_read": notification.is_read,
        "import_count": notification.import_count,
        "review_url": review_url,
    }
