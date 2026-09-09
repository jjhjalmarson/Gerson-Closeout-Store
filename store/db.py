"""Schema and data access. SQLAlchemy Core so tests run on SQLite and
production runs on Postgres with the same code.

The schema is the *entire* knowledge of the store. Compare it with the AOI
`closeout_prices` table: no cost, no dates of receipt, no bank fields.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

import logging

import sqlalchemy as sa
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

metadata = sa.MetaData()

products = sa.Table(
    "products", metadata,
    sa.Column("sku", sa.String(64), primary_key=True),
    sa.Column("internal_id", sa.String(32)),
    sa.Column("description", sa.Text, nullable=False, default=""),
    sa.Column("image_url", sa.Text, default=""),
    sa.Column("brand", sa.String(80), default=""),
    sa.Column("category", sa.String(120), default=""),
    sa.Column("subcategory", sa.String(120), default=""),
    sa.Column("company", sa.String(20), nullable=False, default=""),   # gerson | park_hill ("" = any)
    sa.Column("season", sa.String(80), default=""),
    sa.Column("case_pack", sa.Integer, nullable=False, default=1),
    sa.Column("master_pack", sa.Integer, nullable=False, default=0),   # units per master carton (0 = unknown)
    sa.Column("inner_pack", sa.Integer, nullable=False, default=0),    # units per inner pack (0 = none)
    sa.Column("upc", sa.String(32), default=""),
    sa.Column("wholesale", sa.Numeric(12, 2), nullable=False, default=0),
    # The pre-markdown list price from the NetSuite item record, and the anchor
    # the sheet shows (JJ, 2026-09-04).  AOI sends it as ``wholesale`` too, so on
    # this side the two agree and every price the buyer sees is measured off list.
    sa.Column("original_price", sa.Numeric(12, 2), nullable=False, default=0),
    # NetSuite's Base Price, kept only so the two are never confused again.  Park
    # Hill and Glitterville publish a closeout by overwriting Base Price, so on
    # those rows this is what the website charges today and is NOT what the item
    # originally sold for.  Never show it as "wholesale".
    sa.Column("base_price", sa.Numeric(12, 2), nullable=False, default=0),
    # What NetSuite charges today, and which mechanism set it:
    #   ev               -> the item's EV pricing group (Gerson)
    #   base_marked_down -> the markdown is baked into Base Price (Park Hill /
    #                       Glitterville, item status Sale65 / DISCOUNTnn)
    #   base             -> no published markdown; base is the true wholesale
    sa.Column("published_price", sa.Numeric(12, 2), nullable=False, default=0),
    sa.Column("published_basis", sa.String(20), nullable=False, default=""),
    sa.Column("published_label", sa.String(40), nullable=False, default=""),
    sa.Column("published_disc_pct", sa.Integer, nullable=False, default=0),
    sa.Column("closeout_price", sa.Numeric(12, 2), nullable=False, default=0),
    sa.Column("discount_pct", sa.Integer, nullable=False, default=0),
    sa.Column("next_step_date", sa.String(10)),
    sa.Column("next_step_price", sa.Numeric(12, 2)),
    sa.Column("qty_available", sa.Integer, nullable=False, default=0),
    sa.Column("lot", sa.String(200), default=""),
    sa.Column("ship_by", sa.String(10)),
    # What the sheet can badge from (AOI's nightly changelog): the first run this
    # SKU was on the sheet (None = before AOI kept track), the last run its price
    # moved, and the price before it moved. Dates about the sheet, not the goods.
    sa.Column("listed_since", sa.String(10)),
    sa.Column("price_changed_at", sa.String(10)),
    sa.Column("price_was", sa.Numeric(12, 2)),
    # Where AOI put this SKU on the hand-picked list the sheet leads with
    # (1 = first), NULL for everything else.  A merchandising rank and nothing
    # else: it says "show this early", never why.
    sa.Column("featured_rank", sa.Integer),
    # Whether there is a video to watch, so the sheet can say so before a buyer
    # clicks in (JJ, 2026-09-09: "that would be great content"). Derived from the
    # media list at ingest; the gallery itself lives in `media`.
    sa.Column("has_video", sa.Boolean, nullable=False, default=False),
    sa.Column("active", sa.Boolean, nullable=False, default=True),
    sa.Column("updated_at", sa.String(32), nullable=False),
)

# Everything else there is to look at for one SKU, in display order, ``idx`` 0
# being the picture the sheet shows (AOI 2026-09-09).
#
# **Urls, not bytes.** The hero is proxied through /img/<sku> because it can be a
# NetSuite file-cabinet link carrying the account id, and because the sheet must
# render whether or not anything else is up. Everything here comes from Salsify's
# public CDN, so it is linked directly: caching 2.2x more images would multiply
# the store's Postgres for pictures a buyer sees only after clicking into an
# item, and videos are far too big to hold at all. A thumbnail that fails to load
# is a thumbnail that fails to load.
media = sa.Table(
    "media", metadata,
    sa.Column("sku", sa.String(64), primary_key=True),
    sa.Column("idx", sa.Integer, primary_key=True),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("kind", sa.String(10), nullable=False, default="image"),   # image | video
    sa.Column("updated_at", sa.String(32), nullable=False),
)

# Every digest that went out: when, to how many, covering what. The weekly
# auto-send reads the last row so a re-run of the feed never mails twice.
digest_runs = sa.Table(
    "digest_runs", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("kind", sa.String(20), nullable=False),
    sa.Column("since", sa.String(10)),
    sa.Column("items", sa.Integer, nullable=False, default=0),
    sa.Column("recipients", sa.Integer, nullable=False, default=0),
    sa.Column("sent_by", sa.String(200), default=""),
    sa.Column("sent_at", sa.String(32), nullable=False),
)

customers = sa.Table(
    "customers", metadata,
    sa.Column("customer_id", sa.String(32), primary_key=True),
    sa.Column("entity_id", sa.String(64), default=""),
    sa.Column("company_name", sa.String(200), nullable=False, default=""),
    sa.Column("buyer_class", sa.String(20), nullable=False, default="independent"),
    sa.Column("volume_tier", sa.String(20), default=""),
    sa.Column("rep_name", sa.String(120), default=""),
    sa.Column("house_account", sa.Boolean, nullable=False, default=False),
    sa.Column("accounts_json", sa.Text, nullable=False, default="{}"),   # {company: customer id in that subsidiary}
    # Which price list this account buys off (JJ / Goodwill, 2026-09-09). NULL is
    # the offer sheet everyone has had — wholesale and a blank box; a list id
    # replaces it with the final price we will take, per SKU. The store never
    # learns why a price on a list is what it is; it only shows it.
    sa.Column("price_list_id", sa.String(64)),
    sa.Column("active", sa.Boolean, nullable=False, default=True),
    sa.Column("updated_at", sa.String(32), nullable=False),
)

# The price lists AOI publishes, and the price per SKU on each. A list is named
# once -- "Landed cost + 5%" -- for the admin who assigns it, and never for a
# buyer, who only ever sees "Your price". No basis, no markup and no cost cross:
# one number per line, which is the whole point of a firm-price account.
price_lists = sa.Table(
    "price_lists", metadata,
    sa.Column("list_id", sa.String(64), primary_key=True),
    sa.Column("label", sa.String(160), nullable=False, default=""),
)

price_list_items = sa.Table(
    "price_list_items", metadata,
    sa.Column("list_id", sa.String(64), primary_key=True),
    sa.Column("sku", sa.String(64), primary_key=True),
    sa.Column("price", sa.Numeric(12, 2), nullable=False),
)

customer_emails = sa.Table(
    "customer_emails", metadata,
    sa.Column("email", sa.String(200), primary_key=True),
    sa.Column("customer_id", sa.String(32), nullable=False, index=True),
)

curation = sa.Table(
    "curation", metadata,
    sa.Column("customer_id", sa.String(32), primary_key=True),
    sa.Column("skus_json", sa.Text, nullable=False),
    sa.Column("updated_at", sa.String(32), nullable=False),
)

login_tokens = sa.Table(
    "login_tokens", metadata,
    sa.Column("token", sa.String(64), primary_key=True),
    sa.Column("email", sa.String(200), nullable=False),
    sa.Column("customer_id", sa.String(32), nullable=False, default=""),   # legacy: AOI-fed account id
    sa.Column("subject", sa.String(200)),                                   # cust:<id> | buyer:<id> | admin:<email>
    sa.Column("expires_at", sa.String(32), nullable=False),
    sa.Column("used_at", sa.String(32)),
)

# Admin passwords (JJ, 2026-09-03): an admin signs in by link once, sets a password
# on the portal, and types it from then on. Hash only (werkzeug), keyed by the
# address in STORE_ADMIN_EMAILS; removing the address from the env ends access
# whatever is stored here.
admin_credentials = sa.Table(
    "admin_credentials", metadata,
    sa.Column("email", sa.String(200), primary_key=True),
    sa.Column("password_hash", sa.String(300), nullable=False),
    sa.Column("updated_at", sa.String(32), nullable=False),
)

# Buyers who signed up on the store (JJ, 2026-09-02): approved by an admin here,
# never tied to a NetSuite customer, every company's SKUs visible.
buyers = sa.Table(
    "buyers", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("company", sa.String(200), nullable=False),
    sa.Column("contact", sa.String(200), default=""),
    sa.Column("email", sa.String(200), nullable=False, unique=True),
    sa.Column("phone", sa.String(60), default=""),
    sa.Column("notes", sa.Text, default=""),
    sa.Column("status", sa.String(20), nullable=False, default="pending"),   # pending | approved | suspended | declined
    # Which lane AOI prices this account against (brief S6). Set by an admin on
    # approval, never by the buyer: a regional retailer calling itself a
    # liquidator to reach the bottom floor is the obvious exploit. Defaults to
    # the strictest lane, so an unclassified account never gets the lowest floor.
    sa.Column("buyer_class", sa.String(20), nullable=False, default="regional"),
    # Which price list they buy off, or NULL for the offer sheet (JJ / Goodwill,
    # 2026-09-09). Assigned here rather than in AOI because this is where the
    # real outside buyers live: AOI has never heard of Goodwill of Minnesota.
    sa.Column("price_list_id", sa.String(64)),
    sa.Column("invite_token", sa.String(64)),
    sa.Column("created_at", sa.String(32), nullable=False),
    sa.Column("approved_at", sa.String(32)),
    sa.Column("approved_by", sa.String(200)),
    sa.Column("updated_at", sa.String(32), nullable=False),
)

signup_invites = sa.Table(
    "signup_invites", metadata,
    sa.Column("token", sa.String(64), primary_key=True),
    sa.Column("email", sa.String(200), nullable=False),
    sa.Column("company", sa.String(200), default=""),
    sa.Column("note", sa.Text, default=""),
    sa.Column("created_by", sa.String(200), default=""),
    sa.Column("created_at", sa.String(32), nullable=False),
    sa.Column("used_at", sa.String(32)),
    sa.Column("buyer_id", sa.Integer),
    sa.Column("revoked_at", sa.String(32)),
)

# Invite links (leadership, 2026-09-02): the offer sheet is invitation-only.
# AOI creates and pushes these; anyone holding /i/<token> sees the sheet until
# the invite expires or AOI revokes it. No login, no password.
invites = sa.Table(
    "invites", metadata,
    sa.Column("token", sa.String(64), primary_key=True),
    sa.Column("label", sa.String(200), nullable=False, default=""),       # company / who it was sent to
    sa.Column("contact", sa.String(200), default=""),
    sa.Column("email", sa.String(200), default=""),
    sa.Column("companies_json", sa.Text, nullable=False, default="[]"),   # subsidiaries whose SKUs they see
    sa.Column("expires_at", sa.String(32)),
    sa.Column("buyer_class", sa.String(20), nullable=False, default="regional"),   # set in AOI, rides the feed
    sa.Column("active", sa.Boolean, nullable=False, default=True),
    sa.Column("updated_at", sa.String(32), nullable=False),
)

# Offer drafts, one per buyer key ("inv:<token>" or "cust:<id>"):
# {sku: {"qty": units, "price": offered unit price}}.  Nothing here is a price
# the store set; it is what the buyer typed.
carts = sa.Table(
    "carts", metadata,
    sa.Column("customer_id", sa.String(96), primary_key=True),
    sa.Column("lines_json", sa.Text, nullable=False, default="{}"),
    sa.Column("updated_at", sa.String(32), nullable=False),
)

# What buyers do here, one row per event (JJ, 2026-09-03). The point is the
# things that never become an offer: the SKU opened and left, the search with no
# results, the price typed into a box and abandoned. Every session is a known
# account, so this is a behaviour record per buyer rather than an anonymous
# funnel. AOI pulls it with a cursor and keeps the dossier; nothing here is ever
# shown to a buyer, and no price Gerson set is recorded -- only what they did.
events = sa.Table(
    "events", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("kind", sa.String(40), nullable=False, index=True),
    sa.Column("buyer_key", sa.String(96), nullable=False, index=True),   # inv:<token> | buyer:<id> | cust:<id>
    sa.Column("buyer_label", sa.String(200), default=""),
    sa.Column("buyer_class", sa.String(20), default=""),
    sa.Column("session_id", sa.String(64), default=""),                  # one browser session, for stitching a visit
    sa.Column("sku", sa.String(64), default="", index=True),
    sa.Column("payload_json", sa.Text, nullable=False, default="{}"),
    sa.Column("created_at", sa.String(32), nullable=False, index=True),
)

# Everything the store hands back to AOI. kind: order | offer | application | hold.
outbox = sa.Table(
    "outbox", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("kind", sa.String(20), nullable=False, index=True),
    sa.Column("customer_id", sa.String(32)),
    sa.Column("buyer_key", sa.String(96), index=True),        # "inv:<token>" | "cust:<id>": who to show it to
    sa.Column("payload_json", sa.Text, nullable=False),
    sa.Column("status", sa.String(20), nullable=False, default="pending", index=True),
    sa.Column("created_at", sa.String(32), nullable=False),
    sa.Column("pulled_at", sa.String(32)),
    sa.Column("acked_at", sa.String(32)),
    sa.Column("result_json", sa.Text),
)

# Negotiation rounds pushed by AOI (leadership 2026-09-02, shape borrowed from the
# purchasing app's vendor rounds): one token per round. The buyer opens /o/<token>,
# sees the trail, and accepts / counters / declines; the answer goes to the outbox
# as offer_response. Only price, quantity and the message ever arrive here.
rounds = sa.Table(
    "rounds", metadata,
    sa.Column("token", sa.String(64), primary_key=True),
    sa.Column("offer_id", sa.Integer, nullable=False, index=True),      # the buyer's original outbox item
    sa.Column("round_id", sa.Integer, nullable=False),                  # AOI's id for the round
    sa.Column("round_no", sa.Integer, nullable=False),
    sa.Column("kind", sa.String(20), nullable=False),                   # counter | accept | decline | recorded
    sa.Column("thread_status", sa.String(20), nullable=False, default=""),
    sa.Column("lines_json", sa.Text, nullable=False, default="[]"),
    sa.Column("message", sa.Text, default=""),
    sa.Column("buyer_email", sa.String(200), default=""),
    sa.Column("company", sa.String(200), default=""),
    sa.Column("created_at", sa.String(32), nullable=False),             # when AOI made the round
    sa.Column("received_at", sa.String(32), nullable=False),
    sa.Column("status", sa.String(20), nullable=False, default="open"),   # open | responded | closed
    sa.Column("response_json", sa.Text),
    sa.Column("responded_at", sa.String(32)),
    sa.Column("opened_at", sa.String(32)),                               # first time the buyer opened the link
)

# Product images, fetched once from the feed's source URL and served from here
# so buyers never load a NetSuite / shop URL directly.
images = sa.Table(
    "images", metadata,
    sa.Column("sku", sa.String(64), primary_key=True),
    sa.Column("source_url", sa.Text, nullable=False),
    sa.Column("content_type", sa.String(40)),
    sa.Column("data", sa.LargeBinary),
    sa.Column("status", sa.String(10), nullable=False, default="ok"),   # ok | failed
    sa.Column("fetched_at", sa.String(32), nullable=False),
)

feed_runs = sa.Table(
    "feed_runs", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("kind", sa.String(20), nullable=False),
    sa.Column("count", sa.Integer, nullable=False),
    sa.Column("as_of", sa.String(10)),
    sa.Column("generated_at", sa.String(32)),
    sa.Column("received_at", sa.String(32), nullable=False),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# The lanes AOI prices against. "liquidator" carries the lowest floor by far, so
# anything unrecognised falls back to "regional", never to the loosest lane.
BUYER_CLASSES: tuple[str, ...] = ("independent", "regional", "liquidator")
DEFAULT_BUYER_CLASS = "regional"

# A price list id is a slug AOI coins ("cost_plus_5"). Anything else -- and a
# feed from an older AOI that sends nothing at all -- is no list, which is the
# offer sheet everyone already has.
_LIST_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _clean_class(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in BUYER_CLASSES else DEFAULT_BUYER_CLASS


def clean_list_id(value: Any) -> str:
    """The slug, or "" for anything else -- and "" is the offer sheet, so junk
    can only ever cost a buyer their firm prices, never hand them someone
    else's."""
    v = str(value or "").strip().lower()
    return v if _LIST_ID.match(v) else ""


