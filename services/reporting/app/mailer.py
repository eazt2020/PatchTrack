"""SMTP delivery.

Generation and delivery are tracked independently: a failed email never
invalidates an archived report.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from patchtrack_common.config import get_settings

log = logging.getLogger(__name__)


def send_report_email(
    *, recipients: list[str], subject: str, body: str, pdf_path: Path,
    filename: str,
) -> tuple[str, str]:
    """Return (status, detail). status is 'sent', 'failed' or 'skipped'."""
    settings = get_settings()
    if not recipients:
        return "skipped", "No recipients configured"
    if not settings.smtp_host:
        return "skipped", "SMTP is not configured"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_sender
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
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            if settings.smtp_starttls:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("report email delivery failed: %s", exc)
        return "failed", str(exc)[:500]
    return "sent", ""
