from __future__ import annotations

import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime

from expenses_tracker.models import ParsedEmail

CITI_SENDER_MARKERS = ("citibank.com", "citi.com", "citi alerts")

CITI_SUBJECT_AMOUNT_PATTERNS = [
    re.compile(r"A \$([\d,]+\.\d{2}) transaction was made", re.I),
    re.compile(r"for a \$([\d,]+\.\d{2}) transaction", re.I),
]

CITI_BODY_AMOUNT_PATTERNS = [
    re.compile(r"(?:transaction of|used for a|amount of)\s+\$([\d,]+\.\d{2})", re.I),
    re.compile(r"\$\s*([\d,]+\.\d{2})"),
]

CITI_MERCHANT_PATTERNS = [
    re.compile(
        r"(?:was made|used for a(?:n)?|made for a(?:n)?)\s+\$[\d,]+\.\d{2}\s+transaction\s+at\s+(.+?)\s+on\s+\d",
        re.I,
    ),
    re.compile(
        r"(?:transaction of|charge of)\s+\$[\d,]+\.\d{2}\s+(?:was made|occurred)\s+at\s+(.+?)\s+on\s+\d",
        re.I,
    ),
    re.compile(r"(?:transaction|purchase|charge)\s+at\s+(.+?)\s+on\s+\d", re.I),
    re.compile(
        r"(?:was made|used for a transaction|made for a transaction)\s+at\s+(.+?)\s+on\s+\d{1,2}/\d{1,2}/\d{2,4}",
        re.I,
    ),
    re.compile(r"Merchant[:\s]+(.+?)(?:\.|\n|$)", re.I),
    re.compile(r"merchant name[:\s]+(.+?)(?:\.|\n|$)", re.I),
]

CITI_CARD_PATTERN = re.compile(r"(?:card|account)\s+ending\s+in\s+(\d{4})", re.I)

CITI_TRANSACTION_CARD_PATTERNS = [
    re.compile(
        r"Amount:\s*\$[\d,]+\.\d{2}\s+Card(?:\s+Ending\s+In|\s+ending\s+in)\s+(\d{4})",
        re.I,
    ),
    re.compile(
        r"Card(?:\s+Ending\s+In|\s+ending\s+in)\s+(\d{4})\s+Merchant",
        re.I,
    ),
]

CITI_DATE_PATTERNS = [
    re.compile(r"\bon\s+(\d{1,2}/\d{1,2}/\d{2,4})\b", re.I),
    re.compile(r"(\d{4}-\d{2}-\d{2})"),
]


def is_citi_alert(sender: str, subject: str = "") -> bool:
    sender_lower = sender.lower()
    return any(marker in sender_lower for marker in CITI_SENDER_MARKERS)


def _parse_amount(subject: str, body: str) -> float | None:
    for pattern in CITI_SUBJECT_AMOUNT_PATTERNS:
        match = pattern.search(subject)
        if match:
            return float(match.group(1).replace(",", ""))

    for pattern in CITI_BODY_AMOUNT_PATTERNS:
        match = pattern.search(body)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def _clean_merchant(raw: str) -> str:
    merchant = raw.strip(" .-*\n\r\t")
    merchant = re.sub(r"\s+", " ", merchant)
    merchant = re.sub(
        r"\s+on your .+ card ending in \d{4}.*$",
        "",
        merchant,
        flags=re.I,
    )
    merchant = re.sub(r"\s+Date\s+\d{2}/\d{2}/\d{4}.*$", "", merchant, flags=re.I)
    merchant = re.sub(r"\s+To view transactions.*$", "", merchant, flags=re.I)
    return merchant.strip(" .")


def _parse_merchant(body: str) -> str | None:
    for pattern in CITI_MERCHANT_PATTERNS:
        match = pattern.search(body)
        if match:
            merchant = _clean_merchant(match.group(1))
            if len(merchant) >= 2 and "costco anywhere account" not in merchant.lower():
                return merchant
    return None


def _parse_card_last_four(body: str) -> str | None:
    for pattern in CITI_TRANSACTION_CARD_PATTERNS:
        match = pattern.search(body)
        if match:
            return match.group(1)

    matches = list(CITI_CARD_PATTERN.finditer(body))
    if matches:
        return matches[-1].group(1)
    return None


def _parse_date(body: str, email_date_header: str | None) -> date:
    for pattern in CITI_DATE_PATTERNS:
        match = pattern.search(body)
        if match:
            raw = match.group(1)
            for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(raw, fmt).date()
                except ValueError:
                    continue
    if email_date_header:
        try:
            return parsedate_to_datetime(email_date_header).date()
        except (TypeError, ValueError, IndexError):
            pass
    return date.today()


def parse_citi_message(
    *,
    gmail_message_id: str,
    subject: str,
    sender: str,
    body_text: str,
    email_date_header: str | None,
    card_holders: dict[str, str] | None = None,
) -> ParsedEmail | None:
    amount = _parse_amount(subject, body_text)
    merchant = _parse_merchant(body_text)
    if amount is None or merchant is None:
        return None

    card_last_four = _parse_card_last_four(body_text)
    card_holder = None
    if card_last_four and card_holders:
        card_holder = card_holders.get(card_last_four)

    transaction_date = _parse_date(body_text, email_date_header)

    return ParsedEmail(
        gmail_message_id=gmail_message_id,
        transaction_date=transaction_date,
        merchant=merchant,
        amount=amount,
        currency="USD",
        email_subject=subject,
        email_from=sender,
        body_text=body_text[:500],
        card_last_four=card_last_four,
        card_holder=card_holder,
    )
