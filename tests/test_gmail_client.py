from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

from googleapiclient.errors import HttpError

from expenses_tracker.gmail_client import execute_with_backoff


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


if __name__ == "__main__":
    unittest.main()
