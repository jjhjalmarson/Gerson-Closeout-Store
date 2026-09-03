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
import secrets
from typing import Any, Mapping

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, session,
                   url_for)

from . import mail
from .db import ALL_COMPANIES

bp = Blueprint("shop", __name__)
PAGE_SIZE = 60
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CSV_COLUMNS = ("sku", "description", "brand", "category", "subcategory", "case_pack", "master_pack", "inner_pack",
               "qty_available", "wholesale", "qty", "offer_price", "extended")


def _ctx():
    return current_app.config["STORE"]


def _resolve_buyer() -> dict[str, Any] | None:
    """Who is looking: a buyer approved on the store, an invite-link holder, or a
    signed-in AOI-fed account.  Approved buyers see every company's SKUs."""
    store = _ctx().store
    bid = session.get("buyer_id")
    if bid:
        b = store.buyer(int(bid))
        if b and b["status"] == "approved":
            return {"kind": "buyer", "key": f"buyer:{b['id']}", "token": "", "name": b["company"], "contact": b.get("contact") or "",
                    "email": b["email"], "companies": list(ALL_COMPANIES), "customer_id": None, "rep_name": "",
                    "buyer_id": b["id"], "buyer_class": b.get("buyer_class") or "regional"}
        return None
    tok = session.get("invite")
    if tok:
        inv = store.invite(tok)
        if inv:
            return {"kind": "invite", "key": f"inv:{tok}", "token": tok, "name": inv["label"] or "Guest",
                    "contact": inv.get("contact") or "", "email": inv.get("email") or "",
                    "companies": inv["companies"], "customer_id": None, "rep_name": "",
                    "buyer_class": inv.get("buyer_class") or "regional"}
        return None
    cid = session.get("customer_id")
    if cid:
        cust = store.customer(cid)
        if cust:
            return {"kind": "customer", "key": f"cust:{cust['customer_id']}", "token": "", "name": cust["company_name"],
                    "contact": "", "email": "", "companies": cust["companies"], "customer_id": cust["customer_id"],
                    "rep_name": cust.get("rep_name") or "", "accounts": cust["accounts"],
                    "buyer_class": cust.get("buyer_class") or "regional"}
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


def _login_subject(ctx, email: str) -> str:
    """Who a sign-in link for this email signs in, or '' when nobody may."""
    if email in ctx.cfg.admin_list:
        return f"admin:{email}"
    b = ctx.store.buyer_for_email(email)
    if b and b["status"] == "approved":
        return f"buyer:{b['id']}"
    cust = ctx.store.customer_for_email(email)
    if cust:
        return f"cust:{cust['customer_id']}"
    return ""


@bp.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    ctx = _ctx()
    if _EMAIL.match(email):
        subject = _login_subject(ctx, email)
        if subject:
            token = ctx.store.create_login_token(email, "", ctx.cfg.login_token_minutes, subject=subject)
            link = f"{ctx.cfg.base_url}{url_for('shop.login_token', token=token)}"
            mail.send(ctx.cfg, to=email, subject="Your Gerson Closeouts sign-in link",
                      body=f"Sign in to the Gerson closeout offer sheet:\n\n{link}\n\nThis link works once and expires in "
                           f"{ctx.cfg.login_token_minutes} minutes. If you did not request it, ignore this email.")
    # Same response whether or not the email may sign in: no enumeration.
    return render_template("login.html", sent=True, email=email, website_url=ctx.cfg.website_url)


@bp.get("/login/<token>")
def login_token(token: str):
    ctx = _ctx()
    got = ctx.store.redeem_login_token(token)
    if not got:
        flash("That sign-in link has expired or was already used. Request a new one.")
        return redirect(url_for("shop.login"))
    kind, _, ident = got["subject"].partition(":")
    session.clear()
    session.permanent = True
    if kind == "admin" and ident in ctx.cfg.admin_list:
        session["admin_email"] = ident
        return redirect(url_for("admin.home"))
    if kind == "buyer":
        b = ctx.store.buyer(int(ident))
        if not b or b["status"] != "approved":
            flash("This account is not active. Contact your Gerson representative.")
            return redirect(url_for("shop.login"))
        session["buyer_id"] = b["id"]
    elif kind == "cust":
        session["customer_id"] = ident
    else:
        return redirect(url_for("shop.login"))
    buyer = _resolve_buyer()
    if buyer:
        ev("signed_in", buyer=buyer)
    return redirect(url_for("shop.home"))


