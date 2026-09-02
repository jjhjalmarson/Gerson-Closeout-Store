"""Customer-facing routes: login (magic link against the allowlist), catalog,
cart, checkout into the outbox, and a new-account application.

No price is ever taken from the client: every line is re-priced from the
products table at checkout, and quantities are forced to whole case packs.
"""
from __future__ import annotations

import functools
import re
from typing import Any

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for)

from . import mail

bp = Blueprint("shop", __name__)
PAGE_SIZE = 60
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _ctx():
    return current_app.config["STORE"]


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        cid = session.get("customer_id")
        cust = _ctx().store.customer(cid) if cid else None
        if not cust:
            session.clear()
            return redirect(url_for("shop.login", next=request.path))
        g.customer = cust
        return fn(*a, **kw)
    return wrapper


@bp.get("/login")
def login():
    if session.get("customer_id"):
        return redirect(url_for("shop.home"))
    return render_template("login.html")


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
                      body=f"Sign in to Gerson Closeouts:\n\n{link}\n\nThis link works once and expires in "
                           f"{ctx.cfg.login_token_minutes} minutes. If you did not request it, ignore this email.")
    # Same response whether or not the email is on the allowlist: no enumeration.
    return render_template("login.html", sent=True, email=email)


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
    return render_template("apply.html")


@bp.post("/apply")
def apply_post():
    f = request.form
    payload = {k: (f.get(k) or "").strip() for k in ("company", "contact", "email", "phone", "resale_number", "website",
                                                     "address", "city", "state", "zip", "notes")}
    if not payload["company"] or not _EMAIL.match(payload["email"]):
        flash("Company name and a valid email are required.")
        return render_template("apply.html", form=payload), 400
    _ctx().store.enqueue("application", payload)
    return render_template("apply.html", submitted=True)


