"""POST /webhooks/hyperswitch

Hyperswitch signs the exact serialized body with HMAC-SHA512 using the profile's
payment_response_hash_key (our HYPERSWITCH_WEBHOOK_SECRET) and sends the hex digest
in X-Webhook-Signature-512. So:
  1. verify against the RAW bytes (re-serialized JSON would differ), in constant time;
  2. dedupe by event_id (webhook_events primary key);
  3. treat the payload as a hint only: ignore its status, GET the payment/refund
     from Hyperswitch and apply that (forward-only), exactly like the return page
     and the sweep do. A late, repeated or out-of-order event can't move us backward.
A processing failure returns 503 and leaves processed_at empty, so Hyperswitch's
retry (or the sweep) finishes the job.
"""
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import hyperswitch as hs
from app import payments, refunds
from app.config import settings
from app.db import pool

log = logging.getLogger("zipkart.webhooks")
router = APIRouter()
SIGNATURE_HEADER = "x-webhook-signature-512"


def sign(body: bytes) -> str:
    return hmac.new(settings.hyperswitch_webhook_secret.encode(), body, hashlib.sha512).hexdigest()


def signature_valid(body: bytes, received: str | None) -> bool:
    if not received:
        return False
    return hmac.compare_digest(sign(body), received.strip().lower())


@router.post("/webhooks/hyperswitch")
async def hyperswitch_webhook(request: Request):
    body = await request.body()   # raw bytes, before any parsing
    if not signature_valid(body, request.headers.get(SIGNATURE_HEADER)):
        log.warning("webhook rejected: bad or missing signature")
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        event = json.loads(body)
        event_id, event_type = event["event_id"], event["event_type"]
        content = event.get("content") or {}
        obj = content.get("object") or {}
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "malformed event"}, status_code=400)
    kind = content.get("type")
    object_id = obj.get("refund_id") if kind == "refund_details" else obj.get("payment_id")

    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO webhook_events (event_id, event_type, object_id, payload)
               VALUES (%s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id""",
            (event_id, event_type, object_id, body.decode("utf-8", "replace")),
        )
        if await cur.fetchone() is None:
            cur = await conn.execute("SELECT processed_at FROM webhook_events WHERE event_id = %s", (event_id,))
            if (await cur.fetchone())["processed_at"] is not None:
                return {"status": "duplicate"}
            # Seen before but not finished (earlier attempt failed): process again.

    try:
        outcome = await _process(kind, object_id)
    except (hs.HyperswitchError, hs.HyperswitchUnavailable) as exc:
        log.warning("webhook %s (%s): fetch failed, will retry: %s", event_id, event_type, exc)
        return JSONResponse({"error": "temporarily unable to process"}, status_code=503)

    async with pool.connection() as conn:
        await conn.execute("UPDATE webhook_events SET processed_at = now() WHERE event_id = %s", (event_id,))
    log.info("webhook %s %s %s -> %s", event_id, event_type, object_id, outcome)
    return {"status": outcome}


async def _process(kind: str | None, object_id: str | None) -> str:
    if not object_id:
        return "ignored"
    async with pool.connection() as conn:
        if kind == "payment_details":
            cur = await conn.execute("SELECT 1 FROM payments WHERE payment_id = %s", (object_id,))
        elif kind == "refund_details":
            cur = await conn.execute("SELECT 1 FROM refunds WHERE refund_id = %s", (object_id,))
        else:
            return "ignored"
        if await cur.fetchone() is None:
            return "unknown_object"   # not one of ours (e.g. a manual test payment)

    if kind == "payment_details":
        snap = await hs.retrieve_payment(object_id)          # the payload's status is ignored
        await payments._record(object_id, snap)               # any attempt, active or not
        return f"synced payment {snap.get('status')}"
    snap = await hs.retrieve_refund(object_id)
    await refunds.record_refund(snap)
    return f"synced refund {snap.get('status')}"
