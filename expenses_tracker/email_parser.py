from __future__ import annotations

import base64
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime

from expenses_tracker.citi_parser import is_citi_alert, parse_citi_message
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

DATE_PATTERNS = [
    re.compile(r"(?:on|date)\s*[:\s]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", re.I),
    re.compile(r"(\d{4}-\d{2}-\d{2})"),
]


def _decode_body(payload: dict) -> str:
    parts: list[str] = []

    def walk(part: dict) -> None:
        mime_type = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data and mime_type in ("text/plain", "text/html"):
            raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            if mime_type == "text/html":
                raw = re.sub(r"<[^>]+>", " ", raw)
                raw = re.sub(r"&nbsp;", " ", raw)
                raw = re.sub(r"\s+", " ", raw)
            parts.append(raw)
        for child in part.get("parts", []):
            walk(child)

    walk(payload)
    return "\n".join(parts)


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


def _parse_date(text: str, email_date_header: str | None) -> date:
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            raw = match.group(1)
            for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y-%m-%d"):
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


def _parse_generic_message(
    *,
    gmail_message_id: str,
    subject: str,
    sender: str,
    body_text: str,
    email_date_header: str | None,
) -> ParsedEmail | None:
    combined = f"{subject}\n{body_text}"
    amount = _parse_amount(combined)
    merchant = _parse_merchant(body_text, subject)
    if amount is None or merchant is None:
        return None

    transaction_date = _parse_date(combined, email_date_header)
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

    if is_citi_alert(sender, subject):
        return parse_citi_message(
            gmail_message_id=message["id"],
            subject=subject,
            sender=sender,
            body_text=body_text,
            email_date_header=headers.get("date"),
            card_holders=card_holders,
        )

    return _parse_generic_message(
        gmail_message_id=message["id"],
        subject=subject,
        sender=sender,
        body_text=body_text,
        email_date_header=headers.get("date"),
    )
