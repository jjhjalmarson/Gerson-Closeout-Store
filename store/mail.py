"""Outbound mail for sign-in links.

Backends (``MAIL_BACKEND``):

* ``graph`` — Microsoft Graph ``sendMail`` from a mailbox in the Gerson tenant
  (the same mechanism AOI uses for the Oscar mailbox): client-credentials
  token, then ``POST /users/{mailbox}/sendMail``.  Scope the app registration
  to that one mailbox with an Exchange application access policy so a
  compromised store can, at worst, send as that mailbox and nothing else.
* ``smtp`` — any SMTP relay with STARTTLS.
* ``log`` — print the message to the log (dev / tests).
"""
from __future__ import annotations

import logging
import smtplib
import time
from email.message import EmailMessage
from typing import Any

log = logging.getLogger("store.mail")

_LOGIN_HOST = "https://login.microsoftonline.com"
_GRAPH_HOST = "https://graph.microsoft.com/v1.0"
_SCOPE = "https://graph.microsoft.com/.default"
_token_cache: dict[str, Any] = {"value": "", "expires": 0.0}


class MailError(RuntimeError):
    pass


def _graph_token(cfg, session: Any) -> str:
    now = time.time()
    if _token_cache["value"] and _token_cache["expires"] - 60 > now:
        return _token_cache["value"]
    r = session.post(
        f"{_LOGIN_HOST}/{cfg.graph_tenant_id}/oauth2/v2.0/token",
        data={"client_id": cfg.graph_client_id, "client_secret": cfg.graph_client_secret,
              "scope": _SCOPE, "grant_type": "client_credentials"},
        timeout=30,
    )
    if r.status_code != 200:
        raise MailError(f"Graph token request failed: {r.status_code} {str(r.text)[:200]}")
    body = r.json()
    _token_cache["value"] = body["access_token"]
    _token_cache["expires"] = now + float(body.get("expires_in", 3600))
    return _token_cache["value"]


def _send_graph(cfg, *, to: str, subject: str, body: str, session: Any = None) -> bool:
    import requests
    sess = session or requests.Session()
    token = _graph_token(cfg, sess)
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        },
        "saveToSentItems": False,
    }
    r = sess.post(f"{_GRAPH_HOST}/users/{cfg.graph_sender_mailbox}/sendMail", json=payload,
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, timeout=30)
    if r.status_code == 401:
        _token_cache["value"] = ""        # let the next call fetch a fresh token
    if r.status_code not in (200, 202):
        raise MailError(f"Graph sendMail failed: {r.status_code} {str(r.text)[:200]}")
    return True


def _send_smtp(cfg, *, to: str, subject: str, body: str) -> bool:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg.mail_from, to, subject
    msg.set_content(body)
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
        s.starttls()
        if cfg.smtp_user:
            s.login(cfg.smtp_user, cfg.smtp_password)
        s.send_message(msg)
    return True


def send(cfg, *, to: str, subject: str, body: str, session: Any = None) -> bool:
    """Send one plain-text email. Never raises to the caller: a login attempt
    must not leak whether mail worked. Returns True on success."""
    try:
        if cfg.mail_backend == "graph" and cfg.graph_client_id:
            return _send_graph(cfg, to=to, subject=subject, body=body, session=session)
        if cfg.mail_backend == "smtp" and cfg.smtp_host:
            return _send_smtp(cfg, to=to, subject=subject, body=body)
        log.info("MAIL (log backend) to=%s subject=%s\n%s", to, subject, body)
        return True
    except Exception as exc:
        log.error("mail send to %s failed: %s", to, exc)
        return False
