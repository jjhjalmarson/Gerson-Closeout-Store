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
    # Ceiling on the suggested offer (see Config.suggest_max_disc): the buyer's
    # margin sum runs off MSRP, and MSRP is itself derived from wholesale, so
    # without a cap the suggestion is a fixed fraction of wholesale on every line.
    app.jinja_env.globals["suggest_max_disc"] = cfg.suggest_max_disc

    def _suggest_cap(p):
        """The highest number the sheet will suggest for one item, or None.

        Two ceilings, whichever is lower:

        * the flat site-wide cap off wholesale (``suggest_max_disc``), and
        * **the price we already charge for it publicly** — the ladder step AOI
          sends as ``closeout_price``, which is what the EV pricing groups and
          the website quote today (JJ, 2026-09-08).

        The second one exists because the first alone anchored buyers above our
        own published price: on a written-down SKU the sheet was saying "offer
        $111.22" where the published price was $69.51 and we would have taken
        $20. Nothing here is *shown* as a price — it is a ceiling on the buyer's
        own arithmetic, and it never reaches the floor, which stays in AOI.
        """
        whsl = float(p.get("wholesale") or 0.0)
        if whsl <= 0:
            return None
        caps = []
        if 0.0 < cfg.suggest_max_disc < 1.0:
            caps.append(whsl * (1.0 - cfg.suggest_max_disc))
        published = float(p.get("closeout_price") or 0.0)
        if 0.0 < published < whsl:
            caps.append(published)
        return round(min(caps), 2) if caps else None

    app.jinja_env.globals["suggest_cap"] = _suggest_cap

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
