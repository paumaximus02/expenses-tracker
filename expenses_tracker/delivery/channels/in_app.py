from __future__ import annotations

from expenses_tracker.db import Database
from expenses_tracker.models import NotificationLevel, NotificationType
from expenses_tracker.notifications import format_sync_error, format_sync_result
from expenses_tracker.delivery.events import SyncCompletedEvent


class InAppChannel:
    name = "in_app"

    def __init__(self, db: Database) -> None:
        self.db = db

    def record_sync_completed(
        self,
        event: SyncCompletedEvent,
        *,
        imported_expense_ids: list[int],
    ) -> int | None:
        if event.error is not None:
            title, message = format_sync_error(event.error)
            notification = self.db.create_notification(
                type=NotificationType.SYNC,
                title=title,
                message=message,
                level=NotificationLevel.ERROR,
            )
            return notification.id

        title, message = format_sync_result(
            {
                "messages_checked": event.messages_checked,
                "imported": event.imported,
                "auto_assigned": event.auto_assigned,
                "pending": event.pending,
                "skipped": event.skipped,
            }
        )
        notification = self.db.create_notification(
            type=NotificationType.SYNC,
            title=title,
            message=message,
            level=NotificationLevel.SUCCESS,
        )
        if imported_expense_ids:
            self.db.link_expenses_to_sync_notification(imported_expense_ids, notification.id)
        return notification.id
