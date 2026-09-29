from __future__ import annotations

import json
import logging
import os
import random
import time
from collections.abc import Callable, Collection
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
logger = logging.getLogger(__name__)

# Gmail users().messages().get(format=full) is expensive; stay under per-minute
# "Total Query Cost" by spacing requests and retrying rate-limit responses.
_DEFAULT_GET_PAUSE_SECONDS = 0.12
_MAX_RETRY_ATTEMPTS = 8
_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 60.0


def configure_oauth_transport(redirect_uri: str) -> None:
    """Allow HTTP redirect URIs for local OAuth (http://127.0.0.1:5000/...)."""
    if redirect_uri.startswith("http://"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


def _is_rate_limit_error(exc: HttpError) -> bool:
    if exc.resp is None or exc.resp.status not in (403, 429):
        return False
    try:
        payload = json.loads(exc.content.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        reason = ""
        message = str(exc).lower()
    else:
        errors = payload.get("error", {}).get("errors") or []
        reason = ""
        if errors:
            reason = str(errors[0].get("reason") or "")
        message = str(payload.get("error", {}).get("message") or "").lower()
    return reason in {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "quotaExceeded",
    } or "quota exceeded" in message or "rate limit" in message


def _retry_after_seconds(exc: HttpError) -> float | None:
    if exc.resp is None:
        return None
    raw = exc.resp.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def execute_with_backoff(
    request,
    *,
    max_attempts: int = _MAX_RETRY_ATTEMPTS,
    initial_backoff: float = _INITIAL_BACKOFF_SECONDS,
    max_backoff: float = _MAX_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Execute a googleapiclient request, retrying Gmail rate-limit errors."""
    delay = initial_backoff
    last_error: HttpError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return request.execute()
        except HttpError as exc:
            last_error = exc
            if not _is_rate_limit_error(exc) or attempt >= max_attempts:
                raise
            retry_after = _retry_after_seconds(exc)
            sleep_for = retry_after if retry_after is not None else delay + random.uniform(0, 0.4)
            logger.warning(
                "Gmail rate limit (attempt %s/%s); sleeping %.1fs before retry",
                attempt,
                max_attempts,
                sleep_for,
            )
            sleep(sleep_for)
            delay = min(delay * 2, max_backoff)
    assert last_error is not None
    raise last_error


class GmailClient:
    def __init__(
        self,
        credentials_path: Path,
        token_path: Path | None = None,
        *,
        token_json: str | None = None,
        get_pause_seconds: float = _DEFAULT_GET_PAUSE_SECONDS,
    ) -> None:
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.token_json = token_json
        self.get_pause_seconds = get_pause_seconds
        self._service = None
        self._credentials: Credentials | None = None
        self._last_get_at: float | None = None

    def has_token(self) -> bool:
        if self.token_json:
            return True
        return bool(self.token_path and self.token_path.exists())

    def export_token_json(self) -> str:
        if self._credentials is None:
            raise RuntimeError("Gmail is not authenticated.")
        return self._credentials.to_json()

    def authenticate(self) -> None:
        creds = None
        if self.token_json:
            creds = Credentials.from_authorized_user_info(json.loads(self.token_json), SCOPES)
        elif self.token_path and self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            elif self.token_json is not None:
                raise RuntimeError(
                    "Gmail token is invalid or expired and could not be refreshed. "
                    "Reconnect Gmail in Settings."
                )
            elif self.token_path and self.token_path.exists():
                if not self.credentials_path.exists():
                    raise FileNotFoundError(
                        f"Missing {self.credentials_path}. Download OAuth credentials from "
                        "Google Cloud Console and save them as credentials.json."
                    )
                from google_auth_oauthlib.flow import InstalledAppFlow

                installed = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path),
                    SCOPES,
                )
                creds = installed.run_local_server(port=0)
                self.token_path.write_text(creds.to_json(), encoding="utf-8")
            else:
                raise RuntimeError(
                    "Gmail is not connected for this household. Connect Gmail in Settings."
                )

        if self.token_path and not self.token_json:
            self.token_path.write_text(creds.to_json(), encoding="utf-8")
        elif self.token_json is not None:
            self.token_json = creds.to_json()

        self._credentials = creds
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    @staticmethod
    def create_web_flow(
        credentials_path: Path,
        redirect_uri: str,
        *,
        code_verifier: str | None = None,
    ) -> Flow:
        configure_oauth_transport(redirect_uri)
        kwargs: dict[str, object] = {"redirect_uri": redirect_uri}
        if code_verifier:
            kwargs["code_verifier"] = code_verifier
            kwargs["autogenerate_code_verifier"] = False
        flow = Flow.from_client_secrets_file(
            str(credentials_path),
            scopes=SCOPES,
            **kwargs,
        )
        return flow

    @property
    def service(self):
        if self._service is None:
            self.authenticate()
        return self._service

    def list_message_ids(self, query: str, max_results: int | None = None) -> list[str]:
        """Return Gmail message IDs matching query (cheap list calls only)."""
        ids: list[str] = []
        page_token: str | None = None

        while True:
            page_size = 500
            if max_results is not None:
                page_size = min(500, max(1, max_results - len(ids)))
            request = self.service.users().messages().list(
                userId="me",
                q=query,
                maxResults=page_size,
                pageToken=page_token,
            )
            response = execute_with_backoff(request)
            for ref in response.get("messages", []):
                ids.append(ref["id"])
                if max_results is not None and len(ids) >= max_results:
                    return ids

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return ids

    def _throttle_gets(self) -> None:
        if self.get_pause_seconds <= 0:
            return
        now = time.monotonic()
        if self._last_get_at is not None:
            elapsed = now - self._last_get_at
            remaining = self.get_pause_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_get_at = time.monotonic()

    def get_message(self, message_id: str, *, format: str = "full") -> dict:
        """Fetch one full message, with throttle + rate-limit retries."""
        self._throttle_gets()
        request = (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format=format)
        )
        return execute_with_backoff(request)

    def fetch_messages(
        self,
        query: str,
        max_results: int | None = None,
        *,
        skip_ids: Collection[str] | None = None,
    ) -> list[dict]:
        """List matching IDs, then fetch full messages (skipping skip_ids)."""
        skip = set(skip_ids or ())
        messages: list[dict] = []
        for message_id in self.list_message_ids(query, max_results=None):
            if message_id in skip:
                continue
            messages.append(self.get_message(message_id))
            if max_results is not None and len(messages) >= max_results:
                return messages
        return messages
