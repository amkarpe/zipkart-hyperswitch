"""Store console: login + the shopper's API. Mounted under /store.

Auth: one shared password (STORE_CONSOLE_PASSWORD). Logging in sets an HttpOnly,
SameSite=Strict cookie holding "<expiry>.<HMAC-SHA256(expiry)>". The HMAC key is
derived from the password and the webhook secret, so changing the password logs
everyone out, and the cookie can't be forged or extended without the server's key.
SameSite=Strict also keeps other sites from making the shopper's browser submit actions.
"""
import asyncio
import hashlib
import hmac
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import fulfillment as ff
from app import sweep
from app.config import settings
from app.db import pool
from app.refunds import RefundError, attempts_used, mark_refunded_manually
from app.settlement import store_time
from app.views import _item_view, _seconds_left, hold_breakdown, load_order, load_remedies

COOKIE = "zk_store"
SESSION_SECONDS = 12 * 3600
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_KEY = hashlib.sha256(
    b"zipkart-store-session\0" + settings.store_console_password.encode() + b"\0"
    + settings.hyperswitch_webhook_secret.encode()
).digest()

router = APIRouter()


def _sign(expires: int) -> str:
    return hmac.new(_KEY, str(expires).encode(), hashlib.sha256).hexdigest()


def _valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires, sig = token.split(".", 1)
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(sig, _sign(int(expires)))


def require_store(request: Request) -> None:
    if not _valid(request.cookies.get(COOKIE)):
        raise HTTPException(status_code=401, detail="Store login required.")


# ── Pages and login ────────────────────────────────────────────────────────
@router.get("/store", include_in_schema=False)
async def store_page():
    return FileResponse(STATIC_DIR / "store.html")


class Login(BaseModel):
    password: str = Field(max_length=200)


@router.post("/store/api/login")
async def login(body: Login, response: Response):
    if not hmac.compare_digest(body.password.encode(), settings.store_console_password.encode()):
        await asyncio.sleep(0.5)  # slow down guessing a little
        raise HTTPException(status_code=401, detail="Wrong password.")
    expires = int(time.time()) + SESSION_SECONDS
    response.set_cookie(COOKIE, f"{expires}.{_sign(expires)}", max_age=SESSION_SECONDS, httponly=True,
                        samesite="strict", secure=settings.app_env == "production", path="/")
    return {"ok": True}


@router.post("/store/api/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/store/api/me", dependencies=[Depends(require_store)])
async def me():
    return {"ok": True}


# ── Queue ──────────────────────────────────────────────────────────────────
_background: set[asyncio.Task] = set()


