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


def _buyer(store, email, status="approved", company="Ross Stores", cadence="weekly"):
    with store.engine.begin() as conn:
        conn.execute(D.buyers.insert().values(company=company, contact="Pat", email=email, phone="", notes="",
                                              status=status, buyer_class="regional", digest_cadence=cadence,
                                              created_at=D.now_iso(), updated_at=D.now_iso()))


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


WD = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class CadenceTest(unittest.TestCase):
    """Per-buyer cadence (buyer feedback via JJ, 2026-09-10): Kendra once a
    month, others as it lands, and nobody ever sees the same item twice."""

    def _app(self, weekday, buyers, listed=TODAY):
        app = create_app(_cfg(digest_weekday=weekday))
        app.config["TESTING"] = True
        from unittest import mock
        self.sent = []
        patcher = mock.patch("store.mail.send", side_effect=lambda cfg, **kw: (self.sent.append(kw) or True))
        patcher.start(); self.addCleanup(patcher.stop)
        store = app.config["STORE"].store
        items = [{**CATALOG["items"][0], "listed_since": listed.isoformat()}, {**CATALOG["items"][1], "listed_since": OLD}]
        store.ingest_catalog(items, as_of=listed.isoformat(), generated_at=None)
        for email, cadence in buyers:
            _buyer(store, email, cadence=cadence, company=email.split("@")[0])
        return app

    def _mailed(self):
        return sorted(m["to"] for m in self.sent)

    def test_clean_cadence_never_falls_to_never(self):
        self.assertEqual([D.clean_cadence(v) for v in ("daily", "MONTHLY", "never", "", None, "hourly")],
                         ["daily", "monthly", "never", "weekly", "weekly", "weekly"])

    def test_recipients_by_cadence_and_never_is_left_alone(self):
        app = self._app("", [("d@x.test", "daily"), ("w@x.test", "weekly"), ("m@x.test", "monthly"), ("n@x.test", "never")])
        store = app.config["STORE"].store
        self.assertEqual(sorted(b["email"] for b in digest.recipients(store)), ["d@x.test", "m@x.test", "w@x.test"])
        self.assertEqual([b["email"] for b in digest.recipients(store, cadence="monthly")], ["m@x.test"])
        # "Send now" from /admin: everyone who takes email, never the "never"s
        r = digest.send(app.config["STORE"], since=(TODAY - timedelta(days=7)).isoformat(), sent_by="admin@gerson.test")
        self.assertEqual(r["sent"], 3)
        self.assertNotIn("n@x.test", self._mailed())
        self.assertEqual(store.last_digest("new_arrivals")["cadence"], "")

    def test_master_switch_off_means_nobody_hears_even_daily(self):
        app = self._app("", [("d@x.test", "daily")])
        self.assertIsNone(digest.maybe_auto_send(app.config["STORE"], today=TODAY))
        self.assertEqual(self.sent, [])

    def test_daily_goes_any_day_something_landed_and_only_once(self):
        other = WD[(TODAY.weekday() + 1) % 7]               # not the weekly day: daily does not care
        app = self._app(other, [("d@x.test", "daily"), ("w@x.test", "weekly")])
        ctx = app.config["STORE"]
        r = digest.maybe_auto_send(ctx, today=TODAY)
        self.assertEqual((r["sent"], self._mailed()), (1, ["d@x.test"]))
        self.assertIn("today", self.sent[0]["subject"])
        self.assertIsNone(digest.maybe_auto_send(ctx, today=TODAY))                        # not twice in a day
        self.assertEqual(ctx.store.last_digest("new_arrivals", cadence="daily")["sent_by"], "auto")
        self.assertIsNone(ctx.store.last_digest("new_arrivals", cadence="weekly"))

    def test_window_starts_the_day_after_the_last_send(self):
        app = self._app(WD[(TODAY.weekday() + 1) % 7], [("d@x.test", "daily")])
        ctx = app.config["STORE"]
        digest.maybe_auto_send(ctx, today=TODAY)
        self.assertIn("Lantern", self.sent[-1]["html"])
        tomorrow = TODAY + timedelta(days=1)
        # Nothing new tomorrow: the daily buyer hears nothing, and Lantern is not re-sent.
        self.assertIsNone(digest.maybe_auto_send(ctx, today=tomorrow))
        # Something lands the day after: only that goes.
        ctx.store.ingest_catalog([{**CATALOG["items"][0], "listed_since": TODAY.isoformat()},
                                  {**CATALOG["items"][1], "listed_since": tomorrow.isoformat()}],
                                 as_of=tomorrow.isoformat(), generated_at=None)
        r = digest.maybe_auto_send(ctx, today=tomorrow)
        self.assertEqual(r["sent"], 1)
        self.assertIn("Tree", self.sent[-1]["html"])
        self.assertNotIn("Lantern", self.sent[-1]["html"])
        self.assertEqual(r["runs"][0]["since"], (TODAY + timedelta(days=1)).isoformat())

    def test_weekly_and_monthly_share_the_weekday_and_monthly_waits_four_weeks(self):
        wd = WD[TODAY.weekday()]
        app = self._app(wd, [("w@x.test", "weekly"), ("m@x.test", "monthly")])
        ctx = app.config["STORE"]
        r = digest.maybe_auto_send(ctx, today=TODAY)
        self.assertEqual((r["sent"], self._mailed()), (2, ["m@x.test", "w@x.test"]))
        self.assertEqual(sorted(x["cadence"] for x in r["runs"]), ["monthly", "weekly"])
        # A week on, something new: weekly hears, monthly does not.
        wk = TODAY + timedelta(days=7)
        ctx.store.ingest_catalog([{**CATALOG["items"][0], "listed_since": wk.isoformat()}], as_of=wk.isoformat(), generated_at=None)
        r = digest.maybe_auto_send(ctx, today=wk)
        self.assertEqual([x["cadence"] for x in r["runs"]], ["weekly"])
        # Four weeks on: both.
        mo = TODAY + timedelta(days=28)
        ctx.store.ingest_catalog([{**CATALOG["items"][0], "listed_since": mo.isoformat()}], as_of=mo.isoformat(), generated_at=None)
        r = digest.maybe_auto_send(ctx, today=mo)
        self.assertEqual(sorted(x["cadence"] for x in r["runs"]), ["monthly", "weekly"])
        monthly = [x for x in r["runs"] if x["cadence"] == "monthly"][0]
        self.assertEqual(monthly["since"], (TODAY + timedelta(days=1)).isoformat())          # the day after its own last send
        self.assertIn("this month", [m for m in self.sent if m["to"] == "m@x.test"][-1]["subject"])
        # Not on another weekday, whatever the gap.
        self.assertIsNone(digest.maybe_auto_send(ctx, today=mo + timedelta(days=1)))


