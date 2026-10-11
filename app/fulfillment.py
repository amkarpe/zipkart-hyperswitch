"""Picking, substitutions, finalize & capture, cancel.

Concurrency model:
  * Anything that decides from several rows (headroom, "no substitution pending",
    final amount) runs in one short transaction holding SELECT ... FOR UPDATE on
    the order row. Every writer to an order's items takes that lock first, so
    those decisions can't interleave.
  * Each single-row transition is still a compare-and-set, so a double tap,
    a late tap or a retried request does nothing the second time.
  * Hyperswitch calls happen only after commit; results are recorded in a new
    transaction (payments._record), which moves the order forward.
"""
import logging
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from app import hyperswitch as hs
from app import payments
from app.config import settings
from app.db import pool
from app.pricing import MAX_FIXED_QTY, Totals, totals, weighed_line_cents
from app.views import load_order

log = logging.getLogger("zipkart.fulfillment")

MAX_PICK_WEIGHT_LB = Decimal("50")
# A weighed line may come in at most this much over what the customer ordered, even
# when the hold has room (e.g. after another item was removed). Room in the hold makes
# it financially safe; this keeps it to what the customer actually asked for.
WEIGHT_TOLERANCE_PERCENT = 20


def max_pick_weight_lb(ordered_lb: Decimal) -> Decimal:
    """Ordered weight + tolerance, rounded DOWN to the scale's 0.01 lb."""
    return (ordered_lb * (100 + WEIGHT_TOLERANCE_PERCENT) / 100).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
CAPTURED = ("partially_captured", "succeeded")


class FulfillmentError(Exception):
    def __init__(self, message: str, http_status: int = 409):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


# ── Money: what each line counts for ───────────────────────────────────────
def commitment_cents(item: dict) -> int:
    """What a line may still cost us, for the headroom check.

    An unpicked line counts at its HOLD value (estimate + weight buffer): until
    it's weighed we must assume it could use its whole buffer, or a substitute
    could eat the buffer a heavy chicken breast needs later.
    """
    status = item["status"]
    if status == "pending":
        return item["hold_line_cents"]
    if status in ("picked", "proposed"):   # proposed = reserved for a pending substitution
        return item["final_line_cents"]
    return 0                               # substitution_proposed, substituted, removed, declined


def committed_totals(order: dict, items: list[dict]) -> Totals:
    return totals([(commitment_cents(i), i["taxable"]) for i in items], order["delivery_fee_cents"])


def final_totals(order: dict, items: list[dict]) -> Totals:
    return totals([(i["final_line_cents"], i["taxable"]) for i in items if i["status"] == "picked"],
                  order["delivery_fee_cents"])


def capturable_cents(payment: dict | None) -> int:
    if payment is None or payment["status"] != "requires_capture" or payment["amount_capturable_cents"] is None:
        raise FulfillmentError("The payment hold isn't confirmed for this order.")
    return payment["amount_capturable_cents"]


def headroom_cents(order: dict, items: list[dict], payment: dict) -> int:
    """authorized − committed lines − recalculated tax and delivery − reservations."""
    return capturable_cents(payment) - committed_totals(order, items).total_cents


def _with(items: list[dict], item_id: int, **changes) -> list[dict]:
    """A copy of items with one line changed: lets us test a change before writing it."""
    return [{**i, **changes} if i["id"] == item_id else i for i in items]


# ── Locking helper ─────────────────────────────────────────────────────────
async def _lock(conn, order_id: str, *allowed: str) -> tuple[dict, list[dict], dict | None]:
    cur = await conn.execute("SELECT status FROM orders WHERE id = %s FOR UPDATE", (order_id,))
    row = await cur.fetchone()
    if row is None:
        raise FulfillmentError("Order not found.", 404)
    if allowed and row["status"] not in allowed:
        raise FulfillmentError(f"This order is {row['status'].replace('_', ' ')}; that action isn't available.")
    order, items, payment = await load_order(conn, order_id)
    return order, items, payment