def make_engine(url: str) -> Engine:
    kw: dict[str, Any] = {"future": True}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url.endswith("sqlite://"):
            from sqlalchemy.pool import StaticPool
            kw["poolclass"] = StaticPool
    else:
        kw["pool_pre_ping"] = True
    eng = sa.create_engine(url, **kw)
    metadata.create_all(eng)
    _ensure_columns(eng)
    return eng


def _upsert(conn, table: sa.Table, rows: list[dict[str, Any]], key: str) -> None:
    """Dialect-aware bulk upsert on a single-column primary key."""
    if not rows:
        return
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    # SQLite caps bound variables per statement (999 on older builds); the
    # catalog is ~6,000 rows x 20 columns, so upsert in chunks.
    step = 300
    for i in range(0, len(rows), step):
        stmt = insert(table).values(rows[i:i + step])
        update_cols = {c.name: getattr(stmt.excluded, c.name) for c in table.columns if c.name != key}
        conn.execute(stmt.on_conflict_do_update(index_elements=[key], set_=update_cols))


class Store:
    def __init__(self, engine: Engine):
        self.engine = engine

    # --- ingest -------------------------------------------------------------

    def ingest_catalog(self, items: Iterable[dict[str, Any]], *, as_of: str | None, generated_at: str | None) -> int:
        now = now_iso()
        rows, media_rows = [], []
        for it in items:
            media_rows.extend(self._media_rows(it, now))
            rows.append({
                "sku": str(it["sku"]), "internal_id": str(it.get("internal_id") or ""),
                "description": str(it.get("description") or it["sku"]), "image_url": str(it.get("image_url") or ""),
                "brand": str(it.get("brand") or ""), "category": str(it.get("category") or ""),
                "subcategory": str(it.get("subcategory") or ""), "season": str(it.get("season") or ""),
                "company": str(it.get("company") or ""),
                "case_pack": max(int(it.get("case_pack") or 1), 1), "upc": str(it.get("upc") or ""),
                "master_pack": max(int(float(it.get("master_pack") or 0)), 0), "inner_pack": max(int(float(it.get("inner_pack") or 0)), 0),
                "wholesale": float(it.get("wholesale") or 0), "closeout_price": float(it.get("closeout_price") or 0),
                # Older feeds carry no original: fall back to wholesale so the
                # anchor is never zero, and the row simply reads as un-marked-down.
                "original_price": float(it.get("original_price") or it.get("wholesale") or 0),
                "published_price": float(it.get("published_price") or 0),
                "published_basis": str(it.get("published_basis") or "")[:20],
                "published_label": str(it.get("published_label") or "")[:40],
                "published_disc_pct": int(it.get("published_disc_pct") or 0),
                "base_price": float(it.get("base_price") or it.get("wholesale") or 0),
                "discount_pct": int(it.get("discount_pct") or 0), "next_step_date": it.get("next_step_date"),
                "next_step_price": it.get("next_step_price"), "qty_available": int(it.get("qty_available") or 0),
                "lot": str(it.get("lot") or ""), "ship_by": it.get("ship_by"),
                "listed_since": (str(it.get("listed_since"))[:10] if it.get("listed_since") else None),
                "price_changed_at": (str(it.get("price_changed_at"))[:10] if it.get("price_changed_at") else None),
                "price_was": (float(it["price_was"]) if it.get("price_was") is not None else None),
                # An older AOI sends no rank at all: nothing is featured, and the
                # sheet reads in brand / category order exactly as before.
                "featured_rank": (int(it["featured_rank"]) if it.get("featured_rank") else None),
                "has_video": any(m["kind"] == "video" for m in media_rows if m["sku"] == str(it["sku"])),
                "active": True, "updated_at": now,
            })
        with self.engine.begin() as conn:
            # Full snapshot: anything not in this feed is no longer for sale.
            conn.execute(sa.update(products).values(active=False))
            _upsert(conn, products, rows, "sku")
            # Galleries are replaced wholesale, not merged: a picture withdrawn
            # in Salsify has to disappear here too, and a row keyed (sku, idx)
            # would otherwise keep the old one at the end of the list.
            conn.execute(sa.delete(media))
            if media_rows:
                conn.execute(media.insert(), media_rows)
            conn.execute(feed_runs.insert().values(kind="catalog", count=len(rows), as_of=as_of,
                                                   generated_at=generated_at, received_at=now))
        return len(rows)

    def ingest_customers(self, items: Iterable[dict[str, Any]], *, as_of: str | None, generated_at: str | None) -> int:
        now = now_iso()
        rows, emails = [], []
        for it in items:
            cid = str(it["customer_id"])
            rows.append({
                "customer_id": cid, "entity_id": str(it.get("entity_id") or ""),
                "company_name": str(it.get("company_name") or cid),
                "buyer_class": str(it.get("buyer_class") or "independent"), "volume_tier": str(it.get("volume_tier") or ""),
                "rep_name": str(it.get("rep_name") or ""), "house_account": bool(it.get("house_account")),
                "accounts_json": json.dumps({str(k): str(v) for k, v in (it.get("accounts") or {}).items()}, sort_keys=True),
                "price_list_id": clean_list_id(it.get("price_list_id")) or None,
                "active": True, "updated_at": now,
            })
            for e in it.get("emails") or []:
                e = str(e).strip().lower()
                if e:
                    emails.append({"email": e, "customer_id": cid})
        with self.engine.begin() as conn:
            conn.execute(sa.update(customers).values(active=False))
            _upsert(conn, customers, rows, "customer_id")
            conn.execute(sa.delete(customer_emails))
            # last write wins if an email is listed under two accounts
            seen: dict[str, dict[str, Any]] = {}
            for e in emails:
                seen[e["email"]] = e
            if seen:
                conn.execute(customer_emails.insert(), list(seen.values()))
            conn.execute(feed_runs.insert().values(kind="customers", count=len(rows), as_of=as_of,
                                                   generated_at=generated_at, received_at=now))
        return len(rows)

    def ingest_curation(self, items: Iterable[dict[str, Any]], *, as_of: str | None, generated_at: str | None) -> int:
        now = now_iso()
        rows = [{"customer_id": str(it["customer_id"]), "skus_json": json.dumps([str(s) for s in it.get("skus") or []]),
                 "updated_at": now} for it in items]
        with self.engine.begin() as conn:
            conn.execute(sa.delete(curation))
            _upsert(conn, curation, rows, "customer_id")
            conn.execute(feed_runs.insert().values(kind="curation", count=len(rows), as_of=as_of,
                                                   generated_at=generated_at, received_at=now))
        return len(rows)

    def ingest_prices(self, items: Iterable[dict[str, Any]], *, as_of: str | None, generated_at: str | None) -> int:
        """The price lists AOI publishes, one entry per list:
        ``[{"list_id": "cost_plus_5", "label": "Landed cost + 5%",
        "prices": {"SKU": 6.83, ...}}]``.

        A full snapshot like every other feed: both tables are replaced by what
        arrives, so a list AOI deactivated -- or an empty list of lists, which
        means there are none at all -- simply stops existing, and the buyers on
        it fall back to the offer sheet. Returns the number of lists, matching
        the envelope's ``count``."""
        now = now_iso()
        lists: dict[str, dict[str, Any]] = {}
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for it in items:
            lid = clean_list_id(it.get("list_id"))
            if not lid:
                continue                           # no slug, no list: we would not know who is on it
            lists[lid] = {"list_id": lid, "label": str(it.get("label") or "")[:160]}
            for sku, price in (it.get("prices") or {}).items():
                s = str(sku).strip()
                try:
                    p = round(float(price), 2)
                except (TypeError, ValueError):
                    continue
                if not s or p <= 0:
                    continue                       # a zero is not a price we would honour
                rows[(lid, s)] = {"list_id": lid, "sku": s, "price": p}
        with self.engine.begin() as conn:
            conn.execute(sa.delete(price_list_items))
            conn.execute(sa.delete(price_lists))
            if lists:
                conn.execute(price_lists.insert(), list(lists.values()))
            values = list(rows.values())
            step = 300                             # SQLite's bound-variable cap, as in _upsert
            for i in range(0, len(values), step):
                conn.execute(price_list_items.insert(), values[i:i + step])
            conn.execute(feed_runs.insert().values(kind="prices", count=len(lists), as_of=as_of,
                                                   generated_at=generated_at, received_at=now))
        return len(lists)

    def price_lists(self) -> list[dict[str, Any]]:
        """Every list AOI has published, for the admin who assigns them."""
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(price_lists).order_by(price_lists.c.label, price_lists.c.list_id)).mappings().all()
        return [dict(r) for r in rows]

    def price_list(self, list_id: str) -> dict[str, Any] | None:
        lid = clean_list_id(list_id)
        if not lid:
            return None
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(price_lists).where(price_lists.c.list_id == lid)).mappings().first()
        return dict(r) if r else None

    def list_prices_for(self, list_id: str) -> dict[str, float]:
        """``{sku: price}`` on one list, empty when it holds none."""
        lid = clean_list_id(list_id)
        if not lid:
            return {}
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(price_list_items.c.sku, price_list_items.c.price)
                                .where(price_list_items.c.list_id == lid)).all()
        return {str(sku): float(price) for sku, price in rows}

    def ingest_invites(self, items: Iterable[dict[str, Any]], *, as_of: str | None, generated_at: str | None) -> int:
        """Full snapshot of invite links: anything AOI no longer sends is revoked."""
        now = now_iso()
        rows = []
        for it in items:
            tok = str(it.get("token") or "").strip()
            if not tok:
                continue
            comps = [str(c) for c in (it.get("companies") or []) if str(c) in COMPANY_LABELS]
            rows.append({"token": tok, "label": str(it.get("label") or ""), "contact": str(it.get("contact") or ""),
                         "email": str(it.get("email") or "").strip().lower(), "companies_json": json.dumps(comps),
                         "expires_at": (str(it.get("expires_at") or "")[:32] or None), "active": True, "updated_at": now,
                         "buyer_class": _clean_class(it.get("buyer_class"))})
        with self.engine.begin() as conn:
            conn.execute(sa.update(invites).values(active=False))
            _upsert(conn, invites, rows, "token")
            conn.execute(feed_runs.insert().values(kind="invites", count=len(rows), as_of=as_of,
                                                   generated_at=generated_at, received_at=now))
        return len(rows)

    def invite(self, token: str) -> dict[str, Any] | None:
        """Active, unexpired invite for ``token``, with ``companies`` resolved."""
        tok = str(token or "").strip()
        if not tok:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(invites).where(invites.c.token == tok, invites.c.active.is_(True))).mappings().first()
        if not row:
            return None
        exp = row["expires_at"] or ""
        if exp and exp[:10] < now_iso()[:10]:
            return None
        d = dict(row)
        try:
            comps = [c for c in json.loads(d.get("companies_json") or "[]") if c in COMPANY_LABELS]
        except (TypeError, ValueError):
            comps = []
        d["companies"] = comps or list(ALL_COMPANIES)
        return d

    def feed_status(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            sub = sa.select(feed_runs.c.kind, sa.func.max(feed_runs.c.id).label("mid")).group_by(feed_runs.c.kind).subquery()
            rows = conn.execute(sa.select(feed_runs).join(sub, feed_runs.c.id == sub.c.mid)).mappings().all()
        return [dict(r) for r in rows]

    # --- customers / auth ---------------------------------------------------

    def customer_for_email(self, email: str) -> dict[str, Any] | None:
        e = (email or "").strip().lower()
        with self.engine.connect() as conn:
            row = conn.execute(
                sa.select(customers).join(customer_emails, customer_emails.c.customer_id == customers.c.customer_id)
                .where(customer_emails.c.email == e, customers.c.active.is_(True))
            ).mappings().first()
        return _cust(row) if row else None

    def customer(self, customer_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(customers).where(customers.c.customer_id == str(customer_id))).mappings().first()
        return _cust(row) if row and row["active"] else None

    def create_login_token(self, email: str, customer_id: str, minutes: int, *, subject: str | None = None) -> str:
        """One-time sign-in link. ``subject`` says who it signs in: ``cust:<id>``
        (AOI-fed account, the default when a customer id is given), ``buyer:<id>``
        (signed up here) or ``admin:<email>``."""
        token = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        subj = subject or (f"cust:{customer_id}" if str(customer_id or "").strip() else "")
        with self.engine.begin() as conn:
            conn.execute(login_tokens.insert().values(token=token, email=email.strip().lower(),
                                                      customer_id=str(customer_id or ""), subject=subj, expires_at=exp))
        return token

    def redeem_login_token(self, token: str) -> dict[str, Any] | None:
        """Single use, unexpired. Returns ``{subject, email}`` or None."""
        now = now_iso()
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(login_tokens).where(login_tokens.c.token == token)).mappings().first()
            if not row or row["used_at"] or row["expires_at"] < now:
                return None
            conn.execute(sa.update(login_tokens).where(login_tokens.c.token == token).values(used_at=now))
        subject = row["subject"] or (f"cust:{row['customer_id']}" if row["customer_id"] else "")
        return {"subject": subject, "email": row["email"]} if subject else None

    def set_admin_password_hash(self, email: str, password_hash: str) -> None:
        e = email.strip().lower()
        with self.engine.begin() as conn:
            conn.execute(sa.delete(admin_credentials).where(admin_credentials.c.email == e))
            conn.execute(admin_credentials.insert().values(email=e, password_hash=password_hash, updated_at=now_iso()))

    def admin_password_hash(self, email: str) -> str:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(admin_credentials.c.password_hash)
                               .where(admin_credentials.c.email == email.strip().lower())).first()
        return row[0] if row else ""

    # --- buyers who signed up here + their invitations ----------------------

    def create_signup_invite(self, email: str, *, company: str = "", note: str = "", created_by: str = "") -> dict[str, Any]:
        token = secrets.token_urlsafe(24)
        with self.engine.begin() as conn:
            conn.execute(signup_invites.insert().values(token=token, email=email.strip().lower(), company=company or "",
                                                        note=note or "", created_by=created_by or "", created_at=now_iso()))
        return self.signup_invite(token, any_state=True)

    def signup_invite(self, token: str, *, any_state: bool = False) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(signup_invites).where(signup_invites.c.token == str(token or ""))).mappings().first()
        if not r or (not any_state and (r["used_at"] or r["revoked_at"])):
            return None
        return dict(r)

    def list_signup_invites(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rs = conn.execute(sa.select(signup_invites).order_by(signup_invites.c.created_at.desc()).limit(limit)).mappings().all()
        return [dict(r) for r in rs]

    def revoke_signup_invite(self, token: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(sa.update(signup_invites).where(signup_invites.c.token == str(token), signup_invites.c.used_at.is_(None))
                         .values(revoked_at=now_iso()))

    def create_buyer(self, *, company: str, contact: str, email: str, phone: str = "", notes: str = "",
                     invite_token: str | None = None, approve: bool = False, approved_by: str = "") -> dict[str, Any]:
        """A sign-up. An email already on file keeps its record (and its status
        unless it was declined, which becomes pending again).

        ``approve`` is the invitation path (JJ, 2026-09-03): an admin already
        decided who this is when they sent the invitation, so the sign-up is
        approved on the spot rather than queued a second time.  A **suspended**
        buyer is never resurrected by an invitation: un-suspending is an admin's
        deliberate act on the portal."""
        now = now_iso()
        e = email.strip().lower()
        with self.engine.begin() as conn:
            existing = conn.execute(sa.select(buyers).where(buyers.c.email == e)).mappings().first()
            if existing:
                vals = {"company": company.strip() or existing["company"], "contact": contact.strip() or existing["contact"],
                        "phone": phone.strip() or existing["phone"], "notes": notes.strip() or existing["notes"], "updated_at": now}
                if existing["status"] == "declined":
                    vals["status"] = "pending"
                if approve and existing["status"] in ("pending", "declined"):
                    vals.update(status="approved", approved_at=now, approved_by=approved_by or "invitation")
                if invite_token:
                    vals["invite_token"] = invite_token
                conn.execute(sa.update(buyers).where(buyers.c.id == existing["id"]).values(**vals))
                bid = int(existing["id"])
            else:
                res = conn.execute(buyers.insert().values(company=company.strip(), contact=contact.strip(), email=e, phone=phone.strip(),
                                                          notes=notes.strip(), status="approved" if approve else "pending",
                                                          invite_token=invite_token, created_at=now, updated_at=now,
                                                          approved_at=now if approve else None,
                                                          approved_by=(approved_by or "invitation") if approve else None))
                bid = int(res.inserted_primary_key[0])
            if invite_token:
                conn.execute(sa.update(signup_invites).where(signup_invites.c.token == invite_token, signup_invites.c.used_at.is_(None))
                             .values(used_at=now, buyer_id=bid))
        return self.buyer(bid)

    def buyer(self, buyer_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(buyers).where(buyers.c.id == int(buyer_id))).mappings().first()
        return dict(r) if r else None

    def buyer_for_email(self, email: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(buyers).where(buyers.c.email == (email or "").strip().lower())).mappings().first()
        return dict(r) if r else None

    def list_buyers(self, *, status: str | None = None) -> list[dict[str, Any]]:
        stmt = sa.select(buyers).order_by(buyers.c.company, buyers.c.id)
        if status:
            stmt = stmt.where(buyers.c.status == status)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings().all()]

    def set_buyer_status(self, buyer_id: int, status: str, *, by: str = "", buyer_class: str | None = None,
                         price_list_id: str | None = None) -> None:
        now = now_iso()
        vals: dict[str, Any] = {"status": status, "updated_at": now}
        if status == "approved":
            vals.update(approved_at=now, approved_by=by)
        if buyer_class is not None:
            vals["buyer_class"] = _clean_class(buyer_class)
        if price_list_id is not None:
            lid = clean_list_id(price_list_id)
            vals["price_list_id"] = (lid if lid and self.price_list(lid) else None)
        with self.engine.begin() as conn:
            conn.execute(sa.update(buyers).where(buyers.c.id == int(buyer_id)).values(**vals))

    def set_buyer_class(self, buyer_id: int, buyer_class: str) -> str:
        """The governed field (brief S6): an admin's call, never the buyer's.
        Returns what was actually stored."""
        cls = _clean_class(buyer_class)
        with self.engine.begin() as conn:
            conn.execute(sa.update(buyers).where(buyers.c.id == int(buyer_id))
                         .values(buyer_class=cls, updated_at=now_iso()))
        return cls

    def set_buyer_price_list(self, buyer_id: int, list_id: str) -> str:
        """Which price list this buyer buys off (JJ / Goodwill, 2026-09-09), or
        "" for the offer sheet.  An admin's call, like the class: it is the
        difference between a buyer bidding and a buyer being quoted.  A list id
        we have never been fed is stored as nothing, so a buyer can only ever
        land back on the offer sheet.  Returns what was actually stored."""
        lid = clean_list_id(list_id)
        if lid and not self.price_list(lid):
            lid = ""
        with self.engine.begin() as conn:
            conn.execute(sa.update(buyers).where(buyers.c.id == int(buyer_id))
                         .values(price_list_id=lid or None, updated_at=now_iso()))
        return lid

    def buyer_offer_counts(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            rs = conn.execute(sa.select(outbox.c.buyer_key, sa.func.count()).where(outbox.c.kind == "offer")
                              .group_by(outbox.c.buyer_key)).all()
        return {str(k): int(n) for k, n in rs if k}

    # --- catalog ------------------------------------------------------------

    def product(self, sku: str, companies: Iterable[str] | None = None) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(products).where(products.c.sku == sku, products.c.active.is_(True))).mappings().first()
        if not row:
            return None
        p = _prod(row)
        if companies is not None and p["company"] and p["company"] not in set(companies):
            return None                                  # not sold by any company this buyer is set up with
        return p

    MEDIA_KINDS: tuple[str, ...] = ("image", "video")
    MAX_MEDIA = 12
    # Hosts whose urls must never reach a buyer's browser (see _media_rows).
    UNLINKABLE_HOSTS: tuple[str, ...] = ("netsuite.com",)

    @classmethod
    def _media_rows(cls, item: dict[str, Any], now: str) -> list[dict[str, Any]]:
        """One row per showable thing, in the order AOI sent them.

        A feed without ``media`` — an older AOI — yields nothing here, and the
        item page falls back to the hero on its own."""
        sku = str(item.get("sku") or "").strip()
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for m in (item.get("media") or []):
            if not isinstance(m, Mapping):
                continue
            url, kind = str(m.get("url") or "").strip(), str(m.get("kind") or "").strip().lower()
            if not url or kind not in cls.MEDIA_KINDS or url in seen:
                continue
            # Everything past the hero is linked into the buyer's browser, and a
            # NetSuite file-cabinet url carries the account id. The hero is
            # exempt because it is served from our own cache at /img/<sku>, never
            # linked; anything after it that is not safe to hand out is dropped
            # rather than exposed, whatever the feed says.
            if out and any(h in url.lower() for h in cls.UNLINKABLE_HOSTS):
                continue
            seen.add(url)
            out.append({"sku": sku, "idx": len(out), "url": url, "kind": kind, "updated_at": now})
            if len(out) >= cls.MAX_MEDIA:
                break
        return out

    def media_for(self, sku: str) -> list[dict[str, Any]]:
        """``[{idx, url, kind}]`` in display order for one SKU."""
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(media).where(media.c.sku == str(sku))
                                .order_by(media.c.idx)).mappings().all()
        return [{"idx": r["idx"], "url": r["url"], "kind": r["kind"]} for r in rows]

    def media_counts(self) -> dict[str, int]:
        """How much there is to look at, for the admin page."""
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(media.c.kind, sa.func.count())
                                .group_by(media.c.kind)).all()
        return {str(k): int(n) for k, n in rows}

    def products_by_skus(self, skus: Iterable[str]) -> dict[str, dict[str, Any]]:
        wanted = [str(s) for s in skus]
        if not wanted:
            return {}
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(products).where(products.c.sku.in_(wanted), products.c.active.is_(True))).mappings().all()
        return {r["sku"]: _prod(r) for r in rows}

    # The featured list leads the default order (JJ, 2026-09-08) -- the SKUs we
    # most want an offer on go in front of the buyer before anything else. Every
    # other sort is the buyer's own question, so featured takes no priority there.
    FEATURED_FIRST = products.c.featured_rank.asc().nulls_last()

    SORTS = {
        "default": (FEATURED_FIRST, products.c.brand, products.c.category, products.c.sku),
        "brand": (products.c.brand, products.c.category, products.c.sku),
        "wholesale_asc": (products.c.wholesale.asc(), products.c.sku),
        "wholesale_desc": (products.c.wholesale.desc(), products.c.sku),
        "qty": (products.c.qty_available.desc(), products.c.sku),
        "value": ((products.c.wholesale * products.c.qty_available).desc(), products.c.sku),   # biggest lots first
        # What went on the sheet most recently; SKUs listed before AOI kept track sort last.
        "newest": (products.c.listed_since.desc().nulls_last(), products.c.brand, products.c.sku),
    }

    # The smallest lot a buyer can take: an inner pack where there is one, else
    # the case. "Cases available" is counted in these, which is how a buyer
    # thinks about depth -- 240 pieces of a six-pack is 40 cases (JJ, 2026-09-03).
    SMALLEST_PACK = sa.func.coalesce(sa.func.nullif(products.c.inner_pack, 0),
                                     sa.func.nullif(products.c.case_pack, 0), 1)

    @staticmethod
    def _in_any(col, values):
        """One value or several: brands, categories and subcategories are all
        multi-select, and an empty choice means no filter at all."""
        vals = [v for v in ([values] if isinstance(values, str) else list(values or [])) if v]
        if not vals:
            return None
        return col == vals[0] if len(vals) == 1 else col.in_(vals)

    @classmethod
    def _priced_on(cls, list_id: str, max_price: float | None = None):
        """Correlated EXISTS on ``price_list_items``: this SKU carries a price
        on that list (optionally at or under ``max_price``). A subquery rather
        than a list of SKUs because a list can hold thousands of them and
        SQLite counts bound variables."""
        conds = [price_list_items.c.list_id == str(list_id),
                 price_list_items.c.sku == products.c.sku,
                 price_list_items.c.price > 0]
        if max_price:
            conds.append(price_list_items.c.price <= float(max_price))
        return sa.select(sa.literal(1)).where(*conds).exists()

    @classmethod
    def _product_filter(cls, *, brand=None, category=None, subcategory=None, q=None, companies=None,
                        min_units=None, min_cases=None, new_since=None, featured_only=False,
                        max_price=None, price_list_id=None):
        """Every word of ``q`` must appear somewhere in SKU, description, brand,
        category or subcategory; ``companies`` limits to the subsidiaries the
        buyer holds an account with (or the invite covers). ``brand`` /
        ``category`` / ``subcategory`` each take one value or a list.

        ``max_price`` is "under $__" measured on **the price this buyer is
        actually looking at**: the price on their list where ``price_list_id``
        is given, the wholesale anchor otherwise.  A buyer on a list sees only
        the SKUs that list carries a price for, so ``price_list_id`` narrows
        the sheet on its own as well."""
        conds = [products.c.active.is_(True), products.c.qty_available > 0]
        if price_list_id:
            conds.append(cls._priced_on(price_list_id, max_price))
        elif max_price:
            conds.append(products.c.wholesale <= float(max_price))
        if companies is not None:
            conds.append(sa.or_(products.c.company == "", products.c.company.in_(list(companies))))
        for col, val in ((products.c.brand, brand), (products.c.category, category),
                         (products.c.subcategory, subcategory)):
            cond = cls._in_any(col, val)
            if cond is not None:
                conds.append(cond)
        if min_units:
            conds.append(products.c.qty_available >= int(min_units))
        if min_cases:
            conds.append(products.c.qty_available >= int(min_cases) * cls.SMALLEST_PACK)
        if new_since:
            conds.append(products.c.listed_since >= str(new_since)[:10])
        if featured_only:
            conds.append(products.c.featured_rank.isnot(None))
        for word in (q or "").split():
            like = f"%{word}%"
            conds.append(sa.or_(products.c.sku.ilike(like), products.c.description.ilike(like), products.c.brand.ilike(like),
                                products.c.category.ilike(like), products.c.subcategory.ilike(like)))
        return conds

    def list_products(self, *, brand=None, category=None, subcategory=None,
                      q: str | None = None, sort: str = "default", min_units: int | None = None,
                      min_cases: int | None = None, companies: Iterable[str] | None = None,
                      new_since: str | None = None, featured_only: bool = False,
                      max_price: float | None = None, price_list_id: str | None = None,
                      limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        stmt = sa.select(products).where(*self._product_filter(brand=brand, category=category, subcategory=subcategory,
                                                               q=q, companies=companies, min_units=min_units,
                                                               min_cases=min_cases, new_since=new_since,
                                                               featured_only=featured_only, max_price=max_price,
                                                               price_list_id=price_list_id))
        stmt = stmt.order_by(*self.SORTS.get(sort or "default", self.SORTS["default"])).limit(limit).offset(offset)
        with self.engine.connect() as conn:
            return [_prod(r) for r in conn.execute(stmt).mappings().all()]

    def count_products(self, *, brand=None, category=None, subcategory=None, q: str | None = None,
                       min_units: int | None = None, min_cases: int | None = None,
                       companies: Iterable[str] | None = None, new_since: str | None = None,
                       featured_only: bool = False, max_price: float | None = None,
                       price_list_id: str | None = None) -> int:
        stmt = sa.select(sa.func.count()).select_from(products).where(
            *self._product_filter(brand=brand, category=category, subcategory=subcategory, q=q, companies=companies,
                                  min_units=min_units, min_cases=min_cases, new_since=new_since,
                                  featured_only=featured_only, max_price=max_price,
                                  price_list_id=price_list_id))
        with self.engine.connect() as conn:
            return int(conn.execute(stmt).scalar() or 0)

    def facets(self, *, brand=None, category=None,
               companies: Iterable[str] | None = None,
               price_list_id: str | None = None) -> dict[str, list[Any]]:
        """Filter choices that still return something: categories narrow to the
        chosen brands, subcategories to brands + categories.  A buyer on a price
        list is offered only the brands the SKUs on it are in."""
        base = [products.c.active.is_(True), products.c.qty_available > 0]
        if price_list_id:
            base.append(self._priced_on(price_list_id))
        if companies is not None:
            base.append(sa.or_(products.c.company == "", products.c.company.in_(list(companies))))
        brand_cond = self._in_any(products.c.brand, brand)
        cat_cond = self._in_any(products.c.category, category)
        in_brand = base + ([brand_cond] if brand_cond is not None else [])
        in_cat = in_brand + ([cat_cond] if cat_cond is not None else [])

        def distinct(col, conds, desc=False):
            order = col.desc() if desc else col
            stmt = sa.select(col).where(*conds).distinct().order_by(order)
            with self.engine.connect() as conn:
                return [r[0] for r in conn.execute(stmt) if r[0] not in (None, "")]

        return {"brands": distinct(products.c.brand, base),
                "categories": distinct(products.c.category, in_brand),
                "subcategories": distinct(products.c.subcategory, in_cat)}

    def curated_for(self, customer_id: str, limit: int = 24, companies: Iterable[str] | None = None) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(curation.c.skus_json).where(curation.c.customer_id == str(customer_id))).first()
        if not row:
            return []
        skus = json.loads(row[0])[:limit]
        by = self.products_by_skus(skus)
        allowed = set(companies) if companies is not None else None
        return [by[s] for s in skus if s in by and by[s]["qty_available"] > 0
                and (allowed is None or not by[s]["company"] or by[s]["company"] in allowed)]

    # --- digests --------------------------------------------------------------

    def record_digest(self, kind: str, *, since: str | None, items: int, recipients: int, sent_by: str = "") -> None:
        with self.engine.begin() as conn:
            conn.execute(digest_runs.insert().values(kind=str(kind)[:20], since=(str(since)[:10] if since else None),
                                                     items=int(items), recipients=int(recipients),
                                                     sent_by=str(sent_by or "")[:200], sent_at=now_iso()))

    def last_digest(self, kind: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(digest_runs).where(digest_runs.c.kind == str(kind))
                               .order_by(digest_runs.c.id.desc()).limit(1)).mappings().first()
        return dict(row) if row else None

    # --- images -------------------------------------------------------------

    def image(self, sku: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(images).where(images.c.sku == str(sku))).mappings().first()
        return dict(row) if row else None

    def put_image(self, sku: str, source_url: str, data: bytes | None, content_type: str | None, *, status: str) -> None:
        with self.engine.begin() as conn:
            _upsert(conn, images, [{"sku": str(sku), "source_url": source_url, "content_type": content_type,
                                    "data": data, "status": status, "fetched_at": now_iso()}], "sku")

    def images_needed(self, limit: int = 400) -> list[tuple[str, str]]:
        """Active products with a source URL and no fresh cache row for that URL."""
        stmt = (sa.select(products.c.sku, products.c.image_url)
                .select_from(products.outerjoin(images, images.c.sku == products.c.sku))
                .where(products.c.active.is_(True), products.c.image_url != "",
                       sa.or_(images.c.sku.is_(None), images.c.source_url != products.c.image_url))
                .order_by(products.c.sku).limit(limit))
        with self.engine.connect() as conn:
            return [(r[0], r[1]) for r in conn.execute(stmt)]

    def image_stats(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            ok = conn.execute(sa.select(sa.func.count()).select_from(images).where(images.c.status == "ok")).scalar() or 0
            failed = conn.execute(sa.select(sa.func.count()).select_from(images).where(images.c.status == "failed")).scalar() or 0
            with_url = conn.execute(sa.select(sa.func.count()).select_from(products)
                                    .where(products.c.active.is_(True), products.c.image_url != "")).scalar() or 0
        return {"cached": int(ok), "failed": int(failed), "products_with_image": int(with_url),
                "pending": max(int(with_url) - int(ok) - int(failed), 0)}

    # --- offer drafts ---------------------------------------------------------

    def draft(self, buyer_key: str) -> dict[str, dict[str, Any]]:
        """``{sku: {qty, price}}`` the buyer has typed so far."""
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(carts.c.lines_json).where(carts.c.customer_id == str(buyer_key))).first()
        raw = json.loads(row[0]) if row else {}
        out: dict[str, dict[str, Any]] = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                out[str(k)] = {"qty": int(v.get("qty") or 0), "price": float(v.get("price") or 0.0)}
        return out

    def set_draft(self, buyer_key: str, lines: Mapping[str, Mapping[str, Any]]) -> None:
        clean = {str(k): {"qty": int(v.get("qty") or 0), "price": round(float(v.get("price") or 0.0), 2)}
                 for k, v in lines.items() if int(v.get("qty") or 0) > 0 and float(v.get("price") or 0.0) > 0}
        with self.engine.begin() as conn:
            _upsert(conn, carts, [{"customer_id": str(buyer_key), "lines_json": json.dumps(clean), "updated_at": now_iso()}],
                    "customer_id")

    # --- outbox -------------------------------------------------------------

    # --- behaviour events ---------------------------------------------------

    def record_event(self, kind: str, *, buyer_key: str, buyer_label: str = "", buyer_class: str = "",
                     session_id: str = "", sku: str = "", payload: dict[str, Any] | None = None) -> int:
        """One thing a buyer did. Never raises into a request: a lost event is
        worth less than a broken page."""
        try:
            with self.engine.begin() as conn:
                res = conn.execute(events.insert().values(
                    kind=str(kind)[:40], buyer_key=str(buyer_key)[:96], buyer_label=str(buyer_label or "")[:200],
                    buyer_class=str(buyer_class or "")[:20], session_id=str(session_id or "")[:64],
                    sku=str(sku or "")[:64], payload_json=json.dumps(payload or {}), created_at=now_iso()))
                return int(res.inserted_primary_key[0])
        except Exception:                                  # noqa: BLE001
            log.exception("event %s not recorded", kind)
            return 0

    def events_since(self, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """Cursor pull for AOI: everything above ``after_id``, oldest first."""
        stmt = (sa.select(events).where(events.c.id > int(after_id))
                .order_by(events.c.id).limit(max(1, min(int(limit), 2000))))
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"])
            except (TypeError, ValueError):
                payload = {}
            out.append({"id": r["id"], "kind": r["kind"], "buyer_key": r["buyer_key"], "buyer_label": r["buyer_label"],
                        "buyer_class": r["buyer_class"], "session_id": r["session_id"], "sku": r["sku"],
                        "payload": payload, "created_at": r["created_at"]})
        return out

    def prune_events(self, keep_days: int = 400) -> int:
        """The store is not the archive: AOI keeps the history."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(keep_days))).isoformat(timespec="seconds")
        with self.engine.begin() as conn:
            return int(conn.execute(sa.delete(events).where(events.c.created_at < cutoff)).rowcount or 0)

    def enqueue(self, kind: str, payload: dict[str, Any], customer_id: str | None = None,
                buyer_key: str | None = None) -> int:
        with self.engine.begin() as conn:
            res = conn.execute(outbox.insert().values(kind=kind, customer_id=customer_id, buyer_key=buyer_key,
                                                      payload_json=json.dumps(payload), status="pending", created_at=now_iso()))
            return int(res.inserted_primary_key[0])

    def pull_outbox(self, limit: int = 200) -> list[dict[str, Any]]:
        """Pending + previously pulled-but-unacked items (so a lost response is retried)."""
        now = now_iso()
        with self.engine.begin() as conn:
            rows = conn.execute(sa.select(outbox).where(outbox.c.status.in_(["pending", "pulled"]))
                                .order_by(outbox.c.id).limit(limit)).mappings().all()
            ids = [r["id"] for r in rows]
            if ids:
                conn.execute(sa.update(outbox).where(outbox.c.id.in_(ids)).values(status="pulled", pulled_at=now))
        return [{"id": r["id"], "kind": r["kind"], "customer_id": r["customer_id"],
                 "payload": json.loads(r["payload_json"]), "created_at": r["created_at"]} for r in rows]

    def ack_outbox(self, results: Iterable[dict[str, Any]]) -> int:
        """``[{id, status: acked|rejected, result: {...}}]`` from AOI."""
        n = 0
        now = now_iso()
        with self.engine.begin() as conn:
            for r in results:
                st = "acked" if str(r.get("status", "acked")) != "rejected" else "rejected"
                res = conn.execute(sa.update(outbox).where(outbox.c.id == int(r["id"]), outbox.c.status == "pulled")
                                   .values(status=st, acked_at=now, result_json=json.dumps(r.get("result") or {})))
                n += res.rowcount or 0
        return n

    def outbox_item(self, outbox_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(outbox).where(outbox.c.id == int(outbox_id))).mappings().first()
        if not r:
            return None
        return {"id": r["id"], "kind": r["kind"], "status": r["status"], "created_at": r["created_at"], "buyer_key": r["buyer_key"],
                "customer_id": r["customer_id"], "payload": json.loads(r["payload_json"]),
                "result": json.loads(r["result_json"]) if r["result_json"] else None}

    # --- negotiation rounds (pushed by AOI) -----------------------------------

    def upsert_round(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Store a round from AOI. Any earlier round on the same offer that is
        still open is closed: one live question per thread, and a stale link
        can no longer answer it."""
        now = now_iso()
        tok = str(body["token"])
        offer_id = int(body["offer_ref"])
        kind = str(body.get("kind") or "counter")
        row = {"token": tok, "offer_id": offer_id, "round_id": int(body.get("round_id") or 0), "round_no": int(body.get("round_no") or 0),
               "kind": kind, "thread_status": str(body.get("thread_status") or ""),
               "lines_json": json.dumps([{"sku": str(l.get("sku") or ""), "qty": int(l.get("qty") or 0), "price": float(l.get("price") or 0.0),
                                          "description": str(l.get("description") or ""), "wholesale": float(l.get("wholesale") or 0.0)}
                                         for l in (body.get("lines") or [])]),
               "message": str(body.get("message") or ""), "buyer_email": str(body.get("buyer_email") or "").strip().lower(),
               "company": str(body.get("company") or ""), "created_at": str(body.get("created_at") or now), "received_at": now,
               "status": "open" if kind == "counter" else "closed"}
        with self.engine.begin() as conn:
            conn.execute(sa.update(rounds).where(rounds.c.offer_id == offer_id, rounds.c.token != tok, rounds.c.status == "open")
                         .values(status="closed"))
            existing = conn.execute(sa.select(rounds.c.status).where(rounds.c.token == tok)).first()
            if existing:
                # a re-push never reopens a round the buyer already answered
                row["status"] = existing[0] if existing[0] != "open" else row["status"]
            _upsert(conn, rounds, [row], "token")
        return self.round(tok)

    def round(self, token: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(rounds).where(rounds.c.token == str(token))).mappings().first()
        return _round(r) if r else None

    def rounds_for_offer(self, offer_id: int) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rs = conn.execute(sa.select(rounds).where(rounds.c.offer_id == int(offer_id)).order_by(rounds.c.round_no, rounds.c.received_at)).mappings().all()
        return [_round(r) for r in rs]

    def latest_round_for_offer(self, offer_id: int) -> dict[str, Any] | None:
        rs = self.rounds_for_offer(offer_id)
        return rs[-1] if rs else None

    def mark_round_opened(self, token: str) -> None:
        """First open of the buyer's link; later opens keep the first time."""
        with self.engine.begin() as conn:
            conn.execute(sa.update(rounds).where(rounds.c.token == str(token), rounds.c.opened_at.is_(None)).values(opened_at=now_iso()))

    def respond_round(self, token: str, response: Mapping[str, Any]) -> bool:
        """Mark an open round answered. False when it was not open (double click, stale link)."""
        with self.engine.begin() as conn:
            res = conn.execute(sa.update(rounds).where(rounds.c.token == str(token), rounds.c.status == "open")
                               .values(status="responded", response_json=json.dumps(dict(response)), responded_at=now_iso()))
            return bool(res.rowcount)

    def outbox_for(self, buyer_key: str, limit: int = 20) -> list[dict[str, Any]]:
        """What this buyer submitted (offers, and any older orders under a customer id)."""
        key = str(buyer_key)
        conds = [outbox.c.buyer_key == key]
        if key.startswith("cust:"):
            conds.append(outbox.c.customer_id == key[5:])
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(outbox).where(sa.or_(*conds))
                                .order_by(outbox.c.id.desc()).limit(limit)).mappings().all()
        return [{"id": r["id"], "kind": r["kind"], "status": r["status"], "created_at": r["created_at"],
                 "payload": json.loads(r["payload_json"]), "result": json.loads(r["result_json"]) if r["result_json"] else None}
                for r in rows]