# --- sign-up (from an admin's invitation, or the open application form) ---------

def _notify_admins_signup(ctx, b: Mapping[str, Any], *, invited: bool) -> None:
    """Tell the admins.  An invited sign-up is an FYI — it is already approved —
    and an open application is a queue item."""
    subject = (f"Closeout sheet: {b['company']} signed up from your invitation" if invited
               else f"Closeout sheet sign-up to approve: {b['company']}")
    tail = (("\nThey were invited, so they are approved and on the sheet already. Suspend them here if that is wrong: "
             if invited else "\nApprove or decline: ")
            + f"{ctx.cfg.base_url}{url_for('admin.home')}\n")
    for to in ctx.cfg.admin_list:
        mail.send(ctx.cfg, to=to, subject=subject,
                  body=(f"{b['company']} / {b['contact']} <{b['email']}>{(' · ' + b['phone']) if b.get('phone') else ''}\n"
                        + (f"Notes: {b['notes']}\n" if b.get("notes") else "")
                        + tail))


def _signup(ctx, form, invite: Mapping[str, Any] | None):
    """Create (or refresh) the buyer behind a sign-up.

    An invitation *is* the approval (JJ, 2026-09-03): an admin chose the address,
    and opening the link proves the buyer holds it, so the account is approved on
    the spot and the sign-up ends on the sheet instead of in a queue.  The open
    ``/apply`` form still waits for a human, and a suspended buyer is never
    reactivated by an invitation.
    """
    payload = {k: (form.get(k) or "").strip() for k in ("company", "contact", "email", "phone", "notes")}
    if invite:
        payload["email"] = invite["email"]          # the invitation fixes the address
    if not payload["company"] or not payload["contact"] or not _EMAIL.match(payload["email"]):
        return None, payload
    b = ctx.store.create_buyer(company=payload["company"], contact=payload["contact"], email=payload["email"],
                               phone=payload["phone"], notes=payload["notes"], invite_token=(invite or {}).get("token"),
                               approve=bool(invite), approved_by=(invite or {}).get("created_by") or "invitation")
    _notify_admins_signup(ctx, b, invited=bool(invite))
    if b["status"] == "approved":
        mail.send(ctx.cfg, to=b["email"], subject="You are in: the Gerson closeout offer sheet",
                  body=(f"Your account for {b['company']} is active — nothing to wait for.\n\n"
                        f"Open the offer sheet: {ctx.cfg.base_url}/\n\n"
                        f"To sign in later go to {ctx.cfg.base_url}/login and enter {b['email']}: we email a one-time "
                        f"link each time, so there is no password to keep.\n"))
    else:
        mail.send(ctx.cfg, to=b["email"], subject="Received: your request for the Gerson closeout offer sheet",
                  body=f"Thank you. The Gerson team reviews requests by hand; you will get a sign-in link by email once {b['company']} is approved.\n")
    return b, payload


@bp.get("/join/<token>")
def join(token: str):
    inv = _ctx().store.signup_invite(token)
    if not inv:
        return render_template("join.html", invalid=True), 404
    return render_template("join.html", form={"email": inv["email"], "company": inv["company"]}, lock_email=True)


@bp.post("/join/<token>")
def join_post(token: str):
    ctx = _ctx()
    inv = ctx.store.signup_invite(token)
    if not inv:
        return render_template("join.html", invalid=True), 404
    b, form = _signup(ctx, request.form, inv)
    if not b:
        flash("Company, your name and a valid email are required.")
        return render_template("join.html", form=form, lock_email=True), 400
    if b["status"] != "approved":        # only a suspended account can land here
        return render_template("join.html", submitted=True, email=b["email"])
    session.clear()                      # opening the invitation proved they hold the address
    session.permanent = True
    session["buyer_id"] = b["id"]
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
    """Open application (no invitation): becomes a pending buyer for the admin
    portal, and is still recorded in the outbox so AOI sees it."""
    ctx = _ctx()
    f = request.form
    payload = {k: (f.get(k) or "").strip() for k in ("company", "contact", "email", "phone", "resale_number", "website",
                                                     "address", "city", "state", "zip", "notes")}
    if not payload["company"] or not _EMAIL.match(payload["email"]):
        flash("Company name and a valid email are required.")
        return render_template("apply.html", form=payload, website_url=ctx.cfg.website_url), 400
    notes = " · ".join(x for x in (payload["notes"], payload["resale_number"] and f"resale {payload['resale_number']}",
                                   payload["website"], ", ".join(x for x in (payload["address"], payload["city"], payload["state"], payload["zip"]) if x)) if x)
    _signup(ctx, {"company": payload["company"], "contact": payload["contact"] or payload["company"], "email": payload["email"],
                  "phone": payload["phone"], "notes": notes}, None)
    ctx.store.enqueue("application", payload)
    return render_template("apply.html", submitted=True, website_url=ctx.cfg.website_url)


