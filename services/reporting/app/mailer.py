"""SMTP delivery.

Generation and delivery are tracked independently: a failed email never
invalidates an archived report.

Relay settings come from :mod:`smtp_config`, so a change saved on the Settings
screen applies to the next delivery without a restart. The resolved relay is
passed through by the caller when it has already read it; otherwise it is read
here.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from .smtp_config import SmtpConfig, resolve_smtp

log = logging.getLogger(__name__)


def _connection(config: SmtpConfig) -> smtplib.SMTP:
    smtp = smtplib.SMTP(config.host, config.port, timeout=30)
    smtp.ehlo()
    if config.starttls:
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
    if config.username:
        smtp.login(config.username, config.password)
    return smtp


def send_report_email(
    *, recipients: list[str], subject: str, body: str, pdf_path: Path,
    filename: str, config: SmtpConfig | None = None,
) -> tuple[str, str]:
    """Return (status, detail). status is 'sent', 'failed' or 'skipped'."""
    config = config or resolve_smtp()
    if not recipients:
        return "skipped", "No recipients configured"
    if not config.host:
        return "skipped", "SMTP is not configured"
    if config.username and not config.password:
        return "failed", (
            "The SMTP password is not available. Re-enter it on the Settings "
            "screen (changing the secrets key clears it)."
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)
    try:
        data = pdf_path.read_bytes()
    except OSError as exc:
        return "failed", f"Report file unreadable: {exc}"
    message.add_attachment(
        data, maintype="application", subtype="pdf", filename=filename
    )

    try:
        with _connection(config) as smtp:
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("report email delivery failed: %s", exc)
        return "failed", str(exc)[:500]
    return "sent", ""


def send_test_email(*, recipient: str, config: SmtpConfig) -> tuple[str, str]:
    """Send a short message to prove the relay accepts mail from this host."""
    message = EmailMessage()
    message["Subject"] = "PatchTrack SMTP test"
    message["From"] = config.sender
    message["To"] = recipient
    message.set_content(
        "This message was sent by PatchTrack to verify the configured SMTP "
        "relay. No action is required."
    )
    try:
        with _connection(config) as smtp:
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("SMTP test failed: %s", exc)
        return "failed", str(exc)[:500]
    return "sent", ""
