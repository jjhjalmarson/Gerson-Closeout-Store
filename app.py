"""Gerson Closeout Store — Flask app factory."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import timedelta

from flask import Flask

from config import Config, load_config
from store import admin, ingest, shop
from store.db import Store, make_engine, msrp_price

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@dataclass
class Ctx:
    cfg: Config
    store: Store


def create_app(cfg: Config | None = None) -> Flask:
    cfg = cfg or load_config()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = cfg.secret_key or os.environ.get("FLASK_TEST_SECRET") or "dev-only-not-secret"
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.base_url.startswith("https://"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,   # catalog feed
    )
    app.config["STORE"] = Ctx(cfg=cfg, store=Store(make_engine(cfg.database_url)))
    app.register_blueprint(ingest.bp)
    app.register_blueprint(shop.bp)
    app.register_blueprint(admin.bp)
    # MSRP off the published wholesale, for any page that shows a price.
    app.jinja_env.filters["msrp"] = msrp_price

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        # Never indexed: this is a gated wholesale surface.
        resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        return resp

    @app.get("/robots.txt")
    def robots():
        return "User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain"}

    @app.errorhandler(404)
    def _404(_e):
        return "Not found", 404

    return app


app = create_app()
