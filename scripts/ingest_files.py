"""Push AOI's saved feed files at a running store — what AOI's publish step does
over the network, for local development.

    python -m scripts.ingest_files --key $STORE_INGEST_KEY --base http://localhost:5000 path/to/closeout_feed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("feed_dir")
    ap.add_argument("--base", default="http://localhost:5000")
    ap.add_argument("--key", required=True)
    a = ap.parse_args(argv)
    for kind in ("catalog", "customers", "curation"):
        p = os.path.join(a.feed_dir, f"{kind}.json")
        if not os.path.exists(p):
            print(f"{kind}: missing {p}")
            continue
        data = open(p, "rb").read()
        req = urllib.request.Request(f"{a.base.rstrip('/')}/ingest/{kind}", data=data, method="POST",
                                     headers={"Content-Type": "application/json", "X-API-Key": a.key})
        with urllib.request.urlopen(req, timeout=120) as r:
            print(kind, r.status, json.loads(r.read().decode())["count"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
