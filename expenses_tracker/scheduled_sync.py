from __future__ import annotations

import logging

from expenses_tracker.config import Settings, get_settings
from expenses_tracker.services import build_global_db, build_services

logger = logging.getLogger(__name__)


def run_scheduled_sync_for_tenant(
    settings: Settings,
    tenant_id: int,
    *,
    only_if_stale: bool = True,
    manage_lock: bool = True,
) -> dict[str, object]:
    global_db = build_global_db(settings)
    tenant = global_db.get_tenant(tenant_id)
    if tenant is None or not tenant.gmail_token_json:
        return {"tenant_id": tenant_id, "started": False, "reason": "not_connected"}

    db, sync = build_services(settings, tenant_id=tenant_id)
    if manage_lock and db.is_sync_in_progress():
        return {"tenant_id": tenant_id, "started": False, "reason": "already_running"}

    if only_if_stale and not db.is_sync_stale(settings.sync_stale_hours):
        return {"tenant_id": tenant_id, "started": False, "reason": "fresh"}

    if manage_lock:
        db.set_sync_in_progress(True)
    try:
        result = sync.sync()
        payload = {
            "tenant_id": tenant_id,
            "started": True,
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
        return payload
    except Exception as exc:
        logger.exception("Scheduled sync failed for tenant %s", tenant_id)
        db.set_last_sync_result({"imported": 0, "notification_id": None, "error": str(exc)})
        return {
            "tenant_id": tenant_id,
            "started": True,
            "imported": 0,
            "notification_id": None,
            "error": str(exc),
        }
    finally:
        if manage_lock:
            db.set_sync_in_progress(False)


def run_scheduled_sync_all(
    settings: Settings | None = None,
    *,
    only_if_stale: bool = True,
) -> list[dict[str, object]]:
    settings = settings or get_settings()
    global_db = build_global_db(settings)
    tenants = global_db.list_tenants_with_gmail()
    return [
        run_scheduled_sync_for_tenant(settings, tenant.id, only_if_stale=only_if_stale)
        for tenant in tenants
    ]
