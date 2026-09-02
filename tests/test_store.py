"""Gerson Closeout Offers — ingest gate, isolation, invites, login, the offer sheet, outbox."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import sqlalchemy as sa

from app import create_app
from config import Config
from store import db as D

KEY = "store-key-123"


def _cfg(**over) -> Config:
    base = dict(secret_key="test-secret", store_ingest_key=KEY, database_url="sqlite://", base_url="http://store.test",
                mail_backend="log", smtp_host="", smtp_port=587, smtp_user="", smtp_password="",
                mail_from="x@y", login_token_minutes=30, graph_tenant_id="", graph_client_id="",
                graph_client_secret="", graph_sender_mailbox="", offer_notify_emails="offers@gerson.test; jj@gerson.test",
                website_url="https://shop.gerson.test")
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
        self.assertIn(">96<", html)
        for hidden in ("$20.00", "$30.00", "20% off", "70% off", "Drops to", "16.25"):
            self.assertNotIn(hidden, html)                                    # no closeout price, tier or step-down
        self.assertIn('name="qty[L1]"', html); self.assertIn('name="price[L1]"', html)
        item = self.client.get("/item/L1").get_data(as_text=True)
        self.assertIn("UPC 1", item); self.assertIn("original wholesale", item); self.assertNotIn("$20.00", item)
        self.assertEqual(self.client.get("/item/NOPE").status_code, 404)

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
        self.assertEqual(p["buyer"], {"kind": "invite", "key": "inv:ross-xyz", "label": "Ross Stores", "customer_id": None, "invite_token": "ross-xyz"})
        self.assertEqual([(l["sku"], l["qty"], l["offer_price"], l["wholesale"]) for l in p["lines"]], [("L1", 24, 10.0, 25.0), ("T2", 8, 35.0, 100.0)])
        self.assertIn("take-all", p["notes"])
        # two designated addresses + the buyer's copy, each with the CSV attached
        self.assertEqual([m["to"] for m in self.sent], ["offers@gerson.test", "jj@gerson.test", "sam@ross.test"])
        self.assertIn("Closeout offer OF-1: Ross Stores", self.sent[0]["subject"])
        self.assertIn("L1", self.sent[0]["body"]); self.assertIn("37% of $1,400.00 wholesale", self.sent[0]["body"])
        self.assertEqual(self.sent[0]["attachments"][0][0], "offer-OF-1.csv")
        self.assertIn(b"L1,Lantern", self.sent[0]["attachments"][0][1])
        self.assertIn("We received your offer", self.sent[2]["subject"])
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
