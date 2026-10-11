"""Read models for the browser. Every number shown to a customer comes from here,
computed from stored order data, never from client-side arithmetic. No secrets."""
from datetime import datetime, timezone

from app.db import pool
from app.remedies import REMEDIABLE_ORDER_STATUSES, _refund_value


def _lb(value) -> str | None:
    return None if value is None else f"{value:.2f}"


def hold_breakdown(order: dict, items: list[dict]) -> dict:
    """estimated total + weight buffer == temporary hold, to the cent."""
    weighed = [i["name"] for i in items if i["pricing_type"] == "weighed" and i["substitute_for_item_id"] is None]
    return {
        "estimated_total_cents": order["estimated_total_cents"],
        "weight_buffer_cents": order["hold_amount_cents"] - order["estimated_total_cents"],
        "weight_buffer_percent": order["weight_buffer_percent"],
        "weighed_items": weighed,
        "hold_amount_cents": order["hold_amount_cents"],
    }


def quote_breakdown(quote, buffer_percent: int) -> dict:
    """Same shape as hold_breakdown, for the cart before an order exists."""
    return {
        "estimated_total_cents": quote.estimated_total_cents,
        "weight_buffer_cents": quote.weight_buffer_cents,
        "weight_buffer_percent": buffer_percent,
        "weighed_items": [l.name for l in quote.lines if l.pricing_type == "weighed"],
        "hold_amount_cents": quote.hold_amount_cents,
    }


async def load_order(conn, order_id: str) -> tuple[dict | None, list[dict], dict | None]:
    cur = await conn.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
    order = await cur.fetchone()
    if order is None:
        return None, [], None
    cur = await conn.execute("SELECT * FROM order_items WHERE order_id = %s ORDER BY id", (order_id,))
    items = await cur.fetchall()
    cur = await conn.execute("SELECT * FROM payments WHERE order_id = %s AND is_active", (order_id,))
    return order, items, await cur.fetchone()


def _item_view(i: dict) -> dict:
    return {
        "id": i["id"],
        "sku": i["sku"],
        "name": i["name"],
        "pricing_type": i["pricing_type"],
        "unit_price_cents": i["unit_price_cents"],
        "quantity": i["quantity"],
        "picked_quantity": i["picked_quantity"],
        "estimated_weight_lb": _lb(i["estimated_weight_lb"]),
        "actual_weight_lb": _lb(i["actual_weight_lb"]),
        "estimated_line_cents": i["estimated_line_cents"],
        "final_line_cents": i["final_line_cents"],
        "status": i["status"],
        "substitute_for_item_id": i["substitute_for_item_id"],
    }


def _seconds_left(expires_at: datetime | None) -> int:
    if expires_at is None:
        return 0
    return max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))


def substitution_prompts(items: list[dict]) -> list[dict]:
    by_id = {i["id"]: i for i in items}
    prompts = []
    for s in items:
        if s["status"] != "proposed":
            continue
        original = by_id[s["substitute_for_item_id"]]
        desc = (f"{s['name']} × {s['quantity']}" if s["pricing_type"] == "fixed"
                else f"{s['name']} ({_lb(s['actual_weight_lb'])} lb)")
        prompts.append({
            "substitute_item_id": s["id"],
            "original_name": original["name"],
            "substitute_description": desc,
            "substitute_line_cents": s["final_line_cents"],
            # Server-computed so the countdown doesn't depend on the customer's clock.
            "seconds_left": _seconds_left(s["substitution_expires_at"]),
        })
    return prompts


def receipt(order: dict, items: list[dict]) -> dict | None:
    """Only once the charge is final: what was held, charged, released, and why."""
    if order["captured_amount_cents"] is None:
        return None
    by_original = {i["substitute_for_item_id"]: i for i in items if i["substitute_for_item_id"] is not None}
    changes = []
    for i in items:
        if i["substitute_for_item_id"] is not None:
            continue
        sub = by_original.get(i["id"])
        if i["status"] == "substituted":
            sd = (f"{sub['name']} × {sub['picked_quantity']}" if sub["pricing_type"] == "fixed"
                  else f"{sub['name']} ({_lb(sub['actual_weight_lb'])} lb)")
            changes.append({"name": i["name"], "kind": "substituted",
                            "detail": f"Replaced with {sd}",
                            "estimated_line_cents": i["estimated_line_cents"],
                            "final_line_cents": sub["final_line_cents"]})
        elif i["status"] == "removed":
            if sub is None or sub["status"] != "declined":
                reason = "Out of stock, removed"
            elif sub["updated_at"] >= sub["substitution_expires_at"]:
                reason = f"Out of stock; no answer on {sub['name']} in time, so removed per your preference"
            else:
                reason = f"Out of stock; you declined {sub['name']}"
            changes.append({"name": i["name"], "kind": "removed", "detail": reason,
                            "estimated_line_cents": i["estimated_line_cents"], "final_line_cents": 0})
        elif i["pricing_type"] == "weighed" and i["actual_weight_lb"] != i["estimated_weight_lb"]:
            changes.append({"name": i["name"], "kind": "weight",
                            "detail": f"Weighed {_lb(i['actual_weight_lb'])} lb (estimated {_lb(i['estimated_weight_lb'])} lb)",
                            "estimated_line_cents": i["estimated_line_cents"],
                            "final_line_cents": i["final_line_cents"]})
        elif i["pricing_type"] == "fixed" and i["picked_quantity"] is not None and i["picked_quantity"] < i["quantity"]:
            changes.append({"name": i["name"], "kind": "quantity",
                            "detail": f"{i['picked_quantity']} of {i['quantity']} available",
                            "estimated_line_cents": i["estimated_line_cents"],
                            "final_line_cents": i["final_line_cents"]})
    return {
        "hold_amount_cents": order["hold_amount_cents"],
        "subtotal_cents": order["final_subtotal_cents"],
        "tax_cents": order["final_tax_cents"],
        "delivery_fee_cents": order["delivery_fee_cents"],
        "charged_cents": order["captured_amount_cents"],
        "released_cents": order["hold_amount_cents"] - order["captured_amount_cents"],
        "changes": changes,
    }