def _original_line(items: list[dict], item_id: int) -> dict:
    for i in items:
        if i["id"] == item_id:
            if i["substitute_for_item_id"] is not None:
                raise FulfillmentError("Substitute lines can't be changed; decline it and propose another.")
            return i
    raise FulfillmentError("Item not found on this order.", 404)


def _parse_weight(value) -> Decimal:
    try:
        weight = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise FulfillmentError("Enter the weight in pounds, e.g. 1.37.", 400)
    if not Decimal("0.01") <= weight <= MAX_PICK_WEIGHT_LB:
        raise FulfillmentError(f"Weight must be between 0.01 and {MAX_PICK_WEIGHT_LB} lb.", 400)
    return weight


# ── Substitution expiry (lazy: applied whenever someone reads or acts) ──────
async def _expire_substitutions(conn, order: dict, items: list[dict]) -> int:
    """Apply the order's preference to unanswered proposals. Caller holds the order lock."""
    if order["status"] != "picking":
        return 0
    cur = await conn.execute(
        "SELECT id FROM order_items WHERE order_id = %s AND status = 'proposed' AND substitution_expires_at <= now()",
        (order["id"],),
    )
    expired = [r["id"] for r in await cur.fetchall()]
    accept = order["substitution_preference"] == "best_match"
    for sub_id in expired:
        await _resolve(conn, sub_id, accept)
    return len(expired)


async def apply_expired_substitutions(order_id: str) -> None:
    """Cheap unlocked check first; take the lock only when something has expired."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM order_items WHERE order_id = %s AND status = 'proposed' "
            "AND substitution_expires_at <= now() LIMIT 1",
            (order_id,),
        )
        if await cur.fetchone() is None:
            return
        async with conn.transaction():
            order, items, _ = await _lock(conn, order_id)
            await _expire_substitutions(conn, order, items)


async def _resolve(conn, sub_id: int, accept: bool) -> bool:
    """proposed -> picked|declined and its original -> substituted|removed, both CAS.
    Returns False if the proposal was already resolved (late or double tap)."""
    cur = await conn.execute(
        """UPDATE order_items SET
               status = %s,
               picked_quantity = CASE WHEN %s AND pricing_type = 'fixed' THEN quantity END,
               updated_at = now()
           WHERE id = %s AND status = 'proposed'
           RETURNING substitute_for_item_id""",
        ("picked" if accept else "declined", accept, sub_id),
    )
    row = await cur.fetchone()
    if row is None:
        return False
    cur = await conn.execute(
        "UPDATE order_items SET status = %s, updated_at = now() WHERE id = %s AND status = 'substitution_proposed'",
        ("substituted" if accept else "removed", row["substitute_for_item_id"]),
    )
    if cur.rowcount != 1:  # can't happen under the order lock; refuse to leave a half-resolved pair
        raise RuntimeError(f"substitute {sub_id}: original line not in substitution_proposed")
    return True


# ── Step 7: store console ──────────────────────────────────────────────────
async def start_picking(order_id: str) -> None:
    """authorized -> picking, only if the server has seen the hold (requires_capture)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """UPDATE orders SET status = 'picking', picking_started_at = now(), updated_at = now()
               WHERE id = %s AND status = 'authorized'
                 AND EXISTS (SELECT 1 FROM payments p
                             WHERE p.order_id = orders.id AND p.is_active AND p.status = 'requires_capture')""",
            (order_id,),
        )
        if cur.rowcount == 1:
            return
        cur = await conn.execute("SELECT status FROM orders WHERE id = %s", (order_id,))
        row = await cur.fetchone()
    if row is None:
        raise FulfillmentError("Order not found.", 404)
    if row["status"] != "picking":  # already picking (another shopper / double click) is fine
        raise FulfillmentError(f"Can't start picking: order is {row['status'].replace('_', ' ')}.")


