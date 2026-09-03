# Gerson Closeout Offers

Invitation-only offer sheet for aged inventory, aimed at key accounts and
regional / off-price buyers (leadership decision 2026-09-02). This service is
deliberately **dumb about why anything is priced the way it is**: it receives
sanitized feeds from the internal AOI engine, shows each SKU's original
wholesale, pack sizes and a blank offer field, collects offers, emails them to
the designated inbox and leaves them in an outbox that AOI polls. Independents
are pointed at the public SuiteCommerce site, where the closeout ladder is
carried by NetSuite pricing groups.

**It holds no credential to AOI or NetSuite.** Every connection starts inside:
AOI pushes to `/ingest/*` with this store's key, and AOI pulls `/outbox` with
the same key. See `docs/closeout-platform-brief.md` §0 / §11 in the AOI repo.

## What the store knows

| Feed | Contents | Never contains |
|---|---|---|
| `catalog` | SKU, description, image, brand, category, case / master / inner pack, wholesale, approximate quantity, company | cost, receipt date, age, bucket, advance rate, floors, tier, state |
| `invites` | invite token, label (who it went to), contact, email, companies, expiry | anything else |
| `customers` | allowlisted NetSuite accounts (id, company, login emails, rep) — may also sign in by magic link | AR, order history, credit |
| `curation` | per-customer SKU lists (kept for AOI compatibility; not shown on the sheet) | the history behind the ranking |

The catalog feed still carries the ladder price for AOI's own use; **the sheet
never shows it** — buyers see original wholesale and type what they will pay.

## Who gets in

Buyers are approved **on the store**, by a person, and are never tied to a
NetSuite customer record (JJ, 2026-09-02). An admin (an address in
`STORE_ADMIN_EMAILS`, signed in at `/admin` with the same one-time email link, or with a
password set on the portal after the first link sign-in)
invites a buyer by email; the buyer signs up at `/join/<token>` (or requests
access at `/apply`); the admin approves, which emails a sign-in link. Approved
buyers see every company's SKUs. AOI's older paths — the customers allowlist
and `/i/<token>` invite links — still work for anything already set up.

## How a buyer uses it

1. Opens `/i/<token>` (created and revoked in AOI's Closeout tab). Allowlisted
   accounts can alternatively request a one-time sign-in link by email.
2. Filters / searches the sheet, enters quantities (snapped to whole case
   packs, capped at what is available) and an offered unit price, saves.
3. Reviews the offer, downloads it as CSV if they like, adds company / contact
   / email / notes and submits.
4. The offer is emailed to every address in `OFFER_NOTIFY_EMAILS` and copied
   to the buyer (text table + CSV attachment), and written to the `outbox` as
   kind `offer`. People answer by email — accept or counter — and AOI's inbox
   keeps the record.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env            # set SECRET_KEY and STORE_INGEST_KEY at minimum
python run_dev.py --feeds path/to/closeout_feed   # seeds SQLite, prints a sign-in link and /i/demo-invite
```

Defaults to SQLite (`store.db`). Set `DATABASE_URL=postgresql+psycopg://...` for
Render Postgres. Emails print to the log unless `MAIL_BACKEND=graph` or `smtp`.
Set `OFFER_NOTIFY_EMAILS` (comma-separated) in production, and `WEBSITE_URL`
so the sign-in page can point independents at the shoppable site.

## Tests

```bash
python -m pytest -q
```
