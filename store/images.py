"""Product image cache.

The catalog feed carries a source image URL (NetSuite file cabinet or the
Park Hill shop). Buyers must never load those directly: the NetSuite URLs
carry the account id, and the store should not depend on another system at
page-render time. So each image is fetched once, resized to a web size, and
stored in the store's own database; templates only ever reference
``/img/<sku>``.

Fetching happens in a background thread after every catalog ingest, and
lazily on first request for anything the thread has not reached yet. A URL
that fails is recorded so it is not hammered on every page view.
"""
from __future__ import annotations

import io
import logging
import threading
from typing import Any

log = logging.getLogger("store.images")

MAX_EDGE = 900          # px; enough for a product page, small enough to keep Postgres modest
JPEG_QUALITY = 82
FETCH_TIMEOUT = 20.0
BATCH = 400             # per background pass
_lock = threading.Lock()
_running = False


def _resize(raw: bytes) -> tuple[bytes, str]:
    """Return ``(bytes, content_type)`` resized to MAX_EDGE. PNG kept for alpha."""
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    im.load()
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if max(im.size) > MAX_EDGE:
        im.thumbnail((MAX_EDGE, MAX_EDGE))
    out = io.BytesIO()
    if has_alpha:
        im.convert("RGBA").save(out, format="PNG", optimize=True)
        return out.getvalue(), "image/png"
    im.convert("RGB").save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return out.getvalue(), "image/jpeg"


def fetch(url: str, *, session: Any = None) -> tuple[bytes, str] | None:
    """Download + resize one image; ``None`` when the URL is not an image."""
    import requests
    sess = session or requests.Session()
    r = sess.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": "GersonCloseoutStore/1.0"})
    if r.status_code != 200:
        return None
    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    body = r.content or b""
    if not body or (ctype and not ctype.startswith("image/")):
        # NetSuite / SCA answer soft-404s with an HTML shell and HTTP 200.
        return None
    try:
        return _resize(body)
    except Exception as exc:
        log.info("image at %s not decodable: %s", url[:80], exc)
        return None


def ensure(store: Any, sku: str, url: str, *, session: Any = None) -> dict[str, Any] | None:
    """Cached image for ``sku`` (fetching if needed). Returns the row or None."""
    row = store.image(sku)
    if row and row.get("source_url") == url and row.get("status") == "ok":
        return row
    if row and row.get("source_url") == url and row.get("status") == "failed":
        return None
    got = None
    try:
        got = fetch(url, session=session)
    except Exception as exc:
        log.info("image fetch failed for %s: %s", sku, exc)
    if got is None:
        store.put_image(sku, url, None, None, status="failed")
        return None
    data, ctype = got
    store.put_image(sku, url, data, ctype, status="ok")
    return store.image(sku)


def refresh_missing(store: Any, *, limit: int = BATCH, session: Any = None) -> dict[str, int]:
    """Fetch images for active products whose cache is missing or stale."""
    todo = store.images_needed(limit=limit)
    ok = failed = 0
    for sku, url in todo:
        row = ensure(store, sku, url, session=session)
        if row:
            ok += 1
        else:
            failed += 1
    return {"attempted": len(todo), "ok": ok, "failed": failed}


def refresh_in_background(store: Any) -> bool:
    """Kick one background pass; returns False if one is already running."""
    global _running
    with _lock:
        if _running:
            return False
        _running = True

    def run():
        global _running
        try:
            total = {"attempted": 0, "ok": 0, "failed": 0}
            while True:
                r = refresh_missing(store)
                for k in total:
                    total[k] += r[k]
                if r["attempted"] < BATCH:
                    break
            log.info("image refresh: %s", total)
        except Exception:
            log.exception("image refresh crashed")
        finally:
            with _lock:
                _running = False

    threading.Thread(target=run, name="image-refresh", daemon=True).start()
    return True
