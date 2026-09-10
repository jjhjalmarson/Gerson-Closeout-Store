"""Emails that bring a buyer back: the new-arrivals digest and the featured list.

Until now a buyer who submitted an offer and heard nothing had no reason and
no prompt to come back; the store held the step-down schedule and the arrival
dates and told nobody.  AOI's nightly changelog stamps every catalog row with
``listed_since``, and this module turns that into the one email a buyer would
actually open: "N new items on the closeout sheet", the items, the pack, what
is available, the original wholesale, and a link to the sheet filtered to
just those.

What it never says: a closeout price, a discount, a step-down date.  The sheet
does not show those to buyers (leadership, 2026-09-02) and neither does this.

**Cadence is per buyer** (buyer feedback via JJ, 2026-09-10: Kendra wants once
a month, others want it as it lands).  Each approved buyer carries a
``digest_cadence``, set on ``/admin``:

* ``daily`` -- after every nightly feed that put something new on the sheet;
* ``weekly`` -- the default and what everyone had before: on ``DIGEST_WEEKDAY``;
* ``monthly`` -- on ``DIGEST_WEEKDAY``, every fourth week;
* ``never`` -- no digest and no featured mailing.

Every cadence's window starts the day after its last send, so nothing is
mailed twice and nothing falls between two sends.  ``DIGEST_WEEKDAY`` stays
the master switch: unset (the default) and nothing sends itself for anyone.

**Featured** (JJ, 2026-09-10: "if we add hot deals, add the option to trigger
an email"): the items AOI marked featured, emailed on demand from ``/admin`` to
every buyer who takes mail at all.  Same table, same silence about price.

How the digest goes out:

* by hand, from ``/admin`` (preview, then "Send to N buyers") -- to everyone
  not on ``never``, whatever their cadence;
* by itself, after the nightly catalog feed, per cadence as above.

Recipients are approved buyers with an email, and nobody else: not pending,
not suspended, not the legacy AOI allowlist, not invite-link holders.
"""
from __future__ import annotations

import html as _h
import logging
from datetime import date, timedelta
from typing import Any

from . import mail
from .db import CADENCES, DEFAULT_CADENCE, clean_cadence

log = logging.getLogger(__name__)

KIND = "new_arrivals"
FEATURED_KIND = "featured"
MAX_ITEMS = 48
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# What each cadence means on /admin, and how the auto-send paces it: the first
# send looks back this many days, and a cadence never fires again inside its
# guard (weekly = 6 so a same-weekday send is never skipped by a late feed).
CADENCE_LABELS = {"daily": "as it lands (after each nightly feed)", "weekly": "once a week",
                  "monthly": "once a month", "never": "no emails"}
_LOOKBACK_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}
_GUARD_DAYS = {"daily": 1, "weekly": 6, "monthly": 27}
_AUTO_CADENCES = ("daily", "weekly", "monthly")

_CSS = "font:14px/1.5 'Segoe UI',Arial,sans-serif;color:#1f2430"
_TH = ("text-align:left;font:600 11px 'Segoe UI',Arial,sans-serif;letter-spacing:.03em;text-transform:uppercase;"
       "color:#5b6472;padding:0 10px 6px;border-bottom:1px solid #d5dae1;white-space:nowrap")
_TD = "text-align:left;padding:8px 10px;border-bottom:1px solid #eef1f4;vertical-align:top"
_FOOT = ("You are getting this because your account is approved on the Gerson closeout offer sheet. "
         "Reply to this email to get it less often, or not at all.")
_CLOSE = ("Original wholesale and pack sizes are shown; you name the price. Our team replies by email with an "
          "acceptance or a counter.")


def cutoff(days: int) -> str:
    return (date.today() - timedelta(days=max(int(days or 7), 1))).isoformat()


def build(store, *, since: str, limit: int = MAX_ITEMS) -> list[dict[str, Any]]:
    """Everything that went on the sheet on or after ``since``, newest first.
    Every company: approved buyers see every company's SKUs."""
    return store.list_products(new_since=since, sort="newest", limit=limit)


def build_featured(store, *, limit: int = MAX_ITEMS) -> list[dict[str, Any]]:
    """The featured list in the order AOI ranked it -- the sheet's own order."""
    return store.list_products(featured_only=True, sort="default", limit=limit)


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


