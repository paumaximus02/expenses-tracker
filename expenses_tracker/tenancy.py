from __future__ import annotations

from expenses_tracker.auth import current_user
from expenses_tracker.config import Settings
from expenses_tracker.db import Database
from expenses_tracker.models import Tenant


def global_db(settings: Settings) -> Database:
    return Database(settings.database_path)


def tenant_db(settings: Settings, tenant_id: int) -> Database:
    return Database(settings.database_path, tenant_id=tenant_id)


def resolve_tenant_id(settings: Settings, db: Database | None = None) -> int:
    if settings.auth_disabled:
        database = db or global_db(settings)
        return database.get_default_tenant_id()

    auth_db = db or global_db(settings)
    user = current_user(auth_db)
    if user is None:
        raise RuntimeError("Authenticated user required to resolve tenant.")
    return user.tenant_id


def get_current_tenant(settings: Settings, tenant_id: int) -> Tenant:
    tenant = global_db(settings).get_tenant(tenant_id)
    if tenant is None:
        raise RuntimeError(f"Tenant {tenant_id} not found.")
    return tenant