async def pick_item(order_id: str, item_id: int, picked_quantity: int | None, actual_weight_lb) -> None:
    async with pool.connection() as conn, conn.transaction():
        order, items, payment = await _lock(conn, order_id, "picking")
        item = _original_line(items, item_id)
        if item["status"] not in ("pending", "picked"):
            raise FulfillmentError(f"{item['name']} is {item['status'].replace('_', ' ')}; it can't be picked now.")

        if item["pricing_type"] == "fixed":
            if not isinstance(picked_quantity, int) or not 1 <= picked_quantity <= item["quantity"]:
                raise FulfillmentError(f"Picked quantity must be 1–{item['quantity']}. "
                                       "Use out of stock if none are available.", 400)
            final, weight = item["unit_price_cents"] * picked_quantity, None
        else:
            weight = _parse_weight(actual_weight_lb)
            limit = max_pick_weight_lb(item["estimated_weight_lb"])
            if weight > limit:
                raise FulfillmentError(f"{item['name']}: the customer ordered {item['estimated_weight_lb']} lb; "
                                       f"pick at most {limit} lb (+{WEIGHT_TOLERANCE_PERCENT}%).", 400)
            final, picked_quantity = weighed_line_cents(item["unit_price_cents"], weight), None

        trial = _with(items, item_id, status="picked", final_line_cents=final)
        room = headroom_cents(order, trial, payment)
        if room < 0:
            raise FulfillmentError(f"{item['name']} at that weight is {_usd(-room)} over the customer's hold. "
                                   "Pick a little less.")
        cur = await conn.execute(
            """UPDATE order_items SET status = 'picked', picked_quantity = %s, actual_weight_lb = %s,
                   final_line_cents = %s, updated_at = now()
               WHERE id = %s AND status IN ('pending', 'picked')""",
            (picked_quantity, weight, final, item_id),
        )
        if cur.rowcount != 1:
            raise FulfillmentError("That line changed; refresh and try again.")


async def mark_out_of_stock(order_id: str, item_id: int) -> None:
    """Out of stock with no substitute: remove the line."""
    async with pool.connection() as conn, conn.transaction():
        _order, items, _ = await _lock(conn, order_id, "picking")
        item = _original_line(items, item_id)
        cur = await conn.execute(
            "UPDATE order_items SET status = 'removed', picked_quantity = NULL, actual_weight_lb = NULL, "
            "final_line_cents = NULL, updated_at = now() WHERE id = %s AND status IN ('pending', 'picked')",
            (item["id"],),
        )
        if cur.rowcount != 1:
            raise FulfillmentError(f"{item['name']} is {item['status'].replace('_', ' ')}; it can't be removed now.")


def _substitute_price(product: dict, quantity, weight) -> tuple[int, int | None, Decimal | None]:
    if product["pricing_type"] == "fixed":
        if not isinstance(quantity, int) or not 1 <= quantity <= MAX_FIXED_QTY:
            raise FulfillmentError(f"Substitute quantity must be 1–{MAX_FIXED_QTY}.", 400)
        return product["unit_price_cents"] * quantity, quantity, None
    w = _parse_weight(weight)
    return weighed_line_cents(product["unit_price_cents"], w), None, w


async def substitute_candidates(order_id: str, item_id: int) -> dict:
    """Read-only list for the shopper: every product, priced, with whether it fits.
    The proposal re-checks under the lock, so this list can be slightly stale safely."""
    async with pool.connection() as conn:
        order, items, payment = await load_order(conn, order_id)
        products = await payments.load_products()
    if order is None:
        raise FulfillmentError("Order not found.", 404)
    item = _original_line(items, item_id)
    capturable = capturable_cents(payment)
    without = _with(items, item_id, status="substitution_proposed")  # the original no longer counts
    base = committed_totals(order, without)

    out = []
    for p in products.values():
        if p["sku"] == item["sku"] or not p["in_stock"]:
            continue
        if p["pricing_type"] == "fixed":
            qty = item["quantity"] if item["pricing_type"] == "fixed" else 1
            line, weight = p["unit_price_cents"] * qty, None
        else:
            qty, weight = None, item["estimated_weight_lb"] if item["pricing_type"] == "weighed" else Decimal("1.00")
            line = weighed_line_cents(p["unit_price_cents"], weight)
        trial = totals([(commitment_cents(i), i["taxable"]) for i in without] + [(line, p["taxable"])],
                       order["delivery_fee_cents"])
        out.append({
            "sku": p["sku"], "name": p["name"], "category": p["category"], "emoji": p["emoji"],
            "pricing_type": p["pricing_type"], "unit_price_cents": p["unit_price_cents"], "unit_label": p["unit_label"],
            "default_quantity": qty, "default_weight_lb": None if weight is None else f"{weight:.2f}",
            "line_cents": line, "fits": trial.total_cents <= capturable,
        })
    out.sort(key=lambda c: (not c["fits"], c["category"] != _category(products, item["sku"]), c["name"]))
    return {"item_id": item_id, "original_name": item["name"],
            "headroom_cents": capturable - base.total_cents, "candidates": out}


