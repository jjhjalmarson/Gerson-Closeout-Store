"""Product image cache — store.images and the gated /img route."""
import io
import unittest

from PIL import Image

from store import images as I
from tests.test_store import CATALOG, CURATION, CUSTOMERS, KEY, StoreTestCase


def _png(size=(1400, 900), mode="RGB"):
    im = Image.new(mode, size, (200, 30, 30, 255) if mode == "RGBA" else (200, 30, 30))
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


class _Resp:
    def __init__(self, body=b"", ctype="image/png", status=200):
        self.content, self.status_code = body, status
        self.headers = {"Content-Type": ctype}


class FakeSession:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        v = self.mapping.get(url)
        if v is None:
            return _Resp(status=404)
        return v


class ResizeAndFetchTest(unittest.TestCase):
    def test_resize_caps_edge_and_picks_format(self):
        data, ctype = I._resize(_png((1400, 900)))
        self.assertEqual(ctype, "image/jpeg")
        w, h = Image.open(io.BytesIO(data)).size
        self.assertEqual(w, I.MAX_EDGE)
        self.assertLess(h, 900)
        data, ctype = I._resize(_png((300, 300), mode="RGBA"))
        self.assertEqual(ctype, "image/png")
        self.assertEqual(Image.open(io.BytesIO(data)).size, (300, 300))     # small images untouched

    def test_fetch_rejects_soft_404_html_and_non_200(self):
        s = FakeSession({"https://x/a": _Resp(b"<html>shell</html>", "text/html"),
                         "https://x/b": _Resp(_png((10, 10)), "image/png", status=500),
                         "https://x/c": _Resp(b"not an image", "image/jpeg")})
        self.assertIsNone(I.fetch("https://x/a", session=s))
        self.assertIsNone(I.fetch("https://x/b", session=s))
        self.assertIsNone(I.fetch("https://x/c", session=s))
        self.assertIsNone(I.fetch("https://x/missing", session=s))
        self.assertIsNotNone(I.fetch("https://x/ok", session=FakeSession({"https://x/ok": _Resp(_png((50, 50)))})))


class ImageRoutesTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.app.config["TESTING"] = True
        self.seed()

    def test_ensure_caches_and_records_failures(self):
        s = FakeSession({"https://img/l1.jpg": _Resp(_png((1200, 600)))})
        row = I.ensure(self.store, "L1", "https://img/l1.jpg", session=s)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["content_type"], "image/jpeg")
        I.ensure(self.store, "L1", "https://img/l1.jpg", session=s)
        self.assertEqual(len(s.calls), 1)                                    # cached: no second fetch
        self.assertIsNone(I.ensure(self.store, "T2", "https://img/none.jpg", session=s))
        self.assertEqual(self.store.image("T2")["status"], "failed")
        I.ensure(self.store, "T2", "https://img/none.jpg", session=s)
        self.assertEqual(s.calls.count("https://img/none.jpg"), 1)          # failure remembered
        # a new source URL for the same SKU is refetched
        s.mapping["https://img/l1-v2.jpg"] = _Resp(_png((100, 100)))
        row = I.ensure(self.store, "L1", "https://img/l1-v2.jpg", session=s)
        self.assertEqual(row["source_url"], "https://img/l1-v2.jpg")

    def test_images_needed_and_refresh_missing(self):
        needed = self.store.images_needed()
        self.assertEqual(needed, [("L1", "https://img/l1.jpg")])            # T2 has no image_url
        s = FakeSession({"https://img/l1.jpg": _Resp(_png((100, 100)))})
        r = I.refresh_missing(self.store, session=s)
        self.assertEqual(r, {"attempted": 1, "ok": 1, "failed": 0})
        self.assertEqual(self.store.images_needed(), [])
        st = self.store.image_stats()
        self.assertEqual((st["cached"], st["products_with_image"], st["pending"]), (1, 1, 0))

    def test_img_route_gated_and_serves_cached_bytes(self):
        self.assertEqual(self.client.get("/img/L1").status_code, 302)          # login required
        self.login()
        self.store.put_image("L1", "https://img/l1.jpg", b"JPEGBYTES", "image/jpeg", status="ok")
        r = self.client.get("/img/L1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/jpeg")
        self.assertEqual(r.get_data(), b"JPEGBYTES")
        self.assertIn("private", r.headers["Cache-Control"])
        self.assertEqual(self.client.get("/img/T2").status_code, 404)          # no source image
        self.assertEqual(self.client.get("/img/NOPE").status_code, 404)

    def test_templates_never_emit_the_source_url(self):
        self.login()
        self.store.put_image("L1", "https://img/l1.jpg", b"x", "image/jpeg", status="ok")
        for path in ("/", "/item/L1"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertNotIn("https://img/l1.jpg", html)
            self.assertIn("/img/L1", html)

    def test_refresh_endpoint_keyed_and_healthz_stats(self):
        self.assertEqual(self.client.post("/images/refresh").status_code, 404)
        r = self.client.post("/images/refresh?limit=5", headers={"X-API-Key": KEY})
        self.assertEqual(r.status_code, 200)
        self.assertIn("attempted", r.get_json())
        self.assertIn("images", self.client.get("/healthz").get_json())


if __name__ == "__main__":
    unittest.main()