class FeaturedMailTest(StoreTestCase):
    """Hot deals went on the sheet; tell the buyers (JJ, 2026-09-10)."""
    FEATURED = {**CATALOG, "items": [CATALOG["items"][0], {**CATALOG["items"][1], "featured_rank": 1}]}

    def setUp(self):
        super().setUp()
        self.ctx = self.app.config["STORE"]

    def test_send_featured_goes_to_everyone_who_takes_mail_and_says_no_price(self):
        self.ingest("catalog", CATALOG)
        _buyer(self.store, "pat@ross.test")
        r = digest.send_featured(self.ctx, sent_by="admin@gerson.test")
        self.assertEqual((r["sent"], r["reason"]), (0, "nothing is featured right now"))
        self.ingest("catalog", self.FEATURED)
        _buyer(self.store, "kendra@monthly.test", cadence="monthly", company="Kendra Co")
        _buyer(self.store, "quiet@never.test", cadence="never", company="Quiet Co")
        r = digest.send_featured(self.ctx, sent_by="admin@gerson.test")
        self.assertEqual((r["sent"], r["items"]), (2, 1))
        self.assertEqual(sorted(m["to"] for m in self.sent), ["kendra@monthly.test", "pat@ross.test"])
        m = self.sent[0]
        self.assertEqual(m["subject"], "1 featured closeout item on the Gerson sheet — our sharpest prices")
        self.assertIn("Tree", m["html"]); self.assertNotIn("Lantern", m["html"])
        self.assertIn("http://store.test/?featured=1", m["html"]); self.assertIn("http://store.test/?featured=1", m["body"])
        self.assertIn("$100.00", m["html"])                                                  # wholesale
        for hidden in ("$30.00", "70%", "featured_rank", "closeout_price"):                  # not the ladder, not the flag
            self.assertNotIn(hidden, m["html"]); self.assertNotIn(hidden, m["body"])
        last = self.store.last_digest("featured")
        self.assertEqual((last["items"], last["recipients"], last["sent_by"]), (1, 2, "admin@gerson.test"))
        self.assertIsNone(self.store.last_digest("new_arrivals"))                            # its own record
        s = digest.status(self.ctx)
        self.assertEqual((s["featured_count"], s["featured_last"]["recipients"], s["by_cadence"]["never"]), (1, 2, 1))


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

    def test_set_cadence_and_featured_routes(self):
        self._admin()
        b = self.store.buyer_for_email("pat@ross.test")
        r = self.client.post(f"/admin/buyers/{b['id']}/cadence", data={"digest_cadence": "monthly"}, follow_redirects=True)
        self.assertIn("once a month", r.get_data(as_text=True))
        self.assertEqual(self.store.buyer(b["id"])["digest_cadence"], "monthly")
        self.client.post(f"/admin/buyers/{b['id']}/cadence", data={"digest_cadence": "bogus"})
        self.assertEqual(self.store.buyer(b["id"])["digest_cadence"], "weekly")                # unknown = the default
        self.assertEqual(self.client.post("/admin/buyers/999/cadence", data={"digest_cadence": "daily"}).status_code, 404)
        home = self.client.get("/admin/").get_data(as_text=True)
        self.assertIn('<option value="weekly" selected>', home)
        self.assertIn("Featured items email", home)
        prev = self.client.get("/admin/featured/preview").get_data(as_text=True)
        self.assertIn("Nothing is featured", prev)
        r = self.client.post("/admin/featured/send", follow_redirects=True)
        self.assertIn("Featured email not sent", r.get_data(as_text=True))
        self.ingest("catalog", FeaturedMailTest.FEATURED)
        self.assertIn("Tree", self.client.get("/admin/featured/preview").get_data(as_text=True))
        r = self.client.post("/admin/featured/send", follow_redirects=True)
        self.assertIn("Featured items emailed to 1 buyer", r.get_data(as_text=True))
        self.assertEqual([m["to"] for m in self.sent], ["pat@ross.test"])
        self.assertIn("Last emailed", self.client.get("/admin/").get_data(as_text=True))

    def test_home_preview_and_send(self):
        self._admin()
        home = self.client.get("/admin/").get_data(as_text=True)
        self.assertIn("New-arrivals digest", home)
        self.assertIn("<b>1</b> item went on the sheet", home)
        self.assertIn("Send now to 1 buyer<", home)
        self.assertIn("<b>1</b> weekly", home)
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
