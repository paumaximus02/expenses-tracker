"""Deterministic email import from user-configured Gmail queries."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from markupsafe import Markup, escape

from expenses_tracker.bucket_matcher import normalize_merchant
from expenses_tracker.citi_parser import (
    extract_card_last_four,
    extract_citi_transaction_block,
    format_citi_display_text,
    is_citi_alert,
)
from expenses_tracker.dates import resolve_transaction_date
from expenses_tracker.db import Database
from expenses_tracker.email_parser import _decode_body, parse_gmail_message
from expenses_tracker.income_parser import parse_income_amount
from expenses_tracker.models import EmailQuery, ExpenseStatus

if TYPE_CHECKING:
    from expenses_tracker.bucket_matcher import BucketMatcher

logger = logging.getLogger(__name__)

SAMPLE_EMAIL_BODY_CHARS = 800


@dataclass
class MessageContext:
    message_id: str
    subject: str
    sender: str
    body: str
    date_header: str | None
    internal_date: str | int | None
    raw: dict


@dataclass
class ImportPreview:
    kind: str
    kind_label: str
    merchant: str | None
    amount: float | None
    date: str | None
    matched: bool
    note: str | None = None
    card_holder: str | None = None
    card_last_four: str | None = None


def build_context(message: dict) -> MessageContext:
    headers = {
        header["name"].lower(): header["value"]
        for header in message.get("payload", {}).get("headers", [])
    }
    return MessageContext(
        message_id=message["id"],
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        body=_decode_body(message.get("payload", {})),
        date_header=headers.get("date"),
        internal_date=message.get("internalDate"),
        raw=message,
    )


def _extract_labeled_value(text: str, label: str) -> str | None:
    pattern = re.compile(
        re.escape(label.strip())
        + r"\s*[:\-]\s*(.+?)(?=\s*(?:note|amount|balance|date)\s*:|[\r\n]|$)",
        re.I,
    )
    match = pattern.search(text)
    if match:
        value = match.group(1).strip(" .-*\t")
        if value:
            return value
    return None


def _extract_labeled_amount(text: str, label: str) -> float | None:
    pattern = re.compile(
        re.escape(label.strip()) + r"\s*[:\-]?\s*\$?\s*([\d,]+\.\d{2})",
        re.I,
    )
    match = pattern.search(text)
    if match:
        return float(match.group(1).replace(",", ""))
    return None


def query_matches(context: MessageContext, email_query: EmailQuery) -> bool:
    if email_query.from_pattern and email_query.from_pattern.lower() not in context.sender.lower():
        return False
    match_text = (email_query.match_text or "").strip()
    if not match_text:
        return False
    haystack = f"{context.subject}\n{context.body}\n{context.sender}".lower()
    return match_text.lower() in haystack


def explain_match_failure(context: MessageContext, email_query: EmailQuery) -> str:
    """Human-readable reason when query_matches() is false."""
    if email_query.from_pattern and email_query.from_pattern.lower() not in context.sender.lower():
        return (
            f"sender {context.sender!r} does not contain from pattern "
            f"{email_query.from_pattern!r}"
        )
    match_text = (email_query.match_text or "").strip()
    if not match_text:
        return "match text is empty"
    return f"match text {match_text!r} not found in subject/body/sender"


def explain_unparsed(
    context: MessageContext,
    email_query: EmailQuery,
    *,
    card_holders: dict[str, str] | None = None,
) -> str:
    """Human-readable reason when apply_email_query would return unparsed."""
    merchant, amount, transaction_date, card_holder, card_last_four = _extract_fields(
        context,
        email_query,
        card_holders=card_holders,
    )
    missing: list[str] = []
    if amount is None:
        label = email_query.amount_label or "(auto-detect)"
        missing.append(f"amount (label={label!r})")
    if transaction_date is None:
        missing.append("transaction date")
    if missing:
        return "could not parse " + " and ".join(missing)

    if email_query.person_mode == "from_card" and email_query.kind != "income":
        if card_last_four is None:
            return "person_mode=from_card but no card last-four found in email"
        if card_holder is None:
            return (
                f"person_mode=from_card but card {card_last_four!r} is not mapped "
                "in Settings → Card holders"
            )
    return "unknown unparsed state"


def _kind_label(kind: str | None) -> str:
    if kind == "income":
        return "Income"
    if kind == "withdrawal":
        return "Withdrawal"
    return "Expense"


def _resolve_person_assignment(
    email_query: EmailQuery,
    combined: str,
    *,
    card_holders: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Return (person_or_card_holder, card_last_four) for this query."""
    if email_query.kind == "income":
        return email_query.person, None

    if email_query.person_mode == "from_card":
        card_last_four = extract_card_last_four(combined)
        card_holder = None
        if card_last_four and card_holders:
            card_holder = card_holders.get(card_last_four)
        return card_holder, card_last_four

    return email_query.person, None


def _extract_fields(
    context: MessageContext,
    email_query: EmailQuery,
    *,
    card_holders: dict[str, str] | None = None,
) -> tuple[str | None, float | None, object, str | None, str | None]:
    combined = f"{context.subject}\n{context.body}"

    amount = None
    if email_query.amount_label:
        amount = _extract_labeled_amount(combined, email_query.amount_label)
    if amount is None:
        amount = parse_income_amount(combined)

    merchant = None
    if email_query.kind in ("income", "withdrawal") and email_query.merchant_name:
        merchant = email_query.merchant_name
    else:
        if email_query.merchant_label:
            merchant = _extract_labeled_value(combined, email_query.merchant_label)
        if merchant is None and email_query.merchant_name:
            merchant = email_query.merchant_name

    transaction_date = resolve_transaction_date(
        combined,
        email_date_header=context.date_header,
        gmail_internal_date_ms=context.internal_date,
    )

    if email_query.kind == "expense" and (merchant is None or amount is None):
        parsed = parse_gmail_message(context.raw, card_holders=card_holders)
        if parsed is not None:
            merchant = merchant or parsed.merchant
            amount = amount if amount is not None else parsed.amount
            transaction_date = parsed.transaction_date

    if merchant is None:
        merchant = email_query.name

    card_holder, card_last_four = _resolve_person_assignment(
        email_query,
        combined,
        card_holders=card_holders,
    )
    return merchant, amount, transaction_date, card_holder, card_last_four


