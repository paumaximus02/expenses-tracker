from __future__ import annotations

import logging
import threading

from expenses_tracker.config import Settings, get_settings
from expenses_tracker.services import build_global_db, build_services

logger = logging.getLogger(__name__)


def _run_sync_job(settings: Settings, tenant_id: int) -> None:
    db, sync = build_services(settings, tenant_id=tenant_id)
    global_db = build_global_db(settings)
    try:
        result = sync.sync()
        payload = {
            "imported": result.get("imported", 0),
            "pending": result.get("pending", 0),
            "skipped": result.get("skipped", 0),
            "notification_id": result.get("notification_id"),
            "error": None,
        }
        db.set_last_sync_result(payload)
        try:
            global_db.update_tenant_gmail_token(tenant_id, sync.gmail.export_token_json())
        except RuntimeError:
            pass
    except Exception as exc:
        logger.exception("Background sync failed for tenant %s", tenant_id)
        db.set_last_sync_result({"imported": 0, "notification_id": None, "error": str(exc)})
    finally:
        db.set_sync_in_progress(False)


def try_start_background_sync(
    settings: Settings | None,
    tenant_id: int,
    *,
    only_if_stale: bool = False,
) -> dict[str, object]:
    settings = settings or get_settings()
    global_db = build_global_db(settings)
    tenant = global_db.get_tenant(tenant_id)
    if tenant is None or not tenant.gmail_token_json:
        return {"started": False, "reason": "not_connected"}

    db, _ = build_services(settings, tenant_id=tenant_id)
    if db.is_sync_in_progress():
        return {"started": False, "reason": "already_running"}

    if only_if_stale and not db.is_sync_stale(settings.sync_stale_hours):
        return {"started": False, "reason": "fresh"}

    db.set_sync_in_progress(True)
    thread = threading.Thread(
        target=_run_sync_job,
        args=(settings, tenant_id),
        daemon=True,
        name=f"sync-tenant-{tenant_id}",
    )
    thread.start()
    return {"started": True}


def sync_status_payload(settings: Settings | None, tenant_id: int) -> dict[str, object]:
    settings = settings or get_settings()
    db, _ = build_services(settings, tenant_id=tenant_id)
    last_sync = db.last_sync_at()
    last_result = db.get_last_sync_result()
    latest_notification = None
    if last_result and last_result.get("notification_id"):
        notification = db.get_notification(int(last_result["notification_id"]))
        if notification is not None:
            latest_notification = {
                "id": notification.id,
                "title": notification.title,
                "message": notification.message,
                "imported": last_result.get("imported", 0),
                "review_url": None,
            }
    return {
        "in_progress": db.is_sync_in_progress(),
        "last_sync_at": last_sync.isoformat() if last_sync else None,
        "unread_count": db.count_unread_notifications(),
        "last_result": last_result,
        "latest_notification": latest_notification,
    }
