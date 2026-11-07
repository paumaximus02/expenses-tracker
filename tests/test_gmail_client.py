from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from googleapiclient.errors import HttpError

from expenses_tracker.gmail_client import GmailClient, execute_with_backoff


def _http_error(status: int, reason: str, message: str = "Quota exceeded") -> HttpError:
    content = json.dumps(
        {
            "error": {
                "errors": [{"reason": reason, "message": message}],
                "message": message,
            }
        }
    ).encode("utf-8")
    resp = Mock()
    resp.status = status
    resp.get = Mock(return_value=None)
    return HttpError(resp, content)


class ExecuteWithBackoffTests(unittest.TestCase):
    def test_retries_rate_limit_then_succeeds(self) -> None:
        sleeps: list[float] = []
        request = Mock()
        request.execute.side_effect = [
            _http_error(403, "rateLimitExceeded"),
            {"id": "msg-1"},
        ]

        result = execute_with_backoff(
            request,
            max_attempts=4,
            initial_backoff=0.5,
            sleep=sleeps.append,
        )

        self.assertEqual(result, {"id": "msg-1"})
        self.assertEqual(request.execute.call_count, 2)
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 0.5)

    def test_non_rate_limit_errors_are_not_retried(self) -> None:
        request = Mock()
        request.execute.side_effect = _http_error(403, "forbidden", "Permission denied")

        with self.assertRaises(HttpError):
            execute_with_backoff(request, max_attempts=4, sleep=lambda _s: None)

        self.assertEqual(request.execute.call_count, 1)


class VerifyConnectionTests(unittest.TestCase):
    def test_disconnected_without_token(self) -> None:
        client = GmailClient(Path("credentials.json"), token_json=None)
        client.token_path = Path("missing-token.json")
        state, detail = client.verify_connection()
        self.assertEqual(state, "disconnected")
        self.assertIsNone(detail)

    def test_needs_reconnect_when_authenticate_fails(self) -> None:
        client = GmailClient(
            Path("credentials.json"),
            token_json='{"token":"x","refresh_token":"y","token_uri":"https://oauth2.googleapis.com/token","client_id":"id","client_secret":"secret"}',
        )
        client.authenticate = Mock(side_effect=RuntimeError("token revoked"))  # type: ignore[method-assign]
        state, detail = client.verify_connection()
        self.assertEqual(state, "needs_reconnect")
        self.assertIn("token revoked", detail or "")

    def test_connected_when_profile_succeeds(self) -> None:
        client = GmailClient(Path("credentials.json"), token_json='{"token":"x"}')
        client.authenticate = Mock()  # type: ignore[method-assign]
        profile_request = Mock()
        profile_request.execute.return_value = {"emailAddress": "user@example.com"}
        service = Mock()
        service.users.return_value.getProfile.return_value = profile_request
        client._service = service
        state, detail = client.verify_connection()
        self.assertEqual(state, "connected")
        self.assertIsNone(detail)


if __name__ == "__main__":
    unittest.main()
