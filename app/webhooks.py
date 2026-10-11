"""POST /webhooks/hyperswitch

Hyperswitch signs the exact serialized body with HMAC-SHA512 using the profile's
payment_response_hash_key (our HYPERSWITCH_WEBHOOK_SECRET) and sends the hex digest
in X-Webhook-Signature-512. Body shape (outgoing webhook):
    {"merchant_id", "event_id", "event_type",
     "content": {"type": "payment_details" | "refund_details" | ..., "object": {<full resource>}},
     "timestamp"}

Rules:
  1. verify the signature against the RAW bytes, in constant time;
  2. store the raw body as TEXT and dedupe by event_id;
  3. treat the payload as a hint: ignore its status, GET the payment/refund from
     Hyperswitch and apply that forward-only (same path as the return page and sweep);
  4. never answer 500. Every outcome is a deliberate status:
       401 bad signature                 (recorded, capped, with a diagnosis; Hyperswitch retries)
       400 signed but unparseable         (recorded in webhook_failures; retrying won't help)
       200 processed / duplicate / not ours / unsupported type
       503 couldn't finish right now      (DB or Hyperswitch hiccup; Hyperswitch retries,
                                           the sweep is the backstop)
"""
import base64
import gzip
import hashlib
import hmac
import json
import logging
import traceback

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import hyperswitch as hs
from app import payments, refunds
from app.config import settings
from app.db import pool

log = logging.getLogger("zipkart.webhooks")
router = APIRouter()
SIGNATURE_HEADER = "x-webhook-signature-512"
# Signature failures are recorded too (they'd otherwise leave no trace), but capped and
# truncated so unauthenticated traffic can't fill the database.
MAX_SIGNATURE_FAILURES_PER_HOUR = 50
MAX_STORED_BODY = 64 * 1024
OBJECT_ID_FIELD = {"payment_details": "payment_id", "refund_details": "refund_id"}


def sign(body: bytes) -> str:
    return hmac.new(settings.hyperswitch_webhook_secret.encode(), body, hashlib.sha512).hexdigest()


def signature_valid(body: bytes, received: str | None) -> bool:
    if not received:
        return False
    # Compare bytes, not str: hmac.compare_digest raises TypeError on non-ASCII str,
    # and a malformed header must be a 401, not a crash.
    return hmac.compare_digest(sign(body).encode(), received.strip().lower().encode("utf-8", "replace"))


class Malformed(ValueError):
    pass


def parse_event(body: bytes) -> dict:
    """Validate just the fields we use. Raises Malformed with a precise reason."""
    try:
        event = json.loads(body)
    except ValueError as exc:
        raise Malformed(f"body is not JSON: {exc}") from exc
    if not isinstance(event, dict):
        raise Malformed(f"body is a JSON {type(event).__name__}, expected an object")
    event_id, event_type = event.get("event_id"), event.get("event_type")
    if not isinstance(event_id, str) or not event_id:
        raise Malformed("missing event_id")
    if not isinstance(event_type, str):
        raise Malformed("missing event_type")
    content = event.get("content")
    if not isinstance(content, dict):
        raise Malformed(f"content is {type(content).__name__}, expected an object")
    kind, obj = content.get("type"), content.get("object")
    object_id = None
    if kind in OBJECT_ID_FIELD:
        if not isinstance(obj, dict):
            raise Malformed(f"content.object is {type(obj).__name__}, expected an object")
        object_id = obj.get(OBJECT_ID_FIELD[kind])
        if not isinstance(object_id, str) or not object_id:
            raise Malformed(f"content.object has no {OBJECT_ID_FIELD[kind]}")
    return {"event_id": event_id, "event_type": event_type, "kind": kind, "object_id": object_id}


def diagnose_signature(body: bytes, received: str | None) -> str:
    """Why a signature didn't verify, without revealing the secret or the expected value."""
    if not received:
        return "no X-Webhook-Signature-512 header"
    r = received.strip()
    secret = settings.hyperswitch_webhook_secret.encode()
    facts = [f"received len={len(r)}", f"hex={all(c in '0123456789abcdefABCDEF' for c in r)}"]
    variants = {"sha512-hex": hmac.new(secret, body, hashlib.sha512).hexdigest(),
                "sha256-hex": hmac.new(secret, body, hashlib.sha256).hexdigest(),
                "sha512-base64": base64.b64encode(hmac.new(secret, body, hashlib.sha512).digest()).decode()}
    if body[:2] == b"\x1f\x8b":
        facts.append("body is gzip")
        try:
            variants["sha512-hex over gunzipped body"] = hmac.new(secret, gzip.decompress(body), hashlib.sha512).hexdigest()
        except OSError:
            facts.append("gunzip failed")
    stripped = body.strip()
    if stripped != body:
        variants["sha512-hex over stripped body"] = hmac.new(secret, stripped, hashlib.sha512).hexdigest()
    matched = [name for name, v in variants.items() if hmac.compare_digest(v.lower().encode(), r.lower().encode("utf-8", "replace"))]
    facts.append(f"matches: {', '.join(matched) if matched else 'none of ' + ', '.join(variants)}")
    return "; ".join(facts)


