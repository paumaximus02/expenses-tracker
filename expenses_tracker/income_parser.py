from __future__ import annotations

import re

from expenses_tracker.dates import resolve_transaction_date
from expenses_tracker.email_parser import _decode_body
from expenses_tracker.models import IncomeRule, ParsedIncomeEmail

# A labeled "Amount:" line is the most reliable signal (e.g. bank ACH alerts).
AMOUNT_LABEL_PATTERN = re.compile(r"\bamount\s*[:\-]\s*\$?\s*([\d,]+\.\d{2})", re.I)

INCOME_AMOUNT_PATTERNS = [
    re.compile(
        r"(?:deposit(?:ed)?|payment|paid|credited|received|net pay|total)"
        r"\s*(?:of|:)?\s*\$?\s*([\d,]+\.\d{2})",
        re.I,
    ),
    re.compile(r"\$\s*([\d,]+\.\d{2})"),
    re.compile(r"USD\s*([\d,]+\.\d{2})", re.I),
]

# Dollar figures that are not the income amount: account balances and alert
# thresholds ("transaction with amount larger than $1.00").
IGNORED_AMOUNT_PHRASES = [
    re.compile(r"balance\s*(?:of|:|\-)?\s*\$?\s*[\d,]+\.\d{2}", re.I),
    re.compile(
        r"(?:larger|greater|more|less|smaller)\s+than\s+\$?\s*[\d,]+\.\d{2}",
        re.I,
    ),
    re.compile(r"at\s+least\s+\$?\s*[\d,]+\.\d{2}", re.I),
    re.compile(r"exceed(?:s|ing)?\s+\$?\s*[\d,]+\.\d{2}", re.I),
]

INCOME_DESCRIPTION_PATTERNS = [
    re.compile(
        r"description\s*[:\-]\s*(.+?)(?=\s*(?:note|amount|balance|date)\s*:|[\r\n]|$)",
        re.I,
    ),
]

# Bank transaction alerts (e.g. LBS FCU) carry a "Description: Withdrawal-..."
# or "Description: Deposit-..." line. They must never fall through to the
# generic card-expense parser, which extracts garbage from them (the greeting
# line becomes the merchant).
BANK_ALERT_PATTERN = re.compile(r"description\s*[:\-]\s*(?:withdrawal|deposit)\b", re.I)


def is_bank_alert_message(message: dict) -> bool:
    headers = {
        header["name"].lower(): header["value"]
        for header in message.get("payload", {}).get("headers", [])
    }
    subject = headers.get("subject", "")
    body_text = _decode_body(message.get("payload", {}))
    return bool(BANK_ALERT_PATTERN.search(f"{subject}\n{body_text}"))


def parse_income_amount(text: str) -> float | None:
    clean_text = text
    for ignored in IGNORED_AMOUNT_PHRASES:
        clean_text = ignored.sub(" ", clean_text)
    for pattern in (AMOUNT_LABEL_PATTERN, *INCOME_AMOUNT_PATTERNS):
        match = pattern.search(clean_text)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def parse_income_description(text: str) -> str | None:
    for pattern in INCOME_DESCRIPTION_PATTERNS:
        match = pattern.search(text)
        if match:
            description = match.group(1).strip(" .-*\t")
            if description:
                return description
    return None


def match_income_rule(
    subject: str,
    body_text: str,
    rules: list[IncomeRule],
) -> IncomeRule | None:
    """Find the income rule whose match text appears in the email.

    The most specific rule (longest match text) wins when several match.
    """
    haystack = f"{subject}\n{body_text}".lower()
    best: IncomeRule | None = None
    for rule in rules:
        needle = rule.match_text.strip().lower()
        if needle and needle in haystack:
            if best is None or len(rule.match_text) > len(best.match_text):
                best = rule
    return best


def parse_income_message(
    message: dict,
    rules: list[IncomeRule],
) -> ParsedIncomeEmail | None:
    """Parse a Gmail message into an income entry if it matches an income rule."""
    headers = {
        header["name"].lower(): header["value"]
        for header in message.get("payload", {}).get("headers", [])
    }
    subject = headers.get("subject", "")
    sender = headers.get("from", "")
    body_text = _decode_body(message.get("payload", {}))

    rule = match_income_rule(subject, body_text, rules)
    if rule is None:
        return None

    combined = f"{subject}\n{body_text}"
    amount = parse_income_amount(combined)
    if amount is None:
        return None

    received_date = resolve_transaction_date(
        combined,
        email_date_header=headers.get("date"),
        gmail_internal_date_ms=message.get("internalDate"),
    )
    return ParsedIncomeEmail(
        gmail_message_id=message["id"],
        received_date=received_date,
        amount=amount,
        currency="USD",
        email_subject=subject,
        email_from=sender,
        rule=rule,
        description=parse_income_description(body_text),
    )
