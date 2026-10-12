# ZipKart

A prototype online payments experience for **US grocery delivery**, built on the
[Juspay Hyperswitch](https://hyperswitch.io) sandbox with **Authorize.net** as the processor.

Grocery checkout totals are estimates. Fixed-price items are exact, but weighed items
(produce, meat, deli) come in a little over or under, and out-of-stock items get
substituted or removed. So ZipKart doesn't charge at checkout. It:

1. **places a hold** for the estimate plus a weight buffer (`capture_method: manual`),
2. lets a shopper **pick** the order (real weights, lower quantities, out of stock),
3. asks the customer to **approve substitutions** that fit inside the remaining hold,
4. **captures the final amount** once (a partial capture; the rest of the hold is released),
5. handles **cancellations** (voiding the hold) and **post-delivery problems** (partial refunds
   or free replacements),
6. keeps everything in sync through **signed webhooks**, the return page, and a
   **reconciliation sweep**.

## How it fits together

```
Browser (plain HTML/JS)            FastAPI (Python)                      Hyperswitch sandbox ─▶ Authorize.net
  checkout  ── SKUs + quantities ─▶ prices the cart server-side ─────────▶ POST /payments (manual capture)
  Web SDK   ◀─ client_secret ─────  (secret API key stays on the server)
  order page ◀─ polls our API ────  Postgres on Neon (orders, items, payments, remedies, refunds, webhook_events)
  store console (/store)            ◀── signed webhooks, GET /payments/{id}, GET /refunds/{id}
```

- **Money is integer cents everywhere**; weights are exact decimals. The browser never sends a price.
- **Every state change is a compare-and-set** (`UPDATE … WHERE status = expected`); a rowcount of 0
  means someone else already did it.
- **No database transaction is held across a Hyperswitch call**: write the intent and commit, call
  Hyperswitch, then record the result in a second transaction.
- **Hyperswitch is the source of truth** for payment and refund status. Webhook payloads, the return
  page's query string and the browser are only hints; we always re-fetch and apply forward-only.
- Every outbound call has a **deterministic ID** (`pay_{order}_{attempt}`,
  `rf_{order}_{item}_{reason}_r{remedy}_a{attempt}`), so a retry after a timeout can't double-charge or
  double-refund: Hyperswitch answers `HE_01` and we fetch instead.

## Run it locally

Requirements: Python 3.10+, a Postgres database (a free [Neon](https://neon.tech) project works), and a
Hyperswitch sandbox account with Authorize.net connected.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill it in (see below)
.venv/bin/python -m scripts.migrate
.venv/bin/uvicorn app.main:app --port 8000 --reload
```

Open http://localhost:8000 (shop) and http://localhost:8000/store (store console).

### Never point local tools at production

Use a **Neon branch** (e.g. `dev`, created from `production` with *Current data* and with
automatic deletion turned off) for everything local. Put its **pooled** connection string in
`DATABASE_URL`, and the production host in `PROD_DB_HOST`:

```
DATABASE_URL=postgresql://…@ep-<dev-endpoint>-pooler.<region>.aws.neon.tech/neondb?sslmode=require
PROD_DB_HOST=ep-<prod-endpoint>-pooler.<region>.aws.neon.tech
```

`scripts/migrate.py`, `scripts/reset_dev_data.py` and the test suite refuse to run when
`DATABASE_URL` points at `PROD_DB_HOST` (pooled or direct), and refuse locally if `PROD_DB_HOST`
isn't set. On Render (`RENDER=true`) migrations run at start: **production is migrated only by a
deploy.**

**Dev must not hold copies of live payment state.** A branch created from production starts with
production's orders, which carry real Hyperswitch payment and refund IDs. Anything on dev that acts
on them (a sweep syncing or voiding a payment, a refund retry, a cancel) would act on the same
sandbox payments production manages: two systems on one payment, each with its own refund attempt
count against Hyperswitch's limit of 11. So, after creating the branch, run once:

```bash
.venv/bin/python -m scripts.reset_dev_data
```

It deletes orders and everything hanging off them (items, payments, remedies, refunds) plus the
copied webhook events and failures, resets the sweep lease, and keeps products and
`schema_migrations`. It shows the target endpoint and row counts and asks you to type the endpoint
id before deleting.

Automatic sweeps (the background timer and the sweep on store-console load) also act on payments,
so they're **off locally**: `SWEEP_TIMER_ENABLED` decides when set, and when unset they run only on
Render. *Run now* in the store console and `POST /internal/sweep` still work for deliberate testing.

Tests (they clean up after themselves and make no Hyperswitch calls):

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

### Configuration (`.env`)

| Variable | What it is |
|---|---|
| `HYPERSWITCH_API_KEY` | Secret key. **Server only**, never sent to the browser. |
| `HYPERSWITCH_PUBLISHABLE_KEY` | Public key for the Web SDK. |
| `HYPERSWITCH_PROFILE_ID` | Business profile with Authorize.net enabled. |
| `HYPERSWITCH_WEBHOOK_SECRET` | The profile's *payment response hash key* (signs webhooks). |
| `DATABASE_URL` | Neon **pooled** connection string (host contains `-pooler`). |
| `APP_BASE_URL` | Where the app is reachable; used for the payment `return_url`. **On Render it must match the service URL exactly** (e.g. `https://zipkart-eiwx.onrender.com`, not a guessed `https://zipkart.onrender.com`). A wrong value sends customers who just paid to someone else's site, with their `client_secret` in the URL. The app logs a warning at startup if it's not https in production or differs from Render's `RENDER_EXTERNAL_URL`. |
| `STORE_CONSOLE_PASSWORD` | **The store console password.** Shoppers log in with it at `/store`. Changing it logs every session out. |
| `WEIGHT_BUFFER_PERCENT` | Extra hold on weighed items (default 15). |
| `SUBSTITUTION_TIMEOUT_SECONDS` | How long a customer has to answer a substitution (default 60). |
| `RECONCILE_AFTER_MINUTES` | How long a payment can sit in a non-final state before the sweep re-checks it. |
| `SETTLEMENT_CUTOFF`, `SETTLEMENT_TIMEZONE`, `SETTLEMENT_MARGIN_HOURS` | Your Authorize.net Transaction Cut-Off Time (default 16:00 America/Los_Angeles) and how long after it a batch is safely settled (default 6 h). A refund is first sent at *first cut-off after the capture + margin*. |
| `REFUND_MAX_AUTO_ATTEMPTS_PER_PAYMENT` | Automatic refund attempts per payment across all its remedies (default 4; Hyperswitch allows 11). |
| `STORE_TIMEZONE` | Times shown in the store console and on customer pages (default America/Chicago). |
| `INTERNAL_SWEEP_SECRET` | Shared secret for `POST /internal/sweep` (header `X-Sweep-Secret`), so an external scheduler can wake the app and run the sweep. Empty disables the endpoint. |
| `SWEEP_INTERVAL_SECONDS` | Background reconciliation interval (default 180). |

## Demo walkthrough

Open **two windows side by side**: the customer on the left, the store console on the right.

1. **Customer: checkout.** Add a few items, including something sold by weight (bananas, chicken)
   and the **Diet Coke 12-pack**. Choose *Best match* or *Remove it* for out-of-stock items, then
   continue to payment. The page explains the hold: *estimated total + weight buffer (15% on the
   weighed items) = temporary hold*. Pay with the test card below.
2. **Customer: confirmation.** You land on the order page: "Order placed!", with the same hold
   breakdown. Until the shopper starts, *Cancel order* voids the hold.
3. **Store: pick.** Log in at `/store` and click *Start picking*. For each line:
   - fixed-price items: confirm the quantity (it can be lower than ordered);
   - weighed items: enter the actual weight (the allowed range is shown: at most +20% of what the
     customer ordered, and never more than the hold can cover);
   - out of stock: remove the line, or *substitute* it. Only products that fit the remaining hold
     can be offered. Try **Diet Coke 6-pack bottles** for the 12-pack.
4. **Customer: substitution.** The order page shows "Diet Coke 12-pack is out of stock. Swap for
   Diet Coke 6-pack… ?" with a countdown. Accept, remove, or let it time out (the customer's
   preference applies).
5. **Store: finalize.** *Finalize & capture* only works when every line is picked and no
   substitution is pending (the button says why when it's blocked). It charges exactly the final
   amount.
6. **Customer: receipt.** "Picked and on its way" with what was held, charged and released, and
   every line that changed (actual weight vs estimate, substitutions, removals, lower quantities).
7. **Customer: report a problem.** Below the receipt: missing (refund), damaged or expired (refund
   or free replacement), with a quantity for fixed-price items. A refund is never sent before the
   capture settles: it's scheduled for the first cut-off after the capture plus a margin
   (*"Refund of $X approved. It will be sent after your payment settles, by Oct 12."*), and the
   sweep sends it when it's due. The store console shows each refund's status, next attempt (store
   time) and attempts used out of 4; anything automation can't finish is marked **needs manual
   refund** with the reason. After refunding it in the Authorize.net dashboard, the shopper
   records it with **Mark refunded manually** (processor transaction ID and a note required),
   and the customer sees the refund as sent.

### Test cards (Authorize.net sandbox)

| Card | Number | |
|---|---|---|
| Visa | `4111 1111 1111 1111` | any future expiry, any CVV |
| Mastercard | `5424 0000 0000 0015` | |
| Amex | `3700 0000 0000 002` | |
| Discover | `6011 0000 0000 0012` | |

To force a decline, use billing ZIP **46282**. If many test payments go through in a short time,
Authorize.net's Fraud Detection Suite may **hold** authorizations for review; ZipKart shows these as
"Your payment is being reviewed". Approve or void them in the Authorize.net dashboard, or relax the
velocity filter.

## Deploying to Render

`render.yaml` defines a free web service:

- **Build:** `pip install -r requirements.txt`
- **Start:** `python -m scripts.migrate && uvicorn app.main:app --host 0.0.0.0 --port $PORT …`
  (migrations run on every start; they're idempotent and lock-protected)
- **Health check:** `/health` (runs a real query against the database)

**Deploys:** every push to `main` deploys automatically (Auto-Deploy: On Commit). After a push,
wait until `/health` reports the new commit (`RENDER_GIT_COMMIT`) before treating it as live.

After the first deploy:
1. Copy the service URL from the Render dashboard **exactly** (Render may add a suffix such as
   `-eiwx`), set `APP_BASE_URL` to it and redeploy. Check the startup log for an `APP_BASE_URL`
   warning.
2. In the Hyperswitch dashboard, set the profile's webhook URL to `https://<app>/webhooks/hyperswitch`.
3. Make a test payment and confirm the webhook deliveries show HTTP 200 in the Hyperswitch dashboard.
   Deliveries that can't be processed are recorded in the `webhook_failures` table (stage, error,
   headers with the signature redacted, raw body).

**Cold starts:** Render's free tier sleeps after ~15 minutes idle; the first request after that
takes up to a minute. Neon also scales to zero, but wakes in well under a second. While the app
sleeps the background sweep doesn't run; it runs again on wake-up and whenever the store console
loads, and Hyperswitch retries webhooks that arrive while the app is asleep.

## What the sandbox taught us (and the code relies on)

- `net_amount = amount + shipping_cost + order_tax_amount`. We send the item hold as `amount` and
  verify `net_amount` equals our hold.
- `amount_to_capture` is the exact total charged and is checked against `net_amount`. Capturing all
  of it gives `succeeded`; less gives `partially_captured` (capture is then final; a second one is `IR_14`).
- **Hyperswitch allows `/cancel` after a partial capture on Authorize.net**, which voids the whole
  capture. Only our order-state checks prevent it, and the sync flags any such change for review.
- `IR_16` means "can't cancel in this state" (already cancelled, or held for review), not only
  "already cancelled".
- Refunds before the nightly settlement fail with Authorize.net error **54** (`UE_4000`), and
  Hyperswitch keeps that failed refund under our `refund_id`, hence the attempt suffix.

## Known limitations

- **Refunds wait for settlement.** Authorize.net only refunds settled captures (error 54 before that).
  Refunds are scheduled for the settlement window, retried only once per window on error 54, and
  capped at 4 automatic attempts per payment, because Hyperswitch refuses any refund (`IR_12`) once a
  payment has 11 on record, failed ones included. Everything else goes to *needs manual refund*.
- **The sweep runs hourly through GitHub Actions** (`.github/workflows/sweep.yml`, minute 17).
  Render's free tier sleeps after 15 minutes without inbound HTTP, a sleeping app runs no timer, and
  Hyperswitch webhooks don't reliably reach it, so an external schedule calls `POST /internal/sweep`
  (header `X-Sweep-Secret`, from the `INTERNAL_SWEEP_SECRET` repository secret). Hourly keeps it
  within the free Actions minutes for a private repo. Scheduled refunds therefore go out within
  about an hour of their settlement window. **The first page load after the app has slept can take
  about a minute** while Render starts it.
- **Free replacements are recorded, not fulfilled.** There's no "next order" credit system yet.
- **No customer accounts.** Anyone with an order's link (a random 12-character ID) can view it and
  report problems on it.
- **One shared store password**, no per-shopper accounts; login is only slowed down, not rate-limited.
- **Delivery isn't modelled**: a captured order is "picked and on its way", and problems can be
  reported from then on.
- **Simple tax** (8.25% on soda and paper goods only) and USD only.
- **Holds expire** after about 7 days at most banks. Fine for same-day delivery; scheduled delivery
  would need the card saved and the authorization taken close to the delivery window.
- **Webhooks in production** need the public URL configured in Hyperswitch (local testing used
  correctly and incorrectly signed sample requests).
- **Automated tests cover the webhook handler only** (`tests/`, using a real-format Hyperswitch
  event). Everything else was tested with scripts against the sandbox and in the browser.

## Project layout

```
app/
  main.py          routes, request validation, log redaction of client secrets
  pricing.py       server-side pricing, hold and totals (one function for every total)
  payments.py      orders, payment attempts, forward-only payment sync
  fulfillment.py   picking, substitutions (headroom + reservation), finalize & capture, cancel
  remedies.py      problem reports and their limits
  refunds.py       refund attempts, error 54 handling, retries
  webhooks.py      signature verification, dedupe, re-fetch
  sweep.py         reconciliation sweep
  store.py         store console API + signed-cookie login
  views.py         read models for the browser (receipt, hold breakdown, prompts)
migrations/        SQL, applied in order by scripts/migrate.py
static/            checkout, order page, store console
```
