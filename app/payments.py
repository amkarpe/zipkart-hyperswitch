"""Orders and payment attempts.

Rules this module enforces:
  * Every state change is a compare-and-set (UPDATE ... WHERE status = expected);
    rowcount 0 means someone else already moved it, so we re-read instead of acting.
  * No DB transaction is ever open across a Hyperswitch call: write intent + commit,
    call Hyperswitch, then record the result in a second transaction.
  * Payment IDs are deterministic (pay_{order_id}_{attempt}), so re-issuing a create
    after a timeout is safe: Hyperswitch answers HE_01 and we fetch instead.
  * Hyperswitch (GET /payments/{id}) is the source of truth; we only move forward.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

import psycopg

from app import hyperswitch as hs
from app.config import settings
from app.db import pool
from app.pricing import Quote, price_cart

log = logging.getLogger("zipkart.payments")

SESSION_EXPIRY_SECONDS = 900
# Don't hand out a client_secret that dies before the customer can type a card number.
SESSION_MIN_REMAINING = timedelta(seconds=120)

# ── Payment status ordering ────────────────────────────────────────────────
# Non-final statuses in the order a healthy payment passes through them.
_PROGRESS = [
    "creating",
    "requires_payment_method",
    "requires_confirmation",
    "requires_customer_action",
    "processing",
    "requires_capture",
    "partially_captured_and_capturable",
]
_FINAL = {"create_failed", "partially_captured", "succeeded", "cancelled", "failed"}
KNOWN_STATUSES = set(_PROGRESS) | _FINAL


def allowed_previous(new_status: str) -> list[str]:
    """Statuses a payment may be in for `new_status` to be applied (forward-only).

    The new status itself is included so a repeat sync can refresh amounts/ids.
    A final status can never be left, and a late "processing" can never undo
    "requires_capture".
    """
    if new_status in _FINAL:
        return _PROGRESS + [new_status]
    rank = _PROGRESS.index(new_status)
    return _PROGRESS[: rank + 1]


# ── Helpers ────────────────────────────────────────────────────────────────
_ID_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"  # no 0/O/1/I/L/U: easy to read aloud to support


def new_order_id() -> str:
    return "ZK" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(10))


def payment_id_for(order_id: str, attempt_no: int) -> str:
    return f"pay_{order_id}_{attempt_no}"


def return_url_for(order_id: str) -> str:
    return f"{settings.app_base_url}/orders/{order_id}/return"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CheckoutError(Exception):
    """Something the customer can act on (bad cart, declined, try again)."""

    def __init__(self, message: str, http_status: int = 400):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


# ── Catalog ────────────────────────────────────────────────────────────────
async def load_products() -> dict[str, dict]:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM products ORDER BY sort_order")
        return {row["sku"]: row for row in await cur.fetchall()}


async def quote_cart(cart: list[dict]) -> Quote:
    return price_cart(cart, await load_products(), settings.weight_buffer_percent)


# ── Hyperswitch payload ────────────────────────────────────────────────────
def _address(order: dict) -> dict:
    first, _, last = order["customer_name"].strip().partition(" ")
    return {
        "address": {
            "first_name": first,
            "last_name": last or first,
            "line1": order["ship_line1"],
            "line2": order["ship_line2"],
            "city": order["ship_city"],
            "state": order["ship_state"],
            "zip": order["ship_zip"],
            "country": "US",
        },
        "phone": {"number": order["customer_phone"], "country_code": "+1"},
        "email": order["customer_email"],
    }


def build_payment_payload(order: dict, items: list[dict], payment_id: str, attempt_no: int) -> dict:
    """amount + shipping_cost + order_tax_amount == net_amount == hold (verified in sandbox)."""
    hold_items = sum(i["hold_line_cents"] for i in items)
    hold_tax = order["hold_amount_cents"] - order["delivery_fee_cents"] - hold_items

    order_details = []
    for i in items:
        if i["pricing_type"] == "fixed":
            order_details.append({
                "product_name": i["name"],
                "product_id": i["sku"],
                "sku": i["sku"],
                "quantity": i["quantity"],
                "amount": i["unit_price_cents"],
                "requires_shipping": True,
            })
        else:
            # Weighed: one line at the hold value so line items sum to `amount`.
            order_details.append({
                "product_name": f"{i['name']} (est. {i['estimated_weight_lb']} lb, "
                                f"incl. {settings.weight_buffer_percent}% weight buffer)",
                "product_id": i["sku"],
                "sku": i["sku"],
                "quantity": 1,
                "amount": i["hold_line_cents"],
                "unit_of_measure": "lb",
                "requires_shipping": True,
            })

    customer_key = hashlib.sha256(order["customer_email"].strip().lower().encode()).hexdigest()[:24]
    address = _address(order)
    return {
        "payment_id": payment_id,
        "amount": hold_items,
        "shipping_cost": order["delivery_fee_cents"],
        "order_tax_amount": hold_tax,
        "currency": order["currency"],
        "capture_method": "manual",
        "authentication_type": "no_three_ds",
        "profile_id": settings.hyperswitch_profile_id,
        "return_url": return_url_for(order["id"]),
        "merchant_order_reference_id": order["id"],
        "description": f"ZipKart order {order['id']}",
        "order_details": order_details,
        "customer": {
            "id": f"cus_{customer_key}",
            "name": order["customer_name"],
            "email": order["customer_email"],
        },
        "billing": address,
        "shipping": address,
        "session_expiry": SESSION_EXPIRY_SECONDS,
        "metadata": {"order_id": order["id"], "attempt_no": str(attempt_no)},
    }


# ── Recording Hyperswitch state ────────────────────────────────────────────
async def _apply_snapshot(conn, payment_id: str, snap: dict) -> str:
    """Record a GET/create response forward-only; returns the resulting local status.
    Runs inside the caller's transaction."""
    new_status = snap.get("status")
    cur = await conn.execute(
        "UPDATE payments SET hs_status_raw = %s, last_synced_at = now() WHERE payment_id = %s RETURNING status",
        (new_status, payment_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"Unknown payment {payment_id}")
    if new_status not in KNOWN_STATUSES or new_status == "creating":
        log.warning("payment %s: unhandled Hyperswitch status %r, left as %s", payment_id, new_status, row["status"])
        return row["status"]

    cur = await conn.execute(
        """UPDATE payments SET
               status = %(status)s,
               amount_capturable_cents = %(capturable)s,
               amount_received_cents = %(received)s,
               client_secret = COALESCE(%(client_secret)s, client_secret),
               expires_at = COALESCE(%(expires_at)s, expires_at),
               connector = COALESCE(%(connector)s, connector),
               connector_transaction_id = COALESCE(%(txn)s, connector_transaction_id),
               error_code = %(error_code)s,
               unified_code = %(unified_code)s,
               error_message = %(error_message)s,
               unified_message = %(unified_message)s,
               updated_at = now()
           WHERE payment_id = %(payment_id)s AND status = ANY(%(allowed)s)
           RETURNING status""",
        {
            "status": new_status,
            "capturable": snap.get("amount_capturable"),
            "received": snap.get("amount_received"),
            "client_secret": snap.get("client_secret"),
            "expires_at": _parse_ts(snap.get("expires_on")),
            "connector": snap.get("connector"),
            "txn": snap.get("connector_transaction_id"),
            "error_code": snap.get("error_code"),
            "unified_code": snap.get("unified_code"),
            "error_message": snap.get("error_message"),
            "unified_message": snap.get("unified_message"),
            "payment_id": payment_id,
            "allowed": allowed_previous(new_status),
        },
    )
    updated = await cur.fetchone()
    if updated is None:
        # Stale or out-of-order view (e.g. "processing" after we saw "requires_capture"): ignore.
        log.info("payment %s: ignored backward transition %s -> %s", payment_id, row["status"], new_status)
        return row["status"]
    return updated["status"]


async def _advance_order(conn, payment_id: str) -> None:
    """Move the order forward based on its ACTIVE payment. Inside the caller's transaction."""
    cur = await conn.execute(
        """SELECT p.status, p.is_active, p.amount_capturable_cents, o.id AS order_id, o.hold_amount_cents
           FROM payments p JOIN orders o ON o.id = p.order_id
           WHERE p.payment_id = %s""",
        (payment_id,),
    )
    row = await cur.fetchone()
    if not row["is_active"]:
        if row["status"] in ("requires_capture", "partially_captured_and_capturable"):
            # An old attempt authorized after we replaced it: a second hold on the card.
            # Flag it; the reconciliation sweep (later step) will void it.
            log.error("payment %s: inactive attempt holds funds; needs void", payment_id)
        return

    if row["status"] in ("failed", "cancelled"):
        # Normal before authorization (customer retries). After the order moved on
        # (authorized/picking/...) it means money state we didn't drive: flag, don't guess.
        # The cancel flow (later step) moves the order to 'cancelling' first, so it's excluded.
        await conn.execute(
            "UPDATE orders SET status = 'needs_review', review_reason = %s, updated_at = now() "
            "WHERE id = %s AND status IN ('authorized', 'picking', 'finalizing')",
            (f"Active payment {payment_id} became {row['status']} after authorization", row["order_id"]),
        )
        return

    if row["status"] == "requires_capture":
        if row["amount_capturable_cents"] == row["hold_amount_cents"]:
            await conn.execute(
                "UPDATE orders SET status = 'authorized', updated_at = now() "
                "WHERE id = %s AND status = 'pending_payment'",
                (row["order_id"],),
            )
        else:
            await conn.execute(
                "UPDATE orders SET status = 'needs_review', review_reason = %s, updated_at = now() "
                "WHERE id = %s AND status = 'pending_payment'",
                (f"Authorized {row['amount_capturable_cents']} but expected hold {row['hold_amount_cents']}",
                 row["order_id"]),
            )


async def _record(payment_id: str, snap: dict) -> str:
    async with pool.connection() as conn, conn.transaction():
        status = await _apply_snapshot(conn, payment_id, snap)
        await _advance_order(conn, payment_id)
        return status


# ── Creating a payment attempt ─────────────────────────────────────────────
async def _load_order(conn, order_id: str) -> tuple[dict, list[dict]]:
    cur = await conn.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
    order = await cur.fetchone()
    if order is None:
        raise CheckoutError("Order not found.", 404)
    cur = await conn.execute("SELECT * FROM order_items WHERE order_id = %s ORDER BY id", (order_id,))
    return order, await cur.fetchall()


async def _start_payment(order_id: str, payment_id: str, attempt_no: int) -> None:
    """Create the Hyperswitch payment for a row already committed as 'creating'.
    Safe to call again for the same payment_id (HE_01 -> fetch)."""
    async with pool.connection() as conn:
        order, items = await _load_order(conn, order_id)
    payload = build_payment_payload(order, items, payment_id, attempt_no)

    try:
        snap = await hs.create_payment(payload)
    except hs.HyperswitchError as exc:
        if exc.code == "HE_01":  # already created by an earlier (timed-out) call
            snap = await hs.retrieve_payment(payment_id)
        else:
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE payments SET status = 'create_failed', error_code = %s, "
                    "error_message = %s, updated_at = now() WHERE payment_id = %s AND status = 'creating'",
                    (exc.code, exc.message, payment_id),
                )
            log.error("create %s failed: %s %s", payment_id, exc.code, exc.message)
            raise CheckoutError("We couldn't start the payment. Please try again.", 502)
    except hs.HyperswitchUnavailable as exc:
        # Outcome unknown. The row stays 'creating'; the next payment-session call
        # re-issues the same payment_id and lands on HE_01 or a fresh create.
        log.warning("create %s outcome unknown: %s", payment_id, exc)
        raise CheckoutError("Payment service is slow to respond. Please try again.", 503)

    if snap.get("net_amount") != order["hold_amount_cents"]:
        # Should be impossible (amount + shipping + tax == hold); refuse to show a wrong hold.
        log.error("create %s: net_amount %s != hold %s", payment_id, snap.get("net_amount"), order["hold_amount_cents"])
        raise CheckoutError("Payment amount mismatch. Please contact support.", 500)
    await _record(payment_id, snap)


