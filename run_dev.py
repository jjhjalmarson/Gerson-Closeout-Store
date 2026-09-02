"""Local dev server with demo data.

    python run_dev.py [--feeds DIR] [--port 5055]

Seeds the SQLite store from AOI feed files when --feeds is given (catalog.json,
customers.json, curation.json), makes sure one demo customer is allowlisted,
prints a one-time sign-in link and a demo invite link, and serves the app. Never use in production.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("SECRET_KEY", "dev-only")
os.environ.setdefault("STORE_INGEST_KEY", "dev-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///dev-store.db")
os.environ.setdefault("MAIL_BACKEND", "log")
os.environ.setdefault("STORE_ADMIN_EMAILS", "admin@example.com")

from app import create_app  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feeds", default="")
    ap.add_argument("--port", type=int, default=5055)
    ap.add_argument("--email", default="demo@example.com")
    a = ap.parse_args()
    app = create_app()
    store = app.config["STORE"].store
    if a.feeds:
        for kind in ("catalog", "customers", "curation", "invites"):
            p = os.path.join(a.feeds, f"{kind}.json")
            if os.path.exists(p):
                body = json.load(open(p, encoding="utf-8"))
                fn = {"catalog": store.ingest_catalog, "customers": store.ingest_customers, "curation": store.ingest_curation,
                      "invites": store.ingest_invites}[kind]
                print(kind, fn(body["items"], as_of=body.get("as_of"), generated_at=body.get("generated_at")))
    if not store.customer_for_email(a.email):
        store.ingest_customers([{"customer_id": "0", "company_name": "Demo Retailer", "emails": [a.email], "buyer_class": "independent",
                                 "rep_name": "Demo Rep", "house_account": False}]
                               + [dict(c, emails=[]) for c in []], as_of=None, generated_at=None)
    cust = store.customer_for_email(a.email)
    tok = store.create_login_token(a.email, cust["customer_id"], 120)
    print(f"\nSign in: http://localhost:{a.port}/login/{tok}")
    # ingest_invites is a full snapshot, so only seed the demo link when the feeds
    # brought none (otherwise it would revoke every real invite just ingested).
    import sqlalchemy as sa
    from store.db import invites as invites_t
    with store.engine.connect() as conn:
        active = [r[0] for r in conn.execute(sa.select(invites_t.c.token).where(invites_t.c.active.is_(True)))]
    if not active:
        store.ingest_invites([{"token": "demo-invite", "label": "Demo Off-Price Buyer", "email": a.email, "companies": []}],
                             as_of=None, generated_at=None)
        active = ["demo-invite"]
    for tok in active:
        print(f"Invite link: http://localhost:{a.port}/i/{tok}")
    adm = store.create_login_token("admin@example.com", "", 120, subject="admin:admin@example.com")
    print(f"Admin portal: http://localhost:{a.port}/login/{adm}\n")
    app.run(port=a.port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
