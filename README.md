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
| `catalog` | SKU, description, image, brand, category, case / master / inner pack, wholesale, approximate quantity, company, `listed_since` (first run on the sheet; null = before AOI kept track), `price_changed_at`, `price_was` | cost, receipt date, age, bucket, advance rate, floors, tier, state |
| `invites` | invite token, label (who it went to), contact, email, companies, expiry | anything else |
| `customers` | allowlisted NetSuite accounts (id, company, login emails, rep, `price_list_id`) — may also sign in by magic link | AR, order history, credit |
| `curation` | per-customer SKU lists (kept for AOI compatibility; not shown on the sheet) | the history behind the ranking |
| `prices` | the price lists and what is on them: `[{list_id, label, prices: {sku: number}}]` | how a price was arrived at — basis, markup, cost |

The catalog feed still carries the ladder price for AOI's own use; **the sheet
never shows it** — buyers see original wholesale and type what they will pay.

## Three ways a buyer is priced (JJ, 2026-09-10)

`pricing_tier` on the buyer decides which surface a signed-in account sees. It
is set per buyer in the **Pricing** column on `/admin` (or, for an allowlisted
AOI account, carried on the `customers` feed as `pricing_tier`; a feed that
sends a `price_list_id` and no tier means cost plus, as before):

* **`offer`** (the default, and everyone else — invite links and buyers who
  signed up here) — **Make an offer**: original wholesale, MSRP and a blank
  offer box. No suggested price of any kind: this is for buyers who price off
  what their own customer will pay and want no hint from us (Bealls).
* **`ev_base`** — **EV base price**: our published closeout price (the lower of
  the ladder step and what NetSuite charges today) is shown beside wholesale as
  the base of every line, with the % off. A quantity alone takes the line **at
  our price**; a typed price is their offer. The payload carries
  `pricing_tier: "ev_base"`, `at_base_lines`, and `our_price` / `at_base` per
  line, so AOI's desk can confirm the lines at our price without a round and
  negotiate only the rest (Steins). A SKU the feed has not priced is a plain
  offer line. "Under $__" is measured on our price.
* **`cost_plus`** — **Cost plus** off one of AOI's price lists: **"Your price"**,
  the number AOI computed for that account, instead of wholesale. No offer box,
  no % of wholesale; a SKU we have not quoted them is off their sheet entirely
  and reads "price on request" on its item page. The line price is set
  **server-side** from the stored price (whatever the client posts is ignored),
  and submitting rides the same pipeline with `price_mode: "firm"` and the
  `price_list_id` in the offer payload, so AOI's desk knows it is an order at
  prices we already quoted (Goodwill, 2026-09-09). A list AOI deactivates drops
  the buyer back to `offer`, never onto stale prices.

`prices` is a full snapshot like the other feeds; an **empty** `items` list is
legal and means no account is on firm prices (unlike `customers` / `invites`,
where an empty feed is refused because it would wipe the allowlist).

Every sheet carries an **"Under $__"** filter, measured on whichever price that
buyer can see. The margin-derived suggested offer that used to sit under each
empty box is gone: tier 2's base is the suggestion, and tier 3 asked for none.

## What brings a buyer back

* **NEW** on the sheet: a SKU wears the badge for 14 days after `listed_since`;
  the header counts them ("12 new items added in the last 14 days") and links
  to the sheet filtered to just those (`?new=1&sort=newest`). "Newest on the
  sheet" is a sort; "only the last 14 days" is a filter.
* **New-arrivals digest** (`store/digest.py`): one email per approved buyer
  with what went on the sheet since they last heard: item, pack, available,
  original wholesale, a link to the new items. No closeout price, as on the
  sheet. **Cadence is per buyer** (buyer feedback, 2026-09-10: Kendra wants
  once a month, others want it as it lands) — `daily` / `weekly` (default) /
  `monthly` / `never`, set in the Emails column on `/admin`. It sends itself
  after the nightly catalog feed once `DIGEST_WEEKDAY` (`mon`..`sun`) is set:
  daily buyers any day something new landed, weekly buyers on that weekday,
  monthly buyers every fourth one; each cadence's window starts the day after
  its last send, so a buyer sees an item once. Unset (the default) means
  nothing sends on its own. "Send now" on `/admin` (preview, then send) goes to
  everyone who takes email, covering the last `DIGEST_DAYS` (7). Every send is
  recorded in `digest_runs` with its cadence.
* **Featured items email**: when AOI's featured list is worth an inbox, `/admin`
  previews and sends it (same table, wholesale only, link to `?featured=1`) to
  every buyer who takes email. On demand only; it never sends itself.
* **Back to where you were**: clicking into an item, reviewing the offer or
  using the header link returns a buyer to the sheet with the same filters,
  sort and page, scrolled to the row they left (buyer feedback, 2026-09-10).
  The sheet remembers its last query string in the session; "Clear" forgets it.

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
