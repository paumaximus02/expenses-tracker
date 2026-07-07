from __future__ import annotations

import base64
import unittest

from expenses_tracker.citi_parser import extract_citi_transaction_block, is_citi_link_only_body
from expenses_tracker.email_parser import _decode_body, parse_gmail_message


def _encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


CITI_PLAIN = (
    "Citi(R)\n\nPlease visit the following link to view your message:\n"
    "http://fm.info6.citi.com/ats/msg.aspx?sg1=abc\n"
)

CITI_HTML = """
<html><body>
<p>Amount: $30.44 Card Ending In 1201 Merchant SPROUTS FARMERS MARKET # SILVERDALE US
Date 07/02/2026 Time 09:54 PM ET</p>
</body></html>
"""


def _citi_multipart_message(*, message_id: str = "citi-html") -> dict:
    return {
        "id": message_id,
        "internalDate": "1751500800000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": "A $30.44 transaction was made on your Costco Anywhere account"},
                {"name": "From", "value": "Citi Alerts <alerts@info6.citi.com>"},
                {"name": "Date", "value": "Thu, 3 Jul 2026 08:00:00 -0700"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _encode(CITI_PLAIN)},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _encode(CITI_HTML)},
                },
            ],
        },
    }


class EmailBodyDecodingTests(unittest.TestCase):
    def test_prefers_html_when_plain_is_link_only(self) -> None:
        body = _decode_body(_citi_multipart_message()["payload"])
        self.assertIn("Amount: $30.44", body)
        self.assertIn("SPROUTS FARMERS MARKET", body)
        self.assertNotIn("Please visit the following link", body)

    def test_parse_citi_multipart_message(self) -> None:
        parsed = parse_gmail_message(_citi_multipart_message())
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.amount, 30.44)
        self.assertIn("SPROUTS", parsed.merchant)

    def test_extract_citi_transaction_block(self) -> None:
        body = _decode_body(_citi_multipart_message()["payload"])
        block = extract_citi_transaction_block(body)
        self.assertIsNotNone(block)
        assert block is not None
        self.assertIn("Amount: $30.44", block)
        self.assertIn("SPROUTS", block)

    def test_link_only_plain_without_html_is_still_link_only(self) -> None:
        self.assertTrue(is_citi_link_only_body(CITI_PLAIN))


if __name__ == "__main__":
    unittest.main()
