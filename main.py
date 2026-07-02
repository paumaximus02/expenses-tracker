from __future__ import annotations

import logging
import os
from datetime import date
from getpass import getpass

import click

from expenses_tracker.auth import hash_password, validate_password

from expenses_tracker.bucket_matcher import analyze_merchants
from expenses_tracker.config import get_settings, resolve_gmail_credentials_path
from expenses_tracker.dates import app_month
from expenses_tracker.gmail_client import GmailClient
from expenses_tracker.models import ExpenseStatus, MatchType
from expenses_tracker.services import build_global_db, build_services

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


def _build_services(tenant_id: int = 1) -> tuple:
    return build_services(tenant_id=tenant_id)


def _tenant_option():
    return click.option(
        "--tenant-id",
        default=1,
        show_default=True,
        help="Household tenant id for CLI commands.",
    )


@click.group()
def cli() -> None:
    """Track monthly expenses from Gmail transaction emails."""


@cli.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=5000, show_default=True)
@click.option(
    "--no-auth",
    is_flag=True,
    help="Disable login for this run (same as AUTH_DISABLED=1).",
)
def serve_command(host: str, port: int, no_auth: bool) -> None:
    """Start the web UI for reviewing expenses."""
    if no_auth:
        os.environ["AUTH_DISABLED"] = "1"
    from expenses_tracker.web import create_app

    app = create_app()
    click.echo(f"Open http://{host}:{port}")
    if no_auth:
        click.echo("Auth disabled for this run.")
    app.run(host=host, port=port, debug=False)


