"""Buyer-facing routes: the invitation-only offer sheet.

Leadership decision 2026-09-02: the store is a bidding / offer surface for key
accounts and regionals.  Anyone holding an invite link (``/i/<token>``, created
and revoked in AOI) — or an allowlisted account signing in by magic link —
sees every SKU's **original wholesale, case pack and a blank offer field**.
They select items, type quantities and offered prices, download the sheet as
CSV if they like, and submit: the offer is emailed to the designated inbox
(``OFFER_NOTIFY_EMAILS``), copied to the buyer, and written to the outbox for
AOI.  Counters happen by email between people; nothing is priced here.

No closeout price, discount or step-down is shown on this surface, and no
price is ever taken from the client except as *the buyer's own offer*.
"""
from __future__ import annotations

import csv
import functools
import io
import re
from typing import Any

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, session,
                   url_for)

from . import mail

bp = Blueprint("shop", __name__)
PAGE_SIZE = 60
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CSV_COLUMNS = ("sku", "description", "brand", "category", "subcategory", "case_pack", "master_pack", "inner_pack",
               "qty_available", "wholesale", "qty", "offer_price", "extended")


def _ctx():
    return current_app.config["STORE"]


def _resolve_buyer() -> dict[str, Any] | None:
    """Who is looking: an invite-link holder or a signed-in allowlisted account."""
    store = _ctx().store
    tok = session.get("invite")
    if tok:
        inv = store.invite(tok)
        if inv:
            return {"kind": "invite", "key": f"inv:{tok}", "token": tok, "name": inv["label"] or "Guest",
                    "contact": inv.get("contact") or "", "email": inv.get("email") or "",
                    "companies": inv["companies"], "customer_id": None, "rep_name": ""}
        return None
    cid = session.get("customer_id")
    if cid:
        cust = store.customer(cid)
        if cust:
            return {"kind": "customer", "key": f"cust:{cust['customer_id']}", "token": "", "name": cust["company_name"],
                    "contact": "", "email": "", "companies": cust["companies"], "customer_id": cust["customer_id"],
                    "rep_name": cust.get("rep_name") or "", "accounts": cust["accounts"]}
    return None


def access_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        buyer = _resolve_buyer()
        if not buyer:
            session.clear()
            return redirect(url_for("shop.login", next=request.path))
        g.buyer = buyer
        return fn(*a, **kw)
    return wrapper


# --- access ---------------------------------------------------------------------

@bp.get("/i/<token>")
def invite(token: str):
    """Open the sheet with an invite link. The token stays in the session so the
    buyer can browse without the link; revoking or expiring it in AOI ends access."""
    inv = _ctx().store.invite(token)
    if not inv:
        flash("That invitation link is no longer valid. Ask your Gerson contact for a new one.")
        return redirect(url_for("shop.login"))
    session.clear()
    session["invite"] = token
    session.permanent = True
    return redirect(url_for("shop.home"))


@bp.get("/login")
def login():
    if _resolve_buyer():
        return redirect(url_for("shop.home"))
    return render_template("login.html", website_url=_ctx().cfg.website_url)


@bp.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    ctx = _ctx()
    if _EMAIL.match(email):
        cust = ctx.store.customer_for_email(email)
        if cust:
            token = ctx.store.create_login_token(email, cust["customer_id"], ctx.cfg.login_token_minutes)
            link = f"{ctx.cfg.base_url}{url_for('shop.login_token', token=token)}"
            mail.send(ctx.cfg, to=email, subject="Your Gerson Closeouts sign-in link",
                      body=f"Sign in to the Gerson closeout offer sheet:\n\n{link}\n\nThis link works once and expires in "
                           f"{ctx.cfg.login_token_minutes} minutes. If you did not request it, ignore this email.")
    # Same response whether or not the email is on the allowlist: no enumeration.
    return render_template("login.html", sent=True, email=email, website_url=ctx.cfg.website_url)


@bp.get("/login/<token>")
def login_token(token: str):
    cust = _ctx().store.redeem_login_token(token)
    if not cust:
        flash("That sign-in link has expired or was already used. Request a new one.")
        return redirect(url_for("shop.login"))
    session.clear()
    session["customer_id"] = cust["customer_id"]
    session.permanent = True
    return redirect(url_for("shop.home"))


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("shop.login"))