def _text(body: bytes) -> str:
    # Postgres TEXT can't hold NUL bytes; everything else is kept verbatim.
    return body.decode("utf-8", "replace").replace("\x00", "\\u0000")


def _headers_for_log(request: Request) -> str:
    return json.dumps({k: ("<redacted>" if k.lower() == SIGNATURE_HEADER else v) for k, v in request.headers.items()})


async def _record_failure(request: Request, body: bytes, stage: str, status: int, error: str,
                          event_id: str | None = None) -> None:
    log.error("webhook %s failure (%s -> %s): %s", event_id or "?", stage, status, error)
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """INSERT INTO webhook_failures (event_id, stage, status_code, error, headers, raw_body)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (event_id, stage, status, error[:4000], _headers_for_log(request), _text(body)),
            )
    except Exception:   # noqa: BLE001 - recording the failure must not become the failure
        log.exception("could not record webhook failure")


async def _record_signature_failure(request: Request, body: bytes, diagnosis: str) -> None:
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM webhook_failures WHERE stage = 'signature' "
                "AND received_at > now() - interval '1 hour'")
            if (await cur.fetchone())["n"] >= MAX_SIGNATURE_FAILURES_PER_HOUR:
                return
            await conn.execute(
                """INSERT INTO webhook_failures (stage, status_code, error, headers, raw_body)
                   VALUES ('signature', 401, %s, %s, %s)""",
                (f"bad signature ({len(body)} bytes): {diagnosis}", _headers_for_log(request),
                 _text(body[:MAX_STORED_BODY])),
            )
    except Exception:   # noqa: BLE001
        log.exception("could not record signature failure")


@router.post("/webhooks/hyperswitch")
async def hyperswitch_webhook(request: Request):
    body = await request.body()   # raw bytes, before any parsing
    received = request.headers.get(SIGNATURE_HEADER)
    if not signature_valid(body, received):
        diagnosis = diagnose_signature(body, received)
        log.warning("webhook rejected: bad or missing signature (%d bytes): %s", len(body), diagnosis)
        await _record_signature_failure(request, body, diagnosis)
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        ev = parse_event(body)
    except Malformed as exc:
        await _record_failure(request, body, "parse", 400, str(exc))
        return JSONResponse({"error": f"malformed event: {exc}"}, status_code=400)

    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO webhook_events (event_id, event_type, object_id, raw_body)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id""",
                (ev["event_id"], ev["event_type"], ev["object_id"], _text(body)),
            )
            if await cur.fetchone() is None:
                cur = await conn.execute("SELECT processed_at FROM webhook_events WHERE event_id = %s",
                                         (ev["event_id"],))
                if (await cur.fetchone())["processed_at"] is not None:
                    return {"status": "duplicate"}
                # Seen before but not finished (earlier attempt failed): process again.
    except Exception as exc:   # noqa: BLE001
        await _record_failure(request, body, "store", 503, f"{exc!r}\n{traceback.format_exc()}", ev["event_id"])
        return JSONResponse({"error": "temporarily unable to store event"}, status_code=503)

    try:
        outcome = await _process(ev["kind"], ev["object_id"])
        async with pool.connection() as conn:
            await conn.execute("UPDATE webhook_events SET processed_at = now() WHERE event_id = %s", (ev["event_id"],))
    except Exception as exc:   # noqa: BLE001 - Hyperswitch hiccup, DB hiccup or a bug: never a 500
        await _record_failure(request, body, "process", 503, f"{exc!r}\n{traceback.format_exc()}", ev["event_id"])
        return JSONResponse({"error": "temporarily unable to process"}, status_code=503)

    log.info("webhook %s %s %s -> %s", ev["event_id"], ev["event_type"], ev["object_id"], outcome)
    return {"status": outcome}


async def _process(kind: str | None, object_id: str | None) -> str:
    if kind not in OBJECT_ID_FIELD:
        return f"ignored ({kind})"   # disputes, mandates, payouts: not used by ZipKart
    async with pool.connection() as conn:
        table, column = ("payments", "payment_id") if kind == "payment_details" else ("refunds", "refund_id")
        cur = await conn.execute(f"SELECT 1 FROM {table} WHERE {column} = %s", (object_id,))
        if await cur.fetchone() is None:
            return "unknown_object"   # not one of ours (e.g. a manual test payment)

    if kind == "payment_details":
        snap = await hs.retrieve_payment(object_id)          # the payload's status is ignored
        await payments._record(object_id, snap)               # any attempt, active or not
        return f"synced payment {snap.get('status')}"
    snap = await hs.retrieve_refund(object_id)
    await refunds.record_refund(snap)
    return f"synced refund {snap.get('status')}"
