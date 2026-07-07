from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import date
from pathlib import Path

from expenses_tracker.db import Database
from expenses_tracker.income_parser import (
    match_income_rule,
    parse_income_amount,
    parse_income_description,
    parse_income_message,
)
from expenses_tracker.models import IncomeRule

LBS_BODY = """Greetings from LBS Financial Credit Union!

ACH transaction with amount larger than $1.00 just posted to your account Checking **3231 - S:9.

Date: 6/5/2026
Description: Deposit-ACH-A-SUNCOAST PROPERT SunCoast Propert (WEB PMTS)
Note:
Amount: $1,312.60
Balance: $16,810.73.

Sincerely,
LBS Financial Credit Union"""


def _rule(
    rule_id: int = 1,
    match_text: str = "ACME PAYROLL",
    source_name: str = "ACME salary",
    bucket_id: int | None = None,
    person: str | None = "Juan",
) -> IncomeRule:
    return IncomeRule(
        id=rule_id,
        match_text=match_text,
        source_name=source_name,
        bucket_id=bucket_id,
        bucket_name=None,
        person=person,
    )


def _gmail_message(
    *,
    message_id: str = "msg-1",
    subject: str = "Direct deposit notice",
    body: str = "ACME PAYROLL deposited $2,512.34 on 05/29/2026 to your account.",
) -> dict:
    encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    return {
        "id": message_id,
        "internalDate": "1748505600000",
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "alerts@bank.com"},
                {"name": "Date", "value": "Fri, 29 May 2026 08:00:00 -0400"},
            ],
            "mimeType": "text/plain",
            "body": {"data": encoded},
        },
    }


class IncomeRuleMatchingTests(unittest.TestCase):
    def test_matches_case_insensitive_in_body(self) -> None:
        rule = _rule(match_text="acme payroll")
        matched = match_income_rule("Deposit notice", "Your ACME Payroll arrived", [rule])
        self.assertIs(matched, rule)

    def test_matches_in_subject(self) -> None:
        rule = _rule(match_text="ACME PAYROLL")
        matched = match_income_rule("ACME PAYROLL payment", "no match here", [rule])
        self.assertIs(matched, rule)

    def test_longest_match_wins(self) -> None:
        generic = _rule(rule_id=1, match_text="payroll")
        specific = _rule(rule_id=2, match_text="ACME PAYROLL")
        matched = match_income_rule("", "ACME PAYROLL deposit", [generic, specific])
        self.assertIs(matched, specific)

    def test_no_match_returns_none(self) -> None:
        rule = _rule(match_text="ACME PAYROLL")
        self.assertIsNone(match_income_rule("Other", "Nothing relevant", [rule]))


class IncomeAmountParsingTests(unittest.TestCase):
    def test_parses_deposit_amount(self) -> None:
        self.assertEqual(parse_income_amount("deposited $2,512.34 today"), 2512.34)

    def test_parses_net_pay(self) -> None:
        self.assertEqual(parse_income_amount("Net pay: $1,000.00"), 1000.0)

    def test_parses_bare_dollar_amount(self) -> None:
        self.assertEqual(parse_income_amount("you received $99.99"), 99.99)

    def test_returns_none_without_amount(self) -> None:
        self.assertIsNone(parse_income_amount("no money mentioned"))

    def test_prefers_amount_label_and_ignores_balance_and_threshold(self) -> None:
        self.assertEqual(parse_income_amount(LBS_BODY), 1312.60)

    def test_ignores_balance_without_amount_label(self) -> None:
        text = "Balance: $16,810.73\nYou received $25.00 today."
        self.assertEqual(parse_income_amount(text), 25.00)

    def test_ignores_threshold_in_collapsed_html_text(self) -> None:
        text = (
            "ACH transaction with amount larger than $1.00 posted. "
            "Amount: $1,312.60 Balance: $16,810.73."
        )
        self.assertEqual(parse_income_amount(text), 1312.60)


class IncomeDescriptionParsingTests(unittest.TestCase):
    def test_parses_description_line(self) -> None:
        self.assertEqual(
            parse_income_description(LBS_BODY),
            "Deposit-ACH-A-SUNCOAST PROPERT SunCoast Propert (WEB PMTS)",
        )

    def test_parses_description_in_collapsed_html_text(self) -> None:
        text = (
            "Date: 6/5/2026 Description: Deposit-ACH-A-SUNCOAST PROPERT "
            "SunCoast Propert (WEB PMTS) Note: Amount: $1,312.60"
        )
        self.assertEqual(
            parse_income_description(text),
            "Deposit-ACH-A-SUNCOAST PROPERT SunCoast Propert (WEB PMTS)",
        )

    def test_returns_none_without_description(self) -> None:
        self.assertIsNone(parse_income_description("Amount: $20.00"))


