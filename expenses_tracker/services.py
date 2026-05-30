from __future__ import annotations

import logging

from expenses_tracker.bucket_matcher import BucketMatcher
from expenses_tracker.config import Settings, get_settings, resolve_gmail_credentials_path
from expenses_tracker.db import Database
from expenses_tracker.gmail_client import GmailClient
from expenses_tracker.models import Tenant
from expenses_tracker.sync import ExpenseSyncService
from expenses_tracker.tenancy import get_current_tenant, global_db, resolve_tenant_id, tenant_db


def build_services(
    settings: Settings | None = None,
    *,
    tenant_id: int | None = None,
) -> tuple[Database, ExpenseSyncService]:
    settings = settings or get_settings()
    resolved_tenant_id = tenant_id if tenant_id is not None else resolve_tenant_id(settings)
    db = tenant_db(settings, resolved_tenant_id)
    db.ensure_default_buckets()
    tenant = get_current_tenant(settings, resolved_tenant_id)
    gmail = GmailClient(
        resolve_gmail_credentials_path(settings),
        token_path=settings.gmail_token_path,
        token_json=tenant.gmail_token_json,
    )
    matcher = BucketMatcher(db)
    sync = ExpenseSyncService(settings, db, gmail, matcher, tenant=tenant)
    return db, sync


def build_global_db(settings: Settings | None = None) -> Database:
    settings = settings or get_settings()
    return global_db(settings)
