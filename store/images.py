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
import re
import threading
from typing import Any

log = logging.getLogger("store.images")

MAX_EDGE = 900          # px; enough for a product page, small enough to keep Postgres modest
# What a gallery url is asked for, by slot. Measured on a real asset 2026-09-09:
# as Salsify ships it 13,265 KB, at w_900 70 KB, at w_120 2.9 KB.
STAGE_WIDTH = 900
THUMB_WIDTH = 120
# Video gets its own cap: the master on 45608 is 76 MB, and at 720px wide it is
# 1.4 MB (measured 2026-09-09). 720 rather than 900 because a video is watched,
# not inspected, and the saving is worth more than the pixels.
VIDEO_WIDTH = 720
JPEG_QUALITY = 82
FETCH_TIMEOUT = 20.0
BATCH = 400             # per background pass
_lock = threading.Lock()
_running = False


# Salsify serves images through Cloudinary, whose transformations live in the
# url path -- but AFTER the signature segment, not before it:
#
#   .../image/upload/w_120/s--boSg-_7H--/asset.jpg   -> 404
#   .../image/upload/s--boSg-_7H--/w_120/asset.jpg   -> 200, 2.9 KB
#
# This matters more than it looks. Salsify hands out the untouched master: the
# asset behind one 56px gallery thumbnail is 13 MB and 6648px wide, so an item
# page with seven views was ~90 MB before this (measured 2026-09-09). The hero
# is unaffected either way -- it is resized once into our own cache.
_SALSIFY = re.compile(
    r"^(https://images\.salsify\.com/(image|video)/upload/s--[^/]+--/)"
    r"(?!(?:[a-z]+_[^/]+,?)+/)(.+)$")


def sized(url: Any, width: int) -> str:
    """``url`` asked for at ``width`` px, where that is something we can ask.

    Images and video both. ``f_auto`` is only asked of images: on a video it
    invites Cloudinary to pick a container, and an mp4 the browser already plays
    is not worth trading for that. A NetSuite link, an already-transformed url
    or anything unrecognised comes back untouched, so this is safe to wrap
    around every url in a template."""
    u = str(url or "")
    m = _SALSIFY.match(u)
    if not m or not width:
        return u
    t = f"w_{int(width)},c_limit,q_auto" if m.group(2) == "video" else f"w_{int(width)},c_limit,f_auto,q_auto"
    return f"{m.group(1)}{t}/{m.group(3)}"


def poster(url: Any, width: int) -> str:
    """A still frame from a video, as an image url, or "" for anything else.

    Cloudinary will render a frame of a video as a jpeg if asked for one by
    extension -- 1.9 KB at 120px, 33 KB at 900px. It turns the video thumbnail
    from a grey box with a triangle on it into a picture of the thing, and gives
    the player something to show before it is pressed."""
    u = str(url or "")
    m = _SALSIFY.match(u)
    if not m or m.group(2) != "video" or not width:
        return ""
    stem = m.group(3).rsplit(".", 1)[0]
    return f"{m.group(1)}w_{int(width)},c_limit,f_auto,q_auto/{stem}.jpg"


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