def _round(r) -> dict[str, Any]:
    d = dict(r)
    try:
        d["lines"] = json.loads(d.pop("lines_json") or "[]")
    except (TypeError, ValueError):
        d["lines"] = []
    d["total"] = round(sum(l["qty"] * l["price"] for l in d["lines"]), 2)
    try:
        d["response"] = json.loads(d.pop("response_json") or "null")
    except (TypeError, ValueError):
        d["response"] = None
    return d


# MSRP: what the item sold for at full price -- the wholesale the buyer can
# already see, marked up at the margin a normal retailer takes, landed on a .99
# price point the way a shelf tag is (JJ, 2026-09-03). It is arithmetic on a
# number already on the page, so it discloses nothing new. Its job is context:
# this is what it was, and this is what a full-price retailer paid for it.
RETAIL_MARGIN = 0.60


def msrp_price(wholesale: float, margin: float = RETAIL_MARGIN) -> float:
    """Wholesale at ``margin`` gross margin, rounded to the nearest $X.99."""
    try:
        w = float(wholesale or 0.0)
    except (TypeError, ValueError):
        return 0.0
    m = float(margin)
    if w <= 0 or not (0.0 < m < 1.0):
        return 0.0
    raw = w / (1.0 - m)
    return round(round(raw + 0.01) - 0.01, 2)          # 762.50 -> 762.99, 762.05 -> 761.99


