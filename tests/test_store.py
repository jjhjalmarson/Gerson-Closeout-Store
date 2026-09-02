"""Gerson Closeout Store — ingest gate, isolation, login, cart math, outbox."""
import re
import unittest
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app import create_app
from config import Config
from store import db as D

KEY = "store-key-123"


def _cfg() -> Config:
    return Config(secret_key="test-secret", store_ingest_key=KEY, database_url="sqlite://", base_url="http://store.test",
                  mail_backend="log", smtp_host="", smtp_port=587, smtp_user="", smtp_password="",
                  mail_from="x@y", login_token_minutes=30, graph_tenant_id="", graph_client_id="",
                  graph_client_secret="", graph_sender_mailbox="")


CATALOG = {"kind": "catalog", "version": 1, "as_of": "2026-09-01", "generated_at": "2026-09-01T07:30:00+00:00", "count": 2, "items": [
    {"sku": "L1", "internal_id": "1", "description": "Lantern", "image_url": "https://img/l1.jpg", "brand": "Fall/Holiday",
     "category": "Christmas", "subcategory": "Ornaments", "season": "Fall-Christmas 2024", "case_pack": 6, "upc": "1",
     "wholesale": 25.0, "closeout_price": 20.0, "discount_pct": 20, "next_step_date": "2027-01-17", "next_step_price": 16.25,
     "qty_available": 96, "lot": "Fall/Holiday | Christmas | Ornaments", "ship_by": "2026-12-15"},
    {"sku": "T2", "internal_id": "2", "description": "Tree", "image_url": "", "brand": "Park Hill Collection", "category": "Decor",
     "subcategory": "", "season": "", "case_pack": 4, "upc": "", "wholesale": 100.0, "closeout_price": 30.0, "discount_pct": 70,
     "next_step_date": None, "next_step_price": None, "qty_available": 8, "lot": "PH | Decor", "ship_by": None},
]}
CUSTOMERS = {"kind": "customers", "as_of": "2026-09-01", "count": 1, "items": [
    {"customer_id": "26003", "entity_id": "1FASACA", "company_name": "Adeline Collective", "emails": ["donna-n@live.com", "buyer@shop.com"],
     "buyer_class": "independent", "volume_tier": "B", "rep_name": "Brooks Mickel", "house_account": False}]}
CURATION = {"kind": "curation", "as_of": "2026-09-01", "count": 1, "items": [{"customer_id": "26003", "skus": ["T2", "L1", "ZZZ"]}]}


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_cfg())
        self.app.config["TESTING"] = True          # no background image fetches in tests
        self.client = self.app.test_client()
        self.store = self.app.config["STORE"].store

    def ingest(self, kind, body, key=KEY):
        return self.client.post(f"/ingest/{kind}", json=body, headers={"X-API-Key": key} if key else {})

    def login(self, email="donna-n@live.com"):
        self.client.post("/login", data={"email": email})
        with self.store.engine.connect() as conn:
            tok = conn.execute(sa.select(D.login_tokens.c.token).order_by(D.login_tokens.c.expires_at.desc())).first()[0]
        return self.client.get(f"/login/{tok}", follow_redirects=False)

    def seed(self):
        self.ingest("catalog", CATALOG); self.ingest("customers", CUSTOMERS); self.ingest("curation", CURATION)


