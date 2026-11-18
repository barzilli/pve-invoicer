"""Send email over SMTP."""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import SmtpConfig


@dataclass(frozen=True)
class Attachment:
    filename: str
    data: bytes
    mimetype: str = "application/octet-stream"


class SmtpMailer:
    def __init__(self, cfg: SmtpConfig):
        self.cfg = cfg

    def send(self, to: list[str], subject: str, body: str, cc: list[str] = (), bcc: list[str] = (),
             attachments: list[Attachment] = ()) -> str:
        msg = EmailMessage()
        msg["From"] = self.cfg.sender
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        domain = self.cfg.sender.rsplit("@", 1)[-1].strip(">")
        msg["Message-ID"] = make_msgid(domain=domain)
        msg.set_content(body)
        for a in attachments:
            maintype, _, subtype = a.mimetype.partition("/")
            msg.add_attachment(a.data, maintype=maintype, subtype=subtype or "octet-stream", filename=a.filename)

        recipients = [*to, *cc, *bcc]
        ctx = ssl.create_default_context()
        if self.cfg.security == "ssl":
            server = smtplib.SMTP_SSL(self.cfg.host, self.cfg.port, context=ctx, timeout=60)
        else:
            server = smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=60)
        with server:
            if self.cfg.security == "starttls":
                server.starttls(context=ctx)
            if self.cfg.username:
                server.login(self.cfg.username, self.cfg.password or "")
            server.send_message(msg, to_addrs=recipients)
        return msg["Message-ID"]
