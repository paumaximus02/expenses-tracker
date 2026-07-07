from __future__ import annotations

import base64
import html as html_module
import re
from datetime import date

from expenses_tracker.dates import resolve_transaction_date

from expenses_tracker.citi_parser import (
    extract_citi_transaction_block,
    has_citi_transaction_details,
    is_citi_alert,
    is_citi_link_only_body,
    parse_citi_message,
)
from expenses_tracker.models import ParsedEmail

AMOUNT_PATTERNS = [
    re.compile(r"(?:amount|total|charged|spent|purchase of)\s*[:\s]*\$?\s*([\d,]+\.\d{2})", re.I),
    re.compile(r"\$\s*([\d,]+\.\d{2})"),
    re.compile(r"USD\s*([\d,]+\.\d{2})", re.I),
]

MERCHANT_PATTERNS = [
    re.compile(r"(?:at|from|merchant|vendor|purchase at)\s+[:\s]*(.+?)(?:\.|\n|$)", re.I),
    re.compile(r"transaction (?:at|with)\s+(.+?)(?:\.|\n|$)", re.I),
    re.compile(r"^(.+?)\s+(?:purchase|transaction|charge)", re.I),
]

def _parse_date(
    text: str,
    email_date_header: str | None,
    gmail_internal_date_ms: str | int | None = None,
) -> date:
    return resolve_transaction_date(
        text,
        email_date_header=email_date_header,
        gmail_internal_date_ms=gmail_internal_date_ms,
    )


def _html_to_text(raw_html: str) -> str:
    text = raw_html
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?i)</tr\s*>", "\n", text)
    text = re.sub(r"(?i)</td\s*>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _decode_body(payload: dict) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict) -> None:
        mime_type = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data and mime_type in ("text/plain", "text/html"):
            raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            if mime_type == "text/plain":
                plain_parts.append(raw)
            else:
                html_parts.append(_html_to_text(raw))
        for child in part.get("parts", []):
            walk(child)

    walk(payload)
    plain = "\n".join(plain_parts).strip()
    html = "\n".join(html_parts).strip()

    if html and has_citi_transaction_details(html):
        if not plain or is_citi_link_only_body(plain):
            return html
    if plain and html:
        if has_citi_transaction_details(html) and not has_citi_transaction_details(plain):
            return html
        return f"{plain}\n{html}"
    return plain or html


def _parse_amount(text: str) -> float | None:
    for pattern in AMOUNT_PATTERNS:
        match = pattern.search(text)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def _parse_merchant(text: str, subject: str) -> str | None:
    for source in (text, subject):
        for pattern in MERCHANT_PATTERNS:
            match = pattern.search(source)
            if match:
                merchant = match.group(1).strip(" .-*")
                if len(merchant) >= 2:
                    return merchant
    cleaned_subject = re.sub(
        r"(transaction|purchase|charge|alert|notification)",
        "",
        subject,
        flags=re.I,
    ).strip(" :-")
    return cleaned_subject or None


def _parse_generic_message(
    *,
    gmail_message_id: str,
    subject: str,
    sender: str,
    body_text: str,
    email_date_header: str | None,
    gmail_internal_date_ms: str | int | None = None,
) -> ParsedEmail | None:
    combined = f"{subject}\n{body_text}"
    amount = _parse_amount(combined)
    merchant = _parse_merchant(body_text, subject)
    if amount is None or merchant is None:
        return None

    transaction_date = _parse_date(combined, email_date_header, gmail_internal_date_ms)
    return ParsedEmail(
        gmail_message_id=gmail_message_id,
        transaction_date=transaction_date,
        merchant=merchant,
        amount=amount,
        currency="USD",
        email_subject=subject,
        email_from=sender,
        body_text=body_text[:500],
    )


def parse_gmail_message(
    message: dict,
    *,
    card_holders: dict[str, str] | None = None,
) -> ParsedEmail | None:
    headers = {
        header["name"].lower(): header["value"]
        for header in message.get("payload", {}).get("headers", [])
    }
    subject = headers.get("subject", "")
    sender = headers.get("from", "")
    body_text = _decode_body(message.get("payload", {}))
    internal_date = message.get("internalDate")

    if is_citi_alert(sender, subject):
        return parse_citi_message(
            gmail_message_id=message["id"],
            subject=subject,
            sender=sender,
            body_text=body_text,
            email_date_header=headers.get("date"),
            gmail_internal_date_ms=internal_date,
            card_holders=card_holders,
        )

    return _parse_generic_message(
        gmail_message_id=message["id"],
        subject=subject,
        sender=sender,
        body_text=body_text,
        email_date_header=headers.get("date"),
        gmail_internal_date_ms=internal_date,
    )