@bp.get("/")
@login_required
def home():
    store = _ctx().store
    a = request.args
    f = {"brand": a.get("brand") or None, "category": a.get("category") or None,
         "subcategory": a.get("subcategory") or None, "discount": a.get("discount") or None, "q": (a.get("q") or "").strip() or None}
    sort = a.get("sort") or "default"
    if sort not in store.SORTS:
        sort = "default"
    try:
        page = max(int(a.get("page") or 1), 1)
    except ValueError:
        page = 1
    filtered = any(f.values())
    curated = store.curated_for(g.customer["customer_id"]) if not filtered and page == 1 else []
    total = store.count_products(**f)
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)
    items = store.list_products(**f, sort=sort, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    cart = store.cart(g.customer["customer_id"])
    args = {k: v for k, v in {**f, "sort": sort if sort != "default" else None}.items() if v}
    return render_template("catalog.html", customer=g.customer, curated=curated, items=items,
                           facets=store.facets(brand=f["brand"], category=f["category"]),
                           brand=f["brand"], category=f["category"], subcategory=f["subcategory"], discount=f["discount"],
                           q=f["q"] or "", sort=sort, page=page, pages=pages, total=total, filtered=filtered,
                           page_args=args, cart_count=sum(cart.values()))


@bp.get("/item/<sku>")
@login_required
def item(sku: str):
    p = _ctx().store.product(sku)
    if not p:
        abort(404)
    cart = _ctx().store.cart(g.customer["customer_id"])
    return render_template("item.html", customer=g.customer, p=p, in_cart=cart.get(sku, 0), cart_count=sum(cart.values()))


@bp.get("/img/<sku>")
@login_required
def image(sku: str):
    """Cached product image, gated like everything else. Fetches on first miss."""
    from flask import Response
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


@bp.post("/cart/add")
@login_required
def cart_add():
    store = _ctx().store
    sku = (request.form.get("sku") or "").strip()
    p = store.product(sku)
    if not p:
        abort(404)
    try:
        qty = int(request.form.get("qty") or 0)
    except ValueError:
        qty = 0
    cart = store.cart(g.customer["customer_id"])
    new_qty = _snap_qty(cart.get(sku, 0) + qty, p["order_unit"], p["qty_available"])
    if new_qty > 0:
        cart[sku] = new_qty
    else:
        cart.pop(sku, None)
    store.set_cart(g.customer["customer_id"], cart)
    flash(f"{p['sku']}: {new_qty} in cart" if new_qty else f"{p['sku']} removed")
    return redirect(request.form.get("next") or url_for("shop.cart"))


@bp.post("/cart/set")
@login_required
def cart_set():
    store = _ctx().store
    cart = store.cart(g.customer["customer_id"])
    by = store.products_by_skus(cart.keys())
    for sku in list(cart):
        raw = request.form.get(f"qty[{sku}]")
        if raw is None:
            continue
        try:
            qty = int(raw or 0)
        except ValueError:
            qty = 0
        p = by.get(sku)
        q = _snap_qty(qty, p["order_unit"], p["qty_available"]) if p else 0
        if q > 0:
            cart[sku] = q
        else:
            cart.pop(sku, None)
    store.set_cart(g.customer["customer_id"], cart)
    return redirect(url_for("shop.cart"))


def _priced_cart(store, customer_id: str) -> tuple[list[dict[str, Any]], float]:
    cart = store.cart(customer_id)
    by = store.products_by_skus(cart.keys())
    lines, total = [], 0.0
    for sku, qty in cart.items():
        p = by.get(sku)
        if not p:
            continue
        q = _snap_qty(qty, p["order_unit"], p["qty_available"])
        if q <= 0:
            continue
        ext = round(q * float(p["closeout_price"]), 2)
        total += ext
        lines.append({"sku": sku, "description": p["description"], "qty": q, "case_pack": p["case_pack"],
                      "order_unit": p["order_unit"], "unit_label": p["unit_label"],
                      "unit_price": float(p["closeout_price"]), "wholesale": float(p["wholesale"]), "extended": ext,
                      "image_url": p["image_url"], "ship_by": p["ship_by"]})
    return lines, round(total, 2)


@bp.get("/cart")
@login_required
def cart():
    lines, total = _priced_cart(_ctx().store, g.customer["customer_id"])
    min_total = float(_ctx().cfg.min_order_total or 0)
    return render_template("cart.html", customer=g.customer, lines=lines, total=total, min_total=min_total,
                           short=round(max(min_total - total, 0.0), 2), cart_count=sum(l["qty"] for l in lines))


@bp.post("/checkout")
@login_required
def checkout():
    store = _ctx().store
    lines, total = _priced_cart(store, g.customer["customer_id"])
    if not lines:
        flash("Your cart is empty.")
        return redirect(url_for("shop.cart"))
    min_total = float(_ctx().cfg.min_order_total or 0)
    if total < min_total:
        flash(f"Orders must total at least ${min_total:,.0f}. Add ${min_total - total:,.2f} more.")
        return redirect(url_for("shop.cart"))
    order = {
        "customer_id": g.customer["customer_id"],
        "company_name": g.customer["company_name"],
        "buyer_class": g.customer["buyer_class"],
        "po_number": (request.form.get("po_number") or "").strip()[:40],
        "notes": (request.form.get("notes") or "").strip()[:1000],
        "requested_ship_date": "",        # closeouts ship at once; the form no longer asks
        "lines": [{"sku": l["sku"], "qty": l["qty"], "unit_price": l["unit_price"]} for l in lines],
        "total": total,
    }
    oid = store.enqueue("order", order, customer_id=g.customer["customer_id"])
    store.set_cart(g.customer["customer_id"], {})
    return render_template("confirm.html", customer=g.customer, order=order, ref=oid, cart_count=0)


@bp.get("/orders")
@login_required
def orders():
    rows = _ctx().store.outbox_for_customer(g.customer["customer_id"])
    cart = _ctx().store.cart(g.customer["customer_id"])
    return render_template("orders.html", customer=g.customer, rows=rows, cart_count=sum(cart.values()))
