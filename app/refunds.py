"""Refund attempts for remedies.

State machine (refund remedies):

    (report) ──▶ awaiting_settlement   next_attempt_at = gate(captured_at). Nothing is sent.
    awaiting_settlement ──(sweep: due, payment attempts < cap)──▶ refund_pending   attempt row a{n} inserted, then POST
    awaiting_settlement ──(sweep: due, payment attempts ≥ cap)──▶ needs_manual_refund
    refund_pending ──succeeded──▶ refunded
    refund_pending ──pending/review──▶ refund_pending                       (sweep GETs it; a GET is not an attempt)
    refund_pending ──failed, error 54──▶ awaiting_settlement               next_attempt_at = gate(now): next window
    refund_pending ──failed, any other code──▶ needs_manual_refund
    refund_pending ──request rejected (IR_12, any other 4xx)──▶ needs_manual_refund
    refund_pending ──timeout/5xx──▶ refund_pending                          (sweep GETs; HE_02 = never received:
                                                                            re-send the SAME refund_id, same attempt)
    refunded, needs_manual_refund: final for automation.
    needs_manual_refund ──(store: "Mark refunded manually", txn id + note)──▶ refunded_manually   final
    awaiting_settlement ──(sweep: config changed, no attempts on the payment)──▶ awaiting_settlement
                          next_attempt_at = gate(captured_at) with current config; audit row written

refund_id = rf_{order}_{item}_{reason}_r{remedy}_a{n}
  * deterministic, so re-sending after a timeout is safe (HE_01 -> fetch instead);
  * includes the remedy id: one line can have several remedies with the same reason;
  * attempt n+1 only after attempt n is confirmed failed (Hyperswitch keeps failed
    refunds under their id).

Budget: Hyperswitch rejects a refund with IR_12 once the payment already has more than
[refund] max_attempts = 10 refunds on record, failed ones included (verified in its
source). We count every attempt we've made on the payment, across all its remedies,
under the order-row lock, and stop automatically at REFUND_MAX_AUTO_ATTEMPTS_PER_PAYMENT.

Error 54 = "does not meet the criteria for issuing a credit": usually "not settled
yet", but Authorize.net also uses it for invalid type, wrong currency, bad reference
or >180 days. So it's treated as "next window", and the per-payment cap is the backstop.
"""
import logging
from datetime import datetime, timezone

from app import hyperswitch as hs
from app.config import settings
from app.db import pool
from app import settlement
from app.settlement import refund_gate

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


def remedy_outcome(refund_status: str, error_code: str | None) -> str | None:
    """Where a remedy goes when its latest attempt reaches refund_status. None = stays put."""
    if refund_status == "succeeded":
        return "refunded"
    if refund_status == "failed":
        return "awaiting_settlement" if error_code == NOT_SETTLED_CODE else "needs_manual_refund"
    return None   # pending / review: still in flight


async def _move_remedy(conn, remedy_id: int, new: str, code: str | None, message: str | None) -> bool:
    """refund_pending -> new, with the scheduling / manual fields that state needs (CAS)."""
    now = datetime.now(timezone.utc)
    cur = await conn.execute(
        """UPDATE remedies SET status = %s,
               next_attempt_at = %s,
               manual_reason = %s,
               last_error_code = COALESCE(%s, last_error_code),
               last_error_message = COALESCE(%s, last_error_message),
               updated_at = now()
           WHERE id = %s AND status = 'refund_pending'""",
        (new,
         refund_gate(now) if new == "awaiting_settlement" else None,
         f"refund failed: {code or 'unknown error'}" if new == "needs_manual_refund" else None,
         code, message, remedy_id),
    )
    if cur.rowcount and new == "needs_manual_refund":
        log.error("remedy %s needs a manual refund: %s %s", remedy_id, code, message)
    return bool(cur.rowcount)