async def load_remedies(conn, order_id: str) -> list[dict]:
    cur = await conn.execute(
        """SELECT r.*, i.name AS item_name,
                  (SELECT f.status FROM refunds f WHERE f.remedy_id = r.id ORDER BY f.attempt_no DESC LIMIT 1)
                      AS last_refund_status
           FROM remedies r JOIN order_items i ON i.id = r.order_item_id
           WHERE r.order_id = %s ORDER BY r.id""",
        (order_id,),
    )
    return await cur.fetchall()


def _usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"


REMEDY_MESSAGES = {
    "approved": "Refund of {amt} approved. It's being processed.",
    "refund_pending": "Refund of {amt} is processing.",
    "awaiting_settlement": "Refund of {amt} approved. It will post once your payment settles.",
    "refunded": "Refunded {amt}. It can take a few days to appear, depending on your bank.",
    "replacement_scheduled": "A free replacement will be added to your next order.",
    "failed": "We couldn't process this refund automatically. Our team will follow up.",
}


def remedy_view(r: dict) -> dict:
    return {
        "id": r["id"],
        "item_id": r["order_item_id"],
        "item_name": r["item_name"],
        "quantity": r["quantity"],
        "reason": r["reason"],
        "resolution": r["resolution"],
        "amount_cents": r["amount_cents"],
        "status": r["status"],
        "note": r["note"],
        "message": REMEDY_MESSAGES[r["status"]].format(amt=_usd(r["amount_cents"])),
    }


def _report_options(order: dict, item: dict, remedies: list[dict]) -> dict | None:
    """What the customer can still report on a delivered line (None = nothing)."""
    if order["status"] not in REMEDIABLE_ORDER_STATUSES or item["status"] != "picked":
        return None
    mine = [r for r in remedies if r["order_item_id"] == item["id"]]
    if item["pricing_type"] == "fixed":
        remaining = (item["picked_quantity"] or 0) - sum(r["quantity"] or 0 for r in mine)
        if remaining < 1:
            return None
        return {"max_quantity": remaining, "refund_per_unit_cents": _refund_value(item, 1)}
    if mine:
        return None
    return {"max_quantity": None, "refund_cents": _refund_value(item, None)}


def customer_view(order: dict, items: list[dict], payment: dict | None, remedies: list[dict] | None = None) -> dict:
    remedies = remedies or []
    return {
        "order_id": order["id"],
        "status": order["status"],
        "currency": order["currency"],
        "customer_name": order["customer_name"],
        "substitution_preference": order["substitution_preference"],
        "subtotal_cents": order["subtotal_cents"],
        "tax_cents": order["tax_cents"],
        "delivery_fee_cents": order["delivery_fee_cents"],
        "estimated_total_cents": order["estimated_total_cents"],
        "hold_amount_cents": order["hold_amount_cents"],
        "hold_breakdown": hold_breakdown(order, items),
        "items": [{**_item_view(i), "report": _report_options(order, i, remedies)} for i in items],
        "remedies": [remedy_view(r) for r in remedies],
        "substitution_prompts": substitution_prompts(items) if order["status"] == "picking" else [],
        "can_cancel": order["status"] in ("pending_payment", "authorized"),
        "cancelled_by": order["cancelled_by"],
        "receipt": receipt(order, items),
        "payment": None if payment is None else {
            "attempt_no": payment["attempt_no"],
            "status": payment["status"],
            "unified_code": payment["unified_code"],
            "message": payment["unified_message"],
        },
    }


async def order_view(order_id: str) -> dict | None:
    async with pool.connection() as conn:
        order, items, payment = await load_order(conn, order_id)
        remedies = await load_remedies(conn, order_id) if order is not None else []
    return None if order is None else customer_view(order, items, payment, remedies)