def featured_link(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/?featured=1"


def _span(days: int) -> str:
    """"today" / "this week" / "this month" / nothing, from how far back the
    window reaches -- the subject line uses it."""
    days = int(days or 0)
    if days <= 1:
        return "today"
    if days <= 7:
        return "this week"
    if days <= 31:
        return "this month"
    return ""


def subject(n: int, days: int) -> str:
    span = _span(days)
    return f"{n} new closeout item{'' if n == 1 else 's'} on the Gerson sheet" + (f" {span}" if span else "")


def featured_subject(n: int) -> str:
    return f"{n} featured closeout item{'' if n == 1 else 's'} on the Gerson sheet — our sharpest prices"


def _window(days: int) -> str:
    days = int(days or 0)
    return "today" if days <= 1 else f"in the last {days} days"


# --- rendering: one table, two headlines ---------------------------------------------

def _rows_text(items: list[dict[str, Any]]) -> list[str]:
    return [f"- {p['description']}  ({p['sku']} · {p.get('brand') or ''}"
            f"{' · ' + p['category'] if p.get('category') else ''})  "
            f"{_pack(p)} · {int(p.get('qty_available') or 0):,} available · wholesale ${float(p.get('wholesale') or 0):,.2f}"
            for p in items]


def _text(items, *, lead: str, link: str, total: int) -> str:
    lines = [lead, "", f"Make an offer: {link}", ""] + _rows_text(items)
    if total > len(items):
        lines.append(f"...and {total - len(items)} more on the sheet.")
    lines += ["", _CLOSE, "", "Gerson closeouts"]
    return "\n".join(lines)


def _html(items, *, headline: str, link: str, cta: str, total: int) -> str:
    # Entities, not raw glyphs: mail clients do not all honour the charset.
    e = lambda v: _h.escape(str(v or "")).replace("·", "&middot;")      # noqa: E731
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
            f"<p style=\"margin:0 0 4px;font-size:16px\">{headline}</p>"
            f"<p style=\"margin:0 0 18px;color:#5b6472\">{_CLOSE}</p>"
            f"<p style=\"margin:0 0 18px\"><a href=\"{e(link)}\" style=\"background:#0e8c8a;color:#fff;text-decoration:none;"
            f"padding:9px 16px;border-radius:6px;font-weight:600;display:inline-block\">{cta}</a></p>"
            f"{table}{more}"
            f"<p style=\"margin:18px 0 0;color:#5b6472;font-size:12px\">{_FOOT}</p></div>")


def text(items: list[dict[str, Any]], *, since: str, base_url: str, total: int | None = None) -> str:
    total = total if total is not None else len(items)
    return _text(items, lead=f"{total} new item{'' if total == 1 else 's'} went on the Gerson closeout offer sheet since {since}.",
                 link=sheet_link(base_url), total=total)


def html(items: list[dict[str, Any]], *, since: str, base_url: str, days: int = 7, total: int | None = None) -> str:
    total = total if total is not None else len(items)
    return _html(items, headline=f"<b>{total} new item{'' if total == 1 else 's'}</b> went on the sheet {_window(days)}.",
                 link=sheet_link(base_url), cta="See what is new and make an offer", total=total)


def featured_text(items: list[dict[str, Any]], *, base_url: str, total: int | None = None) -> str:
    total = total if total is not None else len(items)
    return _text(items, lead=f"{total} featured item{'' if total == 1 else 's'} on the Gerson closeout offer sheet: "
                             f"our sharpest prices, and we will look hard at any offer on them.",
                 link=featured_link(base_url), total=total)


def featured_html(items: list[dict[str, Any]], *, base_url: str, total: int | None = None) -> str:
    total = total if total is not None else len(items)
    return _html(items, headline=f"<b>{total} featured item{'' if total == 1 else 's'}</b> &mdash; our sharpest prices, "
                                 f"and we will look hard at any offer on them.",
                 link=featured_link(base_url), cta="See the featured items and make an offer", total=total)


# --- who ----------------------------------------------------------------------------

def cadence_of(b: dict[str, Any]) -> str:
    return clean_cadence(b.get("digest_cadence")) if b.get("digest_cadence") else DEFAULT_CADENCE


def recipients(store, *, cadence: str | None = None) -> list[dict[str, Any]]:
    """Approved buyers with an email who take mail at all; narrowed to one
    cadence when the auto-send asks."""
    out = []
    for b in store.list_buyers(status="approved"):
        if not str(b.get("email") or "").strip():
            continue
        c = cadence_of(b)
        if c == "never" or (cadence and c != cadence):
            continue
        out.append(b)
    return out


def status(ctx) -> dict[str, Any]:
    """What /admin shows: how much is new, who would get it and how often, when
    it last went; the same for the featured list."""
    store = ctx.store
    since = cutoff(ctx.cfg.digest_days)
    by = {c: 0 for c in CADENCES}
    for b in store.list_buyers(status="approved"):
        if str(b.get("email") or "").strip():
            by[cadence_of(b)] += 1
    wd = (ctx.cfg.digest_weekday or "").lower()[:3]
    return {"since": since, "days": int(ctx.cfg.digest_days or 7), "weekday": wd if wd in _WEEKDAYS else "",
            "new_count": store.count_products(new_since=since), "recipients": len(recipients(store)),
            "by_cadence": by, "labels": CADENCE_LABELS,
            "last": store.last_digest(KIND),
            "last_by_cadence": {c: store.last_digest(KIND, cadence=c) for c in _AUTO_CADENCES},
            "featured_count": store.count_products(featured_only=True),
            "featured_last": store.last_digest(FEATURED_KIND)}


# --- sending ------------------------------------------------------------------------

def _deliver(cfg, to: list[dict[str, Any]], *, subj: str, body: str, page: str, what: str) -> tuple[int, int]:
    sent = failed = 0
    for b in to:
        try:
            if mail.send(cfg, to=b["email"], subject=subj, body=body, html=page):
                sent += 1
            else:
                failed += 1
        except Exception:                                  # noqa: BLE001
            log.exception("%s to %s failed", what, b["email"])
            failed += 1
    return sent, failed