def _prod(r) -> dict[str, Any]:
    d = dict(r)
    for k in ("wholesale", "closeout_price", "next_step_price", "price_was",
              "original_price", "published_price", "base_price"):
        if d.get(k) is not None:
            d[k] = float(d[k])
    # An older feed sends neither: the base price is the only anchor there is.
    if not d.get("original_price"):
        d["original_price"] = d.get("wholesale") or 0.0
    if not d.get("base_price"):
        d["base_price"] = d.get("wholesale") or 0.0
    # True when NetSuite's Base Price is a markdown against the list price the
    # sheet anchors on -- so the website charges less today than the anchor here
    # implies.  Reads correctly from either feed shape: current AOI sends the
    # anchor as `wholesale`, an older one sent the base there and the list in
    # `original_price`.  Internal: nothing shows it to a buyer.
    d["marked_down"] = bool(max(d["original_price"], d.get("wholesale") or 0.0)
                            > d["base_price"] * 1.005)
    d["msrp"] = msrp_price(d.get("wholesale") or 0.0)
    # Depth in the unit a buyer thinks in: the smallest lot they can take.
    sp = int(d.get("inner_pack") or 0) or int(d.get("case_pack") or 0) or 1
    d["smallest_pack"] = sp
    d["cases_available"] = int(d.get("qty_available") or 0) // sp
    d["order_unit"], d["unit_label"] = order_unit(d)
    return d


