"""NEW on the sheet, and the new-arrivals digest — store.digest, the sheet's
badge / filter / sort, the admin buttons, and the once-a-week auto-send.

AOI stamps every catalog row with ``listed_since``; this is what the store
does with it. Nothing here shows or mails a closeout price.
"""
import unittest
from datetime import date, timedelta

import sqlalchemy as sa

from app import create_app
from store import db as D
from store import digest
from tests.test_store import CATALOG, _cfg, StoreTestCase

TODAY = date.today()
RECENT = (TODAY - timedelta(days=2)).isoformat()
OLD = (TODAY - timedelta(days=40)).isoformat()

CATALOG_NEW = {**CATALOG, "items": [
    {**CATALOG["items"][0], "listed_since": RECENT, "price_changed_at": None, "price_was": None},          # L1: new
    {**CATALOG["items"][1], "listed_since": OLD, "price_changed_at": RECENT, "price_was": 40.0},           # T2: old
    {**CATALOG["items"][0], "sku": "Z9", "description": "Zebra", "brand": "Fall/Holiday", "listed_since": None},  # before we kept track
]}


def _buyer(store, email, status="approved", company="Ross Stores"):
    with store.engine.begin() as conn:
        conn.execute(D.buyers.insert().values(company=company, contact="Pat", email=email, phone="", notes="",
                                              status=status, buyer_class="regional", created_at=D.now_iso(),
                                              updated_at=D.now_iso()))


class IngestFieldsTest(StoreTestCase):
    def test_badge_fields_are_stored(self):
        self.ingest("catalog", CATALOG_NEW)
        l1, t2, z9 = self.store.product("L1"), self.store.product("T2"), self.store.product("Z9")
        self.assertEqual(l1["listed_since"], RECENT)
        self.assertIsNone(l1["price_was"])
        self.assertEqual((t2["listed_since"], t2["price_changed_at"], t2["price_was"]), (OLD, RECENT, 40.0))
        self.assertIsNone(z9["listed_since"])

    def test_newest_sort_and_new_filter(self):
        self.ingest("catalog", CATALOG_NEW)
        order = [p["sku"] for p in self.store.list_products(sort="newest")]
        self.assertEqual(order, ["L1", "T2", "Z9"])                     # unknown-since sorts last
        since = (TODAY - timedelta(days=14)).isoformat()
        self.assertEqual([p["sku"] for p in self.store.list_products(new_since=since)], ["L1"])
        self.assertEqual(self.store.count_products(new_since=since), 1)


class SheetTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.ingest("catalog", CATALOG_NEW); self.ingest("customers", __import__("tests.test_store", fromlist=["CUSTOMERS"]).CUSTOMERS)
        self.login()

    def test_badge_header_and_filter(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("1 new</b> item added to the sheet in the last 14 days", html)
        self.assertEqual(html.count('class="badge-new"'), 1)
        self.assertIn(f"on the sheet since {RECENT}", html)
        only = self.client.get("/?new=1&sort=newest").get_data(as_text=True)
        self.assertIn("Lantern", only)
        self.assertNotIn("Zebra", only)
        self.assertIn("showing just those", only)
        item = self.client.get("/item/L1").get_data(as_text=True)
        self.assertIn('class="badge-new"', item)
        self.assertNotIn('class="badge-new"', self.client.get("/item/T2").get_data(as_text=True))

    def test_no_price_leaks_into_the_badge_or_digest(self):
        html = self.client.get("/?new=1").get_data(as_text=True)
        for needle in ("closeout_price", "$20.00", "20%", "2027-01-17", "$16.25"):     # L1's ladder price, discount and step-down
            self.assertNotIn(needle, html)
        page = digest.html(digest.build(self.store, since=(TODAY - timedelta(days=14)).isoformat()),
                           since="x", base_url="http://store.test")
        for needle in ("$20.00", "20%", "2027-01-17", "$16.25"):
            self.assertNotIn(needle, page)


class DigestTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.ingest("catalog", CATALOG_NEW)
        self.ctx = self.app.config["STORE"]
        self.since = (TODAY - timedelta(days=7)).isoformat()

    def test_build_text_and_html(self):
        items = digest.build(self.store, since=self.since)
        self.assertEqual([p["sku"] for p in items], ["L1"])
        page = digest.html(items, since=self.since, base_url="http://store.test", days=7)
        self.assertIn("<b>1 new item</b>", page)
        self.assertIn("Lantern", page)
        self.assertIn("inner 6 &middot; master 24", page)
        self.assertIn("$25.00", page)
        self.assertIn("http://store.test/?new=1&amp;sort=newest", page)
        body = digest.text(items, since=self.since, base_url="http://store.test")
        self.assertIn("- Lantern  (L1 · Fall/Holiday · Christmas)", body)
        self.assertIn("http://store.test/?new=1&sort=newest", body)
        self.assertEqual(digest.subject(1, 7), "1 new closeout item on the Gerson sheet this week")

    def test_send_goes_to_approved_buyers_only_and_is_recorded(self):
        _buyer(self.store, "pat@ross.test")
        _buyer(self.store, "wait@pending.test", status="pending", company="Pending Co")
        _buyer(self.store, "gone@suspended.test", status="suspended", company="Gone Co")
        r = digest.send(self.ctx, since=self.since, sent_by="admin@gerson.test")
        self.assertEqual((r["sent"], r["failed"], r["items"]), (1, 0, 1))
        self.assertEqual([m["to"] for m in self.sent], ["pat@ross.test"])
        self.assertIn("1 new closeout item", self.sent[0]["subject"])
        self.assertIn("Lantern", self.sent[0]["html"])
        last = self.store.last_digest("new_arrivals")
        self.assertEqual((last["items"], last["recipients"], last["sent_by"]), (1, 1, "admin@gerson.test"))

    def test_nothing_new_or_nobody_approved_sends_nothing(self):
        r = digest.send(self.ctx, since=self.since)
        self.assertEqual(r["sent"], 0)
        self.assertIn("no approved buyer", r["reason"])
        r = digest.send(self.ctx, since=(TODAY + timedelta(days=1)).isoformat())
        self.assertEqual(r["sent"], 0)
        self.assertIn("nothing went on the sheet", r["reason"])
        self.assertEqual(self.sent, [])

    def test_status_for_admin(self):
        _buyer(self.store, "pat@ross.test")
        s = digest.status(self.ctx)
        self.assertEqual((s["new_count"], s["recipients"], s["days"], s["weekday"]), (1, 1, 7, ""))
        self.assertIsNone(s["last"])


class AutoSendTest(unittest.TestCase):
    def _app(self, weekday):
        app = create_app(_cfg(digest_weekday=weekday))
        app.config["TESTING"] = True
        from unittest import mock
        self.sent = []
        patcher = mock.patch("store.mail.send", side_effect=lambda cfg, **kw: (self.sent.append(kw) or True))
        patcher.start(); self.addCleanup(patcher.stop)
        store = app.config["STORE"].store
        store.ingest_catalog(CATALOG_NEW["items"], as_of="2026-09-01", generated_at=None)
        _buyer(store, "pat@ross.test")
        return app

    def test_off_by_default(self):
        app = self._app("")
        self.assertIsNone(digest.maybe_auto_send(app.config["STORE"]))
        self.assertEqual(self.sent, [])

    def test_sends_on_its_weekday_once_a_week(self):
        wd = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[TODAY.weekday()]
        app = self._app(wd)
        ctx = app.config["STORE"]
        r = digest.maybe_auto_send(ctx, today=TODAY)
        self.assertEqual(r["sent"], 1)
        self.assertIsNone(digest.maybe_auto_send(ctx, today=TODAY))                       # not twice today
        self.assertIsNone(digest.maybe_auto_send(ctx, today=TODAY + timedelta(days=1)))   # nor tomorrow
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(ctx.store.last_digest("new_arrivals")["sent_by"], "auto")

    def test_wrong_weekday_waits(self):
        other = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[(TODAY.weekday() + 1) % 7]
        app = self._app(other)
        self.assertIsNone(digest.maybe_auto_send(app.config["STORE"], today=TODAY))

    def test_rides_the_catalog_ingest(self):
        wd = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[TODAY.weekday()]
        app = self._app(wd)
        r = app.test_client().post("/ingest/catalog", json=CATALOG_NEW, headers={"X-API-Key": "store-key-123"})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(len(self.sent), 1)
        app.test_client().post("/ingest/catalog", json=CATALOG_NEW, headers={"X-API-Key": "store-key-123"})
        self.assertEqual(len(self.sent), 1)                                                # a re-run does not double-mail


class AdminRoutesTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.ingest("catalog", CATALOG_NEW)
        _buyer(self.store, "pat@ross.test")

    def _admin(self):
        with self.client.session_transaction() as s:
            s["admin_email"] = "admin@gerson.test"

    def test_404_without_admin(self):
        self.assertEqual(self.client.get("/admin/digest/preview").status_code, 404)
        self.assertEqual(self.client.post("/admin/digest/send").status_code, 404)

    def test_home_preview_and_send(self):
        self._admin()
        home = self.client.get("/admin/").get_data(as_text=True)
        self.assertIn("New-arrivals digest", home)
        self.assertIn("<b>1</b> item went on the sheet", home)
        self.assertIn("Send to 1 approved buyer<", home)
        prev = self.client.get("/admin/digest/preview").get_data(as_text=True)
        self.assertIn("Lantern", prev)
        r = self.client.post("/admin/digest/send", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("sent to 1 buyer", r.get_data(as_text=True))
        self.assertEqual([m["to"] for m in self.sent], ["pat@ross.test"])
        home = self.client.get("/admin/").get_data(as_text=True)
        self.assertIn("Last sent", home)
        self.assertIn("by admin@gerson.test", home)


if __name__ == "__main__":
    unittest.main()
