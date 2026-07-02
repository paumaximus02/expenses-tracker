from __future__ import annotations

from datetime import datetime
from html import escape

from expenses_tracker.delivery.events import SyncCompletedEvent, SyncTransactionSummary
from expenses_tracker.models import ExpenseStatus, Notification, NotificationLevel, NotificationType


def format_sync_result(result: dict[str, int]) -> tuple[str, str]:
    title = "Gmail sync completed"
    message = (
        f"{result['messages_checked']} messages checked, "
        f"{result['imported']} imported, "
        f"{result['auto_assigned']} auto-assigned, "
        f"{result['pending']} pending review, "
        f"{result['skipped']} skipped."
    )
    income_checked = result.get("income_checked", 0)
    income_imported = result.get("income_imported", 0)
    if income_checked or income_imported:
        message += (
            f" Income: {income_checked} message"
            f"{'' if income_checked == 1 else 's'} checked, "
            f"{income_imported} imported."
        )
    return title, message


def format_sync_error(error: Exception | str) -> tuple[str, str]:
    title = "Gmail sync failed"
    message = str(error)
    return title, message


def format_sync_email_subject(event: SyncCompletedEvent) -> str:
    if event.error:
        return f"Gmail sync failed for {event.tenant_name}"
    if event.imported == 0:
        return f"Gmail sync completed for {event.tenant_name}"
    return (
        f"{event.imported} new transaction"
        f"{'s' if event.imported != 1 else ''} — "
        f"{event.auto_assigned} auto-assigned, {event.pending} pending review"
    )


def _format_transaction_line(item: SyncTransactionSummary) -> str:
    holder = item.card_holder or "?"
    date_text = item.transaction_date.isoformat()
    amount = f"${item.amount:,.2f}"
    if item.status == ExpenseStatus.PENDING:
        bucket = item.suggested_bucket_name or "Unassigned"
        return f"- {date_text} | {amount} | {item.merchant} | {holder} -> suggest: {bucket}"
    bucket = item.bucket_name or "Unassigned"
    rule_type = "auto-learned" if item.status == ExpenseStatus.AUTO else "confirmed rule"
    return f"- {date_text} | {amount} | {item.merchant} | {holder} -> {bucket} ({rule_type})"


def _format_transaction_rows(items: list[SyncTransactionSummary]) -> str:
    if not items:
        return "None"
    return "\n".join(_format_transaction_line(item) for item in items)


def format_sync_email_text(event: SyncCompletedEvent) -> str:
    if event.error:
        return (
            f"Gmail sync failed for {event.tenant_name}.\n\n"
            f"Error: {event.error}\n"
        )

    lines = [
        f"Gmail sync completed for {event.tenant_name}.",
        "",
        (
            f"{event.messages_checked} messages checked, "
            f"{event.imported} imported, "
            f"{event.auto_assigned} auto-assigned, "
            f"{event.pending} pending review, "
            f"{event.skipped} skipped."
        ),
        "",
        "Auto-assigned:",
        _format_transaction_rows(event.auto_assigned_transactions),
        "",
        "Pending review:",
        _format_transaction_rows(event.pending_transactions),
    ]
    if event.review_url:
        lines.extend(["", f"Review pending transactions: {event.review_url}"])
    return "\n".join(lines)


def _format_transaction_html_rows(items: list[SyncTransactionSummary]) -> str:
    if not items:
        return "<p>None</p>"
    rows = []
    for item in items:
        holder = escape(item.card_holder or "?")
        merchant = escape(item.merchant)
        if item.status == ExpenseStatus.PENDING:
            bucket = escape(item.suggested_bucket_name or "Unassigned")
            assignment = f"suggest: {bucket}"
        else:
            bucket = escape(item.bucket_name or "Unassigned")
            rule_type = "auto-learned" if item.status == ExpenseStatus.AUTO else "confirmed rule"
            assignment = f"{bucket} ({rule_type})"
        rows.append(
            "<tr>"
            f"<td>{escape(item.transaction_date.isoformat())}</td>"
            f"<td>{escape(f'${item.amount:,.2f}')}</td>"
            f"<td>{merchant}</td>"
            f"<td>{holder}</td>"
            f"<td>{assignment}</td>"
            "</tr>"
        )
    return (
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<thead><tr><th>Date</th><th>Amount</th><th>Merchant</th><th>Holder</th><th>Assignment</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def format_sync_email_html(event: SyncCompletedEvent) -> str:
    if event.error:
        return (
            f"<p>Gmail sync failed for <strong>{escape(event.tenant_name)}</strong>.</p>"
            f"<p><strong>Error:</strong> {escape(event.error)}</p>"
        )

    review_link = ""
    if event.review_url:
        review_link = (
            f"<p><a href='{escape(event.review_url)}'>Review pending transactions</a></p>"
        )

    return (
        f"<p>Gmail sync completed for <strong>{escape(event.tenant_name)}</strong>.</p>"
        "<p>"
        f"{event.messages_checked} messages checked, "
        f"{event.imported} imported, "
        f"{event.auto_assigned} auto-assigned, "
        f"{event.pending} pending review, "
        f"{event.skipped} skipped."
        "</p>"
        "<h3>Auto-assigned</h3>"
        f"{_format_transaction_html_rows(event.auto_assigned_transactions)}"
        "<h3>Pending review</h3>"
        f"{_format_transaction_html_rows(event.pending_transactions)}"
        f"{review_link}"
    )


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
