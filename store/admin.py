"""Buyer admin portal (JJ, 2026-09-02): invite buyers by email, approve the ones
who apply on their own, suspend anyone who should no longer see the sheet.

An invitation carries the approval with it (JJ, 2026-09-03): whoever an admin
invites is approved the moment they finish signing up, and lands on the sheet.
The approval queue below is for the open ``/apply`` form only.

Admins are the addresses in ``STORE_ADMIN_EMAILS``; they sign in with the same
one-time email link as buyers.  Nothing here touches NetSuite or AOI: who may
see the sheet is decided on the store, by a person, and an approved buyer is
just a company, a contact and an email.
"""
from __future__ import annotations

import functools

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

from . import mail

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _ctx():
    return current_app.config["STORE"]


def current_admin() -> str:
    email = str(session.get("admin_email") or "").strip().lower()
    return email if email and email in _ctx().cfg.admin_list else ""


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not current_admin():
            abort(404)                      # the portal does not announce itself
        return fn(*a, **kw)
    return wrapper


@bp.get("/")
@admin_required
def home():
    store = _ctx().store
    counts = store.buyer_offer_counts()
    buyers = [dict(b, offers=counts.get(f"buyer:{b['id']}", 0)) for b in store.list_buyers() if b["status"] in ("approved", "suspended")]
    return render_template("admin.html", admin_email=current_admin(), pending=store.list_buyers(status="pending"),
                           buyers=buyers, invites=store.list_signup_invites())


def send_invite_email(ctx, invite: dict) -> bool:
    link = f"{ctx.cfg.base_url}{url_for('shop.join', token=invite['token'])}"
    body = (f"You are invited to Gerson's closeout offer sheet: every closeout item at original wholesale, "
            f"and you make the offer.\n\n"
            + (f"{invite['note']}\n\n" if invite.get("note") else "")
            + f"Sign up here — it takes a minute and you are on the sheet as soon as you finish:\n{link}\n\n"
            f"If this did not reach the right person, forward it — the link is for one sign-up.\n")
    return bool(mail.send(ctx.cfg, to=invite["email"], subject="Invitation: Gerson closeout offer sheet", body=body))


@bp.post("/invite")
@admin_required
def invite():
    ctx = _ctx()
    email = (request.form.get("email") or "").strip().lower()
    if "@" not in email:
        flash("A valid email is required.")
        return redirect(url_for("admin.home"))
    inv = ctx.store.create_signup_invite(email, company=(request.form.get("company") or "").strip(),
                                         note=(request.form.get("note") or "").strip(), created_by=current_admin())
    flash(f"Invitation sent to {email}." if send_invite_email(ctx, inv) else f"Invitation created for {email}, but the email could not be sent.")
    return redirect(url_for("admin.home"))


@bp.post("/invites/<token>/revoke")
@admin_required
def revoke_invite(token: str):
    _ctx().store.revoke_signup_invite(token)
    flash("Invitation withdrawn.")
    return redirect(url_for("admin.home"))


@bp.post("/invites/<token>/resend")
@admin_required
def resend_invite(token: str):
    ctx = _ctx()
    inv = ctx.store.signup_invite(token)
    if not inv:
        flash("That invitation is no longer open.")
    else:
        flash("Invitation re-sent." if send_invite_email(ctx, inv) else "The email could not be sent.")
    return redirect(url_for("admin.home"))


APPROVAL_LINK_MINUTES = 60 * 24


@bp.post("/buyers/<int:buyer_id>/status")
@admin_required
def set_status(buyer_id: int):
    ctx = _ctx()
    status = (request.form.get("status") or "").strip()
    if status not in ("approved", "suspended", "declined"):
        abort(400)
    b = ctx.store.buyer(buyer_id)
    if not b:
        abort(404)
    ctx.store.set_buyer_status(buyer_id, status, by=current_admin())
    if status == "approved":
        token = ctx.store.create_login_token(b["email"], "", APPROVAL_LINK_MINUTES, subject=f"buyer:{buyer_id}")
        link = f"{ctx.cfg.base_url}{url_for('shop.login_token', token=token)}"
        mail.send(ctx.cfg, to=b["email"], subject="Approved: Gerson closeout offer sheet",
                  body=(f"Your account for {b['company']} is approved.\n\nSign in now (this link works once, for 24 hours):\n{link}\n\n"
                        f"After that, go to {ctx.cfg.base_url}/login and enter {b['email']} for a fresh one-time link.\n"))
        flash(f"{b['company']} approved; sign-in link sent to {b['email']}.")
    elif status == "declined":
        flash(f"{b['company']} declined.")
    else:
        flash(f"{b['company']} suspended; they are signed out on their next request.")
    return redirect(url_for("admin.home"))