def send(ctx, *, since: str, sent_by: str = "", cadence: str | None = None,
         today: date | None = None) -> dict[str, Any]:
    """One new-arrivals email per buyer -- everyone who takes mail, or just one
    cadence's worth when the auto-send is working through them.  Records the
    run (under its cadence) even when nobody could be reached, so the pacing
    guards still hold."""
    store, cfg = ctx.store, ctx.cfg
    items = build(store, since=since)
    total = store.count_products(new_since=since)
    if not items:
        return {"sent": 0, "items": 0, "reason": f"nothing went on the sheet since {since}"}
    to = recipients(store, cadence=cadence)
    if not to:
        return {"sent": 0, "items": total,
                "reason": ("no approved buyer has an email address" if cadence is None
                           else f"no buyer is on the {cadence} digest")}
    try:
        days = max(((today or date.today()) - date.fromisoformat(str(since)[:10])).days, 1)
    except ValueError:
        days = int(cfg.digest_days or 7)
    body = text(items, since=since, base_url=cfg.base_url, total=total)
    page = html(items, since=since, base_url=cfg.base_url, days=days, total=total)
    sent, failed = _deliver(cfg, to, subj=subject(total, days), body=body, page=page, what="digest")
    store.record_digest(KIND, since=since, items=total, recipients=sent, sent_by=sent_by, cadence=cadence or "")
    log.info("new-arrivals digest (%s): %d items to %d buyers (%d failed) by %s",
             cadence or "all", total, sent, failed, sent_by or "auto")
    return {"sent": sent, "failed": failed, "items": total, "since": since, "cadence": cadence or ""}


def send_featured(ctx, *, sent_by: str = "") -> dict[str, Any]:
    """The featured list, to every buyer who takes mail.  On demand only: an
    admin decides that this week's picks are worth an inbox."""
    store, cfg = ctx.store, ctx.cfg
    items = build_featured(store)
    total = store.count_products(featured_only=True)
    if not items:
        return {"sent": 0, "items": 0, "reason": "nothing is featured right now"}
    to = recipients(store)
    if not to:
        return {"sent": 0, "items": total, "reason": "no approved buyer has an email address"}
    body = featured_text(items, base_url=cfg.base_url, total=total)
    page = featured_html(items, base_url=cfg.base_url, total=total)
    sent, failed = _deliver(cfg, to, subj=featured_subject(total), body=body, page=page, what="featured mailing")
    store.record_digest(FEATURED_KIND, since=None, items=total, recipients=sent, sent_by=sent_by)
    log.info("featured mailing: %d items to %d buyers (%d failed) by %s", total, sent, failed, sent_by or "auto")
    return {"sent": sent, "failed": failed, "items": total}


def _last_day(last: dict[str, Any] | None) -> date | None:
    if not last or not last.get("sent_at"):
        return None
    try:
        return date.fromisoformat(str(last["sent_at"])[:10])
    except ValueError:
        return None


def due(cadence: str, *, today: date, weekday: str, last: dict[str, Any] | None) -> bool:
    """Does this cadence go out today?  Daily: any day it has not gone yet.
    Weekly / monthly: the configured weekday, and not inside the guard."""
    last_day = _last_day(last)
    if last_day and (today - last_day).days < _GUARD_DAYS[cadence]:
        return False
    if cadence == "daily":
        return True
    return _WEEKDAYS[today.weekday()] == weekday


def window_since(cadence: str, *, today: date, last: dict[str, Any] | None, weekly_days: int) -> str:
    """Where this cadence's window starts: the day after its last send, so no
    item is mailed twice and none is skipped; on a first send, its look-back."""
    last_day = _last_day(last)
    if last_day:
        return (last_day + timedelta(days=1)).isoformat()
    back = weekly_days if cadence == "weekly" else _LOOKBACK_DAYS[cadence]
    return (today - timedelta(days=max(int(back or 1), 1))).isoformat()


def maybe_auto_send(ctx, *, today: date | None = None) -> dict[str, Any] | None:
    """After a catalog feed: work through the cadences that are due.  Returns
    what went, or None when nothing did.  ``DIGEST_WEEKDAY`` unset = never."""
    wd = (ctx.cfg.digest_weekday or "").lower()[:3]
    if wd not in _WEEKDAYS:
        return None
    today = today or date.today()
    runs = []
    for cadence in _AUTO_CADENCES:
        last = ctx.store.last_digest(KIND, cadence=cadence)
        if not due(cadence, today=today, weekday=wd, last=last):
            continue
        if not recipients(ctx.store, cadence=cadence):
            continue
        since = window_since(cadence, today=today, last=last, weekly_days=ctx.cfg.digest_days)
        r = send(ctx, since=since, sent_by="auto", cadence=cadence, today=today)
        if r.get("sent"):
            runs.append(r)
    if not runs:
        return None
    return {"sent": sum(r["sent"] for r in runs), "runs": runs}
