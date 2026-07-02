from __future__ import annotations

import logging
from datetime import datetime, timezone

from expenses_tracker.bucket_matcher import BucketMatcher, normalize_merchant
from expenses_tracker.config import Settings
from expenses_tracker.db import Database
from expenses_tracker.delivery.dispatcher import NotificationDispatcher
from expenses_tracker.email_parser import parse_gmail_message
from expenses_tracker.gmail_client import GmailClient
from expenses_tracker.income_parser import parse_income_message
from expenses_tracker.models import ExpenseStatus, MatchType, Tenant
from expenses_tracker.tenancy import global_db

logger = logging.getLogger(__name__)


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

    def sync(self, *, record_notification: bool = True) -> dict[str, int]:
        if not self.gmail.has_token():
            raise RuntimeError("Gmail is not connected for this household. Connect Gmail in Settings.")
        synced_at = datetime.now(timezone.utc)
        try:
            self.gmail.authenticate()
            messages = self.gmail.fetch_messages(self.tenant.gmail_search_query)
            rules = self.matcher.reload_rules()
            logger.info(
                "Fetched %s Gmail messages for query: %s",
                len(messages),
                self.tenant.gmail_search_query,
            )

            imported = 0
            auto_assigned = 0
            pending = 0
            skipped = 0
            imported_ids: list[int] = []

            for message in messages:
                message_id = message["id"]
                if self.db.expense_exists(message_id):
                    skipped += 1
                    continue

                parsed = parse_gmail_message(message, card_holders=self.tenant.card_holders)
                if parsed is None:
                    skipped += 1
                    continue

                matched_rule = self.matcher.match(parsed.merchant, rules)
                suggested_bucket_id = None
                status = ExpenseStatus.PENDING

                if matched_rule:
                    bucket_id = matched_rule.bucket_id
                    suggested_bucket_id = matched_rule.bucket_id
                    status = ExpenseStatus.AUTO if matched_rule.confirmed_by_user else ExpenseStatus.CONFIRMED
                    auto_assigned += 1
                else:
                    bucket_id = None
                    suggested_bucket_id, _ = self.matcher.suggest_bucket(parsed.merchant, rules)
                    pending += 1

                expense_id = self.db.insert_expense(
                    gmail_message_id=parsed.gmail_message_id,
                    transaction_date=parsed.transaction_date,
                    merchant=parsed.merchant,
                    merchant_normalized=normalize_merchant(parsed.merchant),
                    amount=parsed.amount,
                    currency=parsed.currency,
                    bucket_id=bucket_id,
                    suggested_bucket_id=suggested_bucket_id,
                    status=status,
                    email_subject=parsed.email_subject,
                    email_from=parsed.email_from,
                    card_last_four=parsed.card_last_four,
                    card_holder=parsed.card_holder,
                )
                imported_ids.append(expense_id)
                imported += 1

            income_result = self.sync_income()
            withdrawal_ids = income_result.pop("withdrawal_expense_ids", [])
            imported_ids.extend(withdrawal_ids)

            self.db.set_sync_value("last_sync_at", synced_at.isoformat())
            result = {
                "messages_checked": len(messages),
                "imported": imported,
                "auto_assigned": auto_assigned,
                "pending": pending,
                "skipped": skipped,
                "notification_id": None,
                **income_result,
            }
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

    def sync_income(self) -> dict[str, object]:
        """Import bank emails matching the household's income/withdrawal rules.

        Deposit rules create income entries; withdrawal rules create expenses.
        """
        empty: dict[str, object] = {
            "income_checked": 0,
            "income_imported": 0,
            "income_skipped": 0,
            "withdrawals_imported": 0,
            "withdrawal_expense_ids": [],
        }
        query = (self.tenant.income_gmail_search_query or "").strip()
        rules = self.db.list_income_rules()
        if not query or not rules:
            return empty

        messages = self.gmail.fetch_messages(query)
        logger.info(
            "Fetched %s Gmail messages for income query: %s",
            len(messages),
            query,
        )
        imported = 0
        skipped = 0
        withdrawal_expense_ids: list[int] = []
        for message in messages:
            message_id = message["id"]
            if self.db.income_exists(message_id) or self.db.expense_exists(message_id):
                skipped += 1
                continue

            parsed = parse_income_message(message, rules)
            if parsed is None:
                skipped += 1
                continue

            if parsed.rule.direction == "withdrawal":
                merchant = parsed.rule.source_name
                bucket_id = parsed.rule.expense_bucket_id
                expense_id = self.db.insert_expense(
                    gmail_message_id=parsed.gmail_message_id,
                    transaction_date=parsed.received_date,
                    merchant=merchant,
                    merchant_normalized=normalize_merchant(merchant),
                    amount=parsed.amount,
                    currency=parsed.currency,
                    bucket_id=bucket_id,
                    suggested_bucket_id=bucket_id,
                    status=ExpenseStatus.AUTO if bucket_id else ExpenseStatus.PENDING,
                    email_subject=parsed.email_subject,
                    email_from=parsed.email_from,
                    card_last_four=None,
                    card_holder=parsed.rule.person,
                )
                withdrawal_expense_ids.append(expense_id)
                continue

            self.db.insert_income(
                gmail_message_id=parsed.gmail_message_id,
                received_date=parsed.received_date,
                allocated_month=parsed.received_date.strftime("%Y-%m"),
                source=parsed.description or parsed.rule.source_name,
                amount=parsed.amount,
                currency=parsed.currency,
                bucket_id=parsed.rule.bucket_id,
                person=parsed.rule.person,
                email_subject=parsed.email_subject,
                email_from=parsed.email_from,
            )
            imported += 1

        return {
            "income_checked": len(messages),
            "income_imported": imported,
            "income_skipped": skipped,
            "withdrawals_imported": len(withdrawal_expense_ids),
            "withdrawal_expense_ids": withdrawal_expense_ids,
        }

    def repair_card_holders(self) -> dict[str, int]:
        if not self.gmail.has_token():
            raise RuntimeError("Gmail is not connected for this household. Connect Gmail in Settings.")
        self.gmail.authenticate()
        messages = self.gmail.fetch_messages(self.tenant.gmail_search_query)
        messages_by_id = {message["id"]: message for message in messages}

        updated = 0
        unchanged = 0
        missing = 0

        for expense in self.db.list_expenses():
            message = messages_by_id.get(expense.gmail_message_id)
            if message is None:
                missing += 1
                continue

            parsed = parse_gmail_message(message, card_holders=self.settings.card_holders)
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
            "messages_checked": len(messages),
            "updated": updated,
            "unchanged": unchanged,
            "missing": missing,
        }

    def repair_transaction_dates(self) -> dict[str, int]:
        if not self.gmail.has_token():
            raise RuntimeError("Gmail is not connected for this household. Connect Gmail in Settings.")
        self.gmail.authenticate()
        messages = self.gmail.fetch_messages(self.tenant.gmail_search_query)
        messages_by_id = {message["id"]: message for message in messages}

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
            "messages_checked": len(messages),
            "updated": updated,
            "unchanged": unchanged,
            "missing": missing,
        }

    def confirm_expense(self, expense_id: int, bucket_name: str, create_rule: bool = True) -> None:
        bucket = self.db.resolve_bucket_reference(bucket_name)

        expense = self.db.get_expense(expense_id)
        if expense is None:
            raise ValueError(f"Expense {expense_id} not found")

        self.db.confirm_expense(expense_id, bucket.id)
        if create_rule:
            self.db.upsert_merchant_rule(
                merchant_pattern=expense.merchant,
                bucket_id=bucket.id,
                match_type=MatchType.EXACT,
                confirmed_by_user=True,
            )

    def confirm_expense_by_id(self, expense_id: int, bucket_id: int, create_rule: bool = True) -> None:
        bucket = self.db.get_bucket(bucket_id)
        if bucket is None:
            raise ValueError(f"Bucket {bucket_id} not found")

        expense = self.db.get_expense(expense_id)
        if expense is None:
            raise ValueError(f"Expense {expense_id} not found")

        self.db.confirm_expense(expense_id, bucket.id)
        if create_rule:
            self.db.upsert_merchant_rule(
                merchant_pattern=expense.merchant,
                bucket_id=bucket.id,
                match_type=MatchType.EXACT,
                confirmed_by_user=True,
            )

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
            for expense in self.db.list_expenses(status=ExpenseStatus.PENDING):
                if normalize_merchant(expense.merchant) == normalize_merchant(merchant):
                    self.db.confirm_expense(expense.id, bucket.id)
                    updated += 1
        return updated