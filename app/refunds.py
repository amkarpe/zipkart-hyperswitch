"""Refund attempts for remedies.

refund_id = rf_{order}_{item}_{reason}_r{remedy}_a{n}
  * deterministic, so re-sending after a timeout is safe: Hyperswitch answers HE_01
    and we fetch instead;
  * includes the remedy id because one line can have several remedies (two cartons
    missing, reported at different times). Without it both would share a refund_id
    and the second would be swallowed as an "HE_01 duplicate" and never paid;
  * the attempt number moves to n+1 only after the previous attempt is CONFIRMED
    failed (Hyperswitch keeps the failed refund under its id, so reusing the id would
    hit HE_01 forever).

Authorize.net only refunds settled transactions. Before the nightly settlement,
Hyperswitch returns status=failed with error_code "54" (verified in sandbox). That's
not a real failure: the remedy waits in awaiting_settlement and the sweep retries
after the next settlement cutoff.
"""
import logging

from app import hyperswitch as hs
from app.db import pool

log = logging.getLogger("zipkart.refunds")

NOT_SETTLED_CODE = "54"
CAPTURED = ("partially_captured", "succeeded")


class RefundError(Exception):
    def __init__(self, message: str, http_status: int = 409):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def refund_id_for(order_id: str, item_id: int, reason: str, remedy_id: int, attempt_no: int) -> str:
    return f"rf_{order_id}_{item_id}_{reason}_r{remedy_id}_a{attempt_no}"


