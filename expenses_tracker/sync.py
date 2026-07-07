from __future__ import annotations

import logging
from datetime import datetime, timezone

from expenses_tracker.bucket_matcher import BucketMatcher, normalize_merchant
from expenses_tracker.config import Settings
from expenses_tracker.db import Database
from expenses_tracker.delivery.dispatcher import NotificationDispatcher
from expenses_tracker.email_import import (
    apply_email_query,
    build_context,
    explain_match_failure,
    explain_unparsed,
    query_matches,
)
from expenses_tracker.email_parser import parse_gmail_message
from expenses_tracker.gmail_client import GmailClient
from expenses_tracker.models import ExpenseStatus, MatchType, Tenant
from expenses_tracker.tenancy import global_db

logger = logging.getLogger(__name__)


def _message_subject(message: dict) -> str:
    for header in message.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == "subject":
            return header.get("value", "")
    return ""


class ExpenseSyncService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        gmail: GmailClient,
        matcher: BucketMatcher,
        *,
        tenant: Tenant,
    ) -> None:
        self.settings = settings
        self.db = db
        self.gmail = gmail
        self.matcher = matcher
        self.tenant = tenant

    def _email_query_debug(self, message: str, *args: object) -> None:
        if self.settings is not None and self.settings.email_query_debug:
            logger.info("[email-query] " + message, *args)

    def _email_query_debug_enabled(self) -> bool:
        return self.settings is not None and self.settings.email_query_debug

    def sync(self, *, record_notification: bool = True) -> dict[str, int]:
        if not self.gmail.has_token():
            raise RuntimeError("Gmail is not connected for this household. Connect Gmail in Settings.")
        synced_at = datetime.now(timezone.utc)
        try:
            self.gmail.authenticate()
            result = self.sync_email_queries()
            self.db.set_sync_value("last_sync_at", synced_at.isoformat())
            imported_ids = result.pop("expense_ids", [])
            result["notification_id"] = None

            imported_expenses = [self.db.get_expense(expense_id) for expense_id in imported_ids]
            imported_expenses = [expense for expense in imported_expenses if expense is not None]

            if record_notification:
                auth_db = global_db(self.settings)
                dispatcher = NotificationDispatcher(self.settings, self.db, auth_db=auth_db)
                event = dispatcher.dispatch_sync_completed(
                    tenant=self.tenant,
                    result=result,
                    imported_expense_ids=imported_ids,
                    imported_expenses=imported_expenses,
                    synced_at=synced_at,
                )
                result["notification_id"] = event.notification_id
            return result
        except Exception as exc:
            if record_notification:
                auth_db = global_db(self.settings)
                dispatcher = NotificationDispatcher(self.settings, self.db, auth_db=auth_db)
                event = dispatcher.dispatch_sync_completed(
                    tenant=self.tenant,
                    result={
                        "messages_checked": 0,
                        "imported": 0,
                        "auto_assigned": 0,
                        "pending": 0,
                        "skipped": 0,
                    },
                    imported_expense_ids=[],
                    imported_expenses=[],
                    synced_at=synced_at,
                    error=str(exc),
                )
                logger.info("Recorded sync failure notification %s", event.notification_id)
            raise

    def sync_email_queries(self) -> dict[str, object]:
        """Import emails for each enabled query using its field configuration."""
        empty: dict[str, object] = {
            "messages_checked": 0,
            "imported": 0,
            "auto_assigned": 0,
            "pending": 0,
            "skipped": 0,
            "income_checked": 0,
            "income_imported": 0,
            "income_skipped": 0,
            "withdrawals_imported": 0,
            "expense_ids": [],
        }
        queries = self.db.list_email_queries(enabled_only=True)
        if not queries:
            logger.info("Skipping email import: no enabled email queries configured")
            return empty

        self._email_query_debug(
            "Starting email query sync for tenant %s (%s): %s enabled queries in order: %s",
            self.tenant.id,
            self.tenant.name,
            len(queries),
            ", ".join(query.name for query in queries),
        )

        messages_checked = 0
        imported = 0
        auto_assigned = 0
        pending = 0
        skipped = 0
        income_imported = 0
        withdrawals_imported = 0
        expense_ids: list[int] = []
        seen_ids: set[str] = set()

        for email_query in queries:
            if not (email_query.match_text or "").strip():
                logger.info(
                    "Skipping query '%s': match text not configured yet",
                    email_query.name,
                )
                self._email_query_debug(
                    "Query id=%s name=%r skipped: match text is empty",
                    email_query.id,
                    email_query.name,
                )
                continue

            self._email_query_debug(
                "Running query id=%s name=%r kind=%s match_text=%r gmail_search=%r",
                email_query.id,
                email_query.name,
                email_query.kind,
                email_query.match_text,
                email_query.query,
            )
            query_stats = {
                "fetched": 0,
                "already_claimed": 0,
                "already_imported": 0,
                "no_match": 0,
                "unparsed": 0,
                "imported_income": 0,
                "imported_expense": 0,
                "imported_withdrawal": 0,
            }

            messages = self.gmail.fetch_messages(email_query.query)
            query_stats["fetched"] = len(messages)
            logger.info(
                "Fetched %s Gmail messages for query '%s'",
                len(messages),
                email_query.name,
            )
            if not messages:
                self._email_query_debug(
                    "Query id=%s name=%r: Gmail search returned no messages",
                    email_query.id,
                    email_query.name,
                )

            for message in messages:
                message_id = message["id"]
                if message_id in seen_ids:
                    query_stats["already_claimed"] += 1
                    self._email_query_debug(
                        "Message %s skipped for query %r: already claimed by an earlier "
                        "query in this sync (subject=%r)",
                        message_id,
                        email_query.name,
                        _message_subject(message),
                    )
                    continue
                seen_ids.add(message_id)
                if self.db.expense_exists(message_id) or self.db.income_exists(message_id):
                    skipped += 1
                    query_stats["already_imported"] += 1
                    self._email_query_debug(
                        "Message %s skipped for query %r: already imported (subject=%r)",
                        message_id,
                        email_query.name,
                        _message_subject(message),
                    )
                    continue

                messages_checked += 1
                context = build_context(message)
                if not query_matches(context, email_query):
                    skipped += 1
                    query_stats["no_match"] += 1
                    self._email_query_debug(
                        "Message %s skipped for query %r: %s (subject=%r)",
                        message_id,
                        email_query.name,
                        explain_match_failure(context, email_query),
                        context.subject,
                    )
                    continue

                outcome, record_id = apply_email_query(
                    self.db,
                    context,
                    email_query,
                    card_holders=self.tenant.card_holders,
                    matcher=self.matcher,
                    debug=self._email_query_debug_enabled(),
                )
                if outcome == "unparsed" or record_id is None:
                    skipped += 1
                    query_stats["unparsed"] += 1
                    self._email_query_debug(
                        "Message %s skipped for query %r: unparsed — %s (subject=%r)",
                        message_id,
                        email_query.name,
                        explain_unparsed(
                            context,
                            email_query,
                            card_holders=self.tenant.card_holders,
                        ),
                        context.subject,
                    )
                    continue

                if outcome == "income":
                    income_imported += 1
                    query_stats["imported_income"] += 1
                    self._email_query_debug(
                        "Message %s imported as income id=%s via query %r (subject=%r)",
                        message_id,
                        record_id,
                        email_query.name,
                        context.subject,
                    )
                    continue

                expense_ids.append(record_id)
                imported += 1
                if email_query.kind == "withdrawal":
                    withdrawals_imported += 1
                    query_stats["imported_withdrawal"] += 1
                else:
                    query_stats["imported_expense"] += 1

                expense = self.db.get_expense(record_id)
                if expense is None:
                    continue
                if expense.status == ExpenseStatus.PENDING:
                    pending += 1
                else:
                    auto_assigned += 1
                self._email_query_debug(
                    "Message %s imported as expense id=%s status=%s via query %r "
                    "(merchant=%r amount=%s subject=%r)",
                    message_id,
                    record_id,
                    expense.status.value,
                    email_query.name,
                    expense.merchant,
                    expense.amount,
                    context.subject,
                )

            self._email_query_debug(
                "Query id=%s name=%r finished: fetched=%s already_claimed=%s "
                "already_imported=%s no_match=%s unparsed=%s imported_income=%s "
                "imported_expense=%s imported_withdrawal=%s",
                email_query.id,
                email_query.name,
                query_stats["fetched"],
                query_stats["already_claimed"],
                query_stats["already_imported"],
                query_stats["no_match"],
                query_stats["unparsed"],
                query_stats["imported_income"],
                query_stats["imported_expense"],
                query_stats["imported_withdrawal"],
            )

        self._email_query_debug(
            "Email query sync finished for tenant %s: messages_checked=%s imported=%s "
            "auto_assigned=%s pending=%s skipped=%s income_imported=%s "
            "withdrawals_imported=%s",
            self.tenant.id,
            messages_checked,
            imported,
            auto_assigned,
            pending,
            skipped,
            income_imported,
            withdrawals_imported,
        )

        return {
            "messages_checked": messages_checked,
            "imported": imported,
            "auto_assigned": auto_assigned,
            "pending": pending,
            "skipped": skipped,
            "income_checked": messages_checked,
            "income_imported": income_imported,
            "income_skipped": skipped,
            "withdrawals_imported": withdrawals_imported,
            "expense_ids": expense_ids,
        }

    def _fetch_messages_for_repair(self) -> dict[str, dict]:
        messages_by_id: dict[str, dict] = {}
        for email_query in self.db.list_email_queries(enabled_only=True):
            if email_query.kind != "expense":
                continue
            for message in self.gmail.fetch_messages(email_query.query):
                messages_by_id[message["id"]] = message
        return messages_by_id

    def repair_card_holders(self) -> dict[str, int]:
        if not self.gmail.has_token():
            raise RuntimeError("Gmail is not connected for this household. Connect Gmail in Settings.")
        self.gmail.authenticate()
        messages_by_id = self._fetch_messages_for_repair()

        updated = 0
        unchanged = 0
        missing = 0

        for expense in self.db.list_expenses():
            message = messages_by_id.get(expense.gmail_message_id)
            if message is None:
                missing += 1
                continue

            parsed = parse_gmail_message(message, card_holders=self.tenant.card_holders)
            if parsed is None:
                missing += 1
                continue

            if (
                parsed.card_last_four == expense.card_last_four
                and parsed.card_holder == expense.card_holder
            ):
                unchanged += 1
                continue

            self.db.update_expense_card(
                expense.id,
                card_last_four=parsed.card_last_four,
                card_holder=parsed.card_holder,
            )
            updated += 1

        return {
            "messages_checked": len(messages_by_id),
            "updated": updated,
            "unchanged": unchanged,
            "missing": missing,
        }

    def repair_transaction_dates(self) -> dict[str, int]:
        if not self.gmail.has_token():
            raise RuntimeError("Gmail is not connected for this household. Connect Gmail in Settings.")
        self.gmail.authenticate()
        messages_by_id = self._fetch_messages_for_repair()

        updated = 0
        unchanged = 0
        missing = 0

        for expense in self.db.list_expenses():
            message = messages_by_id.get(expense.gmail_message_id)
            if message is None:
                missing += 1
                continue

            parsed = parse_gmail_message(message, card_holders=self.tenant.card_holders)
            if parsed is None:
                missing += 1
                continue

            if parsed.transaction_date == expense.transaction_date:
                unchanged += 1
                continue

            self.db.update_expense_date(expense.id, parsed.transaction_date)
            updated += 1

        return {
            "messages_checked": len(messages_by_id),
            "updated": updated,
            "unchanged": unchanged,
            "missing": missing,
        }

    def _confirm_matching_pending(
        self,
        merchant: str,
        bucket_id: int,
        *,
        exclude_id: int | None = None,
    ) -> int:
        """Confirm pending expenses with the same normalized merchant. Returns count updated."""
        target = normalize_merchant(merchant)
        updated = 0
        for expense in self.db.list_expenses(status=ExpenseStatus.PENDING):
            if exclude_id is not None and expense.id == exclude_id:
                continue
            if normalize_merchant(expense.merchant) == target:
                self.db.confirm_expense(expense.id, bucket_id)
                updated += 1
        return updated

    def confirm_expense(self, expense_id: int, bucket_name: str, create_rule: bool = True) -> int:
        bucket = self.db.resolve_bucket_reference(bucket_name)
        return self.confirm_expense_by_id(expense_id, bucket.id, create_rule=create_rule)

    def confirm_expense_by_id(self, expense_id: int, bucket_id: int, create_rule: bool = True) -> int:
        bucket = self.db.get_bucket(bucket_id)
        if bucket is None:
            raise ValueError(f"Bucket {bucket_id} not found")

        expense = self.db.get_expense(expense_id)
        if expense is None:
            raise ValueError(f"Expense {expense_id} not found")

        self.db.confirm_expense(expense_id, bucket.id)
        batch_updated = 0
        if create_rule:
            self.db.upsert_merchant_rule(
                merchant_pattern=expense.merchant,
                bucket_id=bucket.id,
                match_type=MatchType.EXACT,
                confirmed_by_user=True,
            )
            batch_updated = self._confirm_matching_pending(
                expense.merchant,
                bucket.id,
                exclude_id=expense_id,
            )
        return batch_updated

    def apply_group_suggestion(
        self,
        merchants: list[str],
        bucket_name: str,
        match_type: MatchType = MatchType.EXACT,
    ) -> int:
        bucket = self.db.resolve_bucket_reference(bucket_name)
        return self.apply_group_suggestion_by_id(merchants, bucket.id, match_type)

    def apply_group_suggestion_by_id(
        self,
        merchants: list[str],
        bucket_id: int,
        match_type: MatchType = MatchType.EXACT,
    ) -> int:
        bucket = self.db.get_bucket(bucket_id)
        if bucket is None:
            raise ValueError(f"Bucket {bucket_id} not found")

        updated = 0
        for merchant in merchants:
            self.db.upsert_merchant_rule(
                merchant_pattern=merchant,
                bucket_id=bucket.id,
                match_type=match_type,
                confirmed_by_user=True,
            )
            updated += self._confirm_matching_pending(merchant, bucket.id)
        return updated
