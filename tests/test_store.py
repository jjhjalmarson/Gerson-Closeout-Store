"""Gerson Closeout Offers — ingest gate, isolation, invites, login, the offer sheet, outbox."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import sqlalchemy as sa

from app import create_app
from config import Config
from store import db as D
from store.db import msrp_price

KEY = "store-key-123"


def _cfg(**over) -> Config:
    base = dict(secret_key="test-secret", store_ingest_key=KEY, database_url="sqlite://", base_url="http://store.test",
                mail_backend="log", smtp_host="", smtp_port=587, smtp_user="", smtp_password="",
                mail_from="x@y", login_token_minutes=30, graph_tenant_id="", graph_client_id="",
                graph_client_secret="", graph_sender_mailbox="", offer_notify_emails="offers@gerson.test; jj@gerson.test",
                website_url="https://shop.gerson.test", admin_emails="Admin@Gerson.test; boss@gerson.test")
    base.update(over)
    return Config(**base)


CATALOG = {"kind": "catalog", "version": 1, "as_of": "2026-09-01", "generated_at": "2026-09-01T07:30:00+00:00", "count": 2, "items": [
    {"sku": "L1", "internal_id": "1", "description": "Lantern", "image_url": "https://img/l1.jpg", "brand": "Fall/Holiday",
     "category": "Christmas", "subcategory": "Ornaments", "season": "Fall-Christmas 2024", "case_pack": 6, "upc": "1",
     "wholesale": 25.0, "closeout_price": 20.0, "discount_pct": 20, "next_step_date": "2027-01-17", "next_step_price": 16.25,
     "qty_available": 96, "lot": "Fall/Holiday | Christmas | Ornaments", "ship_by": None, "master_pack": 24, "inner_pack": 6,
     "company": "gerson"},
    {"sku": "T2", "internal_id": "2", "description": "Tree", "image_url": "", "brand": "Park Hill Collection", "category": "Decor",
     "subcategory": "", "season": "", "case_pack": 4, "upc": "", "wholesale": 100.0, "closeout_price": 30.0, "discount_pct": 70,
     "next_step_date": None, "next_step_price": None, "qty_available": 40, "lot": "PH | Decor", "ship_by": None, "company": "park_hill"},
]}
CUSTOMERS = {"kind": "customers", "as_of": "2026-09-01", "count": 1, "items": [
    {"customer_id": "26003", "entity_id": "1FASACA", "company_name": "Adeline Collective", "emails": ["donna-n@live.com", "buyer@shop.com"],
     "buyer_class": "regional", "volume_tier": "B", "rep_name": "Brooks Mickel", "house_account": False,
     "accounts": {"gerson": "26003", "park_hill": "26777"}}]}
GERSON_ONLY = {**CUSTOMERS, "items": [{**CUSTOMERS["items"][0], "accounts": {"gerson": "26003"}}]}
CURATION = {"kind": "curation", "as_of": "2026-09-01", "count": 1, "items": [{"customer_id": "26003", "skus": ["T2", "L1", "ZZZ"]}]}
INVITES = {"kind": "invites", "as_of": "2026-09-02", "count": 2, "items": [
    {"token": "tjx-abc", "label": "TJX", "contact": "Pat Buyer", "email": "pat@tjx.test", "companies": ["gerson"], "expires_at": None},
    {"token": "ross-xyz", "label": "Ross Stores", "contact": "", "email": "", "companies": [], "expires_at": "2099-01-01"},
]}


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_cfg())
        self.app.config["TESTING"] = True          # no background image fetches in tests
        self.client = self.app.test_client()
        self.store = self.app.config["STORE"].store
        self.sent: list[dict] = []
        patcher = mock.patch("store.mail.send", side_effect=lambda cfg, **kw: (self.sent.append(kw) or True))
        patcher.start()
        self.addCleanup(patcher.stop)

    def ingest(self, kind, body, key=KEY):
        return self.client.post(f"/ingest/{kind}", json=body, headers={"X-API-Key": key} if key else {})

    def login(self, email="donna-n@live.com"):
        self.client.post("/login", data={"email": email})
        with self.store.engine.connect() as conn:
            tok = conn.execute(sa.select(D.login_tokens.c.token).order_by(D.login_tokens.c.expires_at.desc())).first()[0]
        return self.client.get(f"/login/{tok}", follow_redirects=False)

    def use_invite(self, token="ross-xyz"):
        return self.client.get(f"/i/{token}", follow_redirects=False)

    def seed(self):
        self.ingest("catalog", CATALOG); self.ingest("customers", CUSTOMERS); self.ingest("curation", CURATION)
        self.ingest("invites", INVITES)


class IngestGateTest(StoreTestCase):
    def test_requires_the_stores_key(self):
        self.assertEqual(self.ingest("catalog", CATALOG, key=None).status_code, 404)
        self.assertEqual(self.ingest("catalog", CATALOG, key="wrong").status_code, 404)
        self.assertEqual(self.client.get("/outbox").status_code, 404)
        self.assertEqual(self.client.post("/outbox/ack", json={"results": []}).status_code, 404)

    def test_blank_key_disables_ingest_entirely(self):
        app = create_app(_cfg(store_ingest_key=""))
        r = app.test_client().post("/ingest/catalog", json=CATALOG, headers={"X-API-Key": ""})
        self.assertEqual(r.status_code, 404)

    def test_accepts_and_snapshots(self):
        r = self.ingest("catalog", CATALOG)
        self.assertEqual((r.status_code, r.get_json()["count"]), (202, 2))
        self.assertEqual(self.store.product("L1")["wholesale"], 25.0)
        self.ingest("catalog", {**CATALOG, "items": CATALOG["items"][:1]})      # snapshot without T2 deactivates it
        self.assertIsNone(self.store.product("T2"))
        self.assertIsNotNone(self.store.product("L1"))
        st = {f["kind"]: f for f in self.store.feed_status()}
        self.assertEqual(st["catalog"]["count"], 1)
        self.assertEqual(st["catalog"]["as_of"], "2026-09-01")

    def test_invites_snapshot_and_revoke(self):
        r = self.ingest("invites", INVITES)
        self.assertEqual((r.status_code, r.get_json()["count"]), (202, 2))
        self.assertEqual(self.store.invite("tjx-abc")["companies"], ["gerson"])
        self.assertEqual(self.store.invite("ross-xyz")["companies"], ["gerson", "park_hill"])   # none listed = every company
        self.ingest("invites", {**INVITES, "items": INVITES["items"][1:]})           # AOI revoked TJX
        self.assertIsNone(self.store.invite("tjx-abc"))
        self.assertIsNotNone(self.store.invite("ross-xyz"))
        past = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        self.ingest("invites", {**INVITES, "items": [{**INVITES["items"][1], "expires_at": past}]})
        self.assertIsNone(self.store.invite("ross-xyz"))                               # expired
        self.assertIsNone(self.store.invite(""))

    def test_refuses_sensitive_payloads(self):
        dirty = {**CATALOG, "items": [{**CATALOG["items"][0], "avg_cost": 9.0}]}
        r = self.ingest("catalog", dirty)
        self.assertEqual(r.status_code, 422)
        self.assertIn("avg_cost", r.get_json()["error"])
        self.assertIsNone(self.store.product("L1"))                       # nothing written
        r = self.ingest("customers", {**CUSTOMERS, "items": [{**CUSTOMERS["items"][0], "Bucket": "x"}]})
        self.assertEqual(r.status_code, 422)

    def test_refuses_empty_allowlist_feeds(self):
        self.seed()
        r = self.ingest("customers", {**CUSTOMERS, "count": 0, "items": []})
        self.assertEqual(r.status_code, 409)
        self.assertIsNotNone(self.store.customer_for_email("donna-n@live.com"))   # allowlist untouched
        self.assertEqual(self.ingest("invites", {**INVITES, "count": 0, "items": []}).status_code, 409)
        self.assertIsNotNone(self.store.invite("tjx-abc"))
        self.assertEqual(self.ingest("curation", {**CURATION, "count": 0, "items": []}).status_code, 202)

    def test_bad_kind_and_body(self):
        self.assertEqual(self.ingest("costs", CATALOG).status_code, 400)      # "prices" is a kind now; this never will be
        self.assertEqual(self.ingest("catalog", {"items": "nope"}).status_code, 400)

    def test_schema_has_no_sensitive_columns(self):
        cols = {c.name for t in D.metadata.tables.values() for c in t.columns}
        for bad in ("avg_cost", "ats_cost", "receipt_date", "days", "bucket", "adv_rate", "floor_independent", "tier", "capacity_now"):
            self.assertNotIn(bad, cols)

    def test_healthz_and_robots(self):
        self.assertEqual(self.client.get("/healthz").get_json()["ok"], True)
        self.assertIn("Disallow: /", self.client.get("/robots.txt").get_data(as_text=True))
        self.assertEqual(self.client.get("/login").headers["X-Robots-Tag"], "noindex, nofollow")


class AccessTest(StoreTestCase):
    def test_gated_redirects(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        for path in ("/item/L1", "/offer", "/offer.csv", "/offers"):
            self.assertEqual(self.client.get(path).status_code, 302, path)
        self.assertEqual(self.client.post("/offer/set", data={}).status_code, 302)
        html = self.client.get("/login").get_data(as_text=True)
        self.assertIn("by invitation", html)
        self.assertIn("Request access", html)
        self.assertIn("https://shop.gerson.test", html)          # independents are pointed at the website

    def test_invite_link_opens_the_sheet(self):
        self.seed()
        r = self.use_invite("tjx-abc")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/"))
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("TJX", html)
        self.assertIn("Lantern", html)
        self.assertNotIn("Tree", html)                             # Gerson-only invite: no Park Hill SKUs
        self.assertEqual(self.client.get("/item/T2").status_code, 404)
        self.assertIn("Leave", html)                               # invites leave, customers sign out

    def test_invalid_revoked_and_expired_invites(self):
        self.seed()
        r = self.client.get("/i/nope", follow_redirects=True)
        self.assertIn("no longer valid", r.get_data(as_text=True))
        self.use_invite("tjx-abc")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.ingest("invites", {**INVITES, "items": INVITES["items"][1:]})           # revoked in AOI
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)                                          # access ends at once

    def test_magic_link_only_for_allowlisted_emails(self):
        self.seed()
        r = self.client.post("/login", data={"email": "stranger@nowhere.com"})
        self.assertEqual(r.status_code, 200)                                # same page, no enumeration
        self.assertEqual(self.sent, [])
        with self.store.engine.connect() as conn:
            self.assertEqual(conn.execute(sa.select(sa.func.count()).select_from(D.login_tokens)).scalar(), 0)
        r = self.login("BUYER@shop.com")                                    # case-insensitive
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.sent[0]["to"], "buyer@shop.com")
        self.assertIn("/login/", self.sent[0]["body"])
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("Adeline Collective", home.get_data(as_text=True))
        self.assertIn("Brooks Mickel", home.get_data(as_text=True))
        self.assertIn("Sign out", home.get_data(as_text=True))

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
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_application_lands_in_outbox(self):
        r = self.client.post("/apply", data={"company": "New Shop", "email": "owner@newshop.com", "resale_number": "TX-1"})
        self.assertEqual(r.status_code, 200)
        items = self.store.pull_outbox()
        self.assertEqual(items[0]["kind"], "application")
        self.assertEqual(items[0]["payload"]["company"], "New Shop")
        self.assertIsNone(items[0]["customer_id"])
        self.assertEqual(self.client.post("/apply", data={"company": "", "email": "bad"}).status_code, 400)


class SheetTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.use_invite("ross-xyz")

    def test_sheet_shows_wholesale_and_pack_never_the_ladder(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Ross Stores", html)
        self.assertIn("Lantern", html); self.assertIn("Tree", html)          # every company
        self.assertIn("$25.00", html); self.assertIn("$100.00", html)        # original wholesale
        self.assertIn("inner 6 · master 24", html)                            # inner first, then master
        self.assertIn("pack of 4", html)                                        # T2 has no pack data: NetSuite minimum
        self.assertNotIn("case of", html); self.assertNotIn("about ", html)
        self.assertIn(">96", html)                                              # units available
        self.assertIn("16 &times; 6", html.replace("\u00d7", "&times;"))         # ...and in cases of the smallest pack
        for hidden in ("$20.00", "$30.00", "20% off", "70% off", "Drops to", "16.25"):
            self.assertNotIn(hidden, html)                                    # no closeout price, tier or step-down
        self.assertIn('name="qty[L1]"', html); self.assertIn('name="price[L1]"', html)
        item = self.client.get("/item/L1").get_data(as_text=True)
        self.assertIn("UPC 1", item); self.assertIn("original wholesale", item); self.assertNotIn("$20.00", item)
        self.assertEqual(self.client.get("/item/NOPE").status_code, 404)

    def test_filters_take_several_brands_and_a_depth_floor(self):
        """Brands and categories are multi-select, and depth can be asked for in
        units or in cases of the smallest lot a buyer can take (JJ, 2026-09-03)."""
        both = self.client.get("/?brand=Fall%2FHoliday&brand=Park+Hill+Collection").get_data(as_text=True)
        self.assertIn("Lantern", both); self.assertIn("Tree", both)
        one = self.client.get("/?brand=Park+Hill+Collection").get_data(as_text=True)
        self.assertNotIn("Lantern", one); self.assertIn("Tree", one)
        # depth in pieces
        self.assertIn("Lantern", self.client.get("/?min_units=96").get_data(as_text=True))
        deep = self.client.get("/?min_units=97").get_data(as_text=True)
        self.assertNotIn("Lantern", deep); self.assertNotIn("Tree", deep)      # 96 and 40 on hand
        # depth in cases: L1 is 96 in inners of 6 (16), T2 is 40 in packs of 4 (10)
        twelve = self.client.get("/?min_cases=12").get_data(as_text=True)
        self.assertIn("Lantern", twelve); self.assertNotIn("Tree", twelve)
        self.assertNotIn("Lantern", self.client.get("/?min_cases=17").get_data(as_text=True))
        # the controls come back filled in, so a filter can be adjusted not retyped
        page = self.client.get("/?brand=Park+Hill+Collection&min_cases=5").get_data(as_text=True)
        self.assertIn('<option value="Park Hill Collection" selected>', page)
        self.assertIn('name="min_cases" type="number" min="0" step="1" value="5"', page)

    def test_filters_search_sort_and_paging(self):
        html = self.client.get("/?brand=Park+Hill+Collection").get_data(as_text=True)
        self.assertIn("Tree", html); self.assertNotIn("Lantern", html)
        html = self.client.get("/?q=park+tree").get_data(as_text=True)              # every word, any field
        self.assertIn("T2", html); self.assertNotIn(">L1<", html)
        self.assertIn("Nothing matches", self.client.get("/?q=park+lantern").get_data(as_text=True))
        f = self.store.facets(brand="Park Hill Collection")
        self.assertEqual(f["categories"], ["Decor"])
        self.assertNotIn("discounts", f)
        self.assertEqual(self.store.count_products(brand="Fall/Holiday"), 1)
        self.assertEqual([p["sku"] for p in self.store.list_products(sort="value")], ["T2", "L1"])      # 4000 vs 2400
        self.assertEqual([p["sku"] for p in self.store.list_products(sort="wholesale_asc")], ["L1", "T2"])
        from store import shop
        skus = [{**CATALOG["items"][0], "sku": f"P{i:03d}", "internal_id": str(100 + i)} for i in range(shop.PAGE_SIZE + 5)]
        self.ingest("catalog", {**CATALOG, "items": CATALOG["items"] + skus})
        html = self.client.get("/?brand=Fall%2FHoliday").get_data(as_text=True)
        self.assertIn("page 1 of 2", html); self.assertIn("page=2", html)
        html2 = self.client.get("/?brand=Fall%2FHoliday&page=2").get_data(as_text=True)
        self.assertIn("Previous", html2); self.assertNotIn("Next", html2)
        self.assertEqual(self.client.get("/?page=99").status_code, 200)             # clamps, no error


class MsrpTest(unittest.TestCase):
    """MSRP: the wholesale on the page, marked up the way a full-price retailer
    would have, landed on a .99 price point."""

    def test_price_points(self):
        self.assertEqual(D.msrp_price(305.00), 762.99)      # 762.50 rounds up to the .99 above
        self.assertEqual(D.msrp_price(28.22), 70.99)
        self.assertEqual(D.msrp_price(5.68), 13.99)         # 14.20 lands on the .99 *below*: nearest, not up
        self.assertEqual(D.msrp_price(20.00), 49.99)
        self.assertEqual(D.msrp_price(1.08), 2.99)
        self.assertEqual(D.msrp_price(100.0, 0.5), 199.99)  # the margin is a parameter
        for bad in (0, -5, None, "x"):
            self.assertEqual(D.msrp_price(bad), 0.0)
        self.assertEqual(D.msrp_price(10.0, 1.0), 0.0)      # a 100% margin has no price


class OfferTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.use_invite("ross-xyz")

    def test_saving_lines_snaps_and_caps(self):
        r = self.client.post("/offer/set", data={"qty[L1]": "7", "price[L1]": "12.5", "next": "/"})
        self.assertEqual((r.status_code, r.headers["Location"].endswith("/")), (302, True))
        self.assertEqual(self.store.draft("inv:ross-xyz"), {"L1": {"qty": 6, "price": 12.5}})    # whole cases of 6
        self.client.post("/offer/set", data={"qty[L1]": "1000", "price[L1]": "$12.50"})
        self.assertEqual(self.store.draft("inv:ross-xyz")["L1"]["qty"], 96)                       # capped at available
        self.client.post("/offer/set", data={"qty[T2]": "8", "price[T2]": "abc"})                 # bad price: not added
        self.assertNotIn("T2", self.store.draft("inv:ross-xyz"))
        self.client.post("/offer/set", data={"qty[L1]": "", "price[L1]": ""})                     # blank pair removes
        self.assertEqual(self.store.draft("inv:ross-xyz"), {})
        self.client.post("/offer/set", data={"qty[NOPE]": "4", "price[NOPE]": "1"})               # unknown sku ignored
        self.assertEqual(self.store.draft("inv:ross-xyz"), {})
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Review offer", html)
        # a Gerson-only invite cannot put a Park Hill SKU on its offer
        self.use_invite("tjx-abc")
        self.client.post("/offer/set", data={"qty[T2]": "4", "price[T2]": "40"})
        self.assertEqual(self.store.draft("inv:tjx-abc"), {})

    def test_autosave_line_snaps_and_counts(self):
        r = self.client.post("/offer/line", json={"sku": "L1", "qty": "7", "price": "12.5"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"saved": True, "sku": "L1", "line": {"qty": 6, "price": 12.5}, "count": 1})
        r = self.client.post("/offer/line", json={"sku": "T2", "qty": "8", "price": "35"}).get_json()
        self.assertEqual((r["line"], r["count"]), ({"qty": 8, "price": 35.0}, 2))
        r = self.client.post("/offer/line", json={"sku": "L1", "qty": "", "price": "12.5"}).get_json()   # blank qty removes
        self.assertEqual((r["line"], r["count"]), (None, 1))
        self.assertEqual(self.client.post("/offer/line", json={}).status_code, 400)
        self.assertEqual(self.client.post("/offer/line", json={"sku": "NOPE", "qty": 4, "price": 1}).get_json()["line"], None)
        self.assertEqual(self.store.draft("inv:ross-xyz"), {"T2": {"qty": 8, "price": 35.0}})
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="draft-count">1<', html)
        self.assertIn("/offer/line", html)                                       # autosave script wired

    def test_review_page_and_csv(self):
        self.client.post("/offer/set", data={"qty[L1]": "24", "price[L1]": "10", "qty[T2]": "8", "price[T2]": "35"})
        html = self.client.get("/offer").get_data(as_text=True)
        self.assertIn("$240.00", html); self.assertIn("$280.00", html); self.assertIn("$520.00", html)
        self.assertIn("40%", html); self.assertIn("35%", html)                     # % of wholesale per line
        self.assertIn("Download CSV", html); self.assertIn("Submit offer", html)
        csv_text = self.client.get("/offer.csv").get_data(as_text=True)
        lines = csv_text.strip().splitlines()
        self.assertEqual(lines[0].split(",")[:2], ["sku", "description"])
        self.assertIn("L1,Lantern,Fall/Holiday,Christmas,Ornaments,6,24,6,96,25.0,24,10.0,240.0", lines[1])
        self.assertEqual(len(lines), 3)
        self.client.post("/offer/clear")
        self.assertEqual(self.store.draft("inv:ross-xyz"), {})
        self.assertIn("Nothing on it yet", self.client.get("/offer").get_data(as_text=True))

    def test_submit_emails_the_team_and_the_buyer_and_lands_in_the_outbox(self):
        self.client.post("/offer/set", data={"qty[L1]": "24", "price[L1]": "10", "qty[T2]": "8", "price[T2]": "35"})
        r = self.client.post("/offer/submit", data={"company": "", "email": "bad"})
        self.assertEqual(r.status_code, 400)                                            # needs a company and a reply address
        self.assertEqual(self.store.pull_outbox(), [])
        r = self.client.post("/offer/submit", data={"company": "Ross Stores", "contact": "Sam", "email": "Sam@ross.test",
                                                    "phone": "555", "notes": "take-all, ship to Fort Mill"})
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("OF-1", html); self.assertIn("$520.00", html); self.assertIn("sam@ross.test", html)
        items = self.store.pull_outbox()
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual((it["kind"], it["customer_id"]), ("offer", None))
        p = it["payload"]
        self.assertEqual((p["company"], p["email"], p["line_count"], p["units"], p["total"]), ("Ross Stores", "sam@ross.test", 2, 32, 520.0))
        self.assertEqual(p["wholesale_total"], 1400.0)
        self.assertAlmostEqual(p["pct_of_wholesale"], 0.3714, places=4)
        self.assertEqual(p["buyer"], {"kind": "invite", "key": "inv:ross-xyz", "label": "Ross Stores", "customer_id": None,
                                      "invite_token": "ross-xyz", "buyer_class": "regional"})
        self.assertEqual([(l["sku"], l["qty"], l["offer_price"], l["wholesale"]) for l in p["lines"]], [("L1", 24, 10.0, 25.0), ("T2", 8, 35.0, 100.0)])
        self.assertIn("take-all", p["notes"])
        # two designated addresses + the buyer's copy, each with the CSV attached
        self.assertEqual([m["to"] for m in self.sent], ["offers@gerson.test", "jj@gerson.test", "sam@ross.test"])
        self.assertIn("Closeout offer OF-1: Ross Stores", self.sent[0]["subject"])
        self.assertIn("L1", self.sent[0]["body"]); self.assertIn("37% of $1,400.00 wholesale", self.sent[0]["body"])
        self.assertEqual(self.sent[0]["attachments"][0][0], "offer-OF-1.csv")
        self.assertIn(b"L1,Lantern", self.sent[0]["attachments"][0][1])
        # the team's copy carries the buyer and the instructions for the team
        self.assertIn("Reply to the buyer", self.sent[0]["body"])
        self.assertIn("sam@ross.test", self.sent[0]["body"])
        self.assertIn("<table", self.sent[0]["html"]); self.assertIn("$10.00", self.sent[0]["html"])
        # the buyer's copy is addressed to the buyer: none of that belongs in it
        buyer_mail = self.sent[2]
        self.assertIn("We have your offer", buyer_mail["subject"])
        for staff_only in ("Reply to the buyer", "cost", "floors", "NetSuite", "Invite:"):
            self.assertNotIn(staff_only, buyer_mail["body"])
            self.assertNotIn(staff_only, buyer_mail["html"])
        self.assertIn("Your lines are attached as a CSV", buyer_mail["body"])
        self.assertIn("acceptance or a counter", buyer_mail["html"])
        self.assertIn("$520.00", buyer_mail["html"])                                    # their total, in a real table
        self.assertIn("37% of $1,400.00 wholesale", buyer_mail["html"])
        self.assertEqual(self.store.draft("inv:ross-xyz"), {})                          # draft cleared
        html = self.client.get("/offers").get_data(as_text=True)
        self.assertIn("OF-1", html); self.assertIn("reply comes by email", html)
        # an empty offer cannot be submitted
        r = self.client.post("/offer/submit", data={"company": "Ross", "email": "s@ross.test"}, follow_redirects=True)
        self.assertIn("at least one line", r.get_data(as_text=True))

    def test_allowlisted_customer_offers_carry_their_netsuite_id(self):
        self.client.post("/logout")
        self.login()
        self.client.post("/offer/set", data={"qty[T2]": "12", "price[T2]": "30"})
        html = self.client.get("/offer").get_data(as_text=True)
        self.assertIn('value="Adeline Collective"', html)                               # company prefilled
        self.client.post("/offer/submit", data={"company": "Adeline Collective", "email": "donna-n@live.com"})
        it = self.store.pull_outbox()[0]
        self.assertEqual((it["customer_id"], it["payload"]["buyer"]["kind"], it["payload"]["buyer"]["key"]), ("26003", "customer", "cust:26003"))
        self.assertEqual(self.store.outbox_for("cust:26003")[0]["kind"], "offer")
        self.store.ack_outbox([{"id": it["id"], "status": "acked", "result": {"message": "Offer received. We reply by email."}}])
        self.assertIn("Offer received. We reply by email.", self.client.get("/offers").get_data(as_text=True))

    def test_without_a_designated_inbox_the_offer_still_reaches_the_outbox(self):
        app = create_app(_cfg(offer_notify_emails=""))
        app.config["TESTING"] = True
        c = app.test_client()
        c.post("/ingest/catalog", json=CATALOG, headers={"X-API-Key": KEY})
        c.post("/ingest/invites", json=INVITES, headers={"X-API-Key": KEY})
        c.get("/i/ross-xyz")
        c.post("/offer/set", data={"qty[L1]": "6", "price[L1]": "9"})
        with self.assertLogs(app.logger, level="ERROR") as logs:
            r = c.post("/offer/submit", data={"company": "Ross", "email": "s@ross.test"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any("OFFER_NOTIFY_EMAILS" in m for m in logs.output))
        self.assertEqual(len(app.config["STORE"].store.pull_outbox()), 1)


    def test_the_sheet_shows_msrp_and_the_buyers_own_retail_tools(self):
        """MSRP is the anchor -- what it sold for and what a full-price retailer
        paid. Their margin and freight then turn their offer into their retail."""
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("MSRP", html)
        self.assertIn('data-msrp="62.99"', html)              # L1 wholesale $25.00 -> $62.99
        self.assertIn('id="targetMargin"', html); self.assertIn('id="freightFactor"', html)
        self.assertIn("Your retail", html)
        item = self.client.get("/item/L1").get_data(as_text=True)
        self.assertIn("MSRP", item)
        review = self.client.post("/offer/line", json={"sku": "L1", "qty": 24, "price": 10})
        self.assertEqual(review.status_code, 200)
        page = self.client.get("/offer").get_data(as_text=True)
        self.assertIn("MSRP", page); self.assertIn('id="freightFactor"', page)
        # and nothing about our side of it ever appears on a buyer page
        self.assertNotIn("cost", page.lower().replace("closeout", ""))

    def test_the_suggested_offer_is_capped_to_a_closeout_shaped_number(self):
        """MSRP is msrp_price(wholesale), so the buyer's backwards margin sum is a
        fixed multiple of wholesale on every line -- at 50% margin / 25% freight it
        lands at 94% of wholesale, which is not a closeout. The sheet ships the
        wholesale anchor and a flat cap so the suggestion can never sit there."""
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-wholesale="25.00"', html)         # L1, beside data-msrp
        # the cap reaches the page as a number, not an unrendered Jinja expression
        self.assertNotIn("suggest_max_disc", html)
        self.assertIn("const MAX_DISC = Number('0.2')", html)
        # uncapped, 50/25 would suggest 0.9375 x wholesale; the cap holds it at 0.80
        self.assertLess(0.80 * 25.00, 0.9375 * 25.00)

    def test_behaviour_is_recorded_and_pulled_with_a_cursor(self):
        """The point of the events table is what never becomes an offer: a SKU
        opened and left, a search with no results, a price typed and abandoned."""
        self.client.get("/?q=lantern")
        self.client.get("/?q=zzz-nothing-like-this")
        self.client.get("/item/L1")
        self.client.post("/offer/line", json={"sku": "L1", "qty": 24, "price": 10})
        self.client.post("/offer/line", json={"sku": "L1", "qty": 24, "price": 8})   # walked their number down
        self.client.get("/offer")                                        # reviewed it
        self.client.post("/offer/line", json={"sku": "L1", "qty": 0, "price": 0})    # ...and took it off
        kinds = [e["kind"] for e in self.store.events_since(0)]
        self.assertEqual(kinds, ["sheet_viewed", "sheet_viewed", "item_viewed", "line_priced", "line_priced",
                                 "offer_reviewed", "line_removed"])
        evs = self.store.events_since(0)
        self.assertTrue(evs[1]["payload"]["no_results"])                             # what they wanted and we lack
        self.assertEqual(evs[1]["payload"]["q"], "zzz-nothing-like-this")
        self.assertEqual(evs[2]["sku"], "L1")
        priced = evs[4]["payload"]
        self.assertEqual((priced["price"], priced["prev_price"], priced["qty"]), (8.0, 10.0, 24))
        self.assertEqual(evs[6]["payload"]["prev_price"], 8.0)                       # the abandoned number survives
        self.assertEqual({e["buyer_key"] for e in evs}, {"inv:ross-xyz"})
        self.assertEqual(len({e["session_id"] for e in evs}), 1)                     # one visit, stitched
        # AOI pulls with a cursor; a re-pull is harmless and returns nothing new
        r = self.client.get("/events?limit=3", headers={"X-API-Key": KEY})
        first = r.get_json()
        self.assertEqual((first["count"], first["cursor"]), (3, evs[2]["id"]))
        rest = self.client.get(f"/events?after={first['cursor']}", headers={"X-API-Key": KEY}).get_json()
        self.assertEqual([e["kind"] for e in rest["items"]], kinds[3:])
        self.assertEqual(self.client.get("/events").status_code, 404)                # no key, no stream


class NegotiationRoundTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.use_invite("ross-xyz")
        self.client.post("/offer/set", data={"qty[L1]": "24", "price[L1]": "10", "qty[T2]": "8", "price[T2]": "35"})
        self.client.post("/offer/submit", data={"company": "Ross Stores", "email": "sam@ross.test"})
        self.offer_id = self.store.pull_outbox()[0]["id"]
        self.sent.clear()

    def push(self, **over):
        body = {"offer_ref": self.offer_id, "round_id": 7, "round_no": 2, "token": "tok-2", "kind": "counter", "thread_status": "countered",
                "lines": [{"sku": "L1", "qty": 24, "price": 14.0, "description": "Lantern", "wholesale": 25.0},
                          {"sku": "T2", "qty": 8, "price": 45.0, "description": "Tree", "wholesale": 100.0}],
                "message": "Best we can do on the trees.", "created_at": "2026-09-02T21:00:00+00:00", "buyer_email": "sam@ross.test", "company": "Ross Stores"}
        body.update(over)
        return self.client.post("/rounds", json=body, headers={"X-API-Key": KEY})

    def test_push_is_keyed_validated_and_emails_a_link(self):
        self.assertEqual(self.client.post("/rounds", json={}).status_code, 404)
        self.assertEqual(self.push(token="").status_code, 400)
        self.assertEqual(self.push(kind="bribe").status_code, 400)
        self.assertEqual(self.push(offer_ref=999).status_code, 404)
        self.assertEqual(self.push(lines=[{"sku": "L1", "qty": 1, "price": 1, "avg_cost": 3}]).status_code, 422)   # cost never lands here
        r = self.push()
        self.assertEqual((r.status_code, r.get_json()["status"], r.get_json()["emailed"]), (202, "open", True))
        m = self.sent[-1]
        self.assertEqual(m["to"], "sam@ross.test")
        self.assertIn(f"Gerson countered your offer — OF-{self.offer_id}", m["subject"])
        self.assertIn("http://store.test/o/tok-2", m["body"]); self.assertIn("Best we can do", m["body"]); self.assertIn("14.00", m["body"])

    def test_round_page_shows_trail_and_accepting_writes_a_response(self):
        self.push()
        html = self.client.get("/o/tok-2").get_data(as_text=True)
        self.assertIn("Gerson countered your offer", html)
        self.assertIn("$14.00", html); self.assertIn("$10.00", html)            # their price next to yours
        self.assertIn("Best we can do", html)
        self.assertNotIn("cost", html.lower().replace("closeout", ""))          # nothing internal on the page
        self.assertEqual(self.client.get("/o/nope").status_code, 404)
        r = self.client.post("/o/tok-2/respond", data={"action": "accept", "message": "Deal."})
        self.assertEqual(r.status_code, 200)
        self.assertIn("accepted", r.get_data(as_text=True).lower())
        items = [i for i in self.store.pull_outbox() if i["kind"] == "offer_response"]
        self.assertEqual(len(items), 1)
        opened = self.store.round("tok-2")["opened_at"]
        self.assertTrue(opened)                                                     # first GET of the link is remembered
        self.assertEqual(items[0]["payload"], {"offer_ref": self.offer_id, "round_id": 7, "token": "tok-2", "action": "accept", "lines": [],
                                               "message": "Deal.", "email": "sam@ross.test", "opened_at": opened})
        self.assertEqual(items[0]["customer_id"], None)
        # the link is now read-only; a second answer is refused
        html = self.client.get("/o/tok-2").get_data(as_text=True)
        self.assertNotIn('name="action"', html)
        r = self.client.post("/o/tok-2/respond", data={"action": "decline"}, follow_redirects=True)
        self.assertIn("already answered", r.get_data(as_text=True))
        self.store.ack_outbox([{"id": items[0]["id"], "status": "acked"}])
        self.assertEqual(len([i for i in self.store.pull_outbox() if i["kind"] == "offer_response"]), 0)   # nothing new queued

    def test_counter_back_and_same_terms_mean_accept(self):
        self.push()
        r = self.client.post("/o/tok-2/respond", data={"action": "counter", "qty[L1]": "48", "price[L1]": "12", "qty[T2]": "0", "price[T2]": "45",
                                                       "message": "12 if we take 48"})
        self.assertEqual(r.status_code, 200)
        resp = [i for i in self.store.pull_outbox() if i["kind"] == "offer_response"][0]["payload"]
        self.assertEqual((resp["action"], resp["lines"], resp["message"]),
                         ("counter", [{"sku": "L1", "qty": 48, "price": 12.0, "description": "Lantern", "wholesale": 25.0}], "12 if we take 48"))
        # AOI answers with round 3; round 2's link is closed
        self.push(token="tok-3", round_id=8, round_no=3, lines=[{"sku": "L1", "qty": 48, "price": 13.0}])
        self.assertEqual(self.store.round("tok-2")["status"], "responded")
        r = self.client.post("/o/tok-3/respond", data={"action": "counter", "qty[L1]": "48", "price[L1]": "13.00"})
        resp = [i for i in self.store.pull_outbox() if i["kind"] == "offer_response"][-1]["payload"]
        self.assertEqual(resp["action"], "accept")                              # typing the same terms back is acceptance
        html = self.client.get("/offers").get_data(as_text=True)
        self.assertIn("counter answered", html)

    def test_accept_and_decline_pushes_close_the_thread_and_email(self):
        self.push()
        r = self.push(token="tok-3", round_id=8, round_no=3, kind="accept", thread_status="accepted", message="Done.")
        self.assertEqual(r.get_json()["status"], "closed")
        self.assertEqual(self.store.round("tok-2")["status"], "closed")          # superseded
        self.assertIn("Your offer is accepted", self.sent[-1]["subject"])
        self.assertIn("enter the order", self.sent[-1]["body"])
        html = self.client.get("/o/tok-3").get_data(as_text=True)
        self.assertIn("accepted", html.lower()); self.assertNotIn('name="action"', html)
        r = self.client.post("/o/tok-2/respond", data={"action": "accept"}, follow_redirects=True)
        self.assertIn("already answered or the offer is closed", r.get_data(as_text=True))
        r = self.push(token="tok-4", round_id=9, round_no=4, kind="decline", thread_status="declined", lines=[], message="Too low.")
        self.assertIn("declined", self.sent[-1]["subject"].lower()); self.assertIn("Too low.", self.sent[-1]["body"])
        # a re-push of an answered round never reopens it
        self.store.respond_round("tok-4", {"x": 1}) if False else None
        self.push(token="tok-3", round_id=8, round_no=3, kind="accept", thread_status="accepted")
        self.assertEqual(self.store.round("tok-3")["status"], "closed")


class SignupAdminTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.ingest("catalog", CATALOG)

    def _link(self, body, marker):
        import re as _re
        m = _re.search(r"http://store\.test(/[^\s]+)", body)
        self.assertIsNotNone(m, body)
        self.assertIn(marker, m.group(1))
        return m.group(1)

    def admin_login(self, email="admin@gerson.test"):
        self.client.post("/login", data={"email": email})
        return self.client.get(self._link(self.sent[-1]["body"], "/login/"))

    def test_admin_signs_in_by_link_and_portal_is_hidden_from_others(self):
        self.assertEqual(self.client.get("/admin/").status_code, 404)
        r = self.admin_login("ADMIN@gerson.test")
        self.assertEqual((r.status_code, r.headers["Location"].endswith("/admin/")), (302, True))
        html = self.client.get("/admin/").get_data(as_text=True)
        self.assertIn("Buyer admin", html); self.assertIn("admin@gerson.test", html); self.assertIn("Invite a buyer", html)
        self.client.post("/logout")
        self.assertEqual(self.client.get("/admin/").status_code, 404)
        self.client.post("/login", data={"email": "nobody@nowhere.test"})       # unknown: no link, same page
        self.assertEqual([m["to"] for m in self.sent], ["admin@gerson.test"])

    def test_admin_sets_a_password_and_signs_in_without_a_link(self):
        # No password yet: the password path is closed and nothing is emailed.
        r = self.client.post("/login", data={"email": "admin@gerson.test", "password": "not-set-yet-123"})
        self.assertEqual((r.status_code, self.sent), (200, []))
        self.assertIn("do not match", r.get_data(as_text=True))
        self.assertEqual(self.client.get("/admin/").status_code, 404)
        # Sign in by link once, set a password on the portal.
        self.admin_login()
        html = self.client.get("/admin/").get_data(as_text=True)
        self.assertIn("set a password so you can skip the emailed link", html)
        r = self.client.post("/admin/password", data={"password": "short", "password2": "short"}, follow_redirects=True)
        self.assertIn("at least 12 characters", r.get_data(as_text=True))
        r = self.client.post("/admin/password", data={"password": "correct-horse-battery", "password2": "correct-horse-battery-x"}, follow_redirects=True)
        self.assertIn("do not match", r.get_data(as_text=True))
        r = self.client.post("/admin/password", data={"password": "correct-horse-battery", "password2": "correct-horse-battery"}, follow_redirects=True)
        self.assertIn("Password saved", r.get_data(as_text=True))
        self.assertIn("password set, change it here", r.get_data(as_text=True))
        self.client.post("/logout")
        self.assertEqual(self.client.get("/admin/").status_code, 404)
        # Wrong password: no session, no email. Right password: straight to the portal.
        sent_before = len(self.sent)
        r = self.client.post("/login", data={"email": "Admin@Gerson.test", "password": "wrong-wrong-wrong"})
        self.assertEqual((r.status_code, self.client.get("/admin/").status_code, len(self.sent)), (200, 404, sent_before))
        r = self.client.post("/login", data={"email": "Admin@Gerson.test", "password": "correct-horse-battery"})
        self.assertEqual((r.status_code, r.headers["Location"].endswith("/admin/")), (302, True))
        self.assertIn("admin@gerson.test", self.client.get("/admin/").get_data(as_text=True))
        self.assertEqual(len(self.sent), sent_before)
        # Blank password still means "email me a link", for admins and buyers alike.
        self.client.post("/logout")
        self.client.post("/login", data={"email": "admin@gerson.test", "password": ""})
        self.assertEqual(len(self.sent), sent_before + 1)

    def test_password_never_signs_in_a_non_admin_and_throttles_after_misses(self):
        from store import shop as S
        self.ingest("customers", CUSTOMERS)
        # An allowlisted buyer typing a password gets neither a session nor a link.
        r = self.client.post("/login", data={"email": "donna-n@live.com", "password": "whatever-whatever"})
        self.assertEqual((r.status_code, self.sent), (200, []))
        self.assertEqual(self.client.get("/").status_code, 302)
        # A stranger's password can only ever be a password, and only for an admin address.
        self.store.set_admin_password_hash("donna-n@live.com", "pbkdf2:sha256:1$x$y")   # not in STORE_ADMIN_EMAILS
        r = self.client.post("/login", data={"email": "donna-n@live.com", "password": "whatever-whatever"})
        self.assertEqual(self.client.get("/admin/").status_code, 404)
        # Eight misses close the password path for that address; the link still works.
        self.admin_login(); self.client.post("/admin/password", data={"password": "correct-horse-battery", "password2": "correct-horse-battery"}); self.client.post("/logout")
        S._PW_FAILS.clear()
        for _ in range(S._PW_MAX_FAILS):
            self.client.post("/login", data={"email": "admin@gerson.test", "password": "nope-nope-nope"})
        r = self.client.post("/login", data={"email": "admin@gerson.test", "password": "correct-horse-battery"})
        self.assertEqual((r.status_code, self.client.get("/admin/").status_code), (200, 404))
        self.admin_login()
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        S._PW_FAILS.clear()

    def test_invited_signup_is_approved_and_signed_in_at_once(self):
        self.admin_login()
        r = self.client.post("/admin/invite", data={"email": "Pat@TJX.test", "company": "TJX", "note": "Looking forward to it."}, follow_redirects=True)
        self.assertIn("Invitation sent to pat@tjx.test", r.get_data(as_text=True))
        inv_mail = self.sent[-1]
        self.assertEqual(inv_mail["to"], "pat@tjx.test"); self.assertIn("Looking forward", inv_mail["body"])
        join = self._link(inv_mail["body"], "/join/")
        html = self.client.get(join).get_data(as_text=True)
        self.assertIn('value="pat@tjx.test"', html); self.assertIn('value="TJX"', html)
        self.assertEqual(self.client.post(join, data={"company": "TJX", "contact": ""}).status_code, 400)
        # signing up from an invitation approves the account and opens the sheet:
        # the admin already decided, so there is no second approval (JJ, 2026-09-03)
        n = len(self.sent)
        buyer = self.app.test_client()
        r = buyer.post(join, data={"company": "TJX Companies", "contact": "Pat Buyer", "phone": "555", "notes": "HomeGoods, Marshalls"})
        self.assertEqual((r.status_code, r.headers["Location"].endswith("/")), (302, True))
        tos = [m["to"] for m in self.sent[n:]]
        self.assertEqual(sorted(tos), ["admin@gerson.test", "boss@gerson.test", "pat@tjx.test"])   # admins told, buyer welcomed
        self.assertIn("approved and on the sheet already", next(m for m in self.sent[n:] if m["to"] == "boss@gerson.test")["body"])
        self.assertIn("is active", next(m for m in self.sent[n:] if m["to"] == "pat@tjx.test")["body"])
        self.assertEqual(self.client.get(join).status_code, 404)                              # one sign-up per invitation
        b = self.store.buyer_for_email("pat@tjx.test")
        self.assertEqual((b["status"], b["company"], b["contact"], b["approved_by"], b["invite_token"] is not None),
                         ("approved", "TJX Companies", "Pat Buyer", "admin@gerson.test", True))
        html = self.client.get("/admin/").get_data(as_text=True)
        self.assertIn("Nobody is waiting", html); self.assertIn("TJX Companies", html)         # no approval queued
        html = buyer.get("/").get_data(as_text=True)
        self.assertIn("TJX Companies", html); self.assertIn("Lantern", html); self.assertIn("Tree", html); self.assertIn("Sign out", html)
        buyer.post("/offer/line", json={"sku": "L1", "qty": 24, "price": 10})
        html = buyer.get("/offer").get_data(as_text=True)
        self.assertIn('value="TJX Companies"', html); self.assertIn('value="pat@tjx.test"', html); self.assertIn('value="Pat Buyer"', html)
        buyer.post("/offer/submit", data={"company": "TJX Companies", "contact": "Pat Buyer", "email": "pat@tjx.test"})
        it = [i for i in self.store.pull_outbox() if i["kind"] == "offer"][0]
        self.assertEqual((it["customer_id"], it["payload"]["buyer"]["kind"], it["payload"]["buyer"]["key"]), (None, "buyer", f"buyer:{b['id']}"))
        # a fresh link by email works for an approved buyer; suspension ends access at once
        n = len(self.sent)
        buyer.post("/login", data={"email": "pat@tjx.test"})
        self.assertEqual(self.sent[-1]["to"], "pat@tjx.test"); self.assertEqual(len(self.sent), n + 1)
        self.client.post(f"/admin/buyers/{b['id']}/status", data={"status": "suspended"})
        self.assertEqual(buyer.get("/").status_code, 302)
        html = self.client.get("/admin/").get_data(as_text=True)
        self.assertIn("Reactivate", html); self.assertIn(">1<", html)                        # offers count
        # a fresh invitation never reactivates a suspended buyer: that is an admin's act
        inv3 = self.store.create_signup_invite("pat@tjx.test", created_by="admin@gerson.test")
        r = self.app.test_client().post(f"/join/{inv3['token']}", data={"company": "TJX Companies", "contact": "Pat Buyer"})
        self.assertEqual(r.status_code, 200); self.assertIn("not active", r.get_data(as_text=True))
        self.assertEqual(self.store.buyer(b["id"])["status"], "suspended")
        # declined sign-ups can apply again; withdrawn invitations die
        self.client.post(f"/admin/buyers/{b['id']}/status", data={"status": "declined"})
        self.store.create_buyer(company="TJX", contact="Pat", email="pat@tjx.test")
        self.assertEqual(self.store.buyer(b["id"])["status"], "pending")
        inv2 = self.store.create_signup_invite("x@y.test", created_by="admin@gerson.test")
        self.client.post(f"/admin/invites/{inv2['token']}/revoke")
        self.assertEqual(self.client.get(f"/join/{inv2['token']}").status_code, 404)

    def test_buyer_class_is_the_admins_call_and_rides_the_offer(self):
        """The governed field (brief S6): a buyer never picks the lane their
        offers are priced against, and an unrecognised value falls back to the
        strictest one rather than the lowest floor."""
        self.admin_login()
        self.client.post("/admin/invite", data={"email": "pat@tjx.test", "company": "TJX"}, follow_redirects=True)
        join = self._link(self.sent[-1]["body"], "/join/")
        buyer = self.app.test_client()
        buyer.post(join, data={"company": "TJX Companies", "contact": "Pat Buyer"})
        b = self.store.buyer_for_email("pat@tjx.test")
        self.assertEqual(b["buyer_class"], "regional")                     # never liquidator by default
        r = self.client.post(f"/admin/buyers/{b['id']}/class", data={"buyer_class": "liquidator"}, follow_redirects=True)
        self.assertIn("priced as liquidator", r.get_data(as_text=True))
        self.assertEqual(self.store.buyer(b["id"])["buyer_class"], "liquidator")
        self.client.post(f"/admin/buyers/{b['id']}/class", data={"buyer_class": "vip"})
        self.assertEqual(self.store.buyer(b["id"])["buyer_class"], "regional")   # junk never widens the lane
        self.assertEqual(self.app.test_client().post(f"/admin/buyers/{b['id']}/class",
                                                    data={"buyer_class": "liquidator"}).status_code, 404)   # not an admin
        # the class travels with the offer so AOI prices it against the right floor
        self.store.set_buyer_class(b["id"], "liquidator")
        buyer.post("/offer/line", json={"sku": "L1", "qty": 24, "price": 10})
        buyer.post("/offer/submit", data={"company": "TJX Companies", "contact": "Pat Buyer", "email": "pat@tjx.test"})
        it = [i for i in self.store.pull_outbox() if i["kind"] == "offer"][0]
        self.assertEqual(it["payload"]["buyer"]["buyer_class"], "liquidator")

    def test_open_application_becomes_a_pending_buyer(self):
        r = self.client.post("/apply", data={"company": "New Shop", "contact": "Owner", "email": "owner@newshop.com", "resale_number": "TX-1", "city": "Austin", "state": "TX"})
        self.assertEqual(r.status_code, 200)
        b = self.store.buyer_for_email("owner@newshop.com")
        self.assertEqual((b["status"], b["company"], b["contact"]), ("pending", "New Shop", "Owner"))
        self.assertIn("resale TX-1", b["notes"]); self.assertIn("Austin, TX", b["notes"])
        self.assertEqual([i["kind"] for i in self.store.pull_outbox()], ["application"])   # AOI still sees it
        self.assertEqual(sorted(m["to"] for m in self.sent), ["admin@gerson.test", "boss@gerson.test", "owner@newshop.com"])


class OutboxProtocolTest(StoreTestCase):
    def test_pull_marks_pulled_and_retries_until_acked(self):
        self.store.enqueue("order", {"total": 1}, "26003", buyer_key="cust:26003")
        self.store.enqueue("offer", {"sku": "L1"}, None, buyer_key="inv:ross-xyz")
        r = self.client.get("/outbox?limit=1", headers={"X-API-Key": KEY}).get_json()
        self.assertEqual(r["count"], 1)
        first = r["items"][0]["id"]
        r = self.client.get("/outbox", headers={"X-API-Key": KEY}).get_json()
        self.assertEqual([i["id"] for i in r["items"]], [first, first + 1])      # unacked item comes back
        self.assertIsNone(r["items"][1]["customer_id"])                          # invite offers carry no NetSuite id
        r = self.client.post("/outbox/ack", json={"results": [{"id": first, "status": "acked", "result": {"tranid": "SO1"}},
                                                              {"id": first + 1, "status": "rejected", "result": {"message": "declined"}}]},
                             headers={"X-API-Key": KEY}).get_json()
        self.assertEqual(r["acked"], 2)
        self.assertEqual(self.client.get("/outbox", headers={"X-API-Key": KEY}).get_json()["count"], 0)
        self.assertEqual({x["status"] for x in self.store.outbox_for("cust:26003")}, {"acked"})
        self.assertEqual({x["status"] for x in self.store.outbox_for("inv:ross-xyz")}, {"rejected"})
        # acking an item that was never pulled is ignored
        self.store.enqueue("order", {"total": 2}, "26003")
        self.assertEqual(self.store.ack_outbox([{"id": 3, "status": "acked"}]), 0)
        # legacy rows keyed only by customer id still show for that customer
        self.assertEqual(len(self.store.outbox_for("cust:26003")), 2)


class SnapTest(unittest.TestCase):
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


class PublishedPriceIngestTest(StoreTestCase):
    """AOI now sends the price NetSuite already charges and the mechanism behind
    it, plus the pre-markdown Original Price.  Park Hill / Glitterville closeouts
    are published by overwriting Base Price, so ``wholesale`` on those rows is
    already discounted and is NOT what the item originally sold for.
    """

    # Current AOI: the anchor rides as `wholesale`, NetSuite's Base Price beside it.
    MARKED_DOWN = {**CATALOG, "items": [
        {**CATALOG["items"][0], "sku": "EAB16064", "wholesale": 39.73, "original_price": 39.73,
         "base_price": 13.91, "published_price": 13.91, "published_basis": "base_marked_down",
         "published_label": "marked down", "published_disc_pct": 65},
    ]}
    # The shape AOI sent before the anchor moved: base in `wholesale`, list beside it.
    LEGACY_SHAPE = {**CATALOG, "items": [
        {**CATALOG["items"][0], "sku": "EAB16064", "wholesale": 13.91, "original_price": 39.73},
    ]}

    def test_published_fields_are_stored_and_read_back(self):
        self.assertEqual(self.ingest("catalog", self.MARKED_DOWN).status_code, 202)
        p = self.store.product("EAB16064")
        self.assertEqual(p["original_price"], 39.73)
        self.assertEqual(p["published_price"], 13.91)
        self.assertEqual(p["published_basis"], "base_marked_down")
        self.assertEqual(p["published_disc_pct"], 65)

    def test_the_sheet_anchors_on_the_list_price_not_the_marked_down_base(self):
        self.ingest("catalog", self.MARKED_DOWN)
        p = self.store.product("EAB16064")
        self.assertEqual(p["wholesale"], 39.73)          # what the buyer offers against
        self.assertEqual(p["base_price"], 13.91)         # what NetSuite charges today
        self.assertEqual(p["msrp"], msrp_price(39.73))   # and the retail anchor follows it

    def test_the_older_feed_shape_is_still_read_correctly(self):
        self.assertEqual(self.ingest("catalog", self.LEGACY_SHAPE).status_code, 202)
        p = self.store.product("EAB16064")
        self.assertEqual(p["original_price"], 39.73)
        self.assertTrue(p["marked_down"])

    def test_a_marked_down_row_is_flagged_as_such(self):
        self.ingest("catalog", self.MARKED_DOWN)
        self.assertTrue(self.store.product("EAB16064")["marked_down"])

    def test_a_normal_row_is_not_flagged(self):
        self.ingest("catalog", CATALOG)
        p = self.store.product("L1")
        self.assertFalse(p["marked_down"])
        self.assertEqual(p["original_price"], p["wholesale"])
        self.assertEqual(p["base_price"], p["wholesale"])

    def test_an_older_feed_without_the_fields_still_ingests(self):
        # Nothing in the payload but the fields AOI has always sent.
        legacy = {**CATALOG, "items": [{k: v for k, v in CATALOG["items"][0].items()}]}
        self.assertEqual(self.ingest("catalog", legacy).status_code, 202)
        p = self.store.product("L1")
        self.assertEqual(p["original_price"], 25.0)      # falls back to wholesale
        self.assertEqual(p["published_price"], 0.0)
        self.assertEqual(p["published_basis"], "")
        self.assertFalse(p["marked_down"])


class SuggestedPriceTest(StoreTestCase):
    """The buyer's own margin sum, run backwards, so nobody faces 500 blank boxes:
    suggested = MSRP x (1 - target margin) x (1 - freight factor), the exact
    inverse of the "your retail" column that was already there.  It is arithmetic
    on numbers already on the page — no cost, no floor, no ladder step — and both
    of the buyer's numbers stay in their browser."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.login()

    def test_the_sheet_carries_a_slot_and_a_fill_control(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="suggest"', html)
        self.assertIn('id="fillFromMargin"', html)
        # It must not submit the offer form by accident.
        self.assertIn('<button type="button" id="fillFromMargin"', html)

    def test_the_suggestion_is_computed_in_the_browser_not_served(self):
        # No price is rendered into the slot server-side: it depends on two
        # numbers we never receive.
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('<span class="suggest"></span>', html)
        self.assertIn("suggestedFor", html)

    def test_a_fixed_price_page_offers_no_suggestion(self):
        # The round page prices the line itself; there is nothing to suggest, and
        # the shared script no-ops because the page has no .suggest slot.
        page = self.client.get("/offer").get_data(as_text=True)
        self.assertNotIn('class="suggest"', page)
        self.assertNotIn('id="fillFromMargin"', page)

    def test_the_sheet_still_leaks_nothing(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("cost", html.lower().replace("closeout", ""))
        for hidden in ("floor", "avg_cost", "base_price", "published_price"):
            self.assertNotIn(hidden, html.lower())


class FeaturedTest(StoreTestCase):
    """The hand-picked SKUs AOI wants the sheet to lead with (JJ, 2026-09-08).

    The store's whole knowledge of them is a rank: it sorts and badges on it,
    and never learns why a SKU is on the list."""

    FEATURED = {**CATALOG, "items": [CATALOG["items"][0], {**CATALOG["items"][1], "featured_rank": 1}]}

    def setUp(self):
        super().setUp()
        self.seed()
        self.use_invite("ross-xyz")

    def test_a_ranked_sku_leads_the_default_order(self):
        # By brand, Fall/Holiday (L1) comes before Park Hill (T2); featured wins.
        self.assertEqual([p["sku"] for p in self.store.list_products()], ["L1", "T2"])
        self.ingest("catalog", self.FEATURED)
        self.assertEqual([p["sku"] for p in self.store.list_products()], ["T2", "L1"])

    def test_the_buyers_own_sort_is_never_overridden(self):
        self.ingest("catalog", self.FEATURED)
        self.assertEqual([p["sku"] for p in self.store.list_products(sort="wholesale_asc")], ["L1", "T2"])
        self.assertEqual([p["sku"] for p in self.store.list_products(sort="brand")], ["L1", "T2"])

    def test_the_sheet_badges_them_and_offers_them_on_their_own(self):
        self.ingest("catalog", self.FEATURED)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="badge-featured"', html)
        self.assertIn("1 featured</b> item", html)
        self.assertIn('name="featured" value="1"', html)
        just = self.client.get("/?featured=1").get_data(as_text=True)
        self.assertIn("Tree", just); self.assertNotIn("Lantern", just)
        self.assertIn("showing just those", just)
        self.assertIn('class="badge-featured"', self.client.get("/item/T2").get_data(as_text=True))
        self.assertNotIn('class="badge-featured"', self.client.get("/item/L1").get_data(as_text=True))

    def test_nothing_featured_leaves_the_sheet_exactly_as_it_was(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn('class="badge-featured"', html)
        self.assertNotIn("featured</b> item", html)
        self.assertNotIn('name="featured" value="1"', html)
        self.assertEqual([p["sku"] for p in self.store.list_products()], ["L1", "T2"])

    def test_the_list_is_replaced_by_each_feed_not_added_to(self):
        self.ingest("catalog", self.FEATURED)
        self.assertEqual(self.store.count_products(featured_only=True), 1)
        self.ingest("catalog", CATALOG)                       # a feed with no ranks at all
        self.assertEqual(self.store.count_products(featured_only=True), 0)

    def test_a_rank_explains_nothing_to_the_buyer(self):
        self.ingest("catalog", self.FEATURED)
        for page in ("/", "/?featured=1", "/item/T2"):
            html = self.client.get(page).get_data(as_text=True).lower()
            for hidden in ("written down", "write-down", "below cost", "featured_rank"):
                self.assertNotIn(hidden, html)


class SuggestCapTest(StoreTestCase):
    """The suggestion never exceeds what we already charge publicly (JJ, 2026-09-08).

    The flat cap alone was one number for the whole sheet, and on a written-down
    SKU it anchored the buyer far above our own published price."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.use_invite("ross-xyz")

    def _caps(self, html):
        import re
        return dict(zip(re.findall(r'name="qty\[(\w+)\]"', html),
                        re.findall(r'data-suggest-max="([\d.]+)"', html)))

    def test_the_ceiling_is_the_lower_of_the_flat_cap_and_our_published_price(self):
        caps = self._caps(self.client.get("/").get_data(as_text=True))
        # L1: $25 wholesale, published $20. Flat cap 20% off = $20.00; equal, so $20.00.
        self.assertEqual(caps["L1"], "20.00")
        # T2: $100 wholesale, published $30. Flat cap would be $80 — the published
        # price is far lower and governs.
        self.assertEqual(caps["T2"], "30.00")

    def test_an_unladdered_row_keeps_the_flat_cap(self):
        # closeout_price == wholesale is "no discount published": only the flat cap applies.
        flat = {**CATALOG, "items": [{**CATALOG["items"][1], "closeout_price": 100.0}]}
        self.ingest("catalog", flat)
        self.assertEqual(self._caps(self.client.get("/").get_data(as_text=True))["T2"], "80.00")

    def test_the_ceiling_is_never_the_floor_and_never_shown_as_a_price(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("$30.00", html)          # still not rendered as a price
        for hidden in ("floor", "closeout_price", "featured_floor"):
            self.assertNotIn(hidden, html.lower())

    def test_the_ceiling_is_computed_where_the_config_lives(self):
        from app import create_app
        app = create_app(_cfg(suggest_max_disc=0.5))
        cap = app.jinja_env.globals["suggest_cap"]
        self.assertEqual(cap({"wholesale": 100.0, "closeout_price": 30.0}), 30.0)   # published wins
        self.assertEqual(cap({"wholesale": 100.0, "closeout_price": 0.0}), 50.0)    # flat cap
        self.assertEqual(cap({"wholesale": 100.0, "closeout_price": 100.0}), 50.0)  # no markdown
        self.assertIsNone(cap({"wholesale": 0.0, "closeout_price": 30.0}))          # no anchor
        off = create_app(_cfg(suggest_max_disc=1.0)).jinja_env.globals["suggest_cap"]
        self.assertEqual(off({"wholesale": 100.0, "closeout_price": 30.0}), 30.0)   # suppressed in JS


class GalleryTest(StoreTestCase):
    """Additional images and video, on the item page (JJ, 2026-09-09).

    The sheet still shows one thumbnail; clicking in shows everything AOI has."""

    MEDIA = [{"url": "https://images.salsify.com/image/upload/s--a--/hero.jpg", "kind": "image"},
             {"url": "https://images.salsify.com/image/upload/s--b--/detail.jpg", "kind": "image"},
             {"url": "https://images.salsify.com/image/upload/s--c--/glam.jpg", "kind": "image"},
             {"url": "https://images.salsify.com/video/upload/s--d--/demo.mp4", "kind": "video"}]

    def _feed(self, media, sku="L1"):
        items = [({**it, "media": media} if it["sku"] == sku else it) for it in CATALOG["items"]]
        return {**CATALOG, "items": items}

    def setUp(self):
        super().setUp()
        self.seed()
        self.use_invite("ross-xyz")

    def test_the_gallery_arrives_in_order_and_is_read_back_in_order(self):
        self.ingest("catalog", self._feed(self.MEDIA))
        got = self.store.media_for("L1")
        self.assertEqual([m["idx"] for m in got], [0, 1, 2, 3])
        self.assertEqual([m["kind"] for m in got], ["image", "image", "image", "video"])
        self.assertEqual(self.store.media_counts(), {"image": 3, "video": 1})

    def test_the_item_page_shows_thumbnails_a_video_and_a_count(self):
        self.ingest("catalog", self._feed(self.MEDIA))
        html = self.client.get("/item/L1").get_data(as_text=True)
        self.assertIn('class="thumbs"', html)
        self.assertIn("3 photos", html)
        self.assertIn("<b>1 video</b>", html)
        self.assertIn("demo.mp4", html)                       # the video is linked
        self.assertIn('id="stageVid"', html)
        # The hero keeps coming off our own cache, never off the source url.
        self.assertIn('data-src="/img/L1"', html)
        self.assertNotIn("hero.jpg", html)
        # ...and the extras are linked straight from Salsify's CDN.
        self.assertIn("detail.jpg", html)

    def test_the_sheet_says_there_is_a_video_without_carrying_one(self):
        """A video is the best thing on the page, so the sheet points at it —
        but the gallery itself stays on the item page (JJ, 2026-09-09)."""
        self.ingest("catalog", self._feed(self.MEDIA))
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="badge-video"', html)              # L1 has one
        self.assertNotIn('class="thumbs"', html)                # ...but no gallery here
        self.assertNotIn("detail.jpg", html)
        self.assertNotIn("demo.mp4", html)

    def test_no_video_no_badge(self):
        self.ingest("catalog", self._feed(self.MEDIA[:2]))      # photos only
        self.assertFalse(self.store.product("L1")["has_video"])
        self.assertNotIn('class="badge-video"', self.client.get("/").get_data(as_text=True))

    def test_the_flag_follows_the_feed(self):
        self.ingest("catalog", self._feed(self.MEDIA))
        self.assertTrue(self.store.product("L1")["has_video"])
        self.ingest("catalog", self._feed(self.MEDIA[:1]))
        self.assertFalse(self.store.product("L1")["has_video"])

    def test_an_older_feed_leaves_the_item_page_as_it_was(self):
        html = self.client.get("/item/L1").get_data(as_text=True)     # seeded, no media
        self.assertEqual(self.store.media_for("L1"), [])
        self.assertNotIn('class="thumbs"', html)
        self.assertNotIn("more view", html)
        self.assertIn('src="/img/L1"', html)                  # the hero, exactly as before

    def test_one_picture_is_not_a_gallery(self):
        self.ingest("catalog", self._feed(self.MEDIA[:1]))
        html = self.client.get("/item/L1").get_data(as_text=True)
        self.assertNotIn('class="thumbs"', html)
        self.assertNotIn("<script>", html.split("</form>")[-1])       # no swap script either

    def test_each_feed_replaces_the_gallery_rather_than_adding_to_it(self):
        self.ingest("catalog", self._feed(self.MEDIA))
        self.ingest("catalog", self._feed(self.MEDIA[:2]))
        self.assertEqual(len(self.store.media_for("L1")), 2)
        self.ingest("catalog", CATALOG)                                # a feed with no media at all
        self.assertEqual(self.store.media_for("L1"), [])

    def test_a_netsuite_url_is_never_linked_past_the_hero(self):
        ns = "https://4253816.app.netsuite.com/core/media/media.nl?id=1&c=4253816"
        self.ingest("catalog", self._feed([{"url": ns, "kind": "image"}] + self.MEDIA[1:2]
                                          + [{"url": ns.replace("id=1", "id=2"), "kind": "image"}]))
        urls = [m["url"] for m in self.store.media_for("L1")]
        self.assertEqual(urls, [ns, self.MEDIA[1]["url"]])            # the second one is dropped
        html = self.client.get("/item/L1").get_data(as_text=True)
        self.assertNotIn("netsuite.com", html)                        # the hero goes through /img

    def test_junk_and_repeats_never_reach_the_table(self):
        self.ingest("catalog", self._feed([
            self.MEDIA[0], self.MEDIA[0],                             # repeat
            {"url": "https://img/manual.pdf", "kind": "document"},    # not showable
            {"url": "", "kind": "image"},                             # no url
            "not-a-mapping",
            self.MEDIA[1],
        ]))
        self.assertEqual([m["url"] for m in self.store.media_for("L1")],
                         [self.MEDIA[0]["url"], self.MEDIA[1]["url"]])

    def test_capped(self):
        many = [{"url": f"https://images.salsify.com/image/upload/s--x--/{n}.jpg", "kind": "image"}
                for n in range(30)]
        self.ingest("catalog", self._feed(many))
        self.assertEqual(len(self.store.media_for("L1")), D.Store.MAX_MEDIA)

    def test_the_gallery_is_recorded_in_what_the_buyer_did(self):
        self.ingest("catalog", self._feed(self.MEDIA))
        self.client.get("/item/L1")
        ev = [e for e in self.store.events_since(0) if e["kind"] == "item_viewed"][-1]
        self.assertEqual((ev["payload"]["media"], ev["payload"]["videos"]), (4, 1))


# --- price lists (JJ / Goodwill, 2026-09-09) ---------------------------------
# AOI publishes the lists and the price per SKU on each; the store decides who
# buys off which, on /admin, because the real buyers are approved here and AOI
# has never heard of them. A buyer on a list is quoted, not bidding: "Your
# price" replaces the whole bidding surface.

LIST_CUSTOMERS = {**CUSTOMERS, "items": [{**CUSTOMERS["items"][0], "price_list_id": "cost_plus_5"}]}
PRICES = {"kind": "prices", "as_of": "2026-09-09", "generated_at": "2026-09-09T07:30:00+00:00", "count": 1,
          "items": [{"list_id": "cost_plus_5", "label": "Landed cost + 5%", "prices": {"L1": 6.83}}]}


class PriceListIngestTest(StoreTestCase):
    def test_the_customers_feed_carries_the_list_and_nothing_means_the_offer_sheet(self):
        self.ingest("customers", CUSTOMERS)                      # an older AOI sends no list at all
        self.assertEqual(self.store.customer("26003")["price_list_id"], "")
        self.assertEqual(self.ingest("customers", LIST_CUSTOMERS).status_code, 202)
        self.assertEqual(self.store.customer("26003")["price_list_id"], "cost_plus_5")
        for junk in ("Landed Cost + 5%", "  ", None, "_leading"):
            self.ingest("customers", {**CUSTOMERS, "items": [{**CUSTOMERS["items"][0], "price_list_id": junk}]})
            self.assertEqual(self.store.customer("26003")["price_list_id"], "")   # never someone else's prices

    def test_prices_are_a_full_snapshot_of_the_lists_and_an_empty_one_is_legal(self):
        r = self.ingest("prices", PRICES)
        self.assertEqual((r.status_code, r.get_json()["count"]), (202, 1))
        self.assertEqual(self.store.price_lists(), [{"list_id": "cost_plus_5", "label": "Landed cost + 5%"}])
        self.assertEqual(self.store.price_list("cost_plus_5")["label"], "Landed cost + 5%")
        self.assertEqual(self.store.list_prices_for("cost_plus_5"), {"L1": 6.83})
        # a second push replaces the lot: the label moves, L1 moves, a second list
        # arrives and nothing is merged
        r = self.ingest("prices", {**PRICES, "count": 2, "items": [
            {"list_id": "cost_plus_5", "label": "Landed cost + 5% (2027)", "prices": {"L1": 7.0, "T2": 21.5}},
            {"list_id": "ladder", "label": "Published ladder", "prices": {"T2": 30.0}}]})
        self.assertEqual(r.get_json()["count"], 2)
        self.assertEqual([p["list_id"] for p in self.store.price_lists()], ["cost_plus_5", "ladder"])
        self.assertEqual(self.store.price_list("cost_plus_5")["label"], "Landed cost + 5% (2027)")
        self.assertEqual(self.store.list_prices_for("cost_plus_5"), {"L1": 7.0, "T2": 21.5})
        self.assertEqual(self.store.list_prices_for("ladder"), {"T2": 30.0})
        # and an empty list of lists is how AOI says "there are none" -- unlike customers /
        # invites, where an empty feed would wipe the allowlist and is refused
        r = self.ingest("prices", {**PRICES, "count": 0, "items": []})
        self.assertEqual((r.status_code, r.get_json()["count"]), (202, 0))
        self.assertEqual(self.store.price_lists(), [])
        self.assertEqual(self.store.list_prices_for("cost_plus_5"), {})
        self.assertIsNone(self.store.price_list("cost_plus_5"))
        self.assertEqual(self.store.list_prices_for(""), {})
        self.assertIn("prices", {f["kind"] for f in self.store.feed_status()})

    def test_a_list_with_no_slug_and_prices_that_are_not_prices_are_dropped(self):
        self.ingest("prices", {**PRICES, "items": [
            {"list_id": "cost_plus_5", "label": "", "prices": {"L1": 0, "T2": "x", "Z": 4}},
            {"list_id": "Not A Slug", "label": "junk", "prices": {"L1": 9}},
            {"label": "nameless", "prices": {"L1": 9}}]})
        self.assertEqual([p["list_id"] for p in self.store.price_lists()], ["cost_plus_5"])
        self.assertEqual(self.store.list_prices_for("cost_plus_5"), {"Z": 4.0})

    def test_how_a_price_was_reached_is_refused_at_the_door(self):
        for bad in ({"list_id": "cost_plus_5", "prices": {"L1": 6.83}, "firm_basis": "cost_plus"},
                    {"list_id": "cost_plus_5", "prices": {"L1": 6.83}, "firm_markup_pct": 5.0},
                    {"list_id": "cost_plus_5", "prices": {"L1": 6.83}, "markup": 1.05},
                    {"list_id": "cost_plus_5", "prices": {"L1": 6.83}, "price_basis": "ladder"}):
            self.assertEqual(self.ingest("prices", {**PRICES, "items": [bad]}).status_code, 422)
        self.assertEqual(self.store.price_lists(), [])


class FirmBuyerSheetTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.ingest("catalog", CATALOG)
        self.ingest("prices", PRICES)
        self.ingest("customers", LIST_CUSTOMERS)
        self.login()

    def test_the_sheet_is_our_price_and_nothing_to_bid_with(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Your price", html)
        self.assertIn("$6.83", html)
        self.assertIn("Lantern", html)
        self.assertNotIn("Tree", html)                              # T2 is not on their list: not on their sheet
        self.assertNotIn("$25.00", html)                            # no wholesale anywhere
        self.assertNotIn('name="price[L1]"', html)                  # no offer box
        self.assertNotIn("targetMargin", html)                      # no margin tools
        self.assertNotIn("MAX_DISC", html)                          # and no suggestion block at all
        self.assertNotIn("% of wholesale", html)
        self.assertNotIn("Landed cost", html)                       # the label is ours, never theirs
        self.assertIn("MSRP", html); self.assertIn("$62.99", html)   # MSRP still anchors it
        self.assertIn('name="qty[L1]"', html)
        self.assertIn("order request", html)

    def test_the_item_page_quotes_or_says_price_on_request(self):
        page = self.client.get("/item/L1").get_data(as_text=True)
        self.assertIn("your price", page); self.assertIn("$6.83", page)
        self.assertNotIn("original wholesale", page); self.assertNotIn("$25.00", page)
        self.assertNotIn('name="price[L1]"', page)
        self.assertIn('name="qty[L1]"', page)
        other = self.client.get("/item/T2").get_data(as_text=True)
        self.assertIn("Price on request", other)
        self.assertNotIn('name="qty[T2]"', other)                   # nothing to order until we quote it
        self.assertNotIn("$100.00", other)

    def test_under_a_dollar_figure_filters_on_the_price_the_buyer_can_see(self):
        self.ingest("prices", {**PRICES, "items": [{**PRICES["items"][0], "prices": {"L1": 6.83, "T2": 21.5}}]})
        both = self.client.get("/?max_price=25").get_data(as_text=True)
        self.assertIn("Lantern", both); self.assertIn("Tree", both)
        under7 = self.client.get("/?max_price=7").get_data(as_text=True)
        self.assertIn("Lantern", under7); self.assertNotIn("Tree", under7)
        self.assertIn('name="max_price" type="number" min="0" step="0.01" value="7"', under7)
        # composes with the other filters, and rides paging
        self.assertIn("Nothing matches",
                      self.client.get("/?max_price=7&brand=Park+Hill+Collection").get_data(as_text=True))
        self.assertIn("max_price=7", self.client.get("/?max_price=7&page=1").get_data(as_text=True))
        self.assertEqual(self.client.get("/?max_price=0.01").status_code, 200)

    def test_an_offer_buyer_gets_the_same_filter_on_wholesale(self):
        self.client.post("/logout")
        self.ingest("invites", INVITES)
        self.use_invite("ross-xyz")
        html = self.client.get("/?max_price=50").get_data(as_text=True)
        self.assertIn("Lantern", html); self.assertNotIn("Tree", html)       # $25 in, $100 out
        self.assertIn("$25.00", html)                                        # and the sheet is otherwise unchanged
        self.assertIn('name="price[L1]"', html)

    def test_a_list_that_stops_being_published_drops_the_buyer_back_on_the_offer_sheet(self):
        """AOI deactivating a list must not leave anyone staring at an empty
        sheet: it silently costs them their prices, not their access."""
        self.assertIn("Your price", self.client.get("/").get_data(as_text=True))
        self.ingest("prices", {**PRICES, "count": 0, "items": []})
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("$25.00", html); self.assertIn('name="price[L1]"', html)   # wholesale and a blank box
        self.assertIn("Tree", html)                                              # and the whole catalog back
        self.assertNotIn("Your price", html)
        self.assertEqual(self.store.customer("26003")["price_list_id"], "cost_plus_5")   # the assignment stands


class FirmBuyerOrderTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.ingest("catalog", CATALOG)
        self.ingest("prices", PRICES)
        self.ingest("customers", LIST_CUSTOMERS)
        self.login()

    def test_the_line_is_priced_by_us_whatever_the_buyer_posts(self):
        self.client.post("/offer/set", data={"qty[L1]": "12", "price[L1]": "0.25"})
        self.assertEqual(self.store.draft("cust:26003"), {"L1": {"qty": 12, "price": 6.83}})
        r = self.client.post("/offer/line", json={"sku": "L1", "qty": "18", "price": "999"}).get_json()
        self.assertEqual(r["line"], {"qty": 18, "price": 6.83})
        # a SKU their list does not carry cannot be ordered at any price
        self.client.post("/offer/set", data={"qty[T2]": "8", "price[T2]": "35"})
        self.assertNotIn("T2", self.store.draft("cust:26003"))

    def test_review_reads_as_an_order_request_at_our_prices(self):
        self.client.post("/offer/set", data={"qty[L1]": "12"})
        html = self.client.get("/offer").get_data(as_text=True)
        self.assertIn("Your order request", html)
        self.assertIn("Your price", html); self.assertIn("$6.83", html); self.assertIn("$81.96", html)   # 12 x 6.83
        self.assertNotIn("$25.00", html); self.assertNotIn("% of wholesale", html)
        self.assertNotIn('name="price[L1]"', html); self.assertNotIn("targetMargin", html)
        self.assertIn("Send order request", html)
        rows = self.client.get("/offer.csv").get_data(as_text=True).strip().splitlines()
        self.assertNotIn("wholesale", rows[0]); self.assertIn("price", rows[0])
        self.assertIn("L1,Lantern", rows[1]); self.assertIn("6.83", rows[1])
        self.assertNotIn("25.0", rows[1])

    def test_submitting_carries_the_list_it_was_quoted_off_and_our_prices(self):
        self.client.post("/offer/set", data={"qty[L1]": "12", "price[L1]": "0.25"})
        r = self.client.post("/offer/submit", data={"company": "Goodwill MN", "email": "jen@goodwill.test"})
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn("Order request received", page); self.assertNotIn("acceptance or a counter", page)
        it = self.store.pull_outbox()[0]
        p = it["payload"]
        self.assertEqual((it["kind"], p["price_mode"], p["price_list_id"], p["customer_id"]),
                         ("offer", "firm", "cost_plus_5", "26003"))
        self.assertEqual([(l["sku"], l["qty"], l["offer_price"]) for l in p["lines"]], [("L1", 12, 6.83)])
        self.assertEqual(p["total"], 81.96)
        self.assertEqual(p["wholesale_total"], 300.0)                   # AOI still gets the anchor; the buyer never did
        ev = [e for e in self.store.events_since(0) if e["kind"] == "offer_submitted"][-1]
        self.assertEqual((ev["payload"]["price_mode"], ev["payload"]["price_list_id"]), ("firm", "cost_plus_5"))
        buyer_mail = self.sent[-1]
        self.assertIn("We have your order request", buyer_mail["subject"])
        self.assertIn("order request", buyer_mail["html"])
        self.assertIn("confirm", buyer_mail["html"])
        for never in ("Wholesale", "% whsl", "$25.00", "Landed cost"):
            self.assertNotIn(never, buyer_mail["html"])
        self.assertNotIn("Whsl", buyer_mail["body"])
        self.assertEqual(buyer_mail["attachments"][0][0], "order-request-OF-%d.csv" % it["id"])
        self.assertNotIn(b"25.0", buyer_mail["attachments"][0][1])

    def test_an_offer_buyer_is_untouched_and_says_so_in_the_payload(self):
        self.client.post("/logout")
        self.ingest("customers", CUSTOMERS)                      # off the list again
        with self.store.engine.begin() as conn:                  # so login() cannot pick up the spent token
            conn.execute(sa.delete(D.login_tokens))
        self.login()
        self.client.post("/offer/set", data={"qty[L1]": "24", "price[L1]": "10"})
        self.assertEqual(self.store.draft("cust:26003"), {"L1": {"qty": 24, "price": 10.0}})
        html = self.client.get("/offer").get_data(as_text=True)
        self.assertIn("Your offer", html); self.assertIn("% of wholesale", html)
        self.client.post("/offer/submit", data={"company": "Adeline Collective", "email": "donna-n@live.com"})
        p = self.store.pull_outbox()[0]["payload"]
        self.assertEqual(p["price_mode"], "offer")
        self.assertNotIn("price_list_id", p)                     # nothing quoted it, so nothing to name
        self.assertEqual([(l["sku"], l["offer_price"]) for l in p["lines"]], [("L1", 10.0)])
        self.assertIn("We have your offer", self.sent[-1]["subject"])

    def test_a_price_that_moves_moves_the_line_with_it(self):
        self.client.post("/offer/set", data={"qty[L1]": "12"})
        self.ingest("prices", {**PRICES, "items": [{**PRICES["items"][0], "prices": {"L1": 7.5}}]})
        html = self.client.get("/offer").get_data(as_text=True)
        self.assertIn("$7.50", html); self.assertIn("$90.00", html)
        # the list survives with L1 off it: still quoted, just not on this item
        self.ingest("prices", {**PRICES, "items": [{**PRICES["items"][0], "prices": {"T2": 21.5}}]})
        self.assertIn("Nothing on it yet", self.client.get("/offer").get_data(as_text=True))


class GoodwillBuyerTest(StoreTestCase):
    """The case this was built for: Goodwill of Minnesota is a ``buyers`` row
    approved on /admin, not an AOI allowlist account (JJ, 2026-09-02). The list
    is assigned here, beside their class, and that is what puts them on firm
    prices."""

    def setUp(self):
        super().setUp()
        self.ingest("catalog", CATALOG)
        self.ingest("prices", PRICES)
        self.buyer = self.app.test_client()
        self.client.post("/login", data={"email": "admin@gerson.test"})
        import re as _re
        self.client.get(_re.search(r"http://store\.test(/[^\s]+)", self.sent[-1]["body"]).group(1))

    def _approve(self, **extra):
        self.buyer.post("/apply", data={"company": "Goodwill of Minnesota", "contact": "Jennifer",
                                        "email": "jen@goodwill.test", "resale_number": "MN-1"})
        b = self.store.buyer_for_email("jen@goodwill.test")
        self.client.post(f"/admin/buyers/{b['id']}/status", data={"status": "approved", **extra})
        import re as _re
        link = _re.search(r"http://store\.test(/login/[^\s]+)",
                          next(m for m in self.sent if m["to"] == "jen@goodwill.test" and "/login/" in m["body"])["body"])
        self.buyer.get(link.group(1))
        return b

    def test_the_admin_dropdown_puts_a_self_signed_up_buyer_on_firm_prices(self):
        b = self._approve()
        html = self.buyer.get("/").get_data(as_text=True)
        self.assertIn("$25.00", html); self.assertIn('name="price[L1]"', html)      # bidding, to start with
        # the dropdown is on the buyers table, next to their class
        admin = self.client.get("/admin/").get_data(as_text=True)
        self.assertIn("Price list", admin); self.assertIn("Offer sheet", admin)
        self.assertIn('<option value="cost_plus_5" >Landed cost + 5%</option>', admin)
        r = self.client.post(f"/admin/buyers/{b['id']}/price_list",
                             data={"price_list_id": "cost_plus_5"}, follow_redirects=True)
        self.assertIn("Landed cost + 5%", r.get_data(as_text=True))
        self.assertEqual(self.store.buyer(b["id"])["price_list_id"], "cost_plus_5")
        self.assertIn('<option value="cost_plus_5" selected>Landed cost + 5%</option>',
                      self.client.get("/admin/").get_data(as_text=True))
        # and that is all it takes: no wholesale, no offer box, our price
        html = self.buyer.get("/").get_data(as_text=True)
        self.assertIn("Your price", html); self.assertIn("$6.83", html)
        self.assertNotIn("$25.00", html); self.assertNotIn('name="price[L1]"', html)
        self.assertNotIn("Tree", html)
        self.buyer.post("/offer/set", data={"qty[L1]": "12", "price[L1]": "0.25"})
        self.buyer.post("/offer/submit", data={"company": "Goodwill of Minnesota", "contact": "Jennifer",
                                               "email": "jen@goodwill.test"})
        p = [i for i in self.store.pull_outbox() if i["kind"] == "offer"][0]["payload"]
        self.assertEqual((p["price_mode"], p["price_list_id"], p["customer_id"]), ("firm", "cost_plus_5", None))
        self.assertEqual([(l["sku"], l["qty"], l["offer_price"]) for l in p["lines"]], [("L1", 12, 6.83)])
        self.assertEqual(p["buyer"]["kind"], "buyer")

    def test_a_list_can_be_taken_away_again_and_junk_never_assigns_one(self):
        b = self._approve()
        self.store.set_buyer_price_list(b["id"], "cost_plus_5")
        r = self.client.post(f"/admin/buyers/{b['id']}/price_list", data={"price_list_id": ""}, follow_redirects=True)
        self.assertIn("back on the offer sheet", r.get_data(as_text=True))
        self.assertIsNone(self.store.buyer(b["id"])["price_list_id"])
        self.assertIn('name="price[L1]"', self.buyer.get("/").get_data(as_text=True))
        # a list we have never been fed is not a list
        self.assertEqual(self.store.set_buyer_price_list(b["id"], "made_up"), "")
        self.assertEqual(self.app.test_client().post(f"/admin/buyers/{b['id']}/price_list",
                                                     data={"price_list_id": "cost_plus_5"}).status_code, 404)
        self.assertIsNone(self.store.buyer(b["id"])["price_list_id"])   # not an admin, not assigned

    def test_the_list_can_be_set_on_the_way_in_beside_approve(self):
        b = self._approve(price_list_id="cost_plus_5", buyer_class="liquidator")
        row = self.store.buyer(b["id"])
        self.assertEqual((row["price_list_id"], row["buyer_class"]), ("cost_plus_5", "liquidator"))
        self.assertIn("Your price", self.buyer.get("/").get_data(as_text=True))


class SizedUrlTest(unittest.TestCase):
    """Gallery urls are asked for at the size the slot needs.

    Salsify hands out the untouched master: the asset behind one 56px thumbnail
    measured **13 MB and 6648px wide** on 2026-09-09, so an item page with seven
    views was ~90 MB. Cloudinary transformations fix it, but only after the
    signature segment — before it, the url 404s."""

    IMG = "https://images.salsify.com/image/upload/s--boSg-_7H--/pzprql3zoeyyezlv6so5.jpg"
    VID = "https://images.salsify.com/video/upload/s--lX-r9FnZ--/qqamoxbcbgu7fhok8exh.mp4"

    def test_the_transformation_goes_after_the_signature(self):
        from store.images import sized
        self.assertEqual(
            sized(self.IMG, 120),
            "https://images.salsify.com/image/upload/s--boSg-_7H--/"
            "w_120,c_limit,f_auto,q_auto/pzprql3zoeyyezlv6so5.jpg")

    def test_video_is_left_alone(self):
        from store.images import sized
        self.assertEqual(sized(self.VID, 900), self.VID)

    def test_an_already_transformed_url_is_not_transformed_twice(self):
        from store.images import sized
        u = "https://images.salsify.com/image/upload/s--x--/w_120,c_limit/a.jpg"
        self.assertEqual(sized(u, 900), u)

    def test_anything_we_do_not_recognise_comes_back_untouched(self):
        from store.images import sized
        for u in ("https://4253816.app.netsuite.com/core/media/media.nl?id=1",
                  "https://example.test/a.jpg", "/img/L1", "", None):
            self.assertEqual(sized(u, 120), str(u or ""))

    def test_no_width_is_no_transformation(self):
        from store.images import sized
        self.assertEqual(sized(self.IMG, 0), self.IMG)


class GallerySizesTest(StoreTestCase):
    """What the item page actually asks the browser to download."""

    MEDIA = [{"url": "https://images.salsify.com/image/upload/s--a--/hero.jpg", "kind": "image"},
             {"url": "https://images.salsify.com/image/upload/s--b--/detail.jpg", "kind": "image"},
             {"url": "https://images.salsify.com/video/upload/s--c--/demo.mp4", "kind": "video"}]

    def setUp(self):
        super().setUp()
        items = [({**it, "media": self.MEDIA} if it["sku"] == "L1" else it) for it in CATALOG["items"]]
        self.ingest("catalog", {**CATALOG, "items": items})
        self.ingest("customers", CUSTOMERS); self.ingest("invites", INVITES)
        self.use_invite("ross-xyz")

    def test_thumbnails_ask_for_thumbnails_and_the_stage_for_a_stage(self):
        html = self.client.get("/item/L1").get_data(as_text=True)
        self.assertIn("w_120,c_limit,f_auto,q_auto/detail.jpg", html)      # the <img> in the strip
        self.assertIn("w_900,c_limit,f_auto,q_auto/detail.jpg", html)      # what clicking it loads
        # Nothing anywhere asks for the untransformed master.
        self.assertNotIn("s--b--/detail.jpg", html)

    def test_the_video_url_is_never_resized(self):
        html = self.client.get("/item/L1").get_data(as_text=True)
        self.assertIn("s--c--/demo.mp4", html)
        self.assertNotIn("w_900,c_limit,f_auto,q_auto/demo.mp4", html)

    def test_the_hero_still_comes_from_our_own_cache(self):
        html = self.client.get("/item/L1").get_data(as_text=True)
        self.assertIn('id="stageImg" src="/img/L1"', html)
        self.assertNotIn("s--a--/hero.jpg", html)