class IncomeMessageParsingTests(unittest.TestCase):
    def test_parses_matching_message(self) -> None:
        rule = _rule()
        parsed = parse_income_message(_gmail_message(), [rule])
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.amount, 2512.34)
        self.assertEqual(parsed.received_date, date(2026, 5, 29))
        self.assertIs(parsed.rule, rule)

    def test_returns_none_when_no_rule_matches(self) -> None:
        rule = _rule(match_text="OTHER EMPLOYER")
        self.assertIsNone(parse_income_message(_gmail_message(), [rule]))

    def test_returns_none_when_amount_missing(self) -> None:
        rule = _rule()
        message = _gmail_message(body="ACME PAYROLL was processed.")
        self.assertIsNone(parse_income_message(message, [rule]))

    def test_parses_lbs_ach_alert(self) -> None:
        rule = _rule(match_text="SUNCOAST PROPERT", source_name="SunCoast rent")
        message = _gmail_message(subject="ACH Transaction Alert", body=LBS_BODY)
        parsed = parse_income_message(message, [rule])
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.amount, 1312.60)
        self.assertEqual(parsed.received_date, date(2026, 6, 5))
        self.assertEqual(
            parsed.description,
            "Deposit-ACH-A-SUNCOAST PROPERT SunCoast Propert (WEB PMTS)",
        )


class IncomeDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "test.db"
        global_db = Database(db_path)
        tenant_id = global_db.get_default_tenant_id()
        self.global_db = global_db
        self.db = Database(db_path, tenant_id=tenant_id)
        self.tenant_id = tenant_id

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_income_bucket_and_rule_round_trip(self) -> None:
        bucket = self.db.create_income_bucket("Salary")
        rule = self.db.create_income_rule(
            match_text="ACME PAYROLL",
            source_name="ACME salary",
            bucket_id=bucket.id,
            person="Juan",
        )
        rules = self.db.list_income_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].bucket_name, "Salary")
        self.assertEqual(rules[0].person, "Juan")

        self.db.update_income_rule(
            rule.id,
            match_text="ACME PAYROLL",
            source_name="ACME wages",
            bucket_id=bucket.id,
            person="Debora",
        )
        updated = self.db.get_income_rule(rule.id)
        self.assertEqual(updated.source_name, "ACME wages")
        self.assertEqual(updated.person, "Debora")

    def test_delete_bucket_in_use_is_rejected(self) -> None:
        bucket = self.db.create_income_bucket("Salary")
        self.db.insert_income(
            gmail_message_id="msg-1",
            received_date=date(2026, 5, 29),
            allocated_month="2026-06",
            source="ACME salary",
            amount=2500.0,
            bucket_id=bucket.id,
            person="Juan",
        )
        with self.assertRaises(ValueError):
            self.db.delete_income_bucket(bucket.id)

    def test_allocated_month_filtering_and_update(self) -> None:
        income_id = self.db.insert_income(
            gmail_message_id="msg-1",
            received_date=date(2026, 5, 29),
            allocated_month="2026-05",
            source="ACME salary",
            amount=2500.0,
            person="Juan",
        )
        self.assertEqual(len(self.db.list_incomes(month="2026-05")), 1)
        self.assertEqual(len(self.db.list_incomes(month="2026-06")), 0)

        # Paycheck received two days early gets reallocated to the next month.
        self.db.update_income(
            income_id,
            allocated_month="2026-06",
            bucket_id=None,
            person="Juan",
        )
        self.assertEqual(len(self.db.list_incomes(month="2026-05")), 0)
        incomes = self.db.list_incomes(month="2026-06")
        self.assertEqual(len(incomes), 1)
        self.assertEqual(incomes[0].received_date, date(2026, 5, 29))

    def test_income_exists_and_monthly_totals(self) -> None:
        bucket = self.db.create_income_bucket("Salary")
        self.db.insert_income(
            gmail_message_id="msg-1",
            received_date=date(2026, 6, 1),
            allocated_month="2026-06",
            source="ACME salary",
            amount=2500.0,
            bucket_id=bucket.id,
            person="Juan",
        )
        self.db.insert_income(
            gmail_message_id="msg-2",
            received_date=date(2026, 6, 15),
            allocated_month="2026-06",
            source="Side gig",
            amount=300.0,
            person="Debora",
        )
        self.assertTrue(self.db.income_exists("msg-1"))
        self.assertFalse(self.db.income_exists("msg-99"))

        totals = self.db.monthly_income_totals("2026-06")
        self.assertEqual(len(totals), 2)
        self.assertEqual(sum(row[2] for row in totals), 2800.0)

        juan_totals = self.db.monthly_income_totals("2026-06", person="Juan")
        self.assertEqual(sum(row[2] for row in juan_totals), 2500.0)

        person_totals = self.db.monthly_income_person_totals("2026-06")
        self.assertEqual(person_totals, [("Juan", 2500.0, 1), ("Debora", 300.0, 1)])

    def test_tenant_income_query_round_trip(self) -> None:
        self.global_db.update_tenant_settings(
            self.tenant_id,
            income_gmail_search_query="from:(payroll.com) subject:(payment)",
        )
        tenant = self.global_db.get_tenant(self.tenant_id)
        self.assertEqual(
            tenant.income_gmail_search_query,
            "from:(payroll.com) subject:(payment)",
        )

    def test_withdrawal_rule_round_trip(self) -> None:
        expense_bucket = self.db.create_bucket("Mortgage")
        rule = self.db.create_income_rule(
            match_text="CMG MORTGAGE",
            source_name="CMG Mortgage",
            bucket_id=99,  # ignored for withdrawal rules
            direction="withdrawal",
            expense_bucket_id=expense_bucket.id,
            person="Juan",
        )
        self.assertEqual(rule.direction, "withdrawal")
        self.assertIsNone(rule.bucket_id)
        self.assertEqual(rule.expense_bucket_id, expense_bucket.id)
        self.assertEqual(rule.expense_bucket_name, "Mortgage")

        with self.assertRaises(ValueError):
            self.db.create_income_rule(
                match_text="BAD DIRECTION",
                source_name="Bad",
                direction="sideways",
            )
        with self.assertRaises(ValueError):
            self.db.delete_bucket(expense_bucket.id)


