"""Post-delivery remedies (missing / damaged / expired → refund or replacement).

Recording a remedy is a pure database step; moving money (the refund call) comes
after, outside this transaction. The insert is where the limits are enforced:

  * fixed lines:   SUM(remedied quantity) <= picked (delivered) quantity
  * weighed lines: one remedy per line (also a partial unique index)
  * whole order:   SUM(refund amounts) <= captured amount

All three are checks across rows, so they run under SELECT ... FOR UPDATE on the
order row. Two concurrent remedy requests for the same order queue behind that
lock, and the second one sees the first one's row when it re-checks.
"""
from app.db import pool
from app.pricing import tax_for

REMEDIABLE_ORDER_STATUSES = ("captured", "delivered")


class RemedyError(Exception):
    def __init__(self, message: str, http_status: int = 400):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def _refund_value(item: dict, quantity: int | None) -> int:
    """Refund for the remedied part of a line, including its share of tax.
    Delivery is never part of it: the delivery happened."""
    if item["pricing_type"] == "fixed":
        value = item["unit_price_cents"] * quantity
    else:
        value = item["final_line_cents"]
    return value + (tax_for(value) if item["taxable"] else 0)


async def insert_remedy(conn, order_id: str, order_item_id: int, reason: str,
                        resolution: str, quantity: int | None, note: str | None = None) -> dict:
    """Validate and insert inside the caller's transaction (the caller commits)."""
    cur = await conn.execute(
        "SELECT status, captured_amount_cents FROM orders WHERE id = %s FOR UPDATE", (order_id,)
    )
    order = await cur.fetchone()
    if order is None:
        raise RemedyError("Order not found.", 404)
    if order["status"] not in REMEDIABLE_ORDER_STATUSES:
        raise RemedyError("Problems can be reported after the order is charged and delivered.", 409)

    cur = await conn.execute(
        "SELECT * FROM order_items WHERE id = %s AND order_id = %s", (order_item_id, order_id)
    )
    item = await cur.fetchone()
    if item is None:
        raise RemedyError("Item not found on this order.", 404)
    if item["status"] != "picked":   # only delivered lines (originals or accepted substitutes)
        raise RemedyError(f"{item['name']} wasn't delivered, so it wasn't charged.", 409)
    if reason == "missing" and resolution != "refund":
        raise RemedyError("Missing items are refunded.", 400)

    cur = await conn.execute(
        "SELECT count(*) AS n, COALESCE(SUM(quantity), 0) AS qty FROM remedies WHERE order_item_id = %s",
        (order_item_id,),
    )
    existing = await cur.fetchone()

    if item["pricing_type"] == "fixed":
        if not isinstance(quantity, int) or quantity < 1:
            raise RemedyError("Choose how many units had a problem.")
        delivered = item["picked_quantity"]
        if delivered is None:
            raise RemedyError("This item has no delivered quantity recorded.", 409)
        remaining = delivered - existing["qty"]
        if quantity > remaining:
            raise RemedyError(
                f"Only {remaining} of {delivered} delivered units can still be reported for {item['name']}.", 409
            )
    else:
        if quantity is not None:
            raise RemedyError("Items sold by weight are reported as a whole line, without a quantity.")
        if item["final_line_cents"] is None:
            raise RemedyError("This item has no final weight recorded.", 409)
        if existing["n"] > 0:
            raise RemedyError(f"A problem with {item['name']} was already reported.", 409)

    amount = _refund_value(item, quantity) if resolution == "refund" else 0
    if resolution == "refund":
        cur = await conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM remedies "
            "WHERE order_id = %s AND resolution = 'refund' AND status <> 'failed'",
            (order_id,),
        )
        refunded = (await cur.fetchone())["total"]
        if refunded + amount > (order["captured_amount_cents"] or 0):
            raise RemedyError("Refunds can't exceed what was charged for this order. Contact support.", 409)

    cur = await conn.execute(
        """INSERT INTO remedies (order_id, order_item_id, pricing_type, quantity, reason, resolution,
               amount_cents, status, note)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING *""",
        (order_id, order_item_id, item["pricing_type"], quantity, reason, resolution, amount,
         "approved" if resolution == "refund" else "replacement_scheduled", note or None),
    )
    return await cur.fetchone()


async def create_remedy(order_id: str, order_item_id: int, reason: str,
                        resolution: str, quantity: int | None, note: str | None = None) -> dict:
    async with pool.connection() as conn, conn.transaction():
        return await insert_remedy(conn, order_id, order_item_id, reason, resolution, quantity, note)