async def start_refund(remedy_id: int, now: datetime | None = None) -> str | None:
    """Send the next attempt for a DUE remedy. Returns the refund_id, or None when nothing
    was sent (not due, not waiting, an attempt in flight, or the budget is used up)."""
    now = now or datetime.now(timezone.utc)
    cap = settings.refund_max_auto_attempts_per_payment
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT order_id FROM remedies WHERE id = %s", (remedy_id,))
        row = await cur.fetchone()
        if row is None:
            raise RefundError("Remedy not found.", 404)
        # One lock per order (= per captured payment): every check below and the attempt
        # insert happen under it, so two overlapping sweeps can't both take the last attempt.
        cur = await conn.execute(
            "SELECT id, captured_amount_cents FROM orders WHERE id = %s FOR UPDATE", (row["order_id"],)
        )
        order = await cur.fetchone()
        cur = await conn.execute("SELECT * FROM remedies WHERE id = %s", (remedy_id,))
        remedy = await cur.fetchone()
        if (remedy["resolution"] != "refund" or remedy["status"] != "awaiting_settlement"
                or remedy["next_attempt_at"] is None or remedy["next_attempt_at"] > now):
            return None   # not scheduled, or not due yet: never sent before its window
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
            """SELECT count(*) AS attempts,
                      COALESCE(SUM(amount_cents) FILTER (WHERE status IN ('pending', 'succeeded')), 0) AS live_total
               FROM refunds WHERE payment_id = %s""",
            (payment["payment_id"],),
        )
        used = await cur.fetchone()
        reason = None
        if used["attempts"] >= cap:
            reason = f"automatic refund attempt limit ({cap}) reached for this payment"
        elif used["live_total"] + remedy["amount_cents"] > (order["captured_amount_cents"] or 0):
            reason = "refund would exceed the captured amount"
        if reason:
            await conn.execute(
                "UPDATE remedies SET status = 'needs_manual_refund', manual_reason = %s, next_attempt_at = NULL, "
                "updated_at = now() WHERE id = %s AND status = 'awaiting_settlement'",
                (reason, remedy_id),
            )
            log.error("remedy %s needs a manual refund: %s", remedy_id, reason)
            return None

        attempt_no = 1 if previous is None else previous["attempt_no"] + 1
        refund_id = refund_id_for(order["id"], remedy["order_item_id"], remedy["reason"], remedy_id, attempt_no)
        await conn.execute(
            """INSERT INTO refunds (refund_id, remedy_id, payment_id, attempt_no, amount_cents)
               VALUES (%s, %s, %s, %s, %s)""",
            (refund_id, remedy_id, payment["payment_id"], attempt_no, remedy["amount_cents"]),
        )
        await conn.execute(
            "UPDATE remedies SET status = 'refund_pending', next_attempt_at = NULL, updated_at = now() "
            "WHERE id = %s AND status = 'awaiting_settlement'",
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
            # Hyperswitch rejected the request itself (e.g. IR_12): record it as failed
            # with its code, which sends the remedy to needs_manual_refund.
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
    """Apply a refund snapshot forward-only (pending -> succeeded|failed) and move its remedy.
    Used for send results, webhooks and GET polls alike; never creates an attempt."""
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
        new = remedy_outcome(status, snap.get("error_code"))
        if new is not None:
            await _move_remedy(conn, row["remedy_id"], new, snap.get("error_code"), snap.get("error_message"))
    return status


async def repair_remedy(remedy_id: int) -> str | None:
    """A remedy left in refund_pending while its latest attempt is already final
    (e.g. the process died between recording the refund and moving the remedy)."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT status, error_code, error_message FROM refunds WHERE remedy_id = %s "
            "ORDER BY attempt_no DESC LIMIT 1",
            (remedy_id,),
        )
        last = await cur.fetchone()
        new = None if last is None else remedy_outcome(last["status"], last["error_code"])
        if new is None:
            return None
        moved = await _move_remedy(conn, remedy_id, new, last["error_code"], last["error_message"])
        return new if moved else None


async def check_pending(refund_id: str) -> str:
    """GET a pending attempt (a GET is never a new attempt). If Hyperswitch never received
    it (HE_02), re-send the SAME refund_id: same attempt row, and if it did land meanwhile
    we get HE_01 and fetch."""
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


async def attempts_used(conn, order_id: str) -> int:
    cur = await conn.execute(
        "SELECT count(*) AS n FROM refunds f JOIN remedies m ON m.id = f.remedy_id WHERE m.order_id = %s",
        (order_id,),
    )
    return (await cur.fetchone())["n"]


async def mark_refunded_manually(order_id: str, remedy_id: int, txn_id: str, note: str) -> bool:
    """needs_manual_refund -> refunded_manually (CAS). False if the remedy isn't waiting
    for a manual refund (already resolved, or not on this order)."""
    txn_id, note = txn_id.strip(), note.strip()
    if not txn_id or not note:
        raise RefundError("Processor transaction ID and a note are both required.", 400)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """UPDATE remedies SET status = 'refunded_manually', manual_refund_txn_id = %s,
                   manual_refund_note = %s, manual_refunded_at = now(), updated_at = now()
               WHERE id = %s AND order_id = %s AND status = 'needs_manual_refund'""",
            (txn_id, note, remedy_id, order_id),
        )
        return cur.rowcount == 1


def reschedule_reason() -> str:
    c = settlement.settings   # read at call time: the config in force now
    return (f"settlement config changed: cutoff {c.settlement_cutoff} {c.settlement_timezone}, "
            f"margin {c.settlement_margin_hours:g}h")


async def reschedule_if_config_changed(remedy_id: int) -> dict | None:
    """Recompute the FIRST attempt time from captured_at with the current settlement config.
    Only for awaiting_settlement remedies on payments with zero refund attempts: once any
    attempt exists, stored times are retry times (set from error 54) and are left alone.
    Runs under the order-row lock, the same lock start_refund takes, so it can't interleave
    with a send on that payment. Compare-and-set on the stored time; the audit row is written
    in the same transaction, so there's exactly one per actual change."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT order_id FROM remedies WHERE id = %s", (remedy_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        cur = await conn.execute(
            "SELECT id, captured_at FROM orders WHERE id = %s FOR UPDATE", (row["order_id"],)
        )
        order = await cur.fetchone()
        cur = await conn.execute(
            """SELECT m.next_attempt_at, m.status, m.resolution,
                      (SELECT count(*) FROM refunds f JOIN payments p ON p.payment_id = f.payment_id
                       WHERE p.order_id = m.order_id) AS payment_attempts
               FROM remedies m WHERE m.id = %s""",
            (remedy_id,),
        )
        m = await cur.fetchone()
        if (m["resolution"] != "refund" or m["status"] != "awaiting_settlement" or m["payment_attempts"] > 0
                or order["captured_at"] is None or m["next_attempt_at"] is None):
            return None
        new = refund_gate(order["captured_at"])
        old = m["next_attempt_at"]
        if new == old:
            return None
        cur = await conn.execute(
            """UPDATE remedies SET next_attempt_at = %s, updated_at = now()
               WHERE id = %s AND status = 'awaiting_settlement' AND next_attempt_at = %s""",
            (new, remedy_id, old),
        )
        if cur.rowcount != 1:
            return None
        reason = reschedule_reason()
        await conn.execute(
            """INSERT INTO remedy_schedule_changes (remedy_id, old_next_attempt_at, new_next_attempt_at, reason)
               VALUES (%s, %s, %s, %s)""",
            (remedy_id, old, new, reason),
        )
    log.warning("remedy %s rescheduled %s -> %s (%s)", remedy_id, old.isoformat(), new.isoformat(), reason)
    return {"old": old.isoformat(), "new": new.isoformat(), "reason": reason}