class WithdrawalSyncTests(unittest.TestCase):
    QUERY = "from:(lbsfcu.org)"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "test.db"
        global_db = Database(db_path)
        self.tenant_id = global_db.get_default_tenant_id()
        self.tenant = global_db.get_tenant(self.tenant_id)
        self.db = Database(db_path, tenant_id=self.tenant_id)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _sync_service(self, messages: list[dict]):
        from expenses_tracker.bucket_matcher import BucketMatcher
        from expenses_tracker.sync import ExpenseSyncService

        class StubGmail:
            def __init__(self, stubbed: list[dict]) -> None:
                self.stubbed = stubbed

            def has_token(self) -> bool:
                return True

            def authenticate(self) -> None:
                pass

            def fetch_messages(self, query: str, max_results: int | None = None) -> list[dict]:
                return self.stubbed

        return ExpenseSyncService(
            None,
            self.db,
            StubGmail(messages),
            BucketMatcher(self.db),
            tenant=self.tenant,
        )

    def _create_withdrawal_query(
        self,
        *,
        match_text: str,
        source_name: str,
        expense_bucket_id: int | None = None,
        person: str | None = None,
        person_mode: str = "fixed",
    ):
        return self.db.create_email_query(
            name=source_name,
            query=self.QUERY,
            kind="withdrawal",
            match_text=match_text,
            merchant_label="Description",
            amount_label="Amount",
            merchant_name=source_name,
            expense_bucket_id=expense_bucket_id,
            person=person,
            person_mode=person_mode,
        )

    def _create_income_query(
        self,
        *,
        match_text: str,
        source_name: str,
        income_bucket_id: int | None = None,
    ):
        return self.db.create_email_query(
            name=source_name,
            query=self.QUERY,
            kind="income",
            match_text=match_text,
            merchant_label="Description",
            amount_label="Amount",
            merchant_name=source_name,
            income_bucket_id=income_bucket_id,
        )

    def test_withdrawal_email_imports_as_expense(self) -> None:
        from expenses_tracker.models import ExpenseStatus

        bucket = self.db.create_bucket("Mortgage")
        self._create_withdrawal_query(
            match_text="CMG MORTGAGE",
            source_name="CMG Mortgage",
            expense_bucket_id=bucket.id,
            person="Juan",
        )
        body = (
            "Withdrawal transaction just posted to your account Checking **3231.\n\n"
            "Date: 6/1/2026\n"
            "Description: Withdrawal-ACH-A-CMG MORTGAGE INC WEBCMG MORTGAGE INC (CMG MORTGA)\n"
            "Amount: $3,145.20\n"
            "Balance: $10,000.00."
        )
        message = _gmail_message(
            message_id="msg-w1",
            subject="ACH Transaction Alert",
            body=body,
        )
        result = self._sync_service([message]).sync(record_notification=False)

        self.assertEqual(result["withdrawals_imported"], 1)
        self.assertEqual(result["income_imported"], 0)
        expenses = self.db.list_expenses()
        self.assertEqual(len(expenses), 1)
        expense = expenses[0]
        self.assertEqual(expense.merchant, "CMG Mortgage")
        self.assertEqual(expense.amount, 3145.20)
        self.assertEqual(expense.transaction_date, date(2026, 6, 1))
        self.assertEqual(expense.bucket_id, bucket.id)
        self.assertEqual(expense.status, ExpenseStatus.AUTO)
        self.assertEqual(expense.card_holder, "Juan")

        rerun = self._sync_service([message]).sync(record_notification=False)
        self.assertEqual(rerun["withdrawals_imported"], 0)
        self.assertEqual(len(self.db.list_expenses()), 1)

    def test_withdrawal_without_bucket_is_pending(self) -> None:
        from expenses_tracker.models import ExpenseStatus

        self._create_withdrawal_query(
            match_text="CMG MORTGAGE",
            source_name="CMG Mortgage",
        )
        message = _gmail_message(
            message_id="msg-w2",
            subject="ACH Transaction Alert",
            body="Description: Withdrawal-ACH-A-CMG MORTGAGE INC\nAmount: $3,145.20",
        )
        result = self._sync_service([message]).sync(record_notification=False)
        self.assertEqual(result["withdrawals_imported"], 1)
        expense = self.db.list_expenses()[0]
        self.assertIsNone(expense.bucket_id)
        self.assertEqual(expense.status, ExpenseStatus.PENDING)

    GOLDMAN_BODY = (
        "Greetings from LBS Financial Credit Union!\n\n"
        "A transaction with amount larger than $1.00 just posted to your account "
        "Checking **3231 - S:9.\n\n"
        "Date: 7/3/2026\n"
        "Description: Withdrawal-ACH-A-GOLDMAN SACHS BA WEBGOLDMAN SACHS BA (TRANSFER)\n"
        "Note:\n"
        "Amount: $5,052.01\n"
        "Balance: $3,354.70.\n\n"
        "Sincerely,\nLBS Financial Credit Union"
    )

    def test_bank_alert_in_main_query_uses_withdrawal_rule(self) -> None:
        bucket = self.db.create_bucket("Investments")
        self._create_withdrawal_query(
            match_text="GOLDMAN SACHS",
            source_name="Goldman Sachs transfer",
            expense_bucket_id=bucket.id,
        )
        message = _gmail_message(
            message_id="msg-gs1",
            subject="Transaction Alert",
            body=self.GOLDMAN_BODY,
        )
        result = self._sync_service([message]).sync(record_notification=False)

        self.assertEqual(result["withdrawals_imported"], 1)
        self.assertEqual(result["imported"], 1)
        expenses = self.db.list_expenses()
        self.assertEqual(len(expenses), 1)
        self.assertEqual(expenses[0].merchant, "Goldman Sachs transfer")
        self.assertEqual(expenses[0].amount, 5052.01)
        self.assertEqual(expenses[0].transaction_date, date(2026, 7, 3))

    def test_unmatched_bank_alert_is_not_imported_as_expense(self) -> None:
        self._create_withdrawal_query(
            match_text="CMG MORTGAGE",
            source_name="CMG Mortgage",
        )
        message = _gmail_message(
            message_id="msg-gs2",
            subject="Transaction Alert",
            body=self.GOLDMAN_BODY,
        )
        result = self._sync_service([message]).sync(record_notification=False)

        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["withdrawals_imported"], 0)
        self.assertEqual(len(self.db.list_expenses()), 0)
        self.assertEqual(len(self.db.list_incomes()), 0)

    def test_deposit_rules_still_import_income(self) -> None:
        self._create_income_query(
            match_text="SUNCOAST PROPERT",
            source_name="Rental",
        )
        message = _gmail_message(
            message_id="msg-d1",
            subject="ACH Transaction Alert",
            body=LBS_BODY,
        )
        result = self._sync_service([message]).sync(record_notification=False)
        self.assertEqual(result["income_imported"], 1)
        self.assertEqual(result["withdrawals_imported"], 0)
        self.assertEqual(len(self.db.list_expenses()), 0)
        self.assertEqual(len(self.db.list_incomes()), 1)


if __name__ == "__main__":
    unittest.main()
