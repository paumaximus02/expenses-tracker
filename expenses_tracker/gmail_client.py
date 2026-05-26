from __future__ import annotations

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def configure_oauth_transport(redirect_uri: str) -> None:
    """Allow HTTP redirect URIs for local OAuth (http://127.0.0.1:5000/...)."""
    if redirect_uri.startswith("http://"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


class GmailClient:
    def __init__(
        self,
        credentials_path: Path,
        token_path: Path | None = None,
        *,
        token_json: str | None = None,
    ) -> None:
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.token_json = token_json
        self._service = None
        self._credentials: Credentials | None = None

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
            elif self.token_path:
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

    def fetch_messages(self, query: str, max_results: int | None = None) -> list[dict]:
        messages: list[dict] = []
        page_token: str | None = None

        while True:
            request = self.service.users().messages().list(
                userId="me",
                q=query,
                maxResults=500,
                pageToken=page_token,
            )
            response = request.execute()
            message_refs = response.get("messages", [])
            for ref in message_refs:
                message = (
                    self.service.users()
                    .messages()
                    .get(userId="me", id=ref["id"], format="full")
                    .execute()
                )
                messages.append(message)
                if max_results is not None and len(messages) >= max_results:
                    return messages

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return messages
