from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from expenses_tracker.config import Settings
from expenses_tracker.delivery.events import SyncCompletedEvent
from expenses_tracker.models import User
from expenses_tracker.notifications import format_sync_email_html, format_sync_email_subject, format_sync_email_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailDeliveryResult:
    sent: int
    failed: int
    skipped: int


class EmailChannel:
    name = "email"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_configured(self) -> bool:
        return bool(self.settings.smtp_host and self.settings.smtp_from)

    def send_sync_completed(
        self,
        event: SyncCompletedEvent,
        recipients: list[User],
    ) -> EmailDeliveryResult:
        opted_in = [user for user in recipients if user.notify_email]
        if not opted_in:
            return EmailDeliveryResult(sent=0, failed=0, skipped=len(recipients))
        if not self.is_configured():
            logger.warning("Email channel enabled but SMTP is not configured.")
            return EmailDeliveryResult(sent=0, failed=0, skipped=len(opted_in))

        subject = format_sync_email_subject(event)
        text_body = format_sync_email_text(event)
        html_body = format_sync_email_html(event)
        sent = 0
        failed = 0
        skipped = len(recipients) - len(opted_in)

        for user in opted_in:
            try:
                self._send_message(user.email, subject, text_body, html_body)
                sent += 1
            except Exception:
                logger.exception("Failed to send sync email to %s", user.email)
                failed += 1

        return EmailDeliveryResult(sent=sent, failed=failed, skipped=skipped)

    def _send_message(
        self,
        to_address: str,
        subject: str,
        text_body: str,
        html_body: str,
    ) -> None:
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = self.settings.smtp_from
        message["To"] = to_address
        message.attach(MIMEText(text_body, "plain", "utf-8"))
        message.attach(MIMEText(html_body, "html", "utf-8"))

        if self.settings.smtp_use_tls:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                if self.settings.smtp_user:
                    smtp.login(self.settings.smtp_user, self.settings.smtp_password or "")
                smtp.send_message(message)
        else:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as smtp:
                if self.settings.smtp_user:
                    smtp.login(self.settings.smtp_user, self.settings.smtp_password or "")
                smtp.send_message(message)
