"""Webhook handler tests built on a real-format Hyperswitch outgoing webhook.

Fixture: tests/fixtures/hyperswitch_payment_event.json is a payment_authorized event in
the exact shape Hyperswitch sends (serde-serialized OutgoingWebhook: merchant_id,
event_id, event_type, content{type, object = the full PaymentsResponse}, timestamp),
built from the real sandbox payment pay_ZKVGFJ63WS26_1 with its client_secret and
customer details replaced by placeholders.

Run:  .venv/bin/python -m pytest -q
The HTTP tests need DATABASE_URL (they write uniquely-named rows and delete them) and
patch the Hyperswitch GET, so they make no network calls to Hyperswitch.
"""
import json
import uuid
from pathlib import Path

import pytest

from app.webhooks import Malformed, parse_event, sign, signature_valid

FIXTURE = Path(__file__).parent / "fixtures" / "hyperswitch_payment_event.json"
REAL = FIXTURE.read_bytes()


def with_event_id(body: bytes, event_id: str) -> bytes:
    return body.replace(b'"evt_fixture_payment_authorized"', json.dumps(event_id).encode())


# ── Signature ───────────────────────────────────────────────────────────────
def test_signature_is_over_raw_bytes():
    assert signature_valid(REAL, sign(REAL))
    assert signature_valid(REAL, sign(REAL).upper())                 # hex case doesn't matter
    reserialized = json.dumps(json.loads(REAL)).encode()             # same JSON, different bytes
    assert not signature_valid(reserialized, sign(REAL))


def test_bad_signatures_are_rejected_not_crashed():
    assert not signature_valid(REAL, None)
    assert not signature_valid(REAL, "")
    assert not signature_valid(REAL, "ab" * 64)
    assert not signature_valid(REAL, "é" * 10)                       # used to raise TypeError -> 500
    assert not signature_valid(REAL.replace(b"payment_authorized", b"payment_failed"), sign(REAL))


# ── Parsing the real format ─────────────────────────────────────────────────
def test_parses_real_payload():
    ev = parse_event(REAL)
    assert ev == {"event_id": "evt_fixture_payment_authorized", "event_type": "payment_authorized",
                  "kind": "payment_details", "object_id": "pay_ZKVGFJ63WS26_1"}


def test_refund_event_uses_refund_id():
    body = json.dumps({"merchant_id": "m", "event_id": "evt_r", "event_type": "refund_failed",
                       "content": {"type": "refund_details", "object": {"refund_id": "rf_x", "payment_id": "pay_x"}}})
    assert parse_event(body.encode())["object_id"] == "rf_x"


def test_unsupported_types_parse_without_object_id():
    body = json.dumps({"event_id": "evt_d", "event_type": "dispute_opened",
                       "content": {"type": "dispute_details", "object": {"dispute_id": "d"}}})
    assert parse_event(body.encode())["object_id"] is None


@pytest.mark.parametrize("body, reason", [
    (b"hello", "not JSON"),
    (b"[]", "JSON list"),
    (b'{"event_type":"x","content":{}}', "event_id"),
    (b'{"event_id":"e","event_type":"x","content":"oops"}', "content is str"),     # old: AttributeError -> 500
    (b'{"event_id":"e","event_type":"x","content":{"type":"payment_details","object":"oops"}}', "object is str"),
    (b'{"event_id":"e","event_type":"x","content":{"type":"payment_details","object":{}}}', "payment_id"),
])
def test_malformed_payloads_raise_malformed(body, reason):
    with pytest.raises(Malformed, match=reason):
        parse_event(body)


# ── Through the HTTP handler (needs the database) ───────────────────────────
@pytest.fixture(scope="module")
def client():
    # Module-scoped: the app's connection pool can be opened once per process.
    from fastapi.testclient import TestClient

    from app import hyperswitch as hs
    from app import sweep
    from app.main import app

    async def no_sweep():   # keep the background sweep out of tests
        return None

    async def fake_retrieve_payment(payment_id):   # Hyperswitch GET -> the fixture's own object
        obj = json.loads(REAL)["content"]["object"]
        assert payment_id == obj["payment_id"]
        return obj

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sweep, "sweep_forever", no_sweep)
        mp.setattr(hs, "retrieve_payment", fake_retrieve_payment)
        with TestClient(app) as c:
            yield c


@pytest.fixture
def event_ids():
    ids = []
    yield ids
    import psycopg

    from app.config import settings
    with psycopg.connect(settings.database_url, prepare_threshold=None) as conn:
        conn.execute("DELETE FROM webhook_events WHERE event_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM webhook_failures WHERE event_id = ANY(%s) OR raw_body LIKE '%%evt_test_%%'", (ids,))


def post(client, body: bytes, signature: str | None = "valid"):
    headers = {"Content-Type": "application/json"}
    if signature == "valid":
        headers["X-Webhook-Signature-512"] = sign(body)
    elif signature:
        headers["X-Webhook-Signature-512"] = signature
    return client.post("/webhooks/hyperswitch", content=body, headers=headers)


def test_real_payload_is_processed_then_deduped(client, event_ids):
    event_ids.append(eid := f"evt_test_{uuid.uuid4().hex}")
    body = with_event_id(REAL, eid)
    first = post(client, body)
    assert first.status_code == 200, first.text
    assert first.json()["status"].startswith("synced payment")
    second = post(client, body)
    assert second.status_code == 200 and second.json() == {"status": "duplicate"}


def test_nul_escape_in_payload_is_stored_not_500(client, event_ids):
    # Old handler: JSONB insert raised UntranslatableCharacter -> 500.
    event_ids.append(eid := f"evt_test_{uuid.uuid4().hex}")
    body = with_event_id(REAL, eid).replace(b'"description":"', b'"description":"\\u0000', 1)
    assert b"\\u0000" in body
    r = post(client, body)
    assert r.status_code == 200, r.text


def test_signed_but_malformed_is_400_and_recorded(client, event_ids):
    eid = f"evt_test_{uuid.uuid4().hex}"
    body = json.dumps({"event_id": eid, "event_type": "x", "content": "oops"}).encode()
    r = post(client, body)
    assert r.status_code == 400 and "content is str" in r.json()["error"]

    import psycopg

    from app.config import settings
    with psycopg.connect(settings.database_url, prepare_threshold=None) as conn:
        row = conn.execute("SELECT stage, status_code, headers FROM webhook_failures WHERE raw_body LIKE %s",
                           (f"%{eid}%",)).fetchone()
    assert row[0] == "parse" and row[1] == 400
    assert sign(body) not in row[2]          # signature value is redacted in the stored headers


def test_unsigned_is_401(client):
    assert post(client, REAL, signature=None).status_code == 401
    assert post(client, REAL, signature="00" * 64).status_code == 401
