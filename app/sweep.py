"""Reconciliation sweep: the safety net behind the return page and webhooks.

Runs every SWEEP_INTERVAL_SECONDS in the background, and when the store console
loads (Render's free tier sleeps, so the timer alone isn't enough). Each part only
ever (1) GETs from Hyperswitch and applies that forward-only, or (2) finishes an
action whose intent is already committed. Nothing is retried blindly.

  a. sync payments stuck in non-final states for longer than RECONCILE_AFTER_MINUTES,
     including checkouts that look unpaid to us (the customer may have paid and
     closed the tab before the return page, with the webhook lost)
  b. finish orders stuck in 'cancelling'
  c. void inactive payment attempts that still hold funds
  d. retry awaiting_settlement refunds for captures made before the last cut-off
  e. check pending refunds (and repair remedies whose refund is already final)

Only one sweep runs at a time: a lease row taken with compare-and-set. (A Postgres
advisory lock would need a session or a transaction held across Hyperswitch calls,
which PgBouncer transaction pooling and our own rules both rule out.)
"""
import asyncio
import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import fulfillment as ff
from app import hyperswitch as hs
from app import payments, refunds
from app.config import settings
from app.db import pool

log = logging.getLogger("zipkart.sweep")

LEASE = timedelta(minutes=3)
SETTLEMENT_MARGIN = timedelta(minutes=30)   # batch settles shortly after the cut-off
BATCH = 50
ABANDONED_BATCH = 20                        # unpaid checkouts fetched per run
SESSION_GRACE = timedelta(minutes=5)        # after expiry, one more look and we stop
_OWNER = f"{socket.gethostname()}:{os.getpid()}"


def last_settlement_cutoff(now: datetime | None = None) -> datetime:
    """The most recent cut-off whose batch has had time to settle. Captures made
    before it are refundable now."""
    tz = ZoneInfo(settings.settlement_timezone)
    hour, minute = (int(x) for x in settings.settlement_cutoff.split(":"))
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    cutoff = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < cutoff + SETTLEMENT_MARGIN:
        cutoff -= timedelta(days=1)
    return cutoff


async def _rows(sql: str, params: tuple = ()) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def _each(label: str, rows: list[dict], fn) -> list[dict]:
    """Run fn per row; one failure never stops the rest of the sweep."""
    results = []
    for row in rows:
        try:
            outcome = await fn(row)
        except Exception as exc:   # noqa: BLE001 - logged and reported, sweep continues
            log.warning("sweep %s %s failed: %r", label, row, exc)
            outcome = f"error: {exc.__class__.__name__}"
        if outcome:
            results.append({**{k: str(v) for k, v in row.items()}, "outcome": outcome})
    return results


# a ──────────────────────────────────────────────────────────────────────────
async def _sync_stale_payments() -> list[dict]:
    rows = await _rows(
        """SELECT payment_id FROM payments
           WHERE status IN ('requires_confirmation', 'requires_customer_action', 'processing',
                            'requires_merchant_action', 'requires_capture', 'partially_captured_and_capturable')
             AND COALESCE(last_synced_at, updated_at) < now() - make_interval(mins => %s)
           ORDER BY COALESCE(last_synced_at, updated_at) LIMIT %s""",
        (settings.reconcile_after_minutes, BATCH),
    )

    # An unpaid checkout is "requires_payment_method" in OUR database until we sync it,
    # even if the customer already paid inside the SDK. Look at those too, but only while
    # the session could still have been paid (until expiry + grace), capped per run, and
    # least-recently-checked first so every abandoned checkout gets its turn.
    rows += await _rows(
        """SELECT p.payment_id FROM payments p JOIN orders o ON o.id = p.order_id
           WHERE p.is_active AND o.status = 'pending_payment'
             AND p.status IN ('requires_payment_method', 'requires_confirmation')
             AND p.created_at < now() - make_interval(mins => %s)
             AND (p.last_synced_at IS NULL OR p.expires_at IS NULL OR p.last_synced_at < p.expires_at + %s)
           ORDER BY COALESCE(p.last_synced_at, p.created_at) LIMIT %s""",
        (settings.reconcile_after_minutes, SESSION_GRACE, ABANDONED_BATCH),
    )

    async def sync(row):
        before = (await _rows("SELECT status FROM payments WHERE payment_id = %s", (row["payment_id"],)))[0]["status"]
        after = await payments._record(row["payment_id"], await hs.retrieve_payment(row["payment_id"]))
        return f"{before} -> {after}" if after != before else None   # report only changes
    return await _each("sync", list({r["payment_id"]: r for r in rows}.values()), sync)


# b ──────────────────────────────────────────────────────────────────────────
async def _finish_cancelling() -> list[dict]:
    rows = await _rows("SELECT id AS order_id, cancelled_by FROM orders WHERE status = 'cancelling' LIMIT %s", (BATCH,))

    async def finish(row):
        # First look: a payment that failed (e.g. declined after a fraud review) holds
        # nothing, and recording it closes the order as cancelled (payments._advance_order).
        async with pool.connection() as conn:
            payment = await payments._active_payment(conn, row["order_id"])
        if payment is not None and payment["status"] not in ("create_failed", "failed", "cancelled"):
            await payments._record(payment["payment_id"], await hs.retrieve_payment(payment["payment_id"]))
        status = (await _rows("SELECT status FROM orders WHERE id = %s", (row["order_id"],)))[0]["status"]
        if status != "cancelling":
            return f"closed as {status}"
        # Still open: resume the cancel (voids a hold that's now cancellable; no-op while under review).
        try:
            return f"cancel -> {await ff.cancel_order(row['order_id'], row['cancelled_by'] or 'store')}"
        except ff.FulfillmentError as exc:
            return f"still cancelling: {exc.message}"
    return await _each("cancelling", rows, finish)


