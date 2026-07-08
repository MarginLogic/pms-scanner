"""Best-effort operator email alerts for unrecoverable failures.

When a scan is quarantined to ``failed/`` (the backend permanently refused it
even after downsampling), a human needs to act on it — it will never be retried
automatically. :class:`Notifier` emails a configurable support address with the
file name and folder so cleanup can happen promptly.

Delivery is deliberately best-effort: any SMTP error is logged and swallowed so
a mail outage can never crash file processing or re-trigger the retry loop this
whole mechanism exists to prevent. If SMTP is not configured, quarantine still
happens and a warning is logged instead.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import NotifySettings

logger = logging.getLogger("scanner.notify")


class Notifier:
    """Sends quarantine alerts over SMTP, if configured."""

    def __init__(self, settings: NotifySettings) -> None:
        self._s = settings

    @property
    def enabled(self) -> bool:
        return bool(self._s.is_configured)

    def notify_quarantine(
        self,
        *,
        filename: str,
        folder: str,
        env: str,
        machine: str,
        reason: str,
    ) -> None:
        """Email support that *filename* was quarantined. Never raises."""
        if not self.enabled:
            logger.warning(
                "%s quarantined to %s but support notification is NOT "
                "configured — set NOTIFY__SMTP_HOST / NOTIFY__FROM_ADDR "
                "(and credentials) to enable email alerts",
                filename,
                folder,
            )
            return

        subject = (
            f"[pms-scanner] Quarantined scan on {machine}/{env}: {filename}"
        )
        body = (
            "pms-scanner could not upload a scan and moved it to a quarantine "
            "folder for manual action.\n\n"
            f"File:        {filename}\n"
            f"Folder:      {folder}\n"
            f"Machine:     {machine}\n"
            f"Environment: {env}\n"
            f"Reason:      {reason}\n\n"
            "This file will NOT be retried automatically. Please review it, "
            "then re-submit or discard it.\n"
        )
        try:
            self._send(subject, body)
            logger.info(
                "Sent quarantine notification for %s to %s",
                filename,
                self._s.support_addr,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to send quarantine notification for %s: %s",
                filename,
                exc,
            )

    def _send(self, subject: str, body: str) -> None:
        s = self._s
        assert s.smtp_host is not None and s.from_addr is not None
        msg = EmailMessage()
        msg["From"] = s.from_addr
        msg["To"] = s.support_addr
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP(
            s.smtp_host, s.smtp_port, timeout=s.timeout_seconds
        ) as smtp:
            if s.use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if s.smtp_username and s.smtp_password is not None:
                smtp.login(
                    s.smtp_username, s.smtp_password.get_secret_value()
                )
            smtp.send_message(msg)