def _category(products: dict, sku: str) -> str | None:
    return products[sku]["category"] if sku in products else None


async def propose_substitute(order_id: str, item_id: int, sku: str, quantity, weight) -> None:
    """Headroom check and reservation in one short transaction under the order lock."""
    products = await payments.load_products()
    product = products.get(sku)
    if product is None or not product["in_stock"]:
        raise FulfillmentError("That product isn't available.", 400)
    line, qty, w = _substitute_price(product, quantity, weight)

    async with pool.connection() as conn, conn.transaction():
        order, items, payment = await _lock(conn, order_id, "picking")
        item = _original_line(items, item_id)
        if item["status"] == "substitution_proposed":
            raise FulfillmentError(f"A substitute for {item['name']} is already waiting for the customer.")
        if item["status"] != "pending":
            raise FulfillmentError(f"{item['name']} is already {item['status'].replace('_', ' ')}; "
                                   "substitutes can only be offered for lines not yet picked.")
        if sku == item["sku"]:
            raise FulfillmentError("Pick a different product as the substitute.", 400)

        trial = _with(items, item_id, status="substitution_proposed") + [
            {"id": -1, "status": "proposed", "final_line_cents": line, "taxable": product["taxable"]}]
        room = headroom_cents(order, trial, payment)
        if room < 0:
            raise FulfillmentError(f"{product['name']} is {_usd(-room)} over the remaining hold. Choose something "
                                   "cheaper or remove the item.")

        cur = await conn.execute(
            "UPDATE order_items SET status = 'substitution_proposed', updated_at = now() "
            "WHERE id = %s AND status = 'pending'",
            (item_id,),
        )
        if cur.rowcount != 1:
            raise FulfillmentError("That line changed; refresh and try again.")
        await conn.execute(
            """INSERT INTO order_items (order_id, sku, name, pricing_type, unit_price_cents, taxable,
                   quantity, estimated_weight_lb, actual_weight_lb, estimated_line_cents, hold_line_cents,
                   final_line_cents, status, substitute_for_item_id, substitution_expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'proposed',%s, now() + %s)""",
            (order_id, product["sku"], product["name"], product["pricing_type"], product["unit_price_cents"],
             product["taxable"], qty, w, w, line, line, line, item_id,
             timedelta(seconds=settings.substitution_timeout_seconds)),
        )


async def decide_substitution(order_id: str, sub_id: int, accept: bool) -> None:
    """Customer's answer. A late or repeated tap changes nothing and says why.
    The outcome is decided inside the transaction but raised after commit, so an
    expiry applied along the way is kept."""
    async with pool.connection() as conn, conn.transaction():
        order, items, _ = await _lock(conn, order_id)
        sub = next((i for i in items if i["id"] == sub_id and i["substitute_for_item_id"] is not None), None)
        if sub is None:
            raise FulfillmentError("Substitution not found.", 404)
        if order["status"] != "picking":
            outcome = "finalized"
        elif sub["status"] != "proposed":
            # Resolved at or after its deadline means the timeout answered, not the customer.
            outcome = "expired" if sub["updated_at"] >= sub["substitution_expires_at"] else "already"
        else:
            await _expire_substitutions(conn, order, items)
            cur = await conn.execute("SELECT status FROM order_items WHERE id = %s", (sub_id,))
            if (await cur.fetchone())["status"] != "proposed":
                outcome = "expired"   # this very proposal just timed out
            else:
                outcome = "applied" if await _resolve(conn, sub_id, accept) else "already"
    if outcome == "finalized":
        raise FulfillmentError("Your order was already finalized, so this swap can't change now.")
    if outcome == "already":
        raise FulfillmentError("You already answered this one.")
    if outcome == "expired":
        raise FulfillmentError("Time ran out on this swap, so your preference was applied.")


