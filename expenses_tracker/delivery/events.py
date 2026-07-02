from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from expenses_tracker.config import Settings
from expenses_tracker.db import Database
from expenses_tracker.models import Expense, ExpenseStatus, Tenant


@dataclass(frozen=True)
class SyncTransactionSummary:
    merchant: str
    amount: float
    currency: str
    transaction_date: date
    card_holder: str | None
    bucket_name: str | None
    suggested_bucket_name: str | None
    status: ExpenseStatus


@dataclass
class SyncCompletedEvent:
    tenant_id: int
    tenant_name: str
    synced_at: datetime
    messages_checked: int
    imported: int
    auto_assigned: int
    pending: int
    skipped: int
    income_checked: int = 0
    income_imported: int = 0
    withdrawals_imported: int = 0
    notification_id: int | None = None
    error: str | None = None
    auto_assigned_transactions: list[SyncTransactionSummary] = field(default_factory=list)
    pending_transactions: list[SyncTransactionSummary] = field(default_factory=list)
    review_url: str | None = None


def _expense_to_summary(expense: Expense) -> SyncTransactionSummary:
    return SyncTransactionSummary(
        merchant=expense.merchant,
        amount=expense.amount,
        currency=expense.currency,
        transaction_date=expense.transaction_date,
        card_holder=expense.card_holder,
        bucket_name=expense.bucket_name,
        suggested_bucket_name=expense.suggested_bucket_name,
        status=expense.status,
    )


def build_sync_completed_event(
    *,
    tenant: Tenant,
    synced_at: datetime,
    result: dict[str, int],
    expenses: list[Expense],
    settings: Settings,
    notification_id: int | None = None,
    error: str | None = None,
) -> SyncCompletedEvent:
    auto_assigned_transactions: list[SyncTransactionSummary] = []
    pending_transactions: list[SyncTransactionSummary] = []
    for expense in expenses:
        if expense.status == ExpenseStatus.PENDING:
            pending_transactions.append(_expense_to_summary(expense))
        else:
            auto_assigned_transactions.append(_expense_to_summary(expense))

    review_url = None
    if notification_id and settings.app_base_url:
        base = settings.app_base_url.rstrip("/")
        review_url = f"{base}/review/sync/{notification_id}"

    return SyncCompletedEvent(
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        synced_at=synced_at,
        messages_checked=result.get("messages_checked", 0),
        imported=result.get("imported", 0),
        auto_assigned=result.get("auto_assigned", 0),
        pending=result.get("pending", 0),
        skipped=result.get("skipped", 0),
        income_checked=result.get("income_checked", 0),
        income_imported=result.get("income_imported", 0),
        withdrawals_imported=result.get("withdrawals_imported", 0),
        notification_id=notification_id,
        error=error,
        auto_assigned_transactions=auto_assigned_transactions,
        pending_transactions=pending_transactions,
        review_url=review_url,
    )