@router.get("/store/api/orders", dependencies=[Depends(require_store)])
async def queue():
    # The console loading is a good moment to reconcile (the timer doesn't run while
    # Render's free instance sleeps). Fire and forget; at most once a minute.
    if settings.sweep_timer_enabled:   # automatic, so same gate as the timer
        task = asyncio.create_task(sweep.sweep_if_stale("store_console"))
        _background.add(task)
        task.add_done_callback(_background.discard)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT o.id, o.status, o.customer_name, o.hold_amount_cents, o.estimated_total_cents,
                      o.captured_amount_cents, o.created_at, o.updated_at,
                      (SELECT count(*) FROM order_items i WHERE i.order_id = o.id AND i.substitute_for_item_id IS NULL) AS lines
               FROM orders o
               JOIN payments p ON p.order_id = o.id AND p.is_active
               WHERE (o.status = 'authorized' AND p.status = 'requires_capture')   -- hold confirmed by the server
                  OR o.status IN ('picking', 'finalizing', 'capturing', 'cancelling', 'needs_review')
                  OR (o.status IN ('captured', 'cancelled') AND o.updated_at > now() - interval '1 day')
               ORDER BY CASE o.status WHEN 'authorized' THEN 1 WHEN 'picking' THEN 0 ELSE 2 END, o.created_at
               LIMIT 100"""
        )
        rows = await cur.fetchall()
    return [{**r, "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat()} for r in rows]


# ── One order ──────────────────────────────────────────────────────────────
async def store_view(order_id: str) -> dict:
    await ff.apply_expired_substitutions(order_id)
    async with pool.connection() as conn:
        order, items, payment = await load_order(conn, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found.")
        remedies = await load_remedies(conn, order_id)
        used = await attempts_used(conn, order_id)
        cur = await conn.execute(
            """SELECT c.remedy_id, c.old_next_attempt_at, c.new_next_attempt_at, c.reason, c.changed_at
               FROM remedy_schedule_changes c JOIN remedies m ON m.id = c.remedy_id
               WHERE m.order_id = %s ORDER BY c.changed_at""",
            (order_id,),
        )
        changes = await cur.fetchall()
    committed = ff.committed_totals(order, items)
    capturable = payment["amount_capturable_cents"] if payment and payment["status"] == "requires_capture" else None
    lines = []
    for i in items:
        v = _item_view(i)
        v["taxable"] = i["taxable"]
        v["hold_line_cents"] = i["hold_line_cents"]
        v["seconds_left"] = _seconds_left(i["substitution_expires_at"]) if i["status"] == "proposed" else None
        # Allowed pick range for an ordered weighed line (substitutes are weighed up front).
        v["max_weight_lb"] = (f"{ff.max_pick_weight_lb(i['estimated_weight_lb']):.2f}"
                              if i["pricing_type"] == "weighed" and i["substitute_for_item_id"] is None else None)
        lines.append(v)
    pending = [i for i in items if i["status"] == "pending"]
    proposed = [i for i in items if i["status"] == "proposed"]
    final = ff.final_totals(order, items) if not pending and not proposed else None
    return {
        "order_id": order["id"],
        "status": order["status"],
        "customer_name": order["customer_name"],
        "address": f"{order['ship_line1']}, {order['ship_city']}, {order['ship_state']} {order['ship_zip']}",
        "substitution_preference": order["substitution_preference"],
        "review_reason": order["review_reason"],
        "hold_breakdown": hold_breakdown(order, items),
        "payment_status": payment["status"] if payment else None,
        "capturable_cents": capturable,
        "committed_total_cents": committed.total_cents,
        "headroom_cents": None if capturable is None else capturable - committed.total_cents,
        "final_preview": None if final is None else final.__dict__,
        "final_amount_cents": order["final_amount_cents"],
        "captured_amount_cents": order["captured_amount_cents"],
        "unpicked": len(pending),
        "awaiting_customer": len(proposed),
        "can_finalize": order["status"] == "picking" and not pending and not proposed,
        "can_retry_capture": order["status"] == "capturing",
        "can_cancel": order["status"] in ff.CANCELLABLE["store"] or order["status"] == "cancelling",
        "substitution_timeout_seconds": settings.substitution_timeout_seconds,
        "weight_tolerance_percent": ff.WEIGHT_TOLERANCE_PERCENT,
        "items": lines,
        "refund_attempts_used": used,
        "refund_attempts_cap": settings.refund_max_auto_attempts_per_payment,
        "store_timezone": settings.store_timezone,
        "remedies": [{
            "id": r["id"], "item_name": r["item_name"], "quantity": r["quantity"], "reason": r["reason"],
            "resolution": r["resolution"], "amount_cents": r["amount_cents"], "status": r["status"],
            "note": r["note"],
            "next_attempt": store_time(r["next_attempt_at"]) if r["next_attempt_at"] else None,
            "manual_reason": r["manual_reason"],
            "last_error": (f"{r['last_error_code']}: {r['last_error_message']}" if r["last_error_code"] else None),
            "manual_refund_txn_id": r["manual_refund_txn_id"],
            "manual_refund_note": r["manual_refund_note"],
            "manual_refunded_at": store_time(r["manual_refunded_at"]) if r["manual_refunded_at"] else None,
            "schedule_changes": [{
                "changed_at": store_time(c["changed_at"]),
                "old": store_time(c["old_next_attempt_at"]),
                "new": store_time(c["new_next_attempt_at"]),
                "reason": c["reason"],
            } for c in changes if c["remedy_id"] == r["id"]],
        } for r in remedies],
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/store/api/orders/{order_id}", dependencies=[Depends(require_store)])
async def get_order(order_id: str):
    return await store_view(order_id)


@router.post("/store/api/orders/{order_id}/start", dependencies=[Depends(require_store)])
async def start(order_id: str):
    await ff.start_picking(order_id)
    return await store_view(order_id)


class Pick(BaseModel):
    picked_quantity: int | None = None
    actual_weight_lb: str | None = Field(default=None, max_length=10)


@router.post("/store/api/orders/{order_id}/items/{item_id}/pick", dependencies=[Depends(require_store)])
async def pick(order_id: str, item_id: int, body: Pick):
    await ff.pick_item(order_id, item_id, body.picked_quantity, body.actual_weight_lb)
    return await store_view(order_id)


@router.post("/store/api/orders/{order_id}/items/{item_id}/out-of-stock", dependencies=[Depends(require_store)])
async def out_of_stock(order_id: str, item_id: int):
    await ff.mark_out_of_stock(order_id, item_id)
    return await store_view(order_id)


@router.get("/store/api/orders/{order_id}/items/{item_id}/substitutes", dependencies=[Depends(require_store)])
async def substitutes(order_id: str, item_id: int):
    return await ff.substitute_candidates(order_id, item_id)


class Propose(BaseModel):
    sku: str = Field(max_length=40)
    quantity: int | None = None
    weight_lb: str | None = Field(default=None, max_length=10)


@router.post("/store/api/orders/{order_id}/items/{item_id}/substitute", dependencies=[Depends(require_store)])
async def propose(order_id: str, item_id: int, body: Propose):
    await ff.propose_substitute(order_id, item_id, body.sku, body.quantity, body.weight_lb)
    return await store_view(order_id)


@router.post("/store/api/orders/{order_id}/capture", dependencies=[Depends(require_store)])
async def capture(order_id: str):
    outcome = await ff.finalize_and_capture(order_id)
    return {"outcome": outcome, "order": await store_view(order_id)}


@router.post("/store/api/orders/{order_id}/cancel", dependencies=[Depends(require_store)])
async def cancel(order_id: str):
    outcome = await ff.cancel_order(order_id, "store")
    return {"outcome": outcome, "order": await store_view(order_id)}


@router.get("/store/api/sweep", dependencies=[Depends(require_store)])
async def sweep_status():
    return await sweep.last_run()


@router.post("/store/api/sweep", dependencies=[Depends(require_store)])
async def sweep_now():
    result = await sweep.run_sweep("store_manual")
    if result is None:
        raise HTTPException(status_code=409, detail="A sweep is already running; try again in a moment.")
    return await sweep.last_run()


class ManualRefund(BaseModel):
    processor_transaction_id: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=500)


@router.post("/store/api/orders/{order_id}/remedies/{remedy_id}/mark-refunded", dependencies=[Depends(require_store)])
async def mark_refunded(order_id: str, remedy_id: int, body: ManualRefund):
    try:
        done = await mark_refunded_manually(order_id, remedy_id, body.processor_transaction_id, body.note)
    except RefundError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message)
    if not done:
        raise HTTPException(status_code=409, detail="That refund isn't waiting for a manual refund (already resolved?).")
    return await store_view(order_id)
