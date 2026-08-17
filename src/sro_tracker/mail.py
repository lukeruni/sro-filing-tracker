"""Mail transports for the weekly report.

Three of them, because the right answer depends on the machine:

  ``file``     writes the HTML and stops. The default, and the only one enabled
               out of the box - a fresh clone must never be one command away
               from mailing real people.
  ``smtp``     a relay, for scheduled unattended runs.
  ``outlook``  hands a pre-filled draft to the desktop Outlook client via COM,
               which is usually the only sanctioned path on a locked-down
               Windows desktop. Displays the draft rather than sending it, so a
               human still presses send.

Sending is opt-in at three levels: transport must be set, recipients must be
configured, and ``send=True`` must be passed. Nothing here fires by accident.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


class MailError(RuntimeError):
    pass


@dataclass(slots=True)
class Delivery:
    transport: str
    delivered: bool
    detail: str
    path: Path | None = None

    def render(self) -> str:
        mark = "sent" if self.delivered else "prepared"
        return f"[{self.transport}] {mark}: {self.detail}"


def deliver(
    cfg: Config,
    *,
    subject: str,
    html_body: str,
    text_body: str,
    attachment: Path | None = None,
    send: bool = False,
) -> Delivery:
    """Deliver the report according to configuration.

    With ``send=False`` every transport stops at "prepared", which is what the
    CLI's preview mode relies on.
    """
    transport = cfg.mail_transport

    if transport == "file" or not send:
        cfg.ensure_dirs()
        path = cfg.export_dir / "weekly-report-preview.html"
        path.write_text(html_body, encoding="utf-8")
        detail = str(path)
        if transport != "file" and not send:
            detail += f"  (transport '{transport}' configured; pass --send to deliver)"
        return Delivery(transport="file", delivered=False, detail=detail, path=path)

    if not cfg.mail_to:
        raise MailError("mail_to is empty; nowhere to send the report.")

    if transport == "smtp":
        return _smtp(cfg, subject, html_body, text_body, attachment)
    if transport == "outlook":
        return _outlook(cfg, subject, html_body, attachment)
    raise MailError(f"unknown mail transport {transport!r}")


def _smtp(
    cfg: Config,
    subject: str,
    html_body: str,
    text_body: str,
    attachment: Path | None,
) -> Delivery:
    if not cfg.mail_from:
        raise MailError("mail_from must be set for the smtp transport.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg.mail_from
    message["To"] = ", ".join(cfg.mail_to)
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    if attachment and attachment.exists():
        data = attachment.read_bytes()
        message.add_attachment(
            data,
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=attachment.name,
        )

    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
            if cfg.smtp_use_tls:
                server.starttls()
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"SMTP delivery via {cfg.smtp_host}:{cfg.smtp_port} failed: {exc}") from exc

    return Delivery("smtp", True, f"sent to {len(cfg.mail_to)} recipient(s) via {cfg.smtp_host}")


def _outlook(cfg: Config, subject: str, html_body: str, attachment: Path | None) -> Delivery:
    """Create a draft in the desktop Outlook client.

    Deliberately calls Display, not Send: the report goes in front of a person
    who chooses to send it. Automating the send itself is a different decision
    and should be a different, explicit function.
    """
    try:
        import win32com.client  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MailError(
            "The outlook transport needs pywin32 (pip install pywin32) and a "
            "desktop Outlook install. Use mail_transport='smtp' or 'file' otherwise."
        ) from exc

    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)
        mail.Subject = subject
        mail.To = "; ".join(cfg.mail_to)
        mail.HTMLBody = html_body
        if attachment and attachment.exists():
            mail.Attachments.Add(str(attachment.resolve()))
        mail.Display(False)
    except Exception as exc:  # noqa: BLE001 - COM raises a wide variety
        raise MailError(f"Outlook automation failed: {exc}") from exc

    return Delivery("outlook", False,
                    f"draft opened in Outlook for {len(cfg.mail_to)} recipient(s)")
