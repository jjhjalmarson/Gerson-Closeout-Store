"""The new-arrivals digest: what went on the sheet lately, emailed to approved buyers.

Until now a buyer who submitted an offer and heard nothing had no reason and
no prompt to come back; the store held the step-down schedule and the arrival
dates and told nobody.  AOI's nightly changelog now stamps every catalog row
with ``listed_since``, and this module turns that into the one email a buyer
would actually open: "N new items on the closeout sheet this week", the items,
the pack, what is available, the original wholesale, and a link to the sheet
filtered to just those.

What it never says: a closeout price, a discount, a step-down date.  The sheet
does not show those to buyers (leadership, 2026-09-02) and neither does this.

How it goes out:

* by hand, from ``/admin`` (preview, then "Send to N approved buyers");
* by itself, after the nightly catalog feed, on ``DIGEST_WEEKDAY`` only, and
  never twice inside six days -- so a feed re-run cannot double-mail.  Empty
  ``DIGEST_WEEKDAY`` (the default) means it never sends on its own.

Recipients are approved buyers with an email, and nobody else: not pending,
not suspended, not the legacy AOI allowlist, not invite-link holders.
"""
from __future__ import annotations

import html as _h
import logging
from datetime import date, timedelta
from typing import Any

from . import mail

log = logging.getLogger(__name__)

KIND = "new_arrivals"
MAX_ITEMS = 48
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_CSS = "font:14px/1.5 'Segoe UI',Arial,sans-serif;color:#1f2430"
_TH = ("text-align:left;font:600 11px 'Segoe UI',Arial,sans-serif;letter-spacing:.03em;text-transform:uppercase;"
       "color:#5b6472;padding:0 10px 6px;border-bottom:1px solid #d5dae1;white-space:nowrap")
_TD = "text-align:left;padding:8px 10px;border-bottom:1px solid #eef1f4;vertical-align:top"


def cutoff(days: int) -> str:
    return (date.today() - timedelta(days=max(int(days or 7), 1))).isoformat()


def build(store, *, since: str, limit: int = MAX_ITEMS) -> list[dict[str, Any]]:
    """Everything that went on the sheet on or after ``since``, newest first.
    Every company: approved buyers see every company's SKUs."""
    return store.list_products(new_since=since, sort="newest", limit=limit)


def _pack(p: dict[str, Any]) -> str:
    inner, master, case = int(p.get("inner_pack") or 0), int(p.get("master_pack") or 0), int(p.get("case_pack") or 0)
    bits = []
    if inner:
        bits.append(f"inner {inner}")
    if master:
        bits.append(f"master {master}")
    return " · ".join(bits) if bits else f"pack of {case or 1}"


