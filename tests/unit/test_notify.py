"""Unit tests for scanner/notify.py — quarantine email alerts."""
from unittest.mock import MagicMock, patch

import pytest
from config import NotifySettings
from notify import Notifier
from pydantic import SecretStr


def _configured(**over: object) -> NotifySettings:
    base = {
        "smtp_host": "smtp-relay.example.com",
        "smtp_port": 587,
        "smtp_username": "bot@mpsinc.io",
        "smtp_password": SecretStr("app-pw"),
        "from_addr": "bot@mpsinc.io",
        "support_addr": "support@mpsinc.io",
    }
    base.update(over)
    return NotifySettings(**base)  # type: ignore[arg-type]


def _args() -> dict[str, str]:
    return dict(
        filename="scan.pdf",
        folder="/mnt/aria/failed",
        env="production",
        machine="macmini",
        reason="too large",
    )


def test_is_configured_requires_host_from_and_support() -> None:
    assert NotifySettings().is_configured is False
    assert NotifySettings(smtp_host="h").is_configured is False  # no from_addr
    assert _configured().is_configured is True


def test_unconfigured_notifier_logs_warning_does_not_send(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    n = Notifier(NotifySettings())
    assert n.enabled is False
    with caplog.at_level(logging.WARNING, logger="scanner.notify"):
        with patch("notify.smtplib.SMTP") as smtp:
            n.notify_quarantine(**_args())
    smtp.assert_not_called()
    assert any("not" in r.getMessage().lower() for r in caplog.records)


def test_configured_notifier_sends_with_starttls_and_login() -> None:
    n = Notifier(_configured())
    with patch("notify.smtplib.SMTP") as smtp_cls:
        conn = MagicMock()
        smtp_cls.return_value.__enter__.return_value = conn
        n.notify_quarantine(**_args())

    smtp_cls.assert_called_once_with("smtp-relay.example.com", 587, timeout=30)
    conn.starttls.assert_called_once()
    conn.login.assert_called_once_with("bot@mpsinc.io", "app-pw")
    conn.send_message.assert_called_once()
    msg = conn.send_message.call_args.args[0]
    assert msg["To"] == "support@mpsinc.io"
    assert msg["From"] == "bot@mpsinc.io"
    assert "scan.pdf" in msg["Subject"]
    assert "/mnt/aria/failed" in msg.get_content()


def test_no_login_when_credentials_absent() -> None:
    n = Notifier(_configured(smtp_username=None, smtp_password=None))
    with patch("notify.smtplib.SMTP") as smtp_cls:
        conn = MagicMock()
        smtp_cls.return_value.__enter__.return_value = conn
        n.notify_quarantine(**_args())
    conn.login.assert_not_called()
    conn.send_message.assert_called_once()


def test_send_failure_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    n = Notifier(_configured())
    with caplog.at_level(logging.ERROR, logger="scanner.notify"):
        with patch("notify.smtplib.SMTP", side_effect=OSError("relay down")):
            n.notify_quarantine(**_args())  # must NOT raise
    assert any("failed" in r.getMessage().lower() for r in caplog.records)


def test_password_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    n = Notifier(_configured())
    with caplog.at_level(logging.DEBUG, logger="scanner.notify"):
        with patch("notify.smtplib.SMTP") as smtp_cls:
            smtp_cls.return_value.__enter__.return_value = MagicMock()
            n.notify_quarantine(**_args())
    assert all("app-pw" not in r.getMessage() for r in caplog.records)