# c ──────────────────────────────────────────────────────────────────────────
async def _void_inactive_holds() -> list[dict]:
    rows = await _rows(
        """SELECT payment_id FROM payments
           WHERE NOT is_active AND status IN ('requires_capture', 'partially_captured_and_capturable') LIMIT %s""",
        (BATCH,),
    )

    async def void(row):
        pid = row["payment_id"]
        snap = await hs.retrieve_payment(pid)          # look first
        if snap.get("status") != "requires_capture":
            await payments._record(pid, snap)
            return f"no hold ({snap.get('status')})"
        try:
            snap = await hs.cancel_payment(pid, "stray_payment_attempt")
        except hs.HyperswitchError as exc:   # IR_16: state moved; fetch and record
            log.info("void %s: %s", pid, exc.code)
            snap = await hs.retrieve_payment(pid)
        await payments._record(pid, snap)
        return f"voided -> {snap.get('status')}"
    return await _each("void", rows, void)


# d ──────────────────────────────────────────────────────────────────────────
async def _retry_settled_refunds() -> list[dict]:
    cutoff = last_settlement_cutoff()
    rows = await _rows(
        """SELECT r.id AS remedy_id, r.status,
                  (SELECT f.refund_id FROM refunds f WHERE f.remedy_id = r.id ORDER BY f.attempt_no DESC LIMIT 1) AS last_refund
           FROM remedies r JOIN orders o ON o.id = r.order_id
           WHERE r.resolution = 'refund'
             AND ((r.status = 'awaiting_settlement' AND o.captured_at < %s)
               OR (r.status = 'approved' AND r.created_at < now() - interval '2 minutes'))  -- intent with no attempt
           LIMIT %s""",
        (cutoff, BATCH),
    )

    async def retry(row):
        if row["last_refund"]:
            # Attempt n+1 only after CONFIRMING attempt n failed at Hyperswitch.
            snap = await hs.retrieve_refund(row["last_refund"])
            await refunds.record_refund(snap)
            if snap.get("status") != "failed":
                return f"not retried: {row['last_refund']} is {snap.get('status')}"
        new_id = await refunds.start_refund(row["remedy_id"])
        status = (await _rows("SELECT status FROM remedies WHERE id = %s", (row["remedy_id"],)))[0]["status"]
        return f"{new_id} -> remedy {status}" if new_id else f"nothing to send (remedy {status})"
    return await _each("refund retry", rows, retry)


# e ──────────────────────────────────────────────────────────────────────────
async def _check_pending_refunds() -> list[dict]:
    pending = await _rows(
        "SELECT refund_id FROM refunds WHERE status = 'pending' AND updated_at < now() - interval '1 minute' LIMIT %s",
        (BATCH,),
    )
    checked = await _each("refund check", pending, lambda r: refunds.check_pending(r["refund_id"]))
    stale = await _rows(
        """SELECT r.id AS remedy_id FROM remedies r
           WHERE r.status = 'refund_pending'
             AND NOT EXISTS (SELECT 1 FROM refunds f WHERE f.remedy_id = r.id AND f.status = 'pending') LIMIT %s""",
        (BATCH,),
    )
    repaired = await _each("remedy repair", stale, lambda r: refunds.repair_remedy(r["remedy_id"]))
    return checked + repaired


# ── Runner ─────────────────────────────────────────────────────────────────
async def run_sweep(trigger: str) -> dict | None:
    """Returns the result, or None if another sweep holds the lease."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """UPDATE sweep_lease SET locked_until = now() + %s, owner = %s
               WHERE id = 1 AND locked_until < now() RETURNING id""",
            (LEASE, f"{_OWNER}:{trigger}"),
        )
        if await cur.fetchone() is None:
            return None
    started = datetime.now(timezone.utc)
    result: dict = {"trigger": trigger, "started_at": started.isoformat()}
    try:
        result["a_payments_synced"] = await _sync_stale_payments()
        result["b_cancelling_finished"] = await _finish_cancelling()
        result["c_stray_holds_voided"] = await _void_inactive_holds()
        result["d_refunds_retried"] = await _retry_settled_refunds()
        result["e_refunds_checked"] = await _check_pending_refunds()
    finally:
        result["seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
        async with pool.connection() as conn:
            await conn.execute(
                """UPDATE sweep_lease SET locked_until = '-infinity', last_run_at = now(), last_result = %s
                   WHERE id = 1 AND owner = %s""",
                (json.dumps(result), f"{_OWNER}:{trigger}"),
            )
    changes = sum(len(v) for k, v in result.items() if k[:2] in ("a_", "b_", "c_", "d_", "e_"))
    log.info("sweep (%s) done in %ss, %s change(s)", trigger, result["seconds"], changes)
    return result


async def last_run() -> dict:
    rows = await _rows("SELECT last_run_at, last_result, locked_until > now() AS running FROM sweep_lease WHERE id = 1")
    r = rows[0]
    return {"last_run_at": r["last_run_at"].isoformat() if r["last_run_at"] else None,
            "running": r["running"], "last_result": r["last_result"]}


async def sweep_if_stale(trigger: str, older_than: timedelta = timedelta(seconds=60)) -> None:
    rows = await _rows("SELECT last_run_at FROM sweep_lease WHERE id = 1")
    last = rows[0]["last_run_at"]
    if last is None or datetime.now(timezone.utc) - last > older_than:
        await run_sweep(trigger)


async def sweep_forever() -> None:
    await asyncio.sleep(15)   # let startup finish (and Neon wake) first
    while True:
        try:
            await run_sweep("timer")
        except Exception:   # noqa: BLE001 - the loop must survive anything
            log.exception("sweep failed")
        await asyncio.sleep(settings.sweep_interval_seconds)