COMPANY_LABELS = {"gerson": "Gerson", "park_hill": "Park Hill"}
ALL_COMPANIES = tuple(COMPANY_LABELS)


def _cust(row) -> dict[str, Any]:
    """Customer row with ``accounts`` ({company: customer id}) and ``companies``
    (which subsidiaries' products they may buy).  A feed without accounts -
    an older AOI - means every company under the login id."""
    d = dict(row)
    try:
        accounts = {str(k): str(v) for k, v in (json.loads(d.get("accounts_json") or "{}") or {}).items()}
    except (TypeError, ValueError):
        accounts = {}
    if not accounts:
        accounts = {c: d["customer_id"] for c in ALL_COMPANIES}
    d["accounts"] = accounts
    d["companies"] = [c for c in ALL_COMPANIES if c in accounts] or list(accounts)
    d["price_list_id"] = clean_list_id(d.get("price_list_id"))
    return d


def order_unit(p: Mapping[str, Any]) -> tuple[int, str]:
    """How many units one click buys.  Whole master cartons while at least one
    is left; then inner packs; then the NetSuite minimum (JJ, 2026-09-02)."""
    master = int(p.get("master_pack") or 0)
    inner = int(p.get("inner_pack") or 0)
    cp = max(int(p.get("case_pack") or 1), 1)
    avail = int(p.get("qty_available") or 0)
    if master > 0 and avail >= master:
        return master, f"master carton of {master}"
    if inner > 0:
        return inner, f"inner pack of {inner}" + (" (fewer than a master carton left)" if master > 0 else "")
    return cp, f"case of {cp}"