# ── Step 9: finalize & capture ─────────────────────────────────────────────
async def finalize_and_capture(order_id: str) -> str:
    """picking -> finalizing -> capturing in one transaction (amount fixed and stored),
    commit, then capture exactly that amount. Re-running it on a 'capturing' order
    reconciles first and only re-sends the capture if Hyperswitch shows none happened."""
    await payments.sync_payment(order_id)  # fresh capturable from Hyperswitch (GET), outside any tx

    async with pool.connection() as conn, conn.transaction():
        order, items, payment = await _lock(conn, order_id, "picking", "capturing")
        resuming = order["status"] == "capturing"
        if not resuming:
            await _expire_substitutions(conn, order, items)
            order, items, payment = await load_order(conn, order_id)
            cur = await conn.execute(
                "UPDATE orders SET status = 'finalizing', updated_at = now() WHERE id = %s AND status = 'picking'",
                (order_id,),
            )
            if cur.rowcount != 1:
                raise FulfillmentError("Order changed; refresh and try again.")
            unpicked = [i["name"] for i in items if i["status"] == "pending"]
            waiting = [i["name"] for i in items if i["status"] == "proposed"]
            if unpicked:
                raise FulfillmentError(f"Not picked yet: {', '.join(unpicked)}.")
            if waiting:
                raise FulfillmentError(f"Waiting for the customer to answer: {', '.join(waiting)}.")
            final = final_totals(order, items)
            if final.subtotal_cents == 0:
                raise FulfillmentError("Nothing was picked. Cancel the order instead to release the hold.")
            capturable = capturable_cents(payment)
            if final.total_cents > capturable:
                raise FulfillmentError(f"Final amount {_usd(final.total_cents)} is more than the hold "
                                       f"({_usd(capturable)}). Adjust the picks.")
            await conn.execute(
                """UPDATE orders SET status = 'capturing', final_subtotal_cents = %s, final_tax_cents = %s,
                       final_amount_cents = %s, updated_at = now()
                   WHERE id = %s AND status = 'finalizing'""",
                (final.subtotal_cents, final.tax_cents, final.total_cents, order_id),
            )
        payment_id, amount = payment["payment_id"], (order["final_amount_cents"] if resuming else final.total_cents)

    if resuming:
        # A previous capture call didn't come back. Look before acting.
        snap = await _get_or_none(payment_id)
        if snap is None:
            raise FulfillmentError("Payment service is slow to respond; the capture is still pending. "
                                   "Try again in a moment.", 503)
        if snap.get("status") != "requires_capture":
            await payments._record(payment_id, snap)   # captured (or something to review): record it
            return "reconciled"

    try:
        snap = await hs.capture_payment(payment_id, amount)
    except hs.HyperswitchError as exc:
        snap = await _get_or_none(payment_id)
        if exc.code == "IR_14":     # already captured (an earlier call landed): fetch and sync
            if snap is not None:
                await payments._record(payment_id, snap)
            return "already_captured"
        if snap is not None:
            await payments._record(payment_id, snap)
        if snap is not None and snap.get("status") == "requires_capture":
            # Definitive rejection and nothing captured: reopen the order so the store can fix it.
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE orders SET status = 'picking', updated_at = now() WHERE id = %s AND status = 'capturing'",
                    (order_id,),
                )
        log.error("capture %s rejected: %s %s", payment_id, exc.code, exc.message)
        raise FulfillmentError(f"The capture was rejected ({exc.code}): {exc.message}", 502)
    except hs.HyperswitchUnavailable as exc:
        # Unknown outcome: never re-send blindly. Fetch; if it's not captured yet, the
        # order stays 'capturing' and the next attempt reconciles before acting.
        log.warning("capture %s outcome unknown: %s", payment_id, exc)
        snap = await _get_or_none(payment_id)
        if snap is not None:
            await payments._record(payment_id, snap)
            if snap.get("status") in CAPTURED:
                return "captured"   # it landed; only the response was lost
        return "unknown"

    await payments._record(payment_id, snap)   # capturing -> captured (in payments._advance_order)
    return "captured"


