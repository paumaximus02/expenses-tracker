from __future__ import annotations

import logging

from expenses_tracker.config import Settings
from expenses_tracker.db import Database
from expenses_tracker.delivery.channels.email import EmailChannel
from expenses_tracker.delivery.channels.in_app import InAppChannel
from expenses_tracker.delivery.channels.sms import SmsChannel
from expenses_tracker.delivery.events import SyncCompletedEvent, build_sync_completed_event
from expenses_tracker.delivery.policy import should_send_sync_email
from expenses_tracker.models import Expense, Tenant

logger = logging.getLogger(__name__)


class NotificationDispatcher:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        auth_db: Database | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.auth_db = auth_db or db
        self.in_app = InAppChannel(db)
        self.email = EmailChannel(settings)
        self.sms = SmsChannel()

    def dispatch_sync_completed(
        self,
        *,
        tenant: Tenant,
        result: dict[str, int],
        imported_expense_ids: list[int],
        imported_expenses: list[Expense],
        synced_at,
        error: str | None = None,
        record_in_app: bool = True,
    ) -> SyncCompletedEvent:
        notification_id = None
        if record_in_app:
            preliminary = build_sync_completed_event(
                tenant=tenant,
                synced_at=synced_at,
                result=result,
                expenses=imported_expenses,
                settings=self.settings,
                error=error,
            )
            notification_id = self.in_app.record_sync_completed(
                preliminary,
                imported_expense_ids=imported_expense_ids if error is None else [],
            )

        event = build_sync_completed_event(
            tenant=tenant,
            synced_at=synced_at,
            result=result,
            expenses=imported_expenses,
            settings=self.settings,
            notification_id=notification_id,
            error=error,
        )

        if should_send_sync_email(self.settings, event):
            recipients = self.auth_db.list_users_by_tenant(tenant.id)
            self.email.send_sync_completed(event, recipients)
            self.sms.send_sync_completed(event, recipients)

        return event