# Gerson Closeout Store

Customer-facing, fully gated storefront for aged inventory. This service is
deliberately **dumb about why anything is priced the way it is**: it receives
three sanitized feeds from the internal AOI engine, lets approved buyers log in,
browse and order, and leaves everything it collects in an outbox that AOI polls.

**It holds no credential to AOI or NetSuite.** Every connection starts inside:
AOI pushes to `/ingest/*` with this store's key, and AOI pulls `/outbox` with
the same key. See `docs/closeout-platform-brief.md` §11 in the AOI repo.

## What the store knows

| Feed | Contents | Never contains |
|---|---|---|
| `catalog` | SKU, description, image, brand, category, case pack, wholesale, closeout price, next step-down date/price, approximate quantity, lot, ship-by | cost, receipt date, age, bucket, advance rate, floors, tier, state |
| `customers` | NetSuite customer id, company, login emails, buyer class, volume tier, rep name | AR, order history, credit |
| `curation` | per-customer ordered SKU lists | the history behind the ranking |

Anything a buyer does (order, offer, application, hold) is written to the
`outbox` table and handed to AOI on its next poll; AOI writes the NetSuite
sales order and returns the decision on the next feed push.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env            # set SECRET_KEY and STORE_INGEST_KEY at minimum
flask --app app run --debug
```

Defaults to SQLite (`store.db`). Set `DATABASE_URL=postgresql+psycopg://...` for
Render Postgres. Emails print to the log unless `MAIL_BACKEND=smtp`.

Seed a dev catalog from the AOI feed files:

```bash
python -m scripts.ingest_files --key $STORE_INGEST_KEY --base http://localhost:5000 path/to/closeout_feed
```

## Tests

```bash
python -m pytest -q
```