class IngestGateTest(StoreTestCase):
    def test_requires_the_stores_key(self):
        self.assertEqual(self.ingest("catalog", CATALOG, key=None).status_code, 404)
        self.assertEqual(self.ingest("catalog", CATALOG, key="wrong").status_code, 404)
        self.assertEqual(self.client.get("/outbox").status_code, 404)
        self.assertEqual(self.client.post("/outbox/ack", json={"results": []}).status_code, 404)

    def test_blank_key_disables_ingest_entirely(self):
        cfg = _cfg().__class__(**{**_cfg().__dict__, "store_ingest_key": ""})
        app = create_app(cfg)
        r = app.test_client().post("/ingest/catalog", json=CATALOG, headers={"X-API-Key": ""})
        self.assertEqual(r.status_code, 404)

    def test_accepts_and_snapshots(self):
        r = self.ingest("catalog", CATALOG)
        self.assertEqual((r.status_code, r.get_json()["count"]), (202, 2))
        self.assertEqual(self.store.product("L1")["closeout_price"], 20.0)
        # second snapshot without T2 deactivates it
        self.ingest("catalog", {**CATALOG, "items": CATALOG["items"][:1]})
        self.assertIsNone(self.store.product("T2"))
        self.assertIsNotNone(self.store.product("L1"))
        st = {f["kind"]: f for f in self.store.feed_status()}
        self.assertEqual(st["catalog"]["count"], 1)
        self.assertEqual(st["catalog"]["as_of"], "2026-09-01")

    def test_refuses_sensitive_payloads(self):
        dirty = {**CATALOG, "items": [{**CATALOG["items"][0], "avg_cost": 9.0}]}
        r = self.ingest("catalog", dirty)
        self.assertEqual(r.status_code, 422)
        self.assertIn("avg_cost", r.get_json()["error"])
        self.assertIsNone(self.store.product("L1"))                       # nothing written
        r = self.ingest("customers", {**CUSTOMERS, "items": [{**CUSTOMERS["items"][0], "Bucket": "x"}]})
        self.assertEqual(r.status_code, 422)

    def test_refuses_empty_customers_feed(self):
        self.seed()
        r = self.ingest("customers", {**CUSTOMERS, "count": 0, "items": []})
        self.assertEqual(r.status_code, 409)
        self.assertIsNotNone(self.store.customer_for_email("donna-n@live.com"))   # allowlist untouched
        self.assertEqual(self.ingest("curation", {**CURATION, "count": 0, "items": []}).status_code, 202)

    def test_bad_kind_and_body(self):
        self.assertEqual(self.ingest("prices", CATALOG).status_code, 400)
        self.assertEqual(self.ingest("catalog", {"items": "nope"}).status_code, 400)

    def test_schema_has_no_sensitive_columns(self):
        cols = {c.name for t in D.metadata.tables.values() for c in t.columns}
        for bad in ("avg_cost", "ats_cost", "receipt_date", "days", "bucket", "adv_rate", "floor_independent", "tier", "capacity_now"):
            self.assertNotIn(bad, cols)

    def test_healthz_and_robots(self):
        self.assertEqual(self.client.get("/healthz").get_json()["ok"], True)
        self.assertIn("Disallow: /", self.client.get("/robots.txt").get_data(as_text=True))
        self.assertEqual(self.client.get("/login").headers["X-Robots-Tag"], "noindex, nofollow")


class LoginTest(StoreTestCase):
    def test_gated_redirects(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        self.assertEqual(self.client.get("/item/L1").status_code, 302)

    def test_magic_link_only_for_allowlisted_emails(self):
        self.seed()
        r = self.client.post("/login", data={"email": "stranger@nowhere.com"})
        self.assertEqual(r.status_code, 200)                                # same page, no enumeration
        with self.store.engine.connect() as conn:
            self.assertEqual(conn.execute(sa.select(sa.func.count()).select_from(D.login_tokens)).scalar(), 0)
        r = self.login("BUYER@shop.com")                                    # case-insensitive
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/"))
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("Adeline Collective", home.get_data(as_text=True))
        self.assertIn("Brooks Mickel", home.get_data(as_text=True))

    def test_token_single_use_and_expiry(self):
        self.seed()
        self.client.post("/login", data={"email": "donna-n@live.com"})
        with self.store.engine.connect() as conn:
            tok = conn.execute(sa.select(D.login_tokens.c.token)).first()[0]
        self.assertEqual(self.client.get(f"/login/{tok}").status_code, 302)
        self.client.post("/logout")
        r = self.client.get(f"/login/{tok}", follow_redirects=True)
        self.assertIn("expired or was already used", r.get_data(as_text=True))
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        with self.store.engine.begin() as conn:
            conn.execute(D.login_tokens.insert().values(token="old", email="donna-n@live.com", customer_id="26003", expires_at=past))
        self.assertIsNone(self.store.redeem_login_token("old"))

    def test_deactivated_customer_is_logged_out(self):
        self.seed()
        self.login()
        other = {**CUSTOMERS["items"][0], "customer_id": "999", "emails": ["someone@else.com"]}
        self.ingest("customers", {**CUSTOMERS, "items": [other]})          # allowlist snapshot without them
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)

    def test_application_lands_in_outbox(self):
        r = self.client.post("/apply", data={"company": "New Shop", "email": "owner@newshop.com", "resale_number": "TX-1"})
        self.assertEqual(r.status_code, 200)
        items = self.store.pull_outbox()
        self.assertEqual(items[0]["kind"], "application")
        self.assertEqual(items[0]["payload"]["company"], "New Shop")
        self.assertIsNone(items[0]["customer_id"])
        self.assertEqual(self.client.post("/apply", data={"company": "", "email": "bad"}).status_code, 400)


class CatalogAndCartTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.login()

    def test_curated_first_and_filters(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Picked for Adeline Collective", html)
        self.assertLess(html.index("T2"), html.index("Lantern"))            # curated order: T2 before L1
        self.assertIn("Drops to $16.25 on 2027-01-17", html)
        self.assertIn("70% off", html)
        html = self.client.get("/?brand=Park+Hill+Collection").get_data(as_text=True)
        self.assertIn("Tree", html)
        self.assertNotIn("Lantern", html)
        self.assertNotIn("Picked for", html)                                # no curation block when filtering
        self.assertEqual(self.client.get("/item/NOPE").status_code, 404)
        self.assertIn("UPC 1", self.client.get("/item/L1").get_data(as_text=True))

    def test_filters_search_and_paging(self):
        html = self.client.get("/?discount=70").get_data(as_text=True)
        self.assertIn("T2", html); self.assertNotIn("L1", html)                    # exact ladder tier
        html = self.client.get("/?q=park+tree").get_data(as_text=True)              # every word, any field
        self.assertIn("T2", html); self.assertNotIn(">L1<", html)
        self.assertIn("Nothing matches", self.client.get("/?q=park+lantern").get_data(as_text=True))
        f = self.store.facets(brand="Park Hill Collection")
        self.assertEqual((f["categories"], f["discounts"]), (["Decor"], [70]))      # narrowed to the brand
        self.assertEqual(self.store.count_products(brand="Fall/Holiday"), 1)
        # paging: 60 per page, next/previous links carry the filters
        from store import shop
        skus = [{**CATALOG["items"][0], "sku": f"P{i:03d}", "internal_id": str(100 + i)} for i in range(shop.PAGE_SIZE + 5)]
        self.ingest("catalog", {**CATALOG, "items": CATALOG["items"] + skus})
        html = self.client.get("/?brand=Fall%2FHoliday").get_data(as_text=True)
        self.assertIn("page 1 of 2", html); self.assertIn("page=2", html)
        html2 = self.client.get("/?brand=Fall%2FHoliday&page=2").get_data(as_text=True)
        self.assertIn("Previous", html2); self.assertNotIn("Next", html2)
        self.assertEqual(self.client.get("/?page=99").status_code, 200)             # clamps, no error
        self.assertEqual([p["sku"] for p in self.store.list_products(sort="discount", limit=1)], ["T2"])

    def test_cart_snaps_to_cases_and_caps_at_available(self):
        self.client.post("/cart/add", data={"sku": "L1", "qty": "7"})       # -> 6
        self.assertEqual(self.store.cart("26003"), {"L1": 6})
        self.client.post("/cart/add", data={"sku": "L1", "qty": "1000"})    # -> capped at 96
        self.assertEqual(self.store.cart("26003"), {"L1": 96})
        self.client.post("/cart/add", data={"sku": "T2", "qty": "abc"})     # bad qty -> nothing added
        self.assertNotIn("T2", self.store.cart("26003"))
        self.client.post("/cart/set", data={"qty[L1]": "0"})
        self.assertEqual(self.store.cart("26003"), {})
        self.assertEqual(self.client.post("/cart/add", data={"sku": "NOPE", "qty": "1"}).status_code, 404)

    def test_checkout_reprices_from_catalog_and_enqueues(self):
        self.client.post("/cart/add", data={"sku": "L1", "qty": "12"})
        self.client.post("/cart/add", data={"sku": "T2", "qty": "4"})
        # price change lands between cart and checkout: checkout uses the current catalog price
        self.ingest("catalog", {**CATALOG, "items": [{**CATALOG["items"][0], "closeout_price": 18.0}, CATALOG["items"][1]]})
        r = self.client.post("/checkout", data={"po_number": "PO-77", "notes": "ring bell", "ship_date": "2026-10-01"})
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("CO-1", html)
        self.assertIn("$336.00", html)                                       # 12 x 18 + 4 x 30
        items = self.store.pull_outbox()
        o = items[0]
        self.assertEqual(o["kind"], "order")
        self.assertEqual(o["customer_id"], "26003")
        self.assertEqual(o["payload"]["po_number"], "PO-77")
        self.assertEqual(o["payload"]["lines"], [{"sku": "L1", "qty": 12, "unit_price": 18.0}, {"sku": "T2", "qty": 4, "unit_price": 30.0}])
        self.assertEqual(o["payload"]["total"], 336.0)
        self.assertEqual(self.store.cart("26003"), {})
        # empty cart cannot check out
        r = self.client.post("/checkout", follow_redirects=True)
        self.assertIn("Your cart is empty", r.get_data(as_text=True))

    def test_orders_page_reflects_ack(self):
        self.client.post("/cart/add", data={"sku": "T2", "qty": "4"})
        self.client.post("/checkout")
        items = self.store.pull_outbox()
        self.store.ack_outbox([{"id": items[0]["id"], "status": "acked", "result": {"tranid": "SO123456"}}])
        html = self.client.get("/orders").get_data(as_text=True)
        self.assertIn("confirmed", html)
        self.assertIn("SO123456", html)


class OutboxProtocolTest(StoreTestCase):
    def test_pull_marks_pulled_and_retries_until_acked(self):
        self.store.enqueue("order", {"total": 1}, "26003")
        self.store.enqueue("offer", {"sku": "L1"}, "26003")
        r = self.client.get("/outbox?limit=1", headers={"X-API-Key": KEY}).get_json()
        self.assertEqual(r["count"], 1)
        first = r["items"][0]["id"]
        r = self.client.get("/outbox", headers={"X-API-Key": KEY}).get_json()
        self.assertEqual([i["id"] for i in r["items"]], [first, first + 1])      # unacked item comes back
        r = self.client.post("/outbox/ack", json={"results": [{"id": first, "status": "acked", "result": {"tranid": "SO1"}},
                                                              {"id": first + 1, "status": "rejected", "result": {"message": "below floor"}}]},
                             headers={"X-API-Key": KEY}).get_json()
        self.assertEqual(r["acked"], 2)
        self.assertEqual(self.client.get("/outbox", headers={"X-API-Key": KEY}).get_json()["count"], 0)
        rows = self.store.outbox_for_customer("26003")
        self.assertEqual({x["status"] for x in rows}, {"acked", "rejected"})
        # acking an item that was never pulled is ignored
        self.store.enqueue("order", {"total": 2}, "26003")
        self.assertEqual(self.store.ack_outbox([{"id": 3, "status": "acked"}]), 0)


class CartMathTest(unittest.TestCase):
    def test_snap(self):
        from store.shop import _snap_qty
        self.assertEqual(_snap_qty(7, 6, 96), 6)
        self.assertEqual(_snap_qty(1000, 6, 96), 96)
        self.assertEqual(_snap_qty(5, 6, 96), 0)
        self.assertEqual(_snap_qty(10, 4, 8), 8)
        self.assertEqual(_snap_qty(-3, 6, 96), 0)
        self.assertEqual(_snap_qty(3, 0, 10), 3)


if __name__ == "__main__":
    unittest.main()
