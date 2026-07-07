from __future__ import annotations

import re

from expenses_tracker.dates import resolve_transaction_date

from expenses_tracker.models import ParsedEmail

CITI_SENDER_MARKERS = ("citibank.com", "citi.com", "citi alerts")

CITI_SUBJECT_AMOUNT_PATTERNS = [
    re.compile(r"A \$([\d,]+\.\d{2}) transaction was made", re.I),
    re.compile(r"for a \$([\d,]+\.\d{2})(?:\s+transaction|\s+at\b)", re.I),
]

CITI_SUBJECT_MERCHANT_PATTERNS = [
    re.compile(r"A \$[\d,]+\.\d{2} transaction was made at (.+?) on ", re.I),
    re.compile(
        r"(?:A )?transaction was made on your .+? for a \$[\d,]+\.\d{2} at (.+?)(?:\.|$)",
        re.I,
    ),
    re.compile(r"for a \$[\d,]+\.\d{2} at (.+?) on your", re.I),
]

CITI_CARD_NOT_PRESENT_SUBJECT = re.compile(
    r"not present for a \$[\d,]+\.\d{2} transaction",
    re.I,
)

CITI_LINK_ONLY_BODY_MARKER = re.compile(
    r"please visit the following link to view your message",
    re.I,
)

CITI_TRANSACTION_DETAILS_MARKER = re.compile(
    r"Amount:\s*\$[\d,]+\.\d{2}|Merchant\s+\S|Card Ending In \d{4}|Card ending in \d{4}",
    re.I,
)

CITI_TRANSACTION_BLOCK_PATTERN = re.compile(
    r"Amount:\s*\$[\d,]+\.\d{2}.+?Date\s+\d{1,2}/\d{1,2}/\d{4}\s+Time\s+[\d:]+\s*[AP]M\s*ET",
    re.I | re.S,
)

CITI_CARD_NOT_PRESENT_BLOCK_PATTERN = re.compile(
    r"Card ending in \d{4}.+?(?:not present|Amount:\s*\$[\d,]+\.\d{2}).+?"
    r"(?:Date\s+\d{1,2}/\d{1,2}/\d{4}|You're receiving this email)",
    re.I | re.S,
)

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
    re.compile(r"Merchant[:\s]+(.+?)(?:\s+Location|\s+Date|\s+Time|To view|If you don't|$)", re.I),
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


def is_citi_alert(sender: str, subject: str = "") -> bool:
    sender_lower = sender.lower()
    return any(marker in sender_lower for marker in CITI_SENDER_MARKERS)


def has_citi_transaction_details(text: str) -> bool:
    return bool(CITI_TRANSACTION_DETAILS_MARKER.search(text or ""))


def is_citi_link_only_body(body: str) -> bool:
    if has_citi_transaction_details(body):
        return False
    normalized = re.sub(r"\s+", " ", (body or "").strip())
    if not normalized:
        return False
    return bool(CITI_LINK_ONLY_BODY_MARKER.search(normalized))


def extract_citi_transaction_block(body: str) -> str | None:
    normalized = re.sub(r"\s+", " ", (body or "").strip())
    if not normalized:
        return None
    for pattern in (CITI_TRANSACTION_BLOCK_PATTERN, CITI_CARD_NOT_PRESENT_BLOCK_PATTERN):
        match = pattern.search(normalized)
        if match:
            return match.group(0).strip()
    if has_citi_transaction_details(normalized):
        amount_match = re.search(r"Amount:\s*\$[\d,]+\.\d{2}", normalized, re.I)
        if amount_match:
            start = max(0, amount_match.start() - 80)
            end = min(len(normalized), amount_match.end() + 220)
            return normalized[start:end].strip()
    return None


def _parse_merchant_from_subject(subject: str) -> str | None:
    if CITI_CARD_NOT_PRESENT_SUBJECT.search(subject):
        return "Card not present"
    for pattern in CITI_SUBJECT_MERCHANT_PATTERNS:
        match = pattern.search(subject)
        if match:
            merchant = _clean_merchant(match.group(1))
            if len(merchant) >= 2:
                return merchant
    return None


def format_citi_display_text(
    *,
    subject: str,
    body: str,
    include_subject_line: bool = True,
) -> str:
    """Format Citi alerts for display, surfacing HTML transaction details when present."""
    subject = (subject or "").strip()
    body = (body or "").strip()
    block = extract_citi_transaction_block(body)
    if block:
        return block
    if not is_citi_link_only_body(body):
        return body
    if subject:
        return subject
    return body


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
    merchant = re.sub(r"\s+Date\s+\d{1,2}/\d{1,2}/\d{4}.*$", "", merchant, flags=re.I)
    merchant = re.sub(r"\s+Location\s+.*$", "", merchant, flags=re.I)
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


def extract_card_last_four(text: str) -> str | None:
    """Return the last four digits of a card referenced in alert text."""
    for pattern in CITI_TRANSACTION_CARD_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)

    matches = list(CITI_CARD_PATTERN.finditer(text))
    if matches:
        return matches[-1].group(1)
    return None


def _parse_card_last_four(body: str) -> str | None:
    return extract_card_last_four(body)


def parse_citi_message(
    *,
    gmail_message_id: str,
    subject: str,
    sender: str,
    body_text: str,
    email_date_header: str | None,
    gmail_internal_date_ms: str | int | None = None,
    card_holders: dict[str, str] | None = None,
) -> ParsedEmail | None:
    amount = _parse_amount(subject, body_text)
    merchant = _parse_merchant(body_text)
    if merchant is None:
        merchant = _parse_merchant_from_subject(subject)
    if amount is None or merchant is None:
        return None

    card_last_four = _parse_card_last_four(body_text)
    card_holder = None
    if card_last_four and card_holders:
        card_holder = card_holders.get(card_last_four)

    date_text = body_text
    if is_citi_link_only_body(body_text):
        date_text = f"{subject}\n{body_text}"

    transaction_date = resolve_transaction_date(
        date_text,
        email_date_header=email_date_header,
        gmail_internal_date_ms=gmail_internal_date_ms,
    )

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
