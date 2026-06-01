from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from expenses_tracker.config import get_settings, resolve_gmail_credentials_path
from expenses_tracker.email_parser import parse_gmail_message
from expenses_tracker.gmail_client import GmailClient


def main() -> None:
    settings = get_settings()
    gmail = GmailClient(resolve_gmail_credentials_path(settings), settings.gmail_token_path)
    gmail.authenticate()
    service = gmail.service

    queries = {
        "current (.env)": settings.gmail_search_query,
        "all citi last 30d": "from:(citibank.com OR citi.com) newer_than:30d",
        "citi + transaction subject": "from:(citibank.com OR citi.com) newer_than:30d subject:transaction",
        "citi last 90d + transaction": "from:(citibank.com OR citi.com) newer_than:90d subject:transaction",
    }

    for label, query in queries.items():
        resp = service.users().messages().list(userId="me", q=query, maxResults=500).execute()
        estimate = resp.get("resultSizeEstimate", 0)
        refs = resp.get("messages", [])
        print(f"{label}: estimate={estimate}, ids_returned={len(refs)}")
        print(f"  {query}\n")

    print("--- Parsing current query messages ---")
    messages = gmail.fetch_messages(settings.gmail_search_query, max_results=500)
    parsed = 0
    failed = 0
    for msg in messages:
        result = parse_gmail_message(msg, card_holders=settings.card_holders)
        subject = next(
            (
                h["value"]
                for h in msg.get("payload", {}).get("headers", [])
                if h["name"].lower() == "subject"
            ),
            "",
        )
        if result:
            parsed += 1
            print(
                f"OK  {result.transaction_date} | ${result.amount:.2f} | "
                f"{result.merchant[:50]} | {subject[:60]}"
            )
        else:
            failed += 1
            print(f"FAIL | {subject[:90]}")

    print(f"\nTotal messages: {len(messages)}, parsed: {parsed}, failed: {failed}")


if __name__ == "__main__":
    main()
