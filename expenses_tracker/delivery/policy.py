from __future__ import annotations

from expenses_tracker.config import NotifyEmailPolicy, Settings
from expenses_tracker.delivery.events import SyncCompletedEvent


def should_send_sync_email(settings: Settings, event: SyncCompletedEvent) -> bool:
    if not settings.notifications_enabled or not settings.notify_email_enabled:
        return False
    if settings.notify_email_debug and event.error is None:
        return True
    policy = settings.notify_email_policy
    if policy == NotifyEmailPolicy.NEVER:
        return False
    if policy == NotifyEmailPolicy.ALWAYS:
        return True
    if policy == NotifyEmailPolicy.ON_ERROR:
        return event.error is not None
    if policy == NotifyEmailPolicy.ON_IMPORT:
        return event.error is None and event.imported > 0
    return False