async def start_refund(remedy_id: int) -> str | None:
    """Open the next refund attempt for a remedy and send it. Returns the refund_id,
    or None if there's nothing to do (already in flight, refunded, or not a refund)."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT order_id FROM remedies WHERE id = %s", (remedy_id,))
        row = await cur.fetchone()
        if row is None:
            raise RefundError("Remedy not found.", 404)
        # Lock the order: the refund cap below sums across all of its refunds.
        cur = await conn.execute(
            "SELECT id, captured_amount_cents FROM orders WHERE id = %s FOR UPDATE", (row["order_id"],)
        )
        order = await cur.fetchone()
        cur = await conn.execute("SELECT * FROM remedies WHERE id = %s", (remedy_id,))
        remedy = await cur.fetchone()
        if remedy["resolution"] != "refund" or remedy["status"] not in ("approved", "awaiting_settlement"):
            return None
        cur = await conn.execute(
            "SELECT * FROM refunds WHERE remedy_id = %s ORDER BY attempt_no DESC LIMIT 1", (remedy_id,)
        )
        previous = await cur.fetchone()
        if previous is not None and previous["status"] != "failed":
            return None   # one live attempt per remedy; only a confirmed failure opens the next
        cur = await conn.execute(
            "SELECT payment_id FROM payments WHERE order_id = %s AND is_active AND status = ANY(%s)",
            (order["id"], list(CAPTURED)),
        )
        payment = await cur.fetchone()
        if payment is None:
            raise RefundError("This order has no captured payment to refund.")
        cur = await conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM refunds "
            "WHERE payment_id = %s AND status IN ('pending', 'succeeded')",
            (payment["payment_id"],),
        )
        live_total = (await cur.fetchone())["total"]
        if live_total + remedy["amount_cents"] > (order["captured_amount_cents"] or 0):
            await conn.execute("UPDATE remedies SET status = 'failed', updated_at = now() WHERE id = %s", (remedy_id,))
            log.error("remedy %s: refund would exceed captured amount", remedy_id)
            return None

        attempt_no = 1 if previous is None else previous["attempt_no"] + 1
        refund_id = refund_id_for(order["id"], remedy["order_item_id"], remedy["reason"], remedy_id, attempt_no)
        await conn.execute(
            """INSERT INTO refunds (refund_id, remedy_id, payment_id, attempt_no, amount_cents)
               VALUES (%s, %s, %s, %s, %s)""",
            (refund_id, remedy_id, payment["payment_id"], attempt_no, remedy["amount_cents"]),
        )
        await conn.execute(
            "UPDATE remedies SET status = 'refund_pending', updated_at = now() "
            "WHERE id = %s AND status IN ('approved', 'awaiting_settlement')",
            (remedy_id,),
        )
        payload = {
            "payment_id": payment["payment_id"],
            "refund_id": refund_id,
            "amount": remedy["amount_cents"],
            "reason": f"{remedy['reason']} item ({remedy['resolution']})",
            "metadata": {"order_id": order["id"], "remedy_id": str(remedy_id), "attempt_no": str(attempt_no)},
        }

    # Committed: the intent exists even if we crash now. Call Hyperswitch outside the tx.
    await send_refund(payload)
    return refund_id


async def send_refund(payload: dict) -> None:
    """POST the refund; on any doubt, GET by our refund_id and record that instead."""
    refund_id = payload["refund_id"]
    try:
        snap = await hs.create_refund(payload)
    except hs.HyperswitchError as exc:
        if exc.code == "HE_01":   # already attempted with this id: fetch and record
            snap = await fetch_refund(refund_id)
        else:
            # Hyperswitch rejected the request itself; nothing was sent to the processor.
            log.error("refund %s rejected: %s %s", refund_id, exc.code, exc.message)
            snap = {"refund_id": refund_id, "status": "failed", "error_code": exc.code, "error_message": exc.message}
    except hs.HyperswitchUnavailable as exc:
        # Unknown outcome. The attempt stays pending; the sweep GETs it and, if
        # Hyperswitch never received it (HE_02), re-sends the same refund_id.
        log.warning("refund %s outcome unknown: %s", refund_id, exc)
        snap = await fetch_refund(refund_id)
    if snap is not None:
        await record_refund(snap)


async def fetch_refund(refund_id: str) -> dict | None:
    try:
        return await hs.retrieve_refund(refund_id)
    except (hs.HyperswitchError, hs.HyperswitchUnavailable) as exc:
        log.warning("retrieve refund %s failed: %s", refund_id, exc)
        return None


async def record_refund(snap: dict) -> str | None:
    """Apply a refund snapshot forward-only (pending -> succeeded|failed) and move its remedy."""
    raw = snap.get("status")
    status = {"succeeded": "succeeded", "failed": "failed", "pending": "pending", "review": "pending"}.get(raw)
    if status is None:
        log.warning("refund %s: unhandled status %r", snap.get("refund_id"), raw)
        return None
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """UPDATE refunds SET status = %s, hs_status_raw = %s,
                   connector_refund_id = COALESCE(%s, connector_refund_id),
                   error_code = %s, unified_code = %s, error_message = %s,
                   last_synced_at = now(), updated_at = now()
               WHERE refund_id = %s AND status = 'pending'
               RETURNING remedy_id""",
            (status, raw, snap.get("connector_refund_id"), snap.get("error_code"), snap.get("unified_code"),
             snap.get("error_message"), snap["refund_id"]),
        )
        row = await cur.fetchone()
        if row is None:   # unknown refund or already final: never move a final refund back
            await conn.execute("UPDATE refunds SET last_synced_at = now() WHERE refund_id = %s", (snap["refund_id"],))
            return None
        if status == "succeeded":
            new_remedy = "refunded"
        elif status == "failed" and snap.get("error_code") == NOT_SETTLED_CODE:
            new_remedy = "awaiting_settlement"
        elif status == "failed":
            new_remedy = "failed"
        else:
            return status
        await conn.execute(
            "UPDATE remedies SET status = %s, updated_at = now() WHERE id = %s AND status = 'refund_pending'",
            (new_remedy, row["remedy_id"]),
        )
        if new_remedy == "failed":
            log.error("refund %s failed: %s %s", snap["refund_id"], snap.get("error_code"), snap.get("error_message"))
    return status


async def repair_remedy(remedy_id: int) -> str | None:
    """A remedy left in refund_pending while its latest attempt is already final
    (e.g. the process died between recording the refund and moving the remedy)."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT status, error_code FROM refunds WHERE remedy_id = %s ORDER BY attempt_no DESC LIMIT 1",
            (remedy_id,),
        )
        last = await cur.fetchone()
        if last is None or last["status"] == "pending":
            return None
        new = ("refunded" if last["status"] == "succeeded"
               else "awaiting_settlement" if last["error_code"] == NOT_SETTLED_CODE else "failed")
        cur = await conn.execute(
            "UPDATE remedies SET status = %s, updated_at = now() WHERE id = %s AND status = 'refund_pending'",
            (new, remedy_id),
        )
        return new if cur.rowcount else None


async def check_pending(refund_id: str) -> str:
    """GET a pending attempt. If Hyperswitch never received it (HE_02), re-send the
    SAME refund_id: safe, because if it did land meanwhile we get HE_01 and fetch."""
    try:
        snap = await hs.retrieve_refund(refund_id)
    except hs.HyperswitchError as exc:
        if exc.code != "HE_02":
            raise
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT f.refund_id, f.payment_id, f.amount_cents, f.attempt_no, m.id AS remedy_id,
                          m.reason, m.resolution, m.order_id
                   FROM refunds f JOIN remedies m ON m.id = f.remedy_id WHERE f.refund_id = %s""",
                (refund_id,),
            )
            r = await cur.fetchone()
        await send_refund({
            "payment_id": r["payment_id"], "refund_id": r["refund_id"], "amount": r["amount_cents"],
            "reason": f"{r['reason']} item ({r['resolution']})",
            "metadata": {"order_id": r["order_id"], "remedy_id": str(r["remedy_id"]), "attempt_no": str(r["attempt_no"])},
        })
        return "resent"
    return await record_refund(snap) or "unchanged"
