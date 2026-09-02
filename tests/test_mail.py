"""Mail backends — store.mail (Graph client-credentials + sendMail, failure isolation)."""
import unittest

from config import Config
from store import mail as M


def _cfg(**kw) -> Config:
    base = dict(secret_key="s", store_ingest_key="k", database_url="sqlite://", base_url="http://store.test",
                mail_backend="graph", smtp_host="", smtp_port=587, smtp_user="", smtp_password="",
                mail_from="closeouts@gersoncompany.com", login_token_minutes=30,
                graph_tenant_id="tenant", graph_client_id="app", graph_client_secret="secret",
                graph_sender_mailbox="oscar@gersoncompany.com")
    base.update(kw)
    return Config(**base)


class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body or {}, text

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, token_status=200, send_status=202):
        self.calls = []
        self.token_status, self.send_status = token_status, send_status

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        self.calls.append((url, data, json, headers))
        if "oauth2/v2.0/token" in url:
            return _Resp(self.token_status, {"access_token": "tok123", "expires_in": 3600}, "denied")
        return _Resp(self.send_status, text="boom")


class GraphMailTest(unittest.TestCase):
    def setUp(self):
        M._token_cache.update({"value": "", "expires": 0.0})

    def test_token_then_sendmail_from_the_mailbox(self):
        s = FakeSession()
        ok = M.send(_cfg(), to="buyer@shop.com", subject="Sign in", body="link", session=s)
        self.assertTrue(ok)
        tok_url, tok_data, _, _ = s.calls[0]
        self.assertEqual(tok_url, "https://login.microsoftonline.com/tenant/oauth2/v2.0/token")
        self.assertEqual(tok_data["grant_type"], "client_credentials")
        self.assertEqual(tok_data["scope"], "https://graph.microsoft.com/.default")
        send_url, _, payload, headers = s.calls[1]
        self.assertEqual(send_url, "https://graph.microsoft.com/v1.0/users/oscar@gersoncompany.com/sendMail")
        self.assertEqual(headers["Authorization"], "Bearer tok123")
        self.assertEqual(payload["message"]["toRecipients"][0]["emailAddress"]["address"], "buyer@shop.com")
        self.assertEqual(payload["message"]["body"]["content"], "link")
        self.assertFalse(payload["saveToSentItems"])
        # token is cached across sends
        M.send(_cfg(), to="b@c.com", subject="x", body="y", session=s)
        self.assertEqual(sum(1 for c in s.calls if "token" in c[0]), 1)

    def test_failures_return_false_and_never_raise(self):
        self.assertFalse(M.send(_cfg(), to="a@b.c", subject="x", body="y", session=FakeSession(token_status=401)))
        M._token_cache.update({"value": "", "expires": 0.0})
        s = FakeSession(send_status=403)
        self.assertFalse(M.send(_cfg(), to="a@b.c", subject="x", body="y", session=s))

    def test_401_on_send_clears_the_cached_token(self):
        s = FakeSession(send_status=401)
        M.send(_cfg(), to="a@b.c", subject="x", body="y", session=s)
        self.assertEqual(M._token_cache["value"], "")

    def test_graph_backend_without_client_id_falls_back_to_log(self):
        self.assertTrue(M.send(_cfg(graph_client_id=""), to="a@b.c", subject="x", body="y", session=FakeSession()))

    def test_log_backend(self):
        self.assertTrue(M.send(_cfg(mail_backend="log"), to="a@b.c", subject="x", body="y"))


if __name__ == "__main__":
    unittest.main()
