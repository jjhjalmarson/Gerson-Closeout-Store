"""Schema and data access. SQLAlchemy Core so tests run on SQLite and
production runs on Postgres with the same code.

The schema is the *entire* knowledge of the store. Compare it with the AOI
`closeout_prices` table: no cost, no dates of receipt, no bank fields.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

import sqlalchemy as sa
from sqlalchemy.engine import Engine

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
    sa.Column("closeout_price", sa.Numeric(12, 2), nullable=False, default=0),
    sa.Column("discount_pct", sa.Integer, nullable=False, default=0),
    sa.Column("next_step_date", sa.String(10)),
    sa.Column("next_step_price", sa.Numeric(12, 2)),
    sa.Column("qty_available", sa.Integer, nullable=False, default=0),
    sa.Column("lot", sa.String(200), default=""),
    sa.Column("ship_by", sa.String(10)),
    sa.Column("active", sa.Boolean, nullable=False, default=True),
    sa.Column("updated_at", sa.String(32), nullable=False),
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
    sa.Column("active", sa.Boolean, nullable=False, default=True),
    sa.Column("updated_at", sa.String(32), nullable=False),
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
    sa.Column("customer_id", sa.String(32), nullable=False),
    sa.Column("expires_at", sa.String(32), nullable=False),
    sa.Column("used_at", sa.String(32)),
)

carts = sa.Table(
    "carts", metadata,
    sa.Column("customer_id", sa.String(32), primary_key=True),
    sa.Column("lines_json", sa.Text, nullable=False, default="{}"),
    sa.Column("updated_at", sa.String(32), nullable=False),
)

# Everything the store hands back to AOI. kind: order | offer | application | hold.
outbox = sa.Table(
    "outbox", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("kind", sa.String(20), nullable=False, index=True),
    sa.Column("customer_id", sa.String(32)),
    sa.Column("payload_json", sa.Text, nullable=False),
    sa.Column("status", sa.String(20), nullable=False, default="pending", index=True),
    sa.Column("created_at", sa.String(32), nullable=False),
    sa.Column("pulled_at", sa.String(32)),
    sa.Column("acked_at", sa.String(32)),
    sa.Column("result_json", sa.Text),
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
        rows = []
        for it in items:
            rows.append({
                "sku": str(it["sku"]), "internal_id": str(it.get("internal_id") or ""),
                "description": str(it.get("description") or it["sku"]), "image_url": str(it.get("image_url") or ""),
                "brand": str(it.get("brand") or ""), "category": str(it.get("category") or ""),
                "subcategory": str(it.get("subcategory") or ""), "season": str(it.get("season") or ""),
                "company": str(it.get("company") or ""),
                "case_pack": max(int(it.get("case_pack") or 1), 1), "upc": str(it.get("upc") or ""),
                "master_pack": max(int(float(it.get("master_pack") or 0)), 0), "inner_pack": max(int(float(it.get("inner_pack") or 0)), 0),
                "wholesale": float(it.get("wholesale") or 0), "closeout_price": float(it.get("closeout_price") or 0),
                "discount_pct": int(it.get("discount_pct") or 0), "next_step_date": it.get("next_step_date"),
                "next_step_price": it.get("next_step_price"), "qty_available": int(it.get("qty_available") or 0),
                "lot": str(it.get("lot") or ""), "ship_by": it.get("ship_by"), "active": True, "updated_at": now,
            })
        with self.engine.begin() as conn:
            # Full snapshot: anything not in this feed is no longer for sale.
            conn.execute(sa.update(products).values(active=False))
            _upsert(conn, products, rows, "sku")
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

    def create_login_token(self, email: str, customer_id: str, minutes: int) -> str:
        token = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        with self.engine.begin() as conn:
            conn.execute(login_tokens.insert().values(token=token, email=email.strip().lower(),
                                                      customer_id=str(customer_id), expires_at=exp))
        return token

    def redeem_login_token(self, token: str) -> dict[str, Any] | None:
        """Single use, unexpired. Returns the customer row or None."""
        now = now_iso()
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(login_tokens).where(login_tokens.c.token == token)).mappings().first()
            if not row or row["used_at"] or row["expires_at"] < now:
                return None
            conn.execute(sa.update(login_tokens).where(login_tokens.c.token == token).values(used_at=now))
        return self.customer(row["customer_id"])

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

    def products_by_skus(self, skus: Iterable[str]) -> dict[str, dict[str, Any]]:
        wanted = [str(s) for s in skus]
        if not wanted:
            return {}
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(products).where(products.c.sku.in_(wanted), products.c.active.is_(True))).mappings().all()
        return {r["sku"]: _prod(r) for r in rows}

    SORTS = {
        "default": (products.c.brand, products.c.category, products.c.sku),
        "discount": (products.c.discount_pct.desc(), products.c.brand, products.c.sku),
        "price_asc": (products.c.closeout_price.asc(), products.c.sku),
        "price_desc": (products.c.closeout_price.desc(), products.c.sku),
        "qty": (products.c.qty_available.desc(), products.c.sku),
        "step": (sa.case((products.c.next_step_date.is_(None), 1), else_=0), products.c.next_step_date, products.c.sku),
    }

    @staticmethod
    def _product_filter(*, brand=None, category=None, subcategory=None, discount=None, q=None, companies=None):
        """Every word of ``q`` must appear somewhere in SKU, description, brand,
        category or subcategory; ``discount`` is the exact ladder tier (20/33/50);
        ``companies`` limits to the subsidiaries the buyer holds an account with."""
        conds = [products.c.active.is_(True), products.c.qty_available > 0]
        if companies is not None:
            conds.append(sa.or_(products.c.company == "", products.c.company.in_(list(companies))))
        if brand:
            conds.append(products.c.brand == brand)
        if category:
            conds.append(products.c.category == category)
        if subcategory:
            conds.append(products.c.subcategory == subcategory)
        if discount is not None and str(discount).strip():
            try:
                conds.append(products.c.discount_pct == int(str(discount).strip().rstrip("%")))
            except ValueError:
                pass
        for word in (q or "").split():
            like = f"%{word}%"
            conds.append(sa.or_(products.c.sku.ilike(like), products.c.description.ilike(like), products.c.brand.ilike(like),
                                products.c.category.ilike(like), products.c.subcategory.ilike(like)))
        return conds

    def list_products(self, *, brand: str | None = None, category: str | None = None, subcategory: str | None = None,
                      discount: Any = None, q: str | None = None, sort: str = "default",
                      companies: Iterable[str] | None = None, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        stmt = sa.select(products).where(*self._product_filter(brand=brand, category=category, subcategory=subcategory,
                                                                discount=discount, q=q, companies=companies))
        stmt = stmt.order_by(*self.SORTS.get(sort or "default", self.SORTS["default"])).limit(limit).offset(offset)
        with self.engine.connect() as conn:
            return [_prod(r) for r in conn.execute(stmt).mappings().all()]

    def count_products(self, *, brand: str | None = None, category: str | None = None, subcategory: str | None = None,
                       discount: Any = None, q: str | None = None, companies: Iterable[str] | None = None) -> int:
        stmt = sa.select(sa.func.count()).select_from(products).where(
            *self._product_filter(brand=brand, category=category, subcategory=subcategory, discount=discount, q=q,
                                  companies=companies))
        with self.engine.connect() as conn:
            return int(conn.execute(stmt).scalar() or 0)

    def facets(self, *, brand: str | None = None, category: str | None = None,
               companies: Iterable[str] | None = None) -> dict[str, list[Any]]:
        """Filter choices that still return something: categories narrow to the
        chosen brand, subcategories and discount tiers to brand + category."""
        base = [products.c.active.is_(True), products.c.qty_available > 0]
        if companies is not None:
            base.append(sa.or_(products.c.company == "", products.c.company.in_(list(companies))))
        in_brand = base + ([products.c.brand == brand] if brand else [])
        in_cat = in_brand + ([products.c.category == category] if category else [])

        def distinct(col, conds, desc=False):
            order = col.desc() if desc else col
            stmt = sa.select(col).where(*conds).distinct().order_by(order)
            with self.engine.connect() as conn:
                return [r[0] for r in conn.execute(stmt) if r[0] not in (None, "")]

        return {"brands": distinct(products.c.brand, base),
                "categories": distinct(products.c.category, in_brand),
                "subcategories": distinct(products.c.subcategory, in_cat),
                "discounts": distinct(products.c.discount_pct, in_cat, desc=True)}

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

    # --- cart ---------------------------------------------------------------

    def cart(self, customer_id: str) -> dict[str, int]:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(carts.c.lines_json).where(carts.c.customer_id == str(customer_id))).first()
        return {k: int(v) for k, v in (json.loads(row[0]) if row else {}).items()}

    def set_cart(self, customer_id: str, lines: dict[str, int]) -> None:
        clean = {str(k): int(v) for k, v in lines.items() if int(v) > 0}
        with self.engine.begin() as conn:
            _upsert(conn, carts, [{"customer_id": str(customer_id), "lines_json": json.dumps(clean), "updated_at": now_iso()}],
                    "customer_id")

    # --- outbox -------------------------------------------------------------

    def enqueue(self, kind: str, payload: dict[str, Any], customer_id: str | None = None) -> int:
        with self.engine.begin() as conn:
            res = conn.execute(outbox.insert().values(kind=kind, customer_id=customer_id, payload_json=json.dumps(payload),
                                                      status="pending", created_at=now_iso()))
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

    def outbox_for_customer(self, customer_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(outbox).where(outbox.c.customer_id == str(customer_id))
                                .order_by(outbox.c.id.desc()).limit(limit)).mappings().all()
        return [{"id": r["id"], "kind": r["kind"], "status": r["status"], "created_at": r["created_at"],
                 "payload": json.loads(r["payload_json"]), "result": json.loads(r["result_json"]) if r["result_json"] else None}
                for r in rows]


def _prod(r) -> dict[str, Any]:
    d = dict(r)
    for k in ("wholesale", "closeout_price", "next_step_price"):
        if d.get(k) is not None:
            d[k] = float(d[k])
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
                           "company": "VARCHAR(20) NOT NULL DEFAULT ''"},
              "customers": {"accounts_json": "TEXT NOT NULL DEFAULT '{}'"}}
    with eng.begin() as conn:
        for table, cols in wanted.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