# --- the sheet ------------------------------------------------------------------

def _session_id() -> str:
    """A stable id for this browser session, so a visit can be stitched back
    together. Random, not derived from anything about the person."""
    sid = session.get("sid")
    if not sid:
        sid = secrets.token_urlsafe(9)
        session["sid"] = sid
    return sid


def ev(kind: str, *, sku: str = "", buyer: Mapping[str, Any] | None = None, **payload: Any) -> None:
    """Record what a buyer did (JJ, 2026-09-03).

    Server-side on purpose: no beacons, no third-party script, and it works with
    JavaScript off.  What it cannot see is dwell time and image zoom; what it
    can see is every SKU opened, every search that found nothing, and every
    price typed into a box and then abandoned -- which is the number that never
    reaches an offer and is worth the most.
    """
    b = buyer if buyer is not None else getattr(g, "buyer", None)
    if not b:
        return
    _ctx().store.record_event(kind, buyer_key=b.get("key", ""), buyer_label=b.get("name", ""),
                              buyer_class=b.get("buyer_class", ""), session_id=_session_id(), sku=sku,
                              payload={k: v for k, v in payload.items() if v not in (None, "")})


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
    # A search that returns nothing is the most useful row in the table: it says
    # what they came for that we do not have.
    ev("sheet_viewed", **f, sort=(sort if sort != "default" else ""), page=(page if page > 1 else None),
       results=total, no_results=(total == 0 and bool(f["q"])) or None)
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
    ev("item_viewed", sku=p["sku"], brand=p.get("brand"), category=p.get("category"),
       wholesale=p.get("wholesale"), qty_available=p.get("qty_available"))
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


def _apply_line(draft: dict, sku: str, product: dict | None, qty_raw: Any, price_raw: Any, allowed: set) -> dict | None:
    """Put one typed pair on the draft (snapped to whole packs, capped at what is
    available) or take the line off when either half is blank / zero / not for
    this buyer. Returns the saved line or None."""
    if not product or (product["company"] and product["company"] not in allowed):
        draft.pop(sku, None)
        return None
    qty = _snap_qty(_num(qty_raw, int), product["case_pack"], product["qty_available"])
    price = round(_num(price_raw), 2)
    was = draft.get(sku)
    if qty > 0 and price > 0:
        draft[sku] = {"qty": qty, "price": price}
        # Append-only: the draft row is overwritten, the history is not. This is
        # where an abandoned price survives, and where a buyer walking their
        # number down while they think shows up.
        if not was or was.get("qty") != qty or was.get("price") != price:
            ev("line_priced", sku=sku, qty=qty, price=price, wholesale=product.get("wholesale"),
               pct_of_wholesale=(round(price / product["wholesale"], 4) if product.get("wholesale") else None),
               qty_available=product.get("qty_available"),
               prev_qty=(was or {}).get("qty"), prev_price=(was or {}).get("price"))
        return draft[sku]
    if was:
        ev("line_removed", sku=sku, prev_qty=was.get("qty"), prev_price=was.get("price"))
    draft.pop(sku, None)
    return None


@bp.post("/offer/line")
@access_required
def offer_line():
    """Autosave: ``{"sku", "qty", "price"}`` as JSON -> the saved line (or null
    when the pair removes it) and the draft's line count."""
    body = request.get_json(silent=True) or {}
    sku = str(body.get("sku") or "").strip()
    if not sku:
        return {"error": "sku required"}, 400
    store = _ctx().store
    draft = store.draft(g.buyer["key"])
    line = _apply_line(draft, sku, store.products_by_skus([sku]).get(sku), body.get("qty"), body.get("price"),
                       set(g.buyer["companies"]))
    store.set_draft(g.buyer["key"], draft)
    return {"saved": True, "sku": sku, "line": line, "count": len(draft)}


