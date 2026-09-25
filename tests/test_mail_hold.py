"""Mail hold (AOI PR #206): an AOI account marked lost ("won't buy from us") or
not a real account arrives on the customers feed with ``mail_hold: true``.

It stays on the allowlist. What stops is marketing mail -- the new-arrivals
digest (by hand and by itself, every cadence) and the featured email. What
goes on is transactional mail: sign-in links (still scanner-safe: GET peeks,
POST redeems), the approval / welcome notes, offer confirmations and the
replies to an offer.
"""
import unittest
from datetime import date, timedelta
from unittest import mock

import sqlalchemy as sa

from app import create_app
from store import db as D
from store import digest
from tests.test_digest import CATALOG_NEW, _buyer
from tests.test_store import CATALOG, CUSTOMERS, INVITES, KEY, StoreTestCase, _cfg

TODAY = date.today()
WD = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# 26003 is held and lists donna + held@ross.test; 27000 is not held and lists
# buyer@shop.com, which the held account lists too.
HELD_FEED = {"kind": "customers", "as_of": "2026-09-25", "count": 2, "items": [
    {**CUSTOMERS["items"][0], "emails": ["donna-n@live.com", "held@ross.test", "buyer@shop.com"], "mail_hold": True},
    {"customer_id": "27000", "company_name": "Open Co", "emails": ["buyer@shop.com", "open@ok.test"],
     "accounts": {"gerson": "27000"}},
]}
FEATURED = {**CATALOG, "items": [dict(CATALOG["items"][0]), {**CATALOG["items"][1], "featured_rank": 1}]}


def _cust_row(store, cid):
    with store.engine.connect() as conn:
        return conn.execute(sa.select(D.customers).where(D.customers.c.customer_id == cid)).mappings().first()


class FeedTest(StoreTestCase):
    def test_parsed_and_stored_missing_is_false(self):
        self.assertEqual(self.ingest("customers", HELD_FEED).status_code, 202)
        self.assertTrue(_cust_row(self.store, "26003")["mail_hold"])
        self.assertFalse(_cust_row(self.store, "27000")["mail_hold"])              # no key = no hold
        # an address on a held account and an open one is held, whichever row wins
        self.assertEqual(self.store.mail_held_emails(), {"donna-n@live.com", "held@ross.test", "buyer@shop.com"})

    def test_strict_parse(self):
        self.assertEqual([D.feed_flag(v) for v in (True, 1, "true", "YES", "1")], [True] * 5)
        self.assertEqual([D.feed_flag(v) for v in (False, 0, None, "", "false", "no", "0", 2, [], {})], [False] * 10)
        feed = {**CUSTOMERS, "items": [{**CUSTOMERS["items"][0], "mail_hold": "false"}]}
        self.ingest("customers", feed)
        self.assertFalse(_cust_row(self.store, "26003")["mail_hold"])
        self.assertEqual(self.store.mail_held_emails(), set())

    def test_next_snapshot_lifts_the_hold(self):
        self.ingest("customers", HELD_FEED)
        self.ingest("customers", CUSTOMERS)                                        # AOI cleared the flag
        self.assertFalse(_cust_row(self.store, "26003")["mail_hold"])
        self.assertEqual(self.store.mail_held_emails(), set())

    def test_empty_feed_guard_still_refuses_and_keeps_holds(self):
        self.ingest("customers", HELD_FEED)
        r = self.ingest("customers", {**CUSTOMERS, "items": []})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(_cust_row(self.store, "26003")["active"])
        self.assertTrue(_cust_row(self.store, "26003")["mail_hold"])
        self.assertIn("held@ross.test", self.store.mail_held_emails())

    def test_all_held_feed_keeps_everyone_on_the_allowlist(self):
        feed = {**HELD_FEED, "items": [{**it, "mail_hold": True} for it in HELD_FEED["items"]]}
        self.assertEqual(self.ingest("customers", feed).status_code, 202)
        self.assertIsNotNone(self.store.customer_for_email("open@ok.test"))
        self.assertIsNotNone(self.store.customer_for_email("donna-n@live.com"))


class MarketingSkippedTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.ctx = self.app.config["STORE"]
        self.ingest("catalog", CATALOG_NEW)
        self.ingest("customers", HELD_FEED)
        _buyer(self.store, "held@ross.test", company="Held Co")
        _buyer(self.store, "pat@ross.test")

    def test_manual_digest_skips_held(self):
        r = digest.send(self.ctx, since=(TODAY - timedelta(days=7)).isoformat(), sent_by="admin@gerson.test")
        self.assertEqual(r["sent"], 1)
        self.assertEqual([m["to"] for m in self.sent], ["pat@ross.test"])

    def test_held_whatever_the_cadence(self):
        _buyer(self.store, "held-daily@ross.test", company="x", cadence="daily")
        self.ingest("customers", {**HELD_FEED, "items": [{**HELD_FEED["items"][0],
                                                          "emails": ["held@ross.test", "held-daily@ross.test"]}]})
        for c in ("daily", "weekly", "monthly", None):
            self.assertNotIn("held@ross.test", [b["email"] for b in digest.recipients(self.store, cadence=c)])
            self.assertNotIn("held-daily@ross.test", [b["email"] for b in digest.recipients(self.store, cadence=c)])

    def test_featured_skips_held(self):
        self.ingest("catalog", FEATURED)
        r = digest.send_featured(self.ctx, sent_by="admin@gerson.test")
        self.assertEqual(r["sent"], 1)
        self.assertEqual([m["to"] for m in self.sent], ["pat@ross.test"])

    def test_nobody_left_sends_nothing(self):
        with self.store.engine.begin() as conn:
            conn.execute(sa.delete(D.buyers).where(D.buyers.c.email == "pat@ross.test"))
        r = digest.send(self.ctx, since=(TODAY - timedelta(days=7)).isoformat())
        self.assertEqual(r["sent"], 0)
        self.ingest("catalog", FEATURED)
        self.assertEqual(digest.send_featured(self.ctx)["sent"], 0)
        self.assertEqual(self.sent, [])

    def test_status_counts_held_apart(self):
        s = digest.status(self.ctx)
        self.assertEqual((s["recipients"], s["mail_held"], s["by_cadence"]["weekly"]), (1, 1, 1))


class AutoSendSkippedTest(unittest.TestCase):
    def test_nightly_auto_send_skips_held(self):
        app = create_app(_cfg(digest_weekday=WD[TODAY.weekday()]))
        app.config["TESTING"] = True
        sent = []
        p = mock.patch("store.mail.send", side_effect=lambda cfg, **kw: (sent.append(kw) or True))
        p.start(); self.addCleanup(p.stop)
        client = app.test_client()
        client.post("/ingest/customers", json=HELD_FEED, headers={"X-API-Key": KEY})
        store = app.config["STORE"].store
        _buyer(store, "held@ross.test", company="Held Co", cadence="daily")
        _buyer(store, "pat@ross.test")
        r = client.post("/ingest/catalog", json=CATALOG_NEW, headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 202)
        self.assertEqual([m["to"] for m in sent], ["pat@ross.test"])

    def test_only_held_buyers_means_no_auto_send(self):
        app = create_app(_cfg(digest_weekday=WD[TODAY.weekday()]))
        app.config["TESTING"] = True
        sent = []
        p = mock.patch("store.mail.send", side_effect=lambda cfg, **kw: (sent.append(kw) or True))
        p.start(); self.addCleanup(p.stop)
        store = app.config["STORE"].store
        store.ingest_customers(HELD_FEED["items"], as_of=None, generated_at=None)
        store.ingest_catalog(CATALOG_NEW["items"], as_of=None, generated_at=None)
        _buyer(store, "held@ross.test", company="Held Co")
        self.assertIsNone(digest.maybe_auto_send(app.config["STORE"], today=TODAY))
        self.assertEqual(sent, [])
        self.assertIsNone(store.last_digest("new_arrivals"))


class TransactionalAllowedTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.ingest("catalog", CATALOG)
        self.ingest("customers", HELD_FEED)
        self.ingest("invites", INVITES)

    def _last_token(self):
        with self.store.engine.connect() as conn:
            return conn.execute(sa.select(D.login_tokens.c.token).order_by(D.login_tokens.c.expires_at.desc())).first()[0]

    def test_held_feed_customer_gets_a_sign_in_link_and_it_stays_scanner_safe(self):
        self.client.post("/login", data={"email": "donna-n@live.com"})
        self.assertEqual([m["to"] for m in self.sent], ["donna-n@live.com"])
        self.assertIn("sign-in link", self.sent[0]["subject"])
        tok = self._last_token()
        self.assertEqual(self.client.get(f"/login/{tok}").status_code, 200)       # a scanner GET peeks...
        self.assertEqual(self.client.get(f"/login/{tok}").status_code, 200)       # ...and spends nothing
        self.assertEqual(self.client.post(f"/login/{tok}").status_code, 302)      # the click redeems
        self.assertEqual(self.client.get("/").status_code, 200)                   # and they are on the sheet
        self.assertIsNone(self.store.peek_login_token(tok))                         # used once, now spent

    def test_held_store_buyer_gets_a_sign_in_link(self):
        _buyer(self.store, "held@ross.test", company="Held Co")
        self.client.post("/login", data={"email": "held@ross.test"})
        self.assertEqual([m["to"] for m in self.sent], ["held@ross.test"])
        self.assertEqual(self.client.post(f"/login/{self._last_token()}").status_code, 302)

    def test_approval_note_still_goes(self):
        _buyer(self.store, "held@ross.test", status="pending", company="Held Co")
        b = self.store.buyer_for_email("held@ross.test")
        with self.client.session_transaction() as s:
            s["admin_email"] = "admin@gerson.test"
        self.client.post(f"/admin/buyers/{b['id']}/status", data={"status": "approved"})
        self.assertEqual([m["to"] for m in self.sent], ["held@ross.test"])
        self.assertIn("Approved", self.sent[0]["subject"])

    def test_welcome_note_on_an_invited_sign_up_still_goes(self):
        inv = self.store.create_signup_invite("held@ross.test", company="Held Co", created_by="admin@gerson.test")
        self.client.post(f"/join/{inv['token']}", data={"company": "Held Co", "contact": "Pat"})
        self.assertIn("held@ross.test", [m["to"] for m in self.sent])
        self.assertIn("You are in", [m for m in self.sent if m["to"] == "held@ross.test"][0]["subject"])

    def test_offer_confirmation_and_round_reply_still_go(self):
        self.login("donna-n@live.com")
        self.sent.clear()
        self.client.post("/offer/set", data={"qty[L1]": "24", "price[L1]": "10"})
        r = self.client.post("/offer/submit", data={"company": "Adeline", "email": "held@ross.test"})
        self.assertEqual(r.status_code, 200)
        buyer_copy = [m for m in self.sent if m["to"] == "held@ross.test"]
        self.assertEqual(len(buyer_copy), 1)
        self.assertIn("We have your offer", buyer_copy[0]["subject"])
        offer_id = self.store.pull_outbox()[0]["id"]
        self.sent.clear()
        body = {"offer_ref": offer_id, "round_id": 7, "round_no": 2, "token": "tok-h", "kind": "counter",
                "thread_status": "countered", "lines": [{"sku": "L1", "qty": 24, "price": 14.0}], "message": "Counter.",
                "created_at": "2026-09-25T21:00:00+00:00", "buyer_email": "held@ross.test", "company": "Adeline"}
        r = self.client.post("/rounds", json=body, headers={"X-API-Key": KEY})
        self.assertEqual((r.status_code, r.get_json()["emailed"]), (202, True))
        self.assertEqual([m["to"] for m in self.sent], ["held@ross.test"])


class AdminListTest(StoreTestCase):
    def test_buyer_list_shows_the_hold_read_only(self):
        self.ingest("catalog", CATALOG_NEW)
        self.ingest("customers", HELD_FEED)
        _buyer(self.store, "held@ross.test", company="Held Co")
        _buyer(self.store, "pat@ross.test")
        with self.client.session_transaction() as s:
            s["admin_email"] = "admin@gerson.test"
        home = self.client.get("/admin/").get_data(as_text=True)
        self.assertEqual(home.count(">Mail held (AOI)</span>"), 1)
        row = home[home.index("Held Co"):]
        row = row[:row.index("</tr>")]
        self.assertIn("Mail held (AOI)", row)
        self.assertNotIn("mail_hold", row)                                          # no control to change it
        self.assertIn("<b>1</b> mail held by AOI", home)
        self.assertIn("Send now to 1 buyer<", home)                                 # the held buyer is not counted


if __name__ == "__main__":
    unittest.main()
