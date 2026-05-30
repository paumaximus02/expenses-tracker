from __future__ import annotations

import logging

from expenses_tracker.delivery.events import SyncCompletedEvent
from expenses_tracker.models import User

logger = logging.getLogger(__name__)


class SmsChannel:
    name = "sms"

    def send_sync_completed(
        self,
        event: SyncCompletedEvent,
        recipients: list[User],
    ) -> None:
        opted_in = [user for user in recipients if user.notify_sms and user.phone]
        if not opted_in:
            return
        logger.info(
            "SMS channel not implemented; would notify %s user(s) for tenant %s",
            len(opted_in),
            event.tenant_id,
        )