async def create_order(cart: list[dict], customer: dict) -> dict:
    quote = await quote_cart(cart)
    order_id = new_order_id()
    payment_id = payment_id_for(order_id, 1)

    # Tx 1: order, items and payment intent, committed before any network call.
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO orders (id, currency, customer_name, customer_email, customer_phone,
                   ship_line1, ship_line2, ship_city, ship_state, ship_zip,
                   subtotal_cents, tax_cents, delivery_fee_cents, estimated_total_cents, hold_amount_cents)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (order_id, settings.currency, customer["name"], customer["email"], customer["phone"],
             customer["line1"], customer.get("line2") or None, customer["city"], customer["state"],
             customer["zip"], quote.subtotal_cents, quote.tax_cents, quote.delivery_fee_cents,
             quote.estimated_total_cents, quote.hold_amount_cents),
        )
        for line in quote.lines:
            await conn.execute(
                """INSERT INTO order_items (order_id, sku, name, pricing_type, unit_price_cents, taxable,
                       quantity, estimated_weight_lb, estimated_line_cents, hold_line_cents)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (order_id, line.sku, line.name, line.pricing_type, line.unit_price_cents, line.taxable,
                 line.quantity, line.estimated_weight_lb, line.estimated_line_cents, line.hold_line_cents),
            )
        await conn.execute(
            "INSERT INTO payments (payment_id, order_id, attempt_no, amount_cents) VALUES (%s, %s, 1, %s)",
            (payment_id, order_id, quote.hold_amount_cents),
        )

    # Network call with no transaction open, then Tx 2 inside _start_payment.
    await _start_payment(order_id, payment_id, 1)
    return await payment_session(order_id)


# ── Sync (return page, later: webhooks and sweep) ──────────────────────────
async def _active_payment(conn, order_id: str) -> dict | None:
    cur = await conn.execute("SELECT * FROM payments WHERE order_id = %s AND is_active", (order_id,))
    return await cur.fetchone()


async def sync_payment(order_id: str) -> dict:
    """Fetch the active payment from Hyperswitch and apply it forward-only."""
    async with pool.connection() as conn:
        payment = await _active_payment(conn, order_id)
    if payment is not None and payment["status"] not in ("creating", "create_failed"):
        try:
            snap = await hs.retrieve_payment(payment["payment_id"])
            await _record(payment["payment_id"], snap)
        except (hs.HyperswitchError, hs.HyperswitchUnavailable) as exc:
            # Keep last known state; webhooks / the sweep will catch up.
            log.warning("sync %s failed: %s", payment["payment_id"], exc)
    return await order_view(order_id)


# ── Payment session: reuse, or replace an expired/failed attempt ───────────
def _session_usable(payment: dict) -> bool:
    expires_at = payment["expires_at"]
    return expires_at is not None and expires_at - datetime.now(timezone.utc) > SESSION_MIN_REMAINING


async def payment_session(order_id: str) -> dict:
    """Return a usable client_secret for a pending order, creating a fresh
    payment attempt when the current one is expired, failed or cancelled."""
    # Bounded: each pass either returns or makes progress (created, cancelled or replaced an
    # attempt). Worst case is racing another tab through cancel -> replace -> create.
    for _ in range(6):
        async with pool.connection() as conn:
            order, _items = await _load_order(conn, order_id)
            payment = await _active_payment(conn, order_id)

        if order["status"] != "pending_payment":
            return {"order_id": order_id, "order_status": order["status"]}

        if payment is not None and payment["status"] == "creating":
            await _start_payment(order_id, payment["payment_id"], payment["attempt_no"])
            continue

        if payment is not None:
            try:
                snap = await hs.retrieve_payment(payment["payment_id"])
                status = await _record(payment["payment_id"], snap)
            except hs.HyperswitchUnavailable:
                raise CheckoutError("Payment service is slow to respond. Please try again.", 503)
            if status in ("requires_customer_action", "processing", "requires_capture"):
                # Customer already submitted a card; send them to the status page.
                return {"order_id": order_id, "order_status": "pending_payment", "payment_status": status}
            if status == "requires_payment_method":
                async with pool.connection() as conn:
                    payment = await _active_payment(conn, order_id)
                if _session_usable(payment):
                    return {
                        "order_id": order_id,
                        "order_status": "pending_payment",
                        "payment_status": status,
                        "client_secret": payment["client_secret"],
                        "publishable_key": settings.hyperswitch_publishable_key,
                        "return_url": return_url_for(order_id),
                        "hold_amount_cents": order["hold_amount_cents"],
                        "estimated_total_cents": order["estimated_total_cents"],
                        "expires_at": payment["expires_at"].isoformat(),
                    }
                # Expired or nearly so: close it at Hyperswitch first, so a tab still
                # holding the old client_secret can't authorize a second hold.
                try:
                    await hs.cancel_payment(payment["payment_id"], "session_replaced")
                except hs.HyperswitchError as exc:
                    log.info("cancel %s: %s %s (re-syncing)", payment["payment_id"], exc.code, exc.message)
                    continue  # e.g. IR_16: state moved (customer confirmed?) - re-read before replacing
                except hs.HyperswitchUnavailable:
                    raise CheckoutError("Payment service is slow to respond. Please try again.", 503)
                await _record(payment["payment_id"], await hs.retrieve_payment(payment["payment_id"]))
            elif status not in ("failed", "cancelled", "create_failed"):
                # Captured/partially captured etc. on a pending order: not ours to "fix" by re-charging.
                return {"order_id": order_id, "order_status": order["status"], "payment_status": status}

        await _replace_payment(order_id, payment)
    raise CheckoutError("Couldn't prepare your payment. Please refresh and try again.", 409)


async def _replace_payment(order_id: str, old: dict | None) -> None:
    """Deactivate a final (failed/cancelled/create_failed) attempt and open attempt n+1.
    Returns quietly if a competing request got there first; the caller re-reads."""
    try:
        await _replace_payment_tx(order_id, old)
    except psycopg.errors.UniqueViolation:
        log.info("order %s: concurrent payment replacement; using the other request's attempt", order_id)


async def _replace_payment_tx(order_id: str, old: dict | None) -> None:
    async with pool.connection() as conn, conn.transaction():
        if old is not None:
            cur = await conn.execute(
                "UPDATE payments SET is_active = FALSE, updated_at = now() "
                "WHERE payment_id = %s AND is_active AND status IN ('failed', 'cancelled', 'create_failed')",
                (old["payment_id"],),
            )
            if cur.rowcount == 0:
                return  # another request replaced it, or it isn't final: caller re-reads
        cur = await conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS n FROM payments WHERE order_id = %s", (order_id,)
        )
        attempt_no = (await cur.fetchone())["n"]
        cur = await conn.execute("SELECT hold_amount_cents FROM orders WHERE id = %s", (order_id,))
        hold = (await cur.fetchone())["hold_amount_cents"]
        # payments_one_active_per_order_idx rejects this if a competing request got here first.
        await conn.execute(
            "INSERT INTO payments (payment_id, order_id, attempt_no, amount_cents) VALUES (%s, %s, %s, %s)",
            (payment_id_for(order_id, attempt_no), order_id, attempt_no, hold),
        )
    # Loop in payment_session() sees 'creating' and runs _start_payment.


# ── Read model for the browser (never includes secrets) ────────────────────
async def order_view(order_id: str) -> dict:
    async with pool.connection() as conn:
        order, items = await _load_order(conn, order_id)
        payment = await _active_payment(conn, order_id)
    return {
        "order_id": order["id"],
        "status": order["status"],
        "currency": order["currency"],
        "subtotal_cents": order["subtotal_cents"],
        "tax_cents": order["tax_cents"],
        "delivery_fee_cents": order["delivery_fee_cents"],
        "estimated_total_cents": order["estimated_total_cents"],
        "hold_amount_cents": order["hold_amount_cents"],
        "customer_name": order["customer_name"],
        "items": [
            {
                "sku": i["sku"],
                "name": i["name"],
                "pricing_type": i["pricing_type"],
                "quantity": i["quantity"],
                "estimated_weight_lb": str(i["estimated_weight_lb"]) if i["estimated_weight_lb"] is not None else None,
                "estimated_line_cents": i["estimated_line_cents"],
                "status": i["status"],
            }
            for i in items
        ],
        "payment": None if payment is None else {
            "attempt_no": payment["attempt_no"],
            "status": payment["status"],
            "unified_code": payment["unified_code"],
            "message": payment["unified_message"],
        },
    }