def _ensure_columns(eng: Engine) -> None:
    """Add columns introduced after a table already existed (create_all never
    alters).  Idempotent; SQLite and Postgres both accept plain ADD COLUMN."""
    insp = sa.inspect(eng)
    wanted = {"products": {"master_pack": "INTEGER NOT NULL DEFAULT 0", "inner_pack": "INTEGER NOT NULL DEFAULT 0",
                           "featured_rank": "INTEGER",
                           "has_video": "BOOLEAN NOT NULL DEFAULT FALSE",
                           "company": "VARCHAR(20) NOT NULL DEFAULT ''",
                           "listed_since": "VARCHAR(10)", "price_changed_at": "VARCHAR(10)", "price_was": "NUMERIC(12,2)",
                           "original_price": "NUMERIC(12,2) NOT NULL DEFAULT 0",
                           "base_price": "NUMERIC(12,2) NOT NULL DEFAULT 0",
                           "published_price": "NUMERIC(12,2) NOT NULL DEFAULT 0",
                           "published_basis": "VARCHAR(20) NOT NULL DEFAULT ''",
                           "published_label": "VARCHAR(40) NOT NULL DEFAULT ''",
                           "published_disc_pct": "INTEGER NOT NULL DEFAULT 0"},
              "customers": {"accounts_json": "TEXT NOT NULL DEFAULT '{}'",
                            "price_list_id": "VARCHAR(64)"},
              "outbox": {"buyer_key": "VARCHAR(96)"},
              "login_tokens": {"subject": "VARCHAR(200)"},
              "rounds": {"opened_at": "VARCHAR(32)"},
              "buyers": {"buyer_class": "VARCHAR(20) NOT NULL DEFAULT 'regional'",
                         "price_list_id": "VARCHAR(64)"},
              "invites": {"buyer_class": "VARCHAR(20) NOT NULL DEFAULT 'regional'"}}
    with eng.begin() as conn:
        for table, cols in wanted.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
