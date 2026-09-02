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

KINDS = ("catalog", "customers", "curation")
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
    store = current_app.config["STORE"].store
    fn = {"catalog": store.ingest_catalog, "customers": store.ingest_customers, "curation": store.ingest_curation}[kind]
    n = fn(body["items"], as_of=body.get("as_of"), generated_at=body.get("generated_at"))
    return jsonify({"accepted": True, "kind": kind, "count": n}), 202


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
    return jsonify({"ok": True, "feeds": store.feed_status()})
