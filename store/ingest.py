"""AOI -> store: /ingest/<kind> and the /outbox pull + ack. All keyed by the
store's own key (``STORE_INGEST_KEY``). AOI is the only caller.

Defensive sanitization: the feed producer already strips sensitive fields, but
this side refuses any payload carrying one anyway. Two independent guards,
two repos, one policy.
"""
from __future__ import annotations

import hmac
from typing import Any

from flask import Blueprint, current_app, jsonify, request

bp = Blueprint("ingest", __name__)

KINDS = ("catalog", "customers", "curation", "invites")
FORBIDDEN_KEYS = frozenset({
    "avg_cost", "ats_cost", "econ_cost", "cost", "unit_cost", "haircut", "receipt_date", "date_source", "days",
    "age_months", "bucket", "aging_bucket", "adv_rate", "advance_rate", "capacity_now", "capacity_at_risk",
    "cliff_date", "cliff_remeasure", "floor_independent", "floor_regional", "floor_liquidator", "floor", "floors",
    "carry_per_month", "accrued_holding", "recovery_pct", "exp_recovery", "krebs_tranche", "tier", "state",
    "rationale", "confidence", "score", "on_hand", "ats_units",
})


def _keyed() -> bool:
    key = current_app.config["STORE"].cfg.store_ingest_key
    given = request.headers.get("X-API-Key", "")
    return bool(key) and hmac.compare_digest(given, key)


def find_forbidden(payload: Any, path: str = "$") -> str | None:
    if isinstance(payload, dict):
        for k, v in payload.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                return f"{path}.{k}"
            hit = find_forbidden(v, f"{path}.{k}")
            if hit:
                return hit
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            hit = find_forbidden(v, f"{path}[{i}]")
            if hit:
                return hit
    return None


@bp.post("/ingest/<kind>")
def ingest(kind: str):
    if not _keyed():
        return jsonify({"error": "not found"}), 404
    if kind not in KINDS:
        return jsonify({"error": f"kind must be one of {list(KINDS)}"}), 400
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        return jsonify({"error": "body must be {kind, items: [...]}"}), 400
    hit = find_forbidden(body)
    if hit:
        current_app.logger.error("ingest %s REFUSED: forbidden key at %s", kind, hit)
        return jsonify({"error": f"forbidden key at {hit}"}), 422
    if kind in ("customers", "invites") and not body["items"]:
        # A customers / invites push replaces the whole allowlist; an empty one
        # would log every buyer out or kill every invite link. Suspend or revoke
        # one at a time in AOI instead.
        current_app.logger.error("ingest %s REFUSED: empty feed would wipe the allowlist", kind)
        return jsonify({"error": f"empty {kind} feed refused: it would deactivate every account"}), 409
    store = current_app.config["STORE"].store
    fn = {"catalog": store.ingest_catalog, "customers": store.ingest_customers, "curation": store.ingest_curation,
          "invites": store.ingest_invites}[kind]
    n = fn(body["items"], as_of=body.get("as_of"), generated_at=body.get("generated_at"))
    if kind == "catalog" and not current_app.config.get("TESTING"):
        from . import images
        images.refresh_in_background(store)
    return jsonify({"accepted": True, "kind": kind, "count": n}), 202


ROUND_KINDS = ("counter", "accept", "decline", "recorded")


@bp.post("/rounds")
def rounds_push():
    """AOI pushes a negotiation round (counter / accept / decline / recorded).
    Price, quantity and message only — the same FORBIDDEN_KEYS guard applies.
    Stores it and emails the buyer: a counter gets a link to /o/<token>."""
    if not _keyed():
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not str(body.get("token") or "").strip() or not str(body.get("offer_ref") or "").isdigit():
        return jsonify({"error": "body must carry token and offer_ref"}), 400
    if body.get("kind") not in ROUND_KINDS:
        return jsonify({"error": f"kind must be one of {list(ROUND_KINDS)}"}), 400
    if not isinstance(body.get("lines", []), list):
        return jsonify({"error": "lines must be a list"}), 400
    hit = find_forbidden(body)
    if hit:
        current_app.logger.error("round push REFUSED: forbidden key at %s", hit)
        return jsonify({"error": f"forbidden key at {hit}"}), 422
    ctx = current_app.config["STORE"]
    offer = ctx.store.outbox_item(int(body["offer_ref"]))
    if not offer or offer["kind"] != "offer":
        return jsonify({"error": "offer not found"}), 404
    rnd = ctx.store.upsert_round(body)
    from .shop import notify_buyer_round
    emailed = notify_buyer_round(ctx, offer, rnd)
    return jsonify({"accepted": True, "token": rnd["token"], "status": rnd["status"], "emailed": emailed}), 202


@bp.post("/images/refresh")
def images_refresh():
    """Key-protected: fetch a batch of missing images now (manual nudge / cron)."""
    if not _keyed():
        return jsonify({"error": "not found"}), 404
    from . import images
    store = current_app.config["STORE"].store
    limit = min(max(int(request.args.get("limit", 200) or 200), 1), 1000)
    return jsonify({**images.refresh_missing(store, limit=limit), **store.image_stats()})


@bp.get("/outbox")
def outbox_pull():
    if not _keyed():
        return jsonify({"error": "not found"}), 404
    limit = min(max(int(request.args.get("limit", 200) or 200), 1), 1000)
    store = current_app.config["STORE"].store
    items = store.pull_outbox(limit=limit)
    return jsonify({"items": items, "count": len(items)})


@bp.post("/outbox/ack")
def outbox_ack():
    if not _keyed():
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    results = body.get("results")
    if not isinstance(results, list):
        return jsonify({"error": "body must be {results: [{id, status, result}]}"}), 400
    store = current_app.config["STORE"].store
    return jsonify({"acked": store.ack_outbox(results)})


@bp.get("/healthz")
def healthz():
    store = current_app.config["STORE"].store
    return jsonify({"ok": True, "feeds": store.feed_status(), "images": store.image_stats()})