@bp.post("/offer/set")
@access_required
def offer_set():
    """No-JavaScript fallback: save every qty[SKU] / price[SKU] pair on the
    submitted form into the buyer's draft (same rules as the autosave)."""
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
        _apply_line(draft, sku, by.get(sku), request.form.get(f"qty[{sku}]"), request.form.get(f"price[{sku}]"), allowed)
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
    ev("offer_reviewed", lines=len(lines), total=total,
       wholesale_total=round(sum(l["wholesale"] * l["qty"] for l in lines), 2))
    return render_template("offer.html", buyer=g.buyer, lines=lines, total=total, draft_count=len(lines),
                           wholesale_total=round(sum(l["wholesale"] * l["qty"] for l in lines), 2), form=_prefill(g.buyer))


@bp.get("/offer.csv")
@access_required
def offer_csv():
    lines, _total = _offer_lines(_ctx().store, g.buyer)
    ev("offer_downloaded", lines=len(lines))
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
                  "customer_id": g.buyer.get("customer_id"), "invite_token": g.buyer.get("token") or "",
                  # Which lane AOI prices this against. Set by an admin here, sent
                  # with the offer; the buyer never sees or chooses it.
                  "buyer_class": g.buyer.get("buyer_class") or "regional"},
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
    ev("offer_submitted", ref=f"OF-{ref}", lines=len(lines), units=payload["units"], total=total,
       wholesale_total=whsl_total, pct_of_wholesale=payload["pct_of_wholesale"])
    store.set_draft(g.buyer["key"], {})
    return render_template("offer_sent.html", buyer=g.buyer, ref=ref, payload=payload, draft_count=0, notified=bool(notified))


@bp.get("/offers")
@access_required
def offers():
    store = _ctx().store
    rows = store.outbox_for(g.buyer["key"])
    for r in rows:
        if r["kind"] == "offer":
            last = store.latest_round_for_offer(r["id"])
            r["round"] = last
    return render_template("offers.html", buyer=g.buyer, rows=rows, draft_count=_draft_count(store, g.buyer))


# --- negotiation: the buyer's side of a round (token is the credential) ------------

ROUND_TITLES = {"counter": "Gerson countered your offer", "accept": "Your offer is accepted",
                "decline": "Your offer was declined", "recorded": "Agreed terms recorded"}


def _offer_lines_from_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"sku": str(l.get("sku") or ""), "qty": int(l.get("qty") or 0), "price": float(l.get("offer_price") or 0.0),
             "description": str(l.get("description") or ""), "wholesale": float(l.get("wholesale") or 0.0)}
            for l in payload.get("lines") or []]


def _round_buyer(offer: Mapping[str, Any]) -> dict[str, Any]:
    """The account behind a tokenized round page: there is no session here, the
    link is the identity."""
    b = (offer.get("payload") or {}).get("buyer") or {}
    return {"key": b.get("key") or "", "name": b.get("label") or "", "buyer_class": b.get("buyer_class") or ""}


def _round_ctx(token: str):
    store = _ctx().store
    rnd = store.round(token)
    if not rnd:
        abort(404)
    offer = store.outbox_item(rnd["offer_id"])
    if not offer:
        abort(404)
    trail = store.rounds_for_offer(rnd["offer_id"])
    return store, rnd, offer, trail


def _round_text(offer: Mapping[str, Any], rnd: Mapping[str, Any], link: str) -> str:
    p = offer["payload"]
    ref = f"OF-{offer['id']}"
    head = [f"{ROUND_TITLES.get(rnd['kind'], 'Update on your offer')} {ref}."]
    if rnd.get("message"):
        head.append(f"Message from Gerson: {rnd['message']}")
    rows = []
    if rnd.get("lines"):
        theirs = {l["sku"]: l for l in _offer_lines_from_payload(p)}
        rows.append(f"{'SKU':<16}{'Qty':>7}{'Your offer':>12}{'Gerson':>10}{'Ext':>12}  Description")
        for l in rnd["lines"]:
            mine = theirs.get(l["sku"], {})
            rows.append(f"{l['sku']:<16}{l['qty']:>7}{(mine.get('price') or 0):>12.2f}{l['price']:>10.2f}{l['qty'] * l['price']:>12,.2f}  {l.get('description', '')[:44]}")
        rows.append(f"Total {rnd['total']:,.2f}")
    tail = {"counter": f"Review and answer here (accept, counter again, or decline):\n{link}",
            "accept": "Nothing further is needed from you: the Gerson team will enter the order and confirm.",
            "recorded": "These are the terms as agreed; the Gerson team will enter the order and confirm.",
            "decline": "You are welcome to submit a new offer from the sheet."}.get(rnd["kind"], link)
    return "\n".join(head) + ("\n\n" + "\n".join(rows) if rows else "") + "\n\n" + tail + "\n"


