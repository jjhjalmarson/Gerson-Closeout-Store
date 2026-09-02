"""Outbound mail for magic links. `log` backend for dev/tests, `smtp` for prod."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

log = logging.getLogger("store.mail")


def send(cfg, *, to: str, subject: str, body: str) -> bool:
    if cfg.mail_backend == "smtp" and cfg.smtp_host:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = cfg.mail_from, to, subject
        msg.set_content(body)
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.starttls()
            if cfg.smtp_user:
                s.login(cfg.smtp_user, cfg.smtp_password)
            s.send_message(msg)
        return True
    log.info("MAIL (log backend) to=%s subject=%s\n%s", to, subject, body)
    return True