@cli.command("create-user")
@click.option("--email", required=True, help="Account email address.")
@_tenant_option()
def create_user_command(email: str, tenant_id: int) -> None:
    """Create a web login account."""
    auth_db = build_global_db()
    password = getpass("Password: ")
    confirm = getpass("Confirm password: ")
    password_error = validate_password(password)
    if password_error:
        raise click.ClickException(password_error)
    if password != confirm:
        raise click.ClickException("Passwords do not match.")
    try:
        user = auth_db.create_user(email, hash_password(password), tenant_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Created account for {user.email} on tenant {tenant_id}.")


@cli.command("auth")
@_tenant_option()
def auth_command(tenant_id: int) -> None:
    """Run Gmail OAuth and store the token for a household."""
    settings = get_settings()
    gmail = GmailClient(resolve_gmail_credentials_path(settings), settings.gmail_token_path)
    gmail.authenticate()
    auth_db = build_global_db()
    auth_db.update_tenant_gmail_token(tenant_id, gmail.export_token_json())
    click.echo(f"Authenticated tenant {tenant_id}. Token saved in the database.")


@cli.command("sync-scheduled")
@click.option(
    "--all-tenants/--tenant",
    "all_tenants",
    default=True,
    show_default=True,
    help="Sync all Gmail-connected households or one tenant.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Run even if the last sync is still fresh.",
)
@_tenant_option()
def sync_scheduled_command(tenant_id: int, all_tenants: bool, force: bool) -> None:
    """Run scheduled Gmail sync (for cron/Task Scheduler or local testing)."""
    from expenses_tracker.scheduled_sync import run_scheduled_sync_all, run_scheduled_sync_for_tenant

    settings = get_settings()
    only_if_stale = not force
    if all_tenants:
        results = run_scheduled_sync_all(settings, only_if_stale=only_if_stale)
        if not results:
            click.echo("No Gmail-connected households found.")
            return
        for result in results:
            _echo_scheduled_result(result)
        return

    result = run_scheduled_sync_for_tenant(settings, tenant_id, only_if_stale=only_if_stale)
    _echo_scheduled_result(result)


def _echo_scheduled_result(result: dict[str, object]) -> None:
    tenant_id = result.get("tenant_id", "?")
    if not result.get("started"):
        click.echo(f"Tenant {tenant_id}: skipped ({result.get('reason', 'unknown')}).")
        return
    if result.get("error"):
        click.echo(f"Tenant {tenant_id}: failed — {result['error']}")
        return
    click.echo(
        f"Tenant {tenant_id}: imported {result.get('imported', 0)}, "
        f"pending {result.get('pending', 0)}, skipped {result.get('skipped', 0)}."
    )


@cli.command("worker")
@click.option(
    "--interval",
    default=lambda: get_settings().sync_interval_seconds,
    show_default="SYNC_INTERVAL_SECONDS",
    help="Seconds between scheduled sync runs.",
)
def worker_command(interval: int) -> None:
    """Run scheduled sync in a loop (optional alternative to OS cron)."""
    import time

    from expenses_tracker.scheduled_sync import run_scheduled_sync_all

    settings = get_settings()
    click.echo(f"Worker started; syncing every {interval} seconds. Press Ctrl+C to stop.")
    while True:
        results = run_scheduled_sync_all(settings, only_if_stale=True)
        for result in results:
            _echo_scheduled_result(result)
        time.sleep(interval)


@cli.command("sync")
@_tenant_option()
def sync_command(tenant_id: int) -> None:
    """Fetch new Gmail messages and import expenses."""
    _, sync = _build_services(tenant_id)
    result = sync.sync()
    message = (
        "Sync complete: "
        f"{result['messages_checked']} messages checked, "
        f"{result['imported']} imported, "
        f"{result['auto_assigned']} auto-assigned, "
        f"{result['pending']} pending review, "
        f"{result['skipped']} skipped."
    )
    if result.get("income_checked"):
        message += (
            f" Income: {result['income_checked']} messages checked, "
            f"{result['income_imported']} imported."
        )
    click.echo(message)


@cli.command("repair-dates")
@_tenant_option()
def repair_dates_command(tenant_id: int) -> None:
    """Re-parse Gmail messages and fix transaction dates."""
    _, sync = _build_services(tenant_id)
    result = sync.repair_transaction_dates()
    click.echo(
        "Date repair complete: "
        f"{result['updated']} updated, "
        f"{result['unchanged']} unchanged, "
        f"{result['missing']} missing/unparseable."
    )


@cli.command("repair-cards")
@_tenant_option()
def repair_cards_command(tenant_id: int) -> None:
    """Re-parse Gmail messages and fix card holder assignments."""
    _, sync = _build_services(tenant_id)
    result = sync.repair_card_holders()
    click.echo(
        "Card repair complete: "
        f"{result['updated']} updated, "
        f"{result['unchanged']} unchanged, "
        f"{result['missing']} missing/unparseable."
    )


@cli.command("review")
@click.option("--limit", default=20, show_default=True, help="Max rows to show.")
@_tenant_option()
def review_command(limit: int, tenant_id: int) -> None:
    """Show pending expenses with suggested buckets."""
    db, _ = _build_services(tenant_id)
    pending = db.list_expenses(status=ExpenseStatus.PENDING)[:limit]
    if not pending:
        click.echo("No pending expenses.")
        return

    for expense in pending:
        suggestion = expense.suggested_bucket_name or "None"
        holder = expense.card_holder or expense.card_last_four or "?"
        click.echo(
            f"[{expense.id}] {expense.transaction_date} | "
            f"${expense.amount:,.2f} | {expense.merchant} | {holder} -> suggest: {suggestion}"
        )


@cli.command("confirm")
@click.argument("expense_id", type=int)
@click.argument("bucket_name")
@click.option("--no-rule", is_flag=True, help="Do not create a merchant rule.")
@_tenant_option()
def confirm_command(expense_id: int, bucket_name: str, no_rule: bool, tenant_id: int) -> None:
    """Confirm one expense bucket and optionally teach a merchant rule."""
    _, sync = _build_services(tenant_id)
    sync.confirm_expense(expense_id, bucket_name, create_rule=not no_rule)
    click.echo(f"Expense {expense_id} assigned to '{bucket_name}'.")


@cli.command("analyze")
@_tenant_option()
def analyze_command(tenant_id: int) -> None:
    """Suggest merchant groups and buckets for pending expenses."""
    db, _ = _build_services(tenant_id)
    merchants = db.distinct_merchants(pending_only=True)
    if not merchants:
        click.echo("No pending merchants to analyze.")
        return

    suggestions = analyze_merchants(merchants, dismissed_keys=db.list_dismissed_group_keys())
    for index, suggestion in enumerate(suggestions, start=1):
        merchant_list = ", ".join(suggestion.merchants)
        bucket_label = suggestion.suggested_bucket or "Review individually"
        click.echo(
            f"\nGroup {index}: {merchant_list}\n"
            f"  Suggested bucket: {bucket_label}\n"
            f"  Reason: {suggestion.reason}\n"
            f"  Dismiss key: {suggestion.group_key}"
        )
    click.echo(
        "\nApply a group with:\n"
        "  python main.py apply-group --merchants \"Costco Gas\" --bucket Gas"
    )


@cli.command("apply-group")
@click.option("--merchants", required=True, help="Comma-separated merchant names.")
@click.option("--bucket", "bucket_name", required=True, help="Bucket name to assign.")
@click.option(
    "--match-type",
    type=click.Choice(["exact", "contains"], case_sensitive=False),
    default="exact",
    show_default=True,
)
@_tenant_option()
def apply_group_command(merchants: str, bucket_name: str, match_type: str, tenant_id: int) -> None:
    """Confirm a merchant group and create reusable rules."""
    _, sync = _build_services(tenant_id)
    merchant_list = [item.strip() for item in merchants.split(",") if item.strip()]
    updated = sync.apply_group_suggestion(
        merchant_list,
        bucket_name,
        match_type=MatchType(match_type.lower()),
    )
    click.echo(f"Applied '{bucket_name}' to {updated} pending expense(s) and saved rule(s).")


@cli.command("buckets")
@_tenant_option()
def buckets_command(tenant_id: int) -> None:
    """List expense buckets."""
    db, _ = _build_services(tenant_id)
    for bucket in db.list_buckets():
        indent = f"  └─ " if bucket.parent_id else ""
        click.echo(f"{bucket.id}: {indent}{bucket.display_path}")


@cli.command("rules")
@_tenant_option()
def rules_command(tenant_id: int) -> None:
    """List learned merchant-to-bucket rules."""
    db, _ = _build_services(tenant_id)
    rules = db.list_merchant_rules()
    if not rules:
        click.echo("No merchant rules yet.")
        return
    for rule in rules:
        confirmed = "confirmed" if rule.confirmed_by_user else "auto"
        click.echo(
            f"{rule.merchant_pattern} ({rule.match_type.value}) -> "
            f"{rule.bucket_name} [{confirmed}]"
        )


@cli.command("report")
@click.option("--month", default=lambda: app_month(), show_default="current month")
@click.option("--person", default=None, help="Filter by card holder, e.g. Juan or Debora.")
@_tenant_option()
def report_command(month: str, person: str | None, tenant_id: int) -> None:
    """Show monthly totals by bucket."""
    db, _ = _build_services(tenant_id)
    totals = db.monthly_totals(month, card_holder=person)
    if not totals:
        label = f"No expenses for {month}"
        if person:
            label += f" ({person})"
        click.echo(f"{label}.")
        return

    title = f"Monthly report for {month}"
    if person:
        title += f" ({person})"
    click.echo(f"{title}\n")
    grand_total = 0.0
    for bucket_id, bucket_name, total, count in totals:
        grand_total += total
        click.echo(f"  {bucket_name:<16} ${total:>10,.2f}  ({count} txns)")
    click.echo(f"\n  {'TOTAL':<16} ${grand_total:>10,.2f}")


if __name__ == "__main__":
    cli()