@bp.get("/apply")
def apply():
    return render_template("apply.html", website_url=_ctx().cfg.website_url)


@bp.post("/apply")
def apply_post():
    f = request.form
    payload = {k: (f.get(k) or "").strip() for k in ("company", "contact", "email", "phone", "resale_number", "website",
                                                     "address", "city", "state", "zip", "notes")}
    if not payload["company"] or not _EMAIL.match(payload["email"]):
        flash("Company name and a valid email are required.")
        return render_template("apply.html", form=payload, website_url=_ctx().cfg.website_url), 400
    _ctx().store.enqueue("application", payload)
    return render_template("apply.html", submitted=True, website_url=_ctx().cfg.website_url)


# --- the sheet ------------------------------------------------------------------

def _draft_count(store, buyer) -> int:
    return len(store.draft(buyer["key"]))


@bp.get("/")
@access_required
def home():
    store = _ctx().store
    a = request.args
    f = {"brand": a.get("brand") or None, "category": a.get("category") or None,
         "subcategory": a.get("subcategory") or None, "q": (a.get("q") or "").strip() or None}
    sort = a.get("sort") or "default"
    if sort not in store.SORTS:
        sort = "default"
    try:
        page = max(int(a.get("page") or 1), 1)
    except ValueError:
        page = 1
    filtered = any(f.values())
    companies = g.buyer["companies"]
    total = store.count_products(**f, companies=companies)
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    items = store.list_products(**f, sort=sort, companies=companies, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    draft = store.draft(g.buyer["key"])
    for p in items:
        d = draft.get(p["sku"]) or {}
        p["draft_qty"] = d.get("qty") or ""
        p["draft_price"] = ("%.2f" % d["price"]) if d.get("price") else ""
    args = {k: v for k, v in {**f, "sort": sort if sort != "default" else None}.items() if v}
    return render_template("sheet.html", buyer=g.buyer, items=items,
                           facets=store.facets(brand=f["brand"], category=f["category"], companies=companies),
                           brand=f["brand"], category=f["category"], subcategory=f["subcategory"], q=f["q"] or "",
                           sort=sort, page=page, pages=pages, total=total, filtered=filtered, page_args=args,
                           draft_count=len(draft))


@bp.get("/item/<sku>")
@access_required
def item(sku: str):
    store = _ctx().store
    p = store.product(sku, companies=g.buyer["companies"])
    if not p:
        abort(404)
    d = store.draft(g.buyer["key"]).get(sku) or {}
    return render_template("item.html", buyer=g.buyer, p=p, draft_qty=d.get("qty") or "",
                           draft_price=("%.2f" % d["price"]) if d.get("price") else "",
                           draft_count=_draft_count(store, g.buyer))


@bp.get("/img/<sku>")
@access_required
def image(sku: str):
    """Cached product image, gated like everything else. Fetches on first miss."""
    from . import images
    store = _ctx().store
    p = store.product(sku)
    if not p or not p.get("image_url"):
        abort(404)
    row = images.ensure(store, sku, p["image_url"])
    if not row or not row.get("data"):
        abort(404)
    resp = Response(bytes(row["data"]), mimetype=row.get("content_type") or "image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


def _snap_qty(qty: int, case_pack: int, available: int) -> int:
    """Whole case packs only, never above what the feed says is available."""
    cp = max(int(case_pack or 1), 1)
    q = max(int(qty or 0), 0)
    q = (q // cp) * cp
    return min(q, (int(available) // cp) * cp)


def _num(raw: Any, cast=float):
    try:
        return cast(float(str(raw).replace("$", "").replace(",", "").strip() or 0))
    except (TypeError, ValueError):
        return cast(0)


@bp.post("/offer/set")
@access_required
def offer_set():
    """Save every qty[SKU] / price[SKU] pair on the submitted form into the
    buyer's draft. A blank or zero pair removes the line. Quantities snap to
    whole case packs and cap at what is available."""
    store = _ctx().store
    draft = store.draft(g.buyer["key"])
    skus = set()
    for k in request.form:
        m = re.match(r"^(?:qty|price)\[(.+)\]$", k)
        if m:
            skus.add(m.group(1))
    by = store.products_by_skus(skus)
    allowed = set(g.buyer["companies"])
    for sku in skus:
        p = by.get(sku)
        if not p or (p["company"] and p["company"] not in allowed):
            draft.pop(sku, None)
            continue
        qty = _snap_qty(_num(request.form.get(f"qty[{sku}]"), int), p["case_pack"], p["qty_available"])
        price = round(_num(request.form.get(f"price[{sku}]")), 2)
        if qty > 0 and price > 0:
            draft[sku] = {"qty": qty, "price": price}
        else:
            draft.pop(sku, None)
    store.set_draft(g.buyer["key"], draft)
    flash(f"{len(draft)} line{'s' if len(draft) != 1 else ''} on your offer.")
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("shop.offer"))


@bp.post("/offer/clear")
@access_required
def offer_clear():
    _ctx().store.set_draft(g.buyer["key"], {})
    flash("Offer cleared.")
    return redirect(url_for("shop.home"))


def _offer_lines(store, buyer) -> tuple[list[dict[str, Any]], float]:
    draft = store.draft(buyer["key"])
    by = store.products_by_skus(draft.keys())
    allowed = set(buyer["companies"])
    lines, total = [], 0.0
    for sku in sorted(draft):
        p = by.get(sku)
        if not p or (p["company"] and p["company"] not in allowed):
            continue
        q = _snap_qty(draft[sku]["qty"], p["case_pack"], p["qty_available"])
        price = round(float(draft[sku]["price"]), 2)
        if q <= 0 or price <= 0:
            continue
        ext = round(q * price, 2)
        total += ext
        whsl = float(p["wholesale"])
        lines.append({"sku": sku, "description": p["description"], "brand": p["brand"], "category": p["category"],
                      "subcategory": p["subcategory"], "company": p["company"], "case_pack": p["case_pack"],
                      "master_pack": p["master_pack"], "inner_pack": p["inner_pack"], "qty_available": p["qty_available"],
                      "wholesale": whsl, "qty": q, "offer_price": price, "extended": ext,
                      "pct_of_wholesale": round(price / whsl, 4) if whsl > 0 else None})
    return lines, round(total, 2)


def _csv(lines: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
    w.writeheader()
    for l in lines:
        w.writerow(l)
    return buf.getvalue()


def _prefill(buyer) -> dict[str, str]:
    name = buyer["name"] if buyer["name"] != "Guest" else ""
    return {"company": name, "contact": buyer.get("contact") or "", "email": buyer.get("email") or "", "phone": "", "notes": ""}


@bp.get("/offer")
@access_required
def offer():
    lines, total = _offer_lines(_ctx().store, g.buyer)
    return render_template("offer.html", buyer=g.buyer, lines=lines, total=total, draft_count=len(lines),
                           wholesale_total=round(sum(l["wholesale"] * l["qty"] for l in lines), 2), form=_prefill(g.buyer))


@bp.get("/offer.csv")
@access_required
def offer_csv():
    lines, _total = _offer_lines(_ctx().store, g.buyer)
    return Response(_csv(lines), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=gerson-closeout-offer.csv"})


def _offer_text(payload: dict[str, Any], ref: int) -> str:
    rows = [f"{'SKU':<16}{'Qty':>7}{'Whsl':>10}{'Offer':>10}{'% whsl':>8}{'Ext':>12}  Description"]
    for l in payload["lines"]:
        pct = f"{l['pct_of_wholesale'] * 100:.0f}%" if l.get("pct_of_wholesale") else ""
        rows.append(f"{l['sku']:<16}{l['qty']:>7}{l['wholesale']:>10.2f}{l['offer_price']:>10.2f}{pct:>8}"
                    f"{l['extended']:>12,.2f}  {l['description'][:48]}")
    who = payload["company"] + (f" / {payload['contact']}" if payload.get("contact") else "")
    totals = f"Lines: {payload['line_count']}   Units: {payload['units']:,}   Offer total: ${payload['total']:,.2f}"
    if payload.get("pct_of_wholesale"):
        totals += f"   ({payload['pct_of_wholesale'] * 100:.0f}% of ${payload['wholesale_total']:,.2f} wholesale)"
    head = [f"Closeout offer OF-{ref} from {who}",
            f"Email: {payload['email']}" + (f"   Phone: {payload['phone']}" if payload.get("phone") else ""), totals]
    if payload.get("notes"):
        head.append(f"Notes: {payload['notes']}")
    buyer = payload.get("buyer") or {}
    if buyer.get("kind") == "invite":
        head.append(f"Invite: {buyer.get('label') or ''}")
    elif buyer.get("customer_id"):
        head.append(f"NetSuite customer: {buyer['customer_id']}")
    return ("\n".join(head) + "\n\n" + "\n".join(rows)
            + "\n\nThe same lines are attached as CSV. Reply to the buyer to accept or counter.\n")


@bp.post("/offer/submit")
@access_required
def offer_submit():
    ctx = _ctx()
    store = ctx.store
    lines, total = _offer_lines(store, g.buyer)
    f = request.form
    form = {"company": (f.get("company") or "").strip()[:200], "contact": (f.get("contact") or "").strip()[:200],
            "email": (f.get("email") or "").strip().lower()[:200], "phone": (f.get("phone") or "").strip()[:60],
            "notes": (f.get("notes") or "").strip()[:2000]}
    if not lines:
        flash("Add at least one line with a quantity and an offered price first.")
        return redirect(url_for("shop.home"))
    if not form["company"] or not _EMAIL.match(form["email"]):
        flash("Your company name and a valid email are required so we can reply.")
        return render_template("offer.html", buyer=g.buyer, lines=lines, total=total, draft_count=len(lines),
                               wholesale_total=round(sum(l["wholesale"] * l["qty"] for l in lines), 2), form=form), 400
    whsl_total = round(sum(l["wholesale"] * l["qty"] for l in lines), 2)
    payload = {
        "buyer": {"kind": g.buyer["kind"], "key": g.buyer["key"], "label": g.buyer["name"],
                  "customer_id": g.buyer.get("customer_id"), "invite_token": g.buyer.get("token") or ""},
        "customer_id": g.buyer.get("customer_id"),
        **form,
        "lines": lines, "line_count": len(lines), "units": int(sum(l["qty"] for l in lines)),
        "total": total, "wholesale_total": whsl_total,
        "pct_of_wholesale": round(total / whsl_total, 4) if whsl_total > 0 else None,
    }
    ref = store.enqueue("offer", payload, customer_id=g.buyer.get("customer_id"), buyer_key=g.buyer["key"])
    text = _offer_text(payload, ref)
    attachment = (f"offer-OF-{ref}.csv", _csv(lines).encode("utf-8"), "text/csv")
    subject = (f"Closeout offer OF-{ref}: {form['company']} — {len(lines)} line{'s' if len(lines) != 1 else ''}, "
               f"${total:,.2f}")
    notified = [to for to in ctx.cfg.offer_notify_list
                if mail.send(ctx.cfg, to=to, subject=subject, body=text, attachments=[attachment])]
    if not ctx.cfg.offer_notify_list:
        current_app.logger.error("OFFER_NOTIFY_EMAILS is not set: offer OF-%s only reached the outbox", ref)
    mail.send(ctx.cfg, to=form["email"], subject=f"We received your offer (OF-{ref})",
              body=(f"Thank you. Your offer OF-{ref} ({len(lines)} lines, ${total:,.2f}) reached the Gerson closeout team; "
                    f"we will reply to this address with an acceptance or a counter.\n\n{text}"), attachments=[attachment])
    store.set_draft(g.buyer["key"], {})
    return render_template("offer_sent.html", buyer=g.buyer, ref=ref, payload=payload, draft_count=0, notified=bool(notified))


@bp.get("/offers")
@access_required
def offers():
    rows = _ctx().store.outbox_for(g.buyer["key"])
    return render_template("offers.html", buyer=g.buyer, rows=rows, draft_count=_draft_count(_ctx().store, g.buyer))