async def _get_or_none(payment_id: str) -> dict | None:
    try:
        return await hs.retrieve_payment(payment_id)
    except (hs.HyperswitchError, hs.HyperswitchUnavailable) as exc:
        log.warning("retrieve %s failed: %s", payment_id, exc)
        return None


# ── Step 10: cancel ────────────────────────────────────────────────────────
CANCELLABLE = {
    "customer": ("pending_payment", "authorized"),             # until the shopper starts picking
    "store": ("pending_payment", "authorized", "picking"),     # any order whose capture hasn't started
}


async def cancel_order(order_id: str, by: str) -> str:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """UPDATE orders SET status = 'cancelling', cancelled_by = %s, updated_at = now()
               WHERE id = %s AND status = ANY(%s) RETURNING status""",
            (by, order_id, list(CANCELLABLE[by])),
        )
        if await cur.fetchone() is None:
            cur = await conn.execute("SELECT status FROM orders WHERE id = %s", (order_id,))
            row = await cur.fetchone()
            if row is None:
                raise FulfillmentError("Order not found.", 404)
            status = row["status"]
            if status == "cancelled":
                return "already_cancelled"
            if status != "cancelling":   # 'cancelling' = an earlier attempt didn't finish: resume it
                if status in ("finalizing", "capturing", "captured", "delivered"):
                    raise FulfillmentError("The final charge has already started, so this order can't be cancelled.")
                if status == "picking":
                    raise FulfillmentError("Your shopper has started picking, so the order can't be cancelled "
                                           "from here. Please contact support.")
                raise FulfillmentError(f"Can't cancel: order is {status.replace('_', ' ')}.")
        payment = await payments._active_payment(conn, order_id)

    if payment is None or payment["status"] in ("create_failed",):
        await _finish_local_cancel(order_id)
        return "cancelled"
    if payment["status"] == "requires_merchant_action":
        # Verified in sandbox: Hyperswitch refuses to cancel a payment the processor is
        # holding for review (IR_16). The order stays 'cancelling' (it can't be picked);
        # the reconciliation sweep re-runs this cancel once the review resolves.
        return "cancelling"
    if payment["status"] not in ("failed", "cancelled"):   # these hold nothing: no call needed
        try:
            snap = await hs.cancel_payment(payment["payment_id"], f"cancelled_by_{by}")
        except hs.HyperswitchError as exc:
            if exc.code == "HE_02":   # never created at Hyperswitch (create timed out before landing)
                await _finish_local_cancel(order_id)
                return "cancelled"
            # IR_16 = already cancelled (or otherwise not cancellable): fetch and sync.
            log.info("cancel %s: %s %s (fetching)", payment["payment_id"], exc.code, exc.message)
            snap = await _get_or_none(payment["payment_id"])
        except hs.HyperswitchUnavailable as exc:
            log.warning("cancel %s outcome unknown: %s", payment["payment_id"], exc)
            snap = await _get_or_none(payment["payment_id"])
        if snap is None:
            raise FulfillmentError("Cancellation is in progress; check again in a moment.", 503)
        await payments._record(payment["payment_id"], snap)   # cancelling -> cancelled
    else:
        async with pool.connection() as conn, conn.transaction():
            await payments._advance_order(conn, payment["payment_id"])

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT status FROM orders WHERE id = %s", (order_id,))
        status = (await cur.fetchone())["status"]
    if status == "cancelling":
        raise FulfillmentError("Cancellation is in progress; check again in a moment.", 503)
    return status


async def _finish_local_cancel(order_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE orders SET status = 'cancelled', cancelled_at = now(), updated_at = now() "
            "WHERE id = %s AND status = 'cancelling'",
            (order_id,),
        )


def _usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"