def apply_email_query(
    db: Database,
    context: MessageContext,
    email_query: EmailQuery,
    *,
    card_holders: dict[str, str] | None = None,
    matcher: BucketMatcher | None = None,
    debug: bool = False,
) -> tuple[str, int | None]:
    """Apply a matched query. Returns (outcome, record_id) where outcome is
    'expense', 'income', or 'unparsed'."""
    merchant, amount, transaction_date, card_holder, card_last_four = _extract_fields(
        context,
        email_query,
        card_holders=card_holders,
    )

    if amount is None or transaction_date is None:
        if debug:
            logger.debug(
                "Unparsed message %s for query %r: %s",
                context.message_id,
                email_query.name,
                explain_unparsed(
                    context,
                    email_query,
                    card_holders=card_holders,
                ),
            )
        return "unparsed", None

    if email_query.kind == "income":
        income_id = db.insert_income(
            gmail_message_id=context.message_id,
            received_date=transaction_date,
            allocated_month=transaction_date.strftime("%Y-%m"),
            source=merchant or email_query.name,
            amount=amount,
            currency="USD",
            bucket_id=email_query.income_bucket_id,
            person=email_query.person,
            email_subject=context.subject,
            email_from=context.sender,
        )
        return "income", income_id

    merchant_name = merchant or email_query.name
    bucket_id = email_query.expense_bucket_id
    suggested_bucket_id = bucket_id
    status = ExpenseStatus.AUTO if bucket_id else ExpenseStatus.PENDING

    if bucket_id is None and matcher is not None:
        matched_rule = matcher.match(merchant_name)
        if matched_rule is not None:
            bucket_id = matched_rule.bucket_id
            suggested_bucket_id = bucket_id
            status = (
                ExpenseStatus.AUTO
                if matched_rule.confirmed_by_user
                else ExpenseStatus.CONFIRMED
            )

    expense_id = db.insert_expense(
        gmail_message_id=context.message_id,
        transaction_date=transaction_date,
        merchant=merchant_name,
        merchant_normalized=normalize_merchant(merchant_name),
        amount=amount,
        currency="USD",
        bucket_id=bucket_id,
        suggested_bucket_id=suggested_bucket_id,
        status=status,
        email_subject=context.subject,
        email_from=context.sender,
        card_last_four=card_last_four,
        card_holder=card_holder,
    )
    return "expense", expense_id


def preview_email_query(
    context: MessageContext,
    email_query: EmailQuery,
    *,
    card_holders: dict[str, str] | None = None,
) -> ImportPreview:
    matched = query_matches(context, email_query)
    if not matched:
        return ImportPreview(
            kind=email_query.kind,
            kind_label=_kind_label(email_query.kind),
            merchant=None,
            amount=None,
            date=None,
            matched=False,
            note="This sample did not match the configured trigger phrase.",
        )

    merchant, amount, transaction_date, card_holder, card_last_four = _extract_fields(
        context,
        email_query,
        card_holders=card_holders,
    )
    date_label = (
        f"{transaction_date.strftime('%b')} {transaction_date.day}, {transaction_date.year}"
        if transaction_date
        else None
    )

    note = None
    if amount is None:
        note = "Amount could not be read from this sample; adjust the field labels."
    elif email_query.person_mode == "from_card" and email_query.kind != "income":
        if card_last_four is None:
            note = "No card ending in #### was found in this sample."
        elif card_holder is None:
            note = (
                f"Card ending in {card_last_four} is not mapped in Settings → Card holders."
            )

    return ImportPreview(
        kind=email_query.kind,
        kind_label=_kind_label(email_query.kind),
        merchant=merchant,
        amount=amount,
        date=date_label,
        matched=True,
        note=note,
        card_holder=card_holder,
        card_last_four=card_last_four,
    )


def sample_body_text(context: MessageContext, *, max_chars: int = SAMPLE_EMAIL_BODY_CHARS) -> str:
    body = context.body or ""
    if is_citi_alert(context.sender, context.subject):
        block = extract_citi_transaction_block(body)
        if block:
            return block[:max_chars]
        if len(body) > max_chars:
            return body[-max_chars:]
    return body[:max_chars]


def format_sample_display(context: MessageContext) -> str:
    body = sample_body_text(context)
    if is_citi_alert(context.sender, context.subject):
        return format_citi_display_text(
            subject=context.subject,
            body=body,
            include_subject_line=False,
        )
    return body


def highlight_match(text: str, match_text: str | None) -> Markup:
    if not text:
        return Markup("")
    if not match_text or match_text.lower() not in text.lower():
        return escape(text)
    pattern = re.compile(re.escape(match_text), re.IGNORECASE)
    parts: list[Markup | str] = []
    last = 0
    for match in pattern.finditer(text):
        parts.append(escape(text[last : match.start()]))
        parts.append(
            Markup('<mark class="match-highlight">')
            + escape(match.group(0))
            + Markup("</mark>")
        )
        last = match.end()
    parts.append(escape(text[last:]))
    return Markup("").join(parts)