def sheet_link(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/?new=1&sort=newest"


def subject(n: int, days: int) -> str:
    return f"{n} new closeout item{'' if n == 1 else 's'} on the Gerson sheet this week" if days <= 7 \
        else f"{n} new closeout item{'' if n == 1 else 's'} on the Gerson sheet"


def text(items: list[dict[str, Any]], *, since: str, base_url: str, total: int | None = None) -> str:
    total = total if total is not None else len(items)
    lines = [f"{total} new item{'' if total == 1 else 's'} went on the Gerson closeout offer sheet since {since}.", "",
             f"Make an offer: {sheet_link(base_url)}", ""]
    for p in items:
        lines.append(f"- {p['description']}  ({p['sku']} · {p.get('brand') or ''}"
                     f"{' · ' + p['category'] if p.get('category') else ''})  "
                     f"{_pack(p)} · {int(p.get('qty_available') or 0):,} available · wholesale ${float(p.get('wholesale') or 0):,.2f}")
    if total > len(items):
        lines.append(f"...and {total - len(items)} more on the sheet.")
    lines += ["", "Original wholesale and pack sizes are shown; you name the price. Our team replies by email with an "
                  "acceptance or a counter.", "", "Gerson closeouts"]
    return "\n".join(lines)


def html(items: list[dict[str, Any]], *, since: str, base_url: str, days: int = 7, total: int | None = None) -> str:
    # Entities, not raw glyphs: mail clients do not all honour the charset.
    e = lambda v: _h.escape(str(v or "")).replace("·", "&middot;")      # noqa: E731
    total = total if total is not None else len(items)
    link = sheet_link(base_url)
    rows = []
    for p in items:
        where = " · ".join(x for x in (p.get("brand"), p.get("category"), p.get("subcategory")) if x)
        rows.append(
            f"<tr><td style=\"{_TD}\"><b>{e(p['description'])}</b>"
            f"<div style=\"color:#5b6472;font-size:12px\">{e(p['sku'])}{' · ' + e(where) if where else ''}</div></td>"
            f"<td style=\"{_TD};white-space:nowrap;color:#5b6472\">{e(_pack(p))}</td>"
            f"<td style=\"{_TD};text-align:right;white-space:nowrap\">{int(p.get('qty_available') or 0):,}</td>"
            f"<td style=\"{_TD};text-align:right;white-space:nowrap\">${float(p.get('wholesale') or 0):,.2f}</td></tr>")
    heads = (("Item", ""), ("Pack", ""), ("Available", ";text-align:right"), ("Wholesale", ";text-align:right"))
    table = ("<table cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse;width:100%;max-width:720px\"><tr>"
             + "".join(f"<th style=\"{_TH}{extra}\">{h}</th>" for h, extra in heads) + "</tr>" + "".join(rows) + "</table>")
    more = (f"<p style=\"margin:10px 0 0;color:#5b6472\">&hellip;and {total - len(items)} more on the sheet.</p>"
            if total > len(items) else "")
    return (f"<div style=\"{_CSS}\">"
            f"<p style=\"margin:0 0 14px;font:600 12px 'Segoe UI',Arial,sans-serif;letter-spacing:.06em;"
            f"text-transform:uppercase;color:#0e8c8a\">Gerson closeout offer sheet</p>"
            f"<p style=\"margin:0 0 4px;font-size:16px\"><b>{total} new item{'' if total == 1 else 's'}</b> went on the sheet "
            f"in the last {int(days)} days.</p>"
            f"<p style=\"margin:0 0 18px;color:#5b6472\">Original wholesale and pack sizes are shown; you name the price. "
            f"Our team replies by email with an acceptance or a counter.</p>"
            f"<p style=\"margin:0 0 18px\"><a href=\"{e(link)}\" style=\"background:#0e8c8a;color:#fff;text-decoration:none;"
            f"padding:9px 16px;border-radius:6px;font-weight:600;display:inline-block\">See what is new and make an offer</a></p>"
            f"{table}{more}"
            f"<p style=\"margin:18px 0 0;color:#5b6472;font-size:12px\">You are getting this because your account is approved "
            f"on the Gerson closeout offer sheet. Reply to this email if you would rather not receive it.</p></div>")


def recipients(store) -> list[dict[str, Any]]:
    return [b for b in store.list_buyers(status="approved") if str(b.get("email") or "").strip()]


def status(ctx) -> dict[str, Any]:
    """What /admin shows: how much is new, who would get it, when it last went."""
    since = cutoff(ctx.cfg.digest_days)
    return {"since": since, "days": int(ctx.cfg.digest_days or 7), "weekday": ctx.cfg.digest_weekday or "",
            "new_count": ctx.store.count_products(new_since=since), "recipients": len(recipients(ctx.store)),
            "last": ctx.store.last_digest(KIND)}


def send(ctx, *, since: str, sent_by: str = "") -> dict[str, Any]:
    """One email per approved buyer.  Records the run even when nobody could
    be reached, so the auto-send's once-a-week guard still holds."""
    store, cfg = ctx.store, ctx.cfg
    items = build(store, since=since)
    total = store.count_products(new_since=since)
    if not items:
        return {"sent": 0, "items": 0, "reason": f"nothing went on the sheet since {since}"}
    to = recipients(store)
    if not to:
        return {"sent": 0, "items": total, "reason": "no approved buyer has an email address"}
    days = int(cfg.digest_days or 7)
    body = text(items, since=since, base_url=cfg.base_url, total=total)
    page = html(items, since=since, base_url=cfg.base_url, days=days, total=total)
    subj = subject(total, days)
    sent = failed = 0
    for b in to:
        try:
            if mail.send(cfg, to=b["email"], subject=subj, body=body, html=page):
                sent += 1
            else:
                failed += 1
        except Exception:                                  # noqa: BLE001
            log.exception("digest to %s failed", b["email"])
            failed += 1
    store.record_digest(KIND, since=since, items=total, recipients=sent, sent_by=sent_by)
    log.info("new-arrivals digest: %d items to %d buyers (%d failed) by %s", total, sent, failed, sent_by or "auto")
    return {"sent": sent, "failed": failed, "items": total, "since": since}


def maybe_auto_send(ctx, *, today: date | None = None) -> dict[str, Any] | None:
    """After a catalog feed: send on the configured weekday, once a week at most."""
    wd = (ctx.cfg.digest_weekday or "").lower()[:3]
    if wd not in _WEEKDAYS:
        return None
    today = today or date.today()
    if _WEEKDAYS[today.weekday()] != wd:
        return None
    last = ctx.store.last_digest(KIND)
    if last and last.get("sent_at"):
        try:
            last_day = date.fromisoformat(str(last["sent_at"])[:10])
        except ValueError:
            last_day = None
        if last_day and (today - last_day).days < 6:
            return None
    return send(ctx, since=cutoff(ctx.cfg.digest_days), sent_by="auto")