def notify_buyer_round(ctx, offer: Mapping[str, Any], rnd: Mapping[str, Any]) -> bool:
    """Email the buyer about a round pushed by AOI. Best effort."""
    to = (rnd.get("buyer_email") or offer["payload"].get("email") or "").strip()
    if not to:
        return False
    link = f"{ctx.cfg.base_url}{url_for('shop.round_view', token=rnd['token'])}"
    subject = f"{ROUND_TITLES.get(rnd['kind'], 'Update on your offer')} — OF-{offer['id']}"
    return bool(mail.send(ctx.cfg, to=to, subject=subject, body=_round_text(offer, rnd, link)))


@bp.get("/o/<token>")
def round_view(token: str):
    store, rnd, offer, trail = _round_ctx(token)
    if rnd["status"] == "open" and not rnd.get("opened_at"):
        store.mark_round_opened(token)                  # response-time signal for the negotiation memory
        rnd = store.round(token) or rnd
    theirs = _offer_lines_from_payload(offer["payload"])
    by_sku = {l["sku"]: l for l in theirs}
    lines = [{**l, "your_price": (by_sku.get(l["sku"]) or {}).get("price"), "your_qty": (by_sku.get(l["sku"]) or {}).get("qty")}
             for l in rnd["lines"]]
    ev("round_viewed", buyer=_round_buyer(offer), round_no=rnd["round_no"], round_kind=rnd["kind"],
       offer_ref=offer["id"], status=rnd["status"])
    return render_template("round.html", rnd=rnd, offer=offer, trail=trail, lines=lines, original=theirs,
                           original_total=round(sum(l["qty"] * l["price"] for l in theirs), 2),
                           open=(rnd["kind"] == "counter" and rnd["status"] == "open"), title=ROUND_TITLES.get(rnd["kind"], "Your offer"))


@bp.post("/o/<token>/respond")
def round_respond(token: str):
    store, rnd, offer, trail = _round_ctx(token)
    if not (rnd["kind"] == "counter" and rnd["status"] == "open"):
        flash("This round was already answered or the offer is closed.")
        return redirect(url_for("shop.round_view", token=token))
    action = (request.form.get("action") or "").strip().lower()
    if action not in ("accept", "counter", "decline"):
        abort(400)
    message = (request.form.get("message") or "").strip()[:2000]
    lines = []
    if action == "counter":
        for l in rnd["lines"]:
            qty = _num(request.form.get(f"qty[{l['sku']}]"), int)
            price = round(_num(request.form.get(f"price[{l['sku']}]")), 2)
            if qty > 0 and price > 0:
                lines.append({"sku": l["sku"], "qty": qty, "price": price, "description": l.get("description", ""),
                              "wholesale": l.get("wholesale", 0.0)})
        if not lines:
            flash("Enter a quantity and a price on at least one line to counter.")
            return redirect(url_for("shop.round_view", token=token))
        if all(any(o["sku"] == l["sku"] and o["qty"] == l["qty"] and abs(o["price"] - l["price"]) < 0.005 for o in rnd["lines"]) for l in lines) \
                and len(lines) == len(rnd["lines"]):
            action = "accept"                               # same terms typed back = acceptance
            lines = []
    response = {"offer_ref": offer["id"], "round_id": rnd["round_id"], "token": token, "action": action,
                "lines": lines, "message": message, "email": rnd.get("buyer_email") or offer["payload"].get("email") or "",
                "opened_at": rnd.get("opened_at")}
    if not store.respond_round(token, response):
        flash("This round was already answered.")
        return redirect(url_for("shop.round_view", token=token))
    store.enqueue("offer_response", response, customer_id=offer.get("customer_id"), buyer_key=offer.get("buyer_key"))
    ev("round_answered", buyer=_round_buyer(offer), action=action, round_no=rnd["round_no"], offer_ref=offer["id"],
       lines=len(lines) or None, total=(round(sum(l["qty"] * l["price"] for l in lines), 2) if lines else None),
       minutes_to_answer=None)
    return render_template("round_done.html", rnd=rnd, offer=offer, action=action, lines=lines, message=message)
