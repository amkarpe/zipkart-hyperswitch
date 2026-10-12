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

import gzip

from app.webhooks import Malformed, diagnose_signature, parse_event, sign, signature_valid

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


def test_signature_diagnosis_explains_mismatches_without_the_secret():
    assert diagnose_signature(REAL, None) == "no X-Webhook-Signature-512 header"
    d = diagnose_signature(REAL, "ab" * 64)
    assert "received len=128" in d and "hex=True" in d and "matches: none of" in d
    # Body compressed in transit, signature computed over the uncompressed JSON:
    zipped = gzip.compress(REAL)
    assert "body is gzip" in diagnose_signature(zipped, sign(REAL))
    assert "matches: sha512-hex over gunzipped body" in diagnose_signature(zipped, sign(REAL))
    assert sign(REAL) not in diagnose_signature(REAL, "00")   # never echoes the expected value


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
# HTTP tests use the real event shape with a SYNTHETIC payment id that only exists for the
# duration of the tests: the dev database must never hold real payment state.
TEST_PAYMENT = "pay_ZKTWEBHOOK01_1"
SYNTHETIC = REAL.replace(b"pay_ZKVGFJ63WS26_1", TEST_PAYMENT.encode())


@pytest.fixture(scope="module")
def client(app_client):
    import psycopg

    from app import hyperswitch as hs
    from app.config import settings

    async def fake_retrieve_payment(payment_id):   # Hyperswitch GET -> the event's own object
        obj = json.loads(SYNTHETIC)["content"]["object"]
        assert payment_id == obj["payment_id"]
        return obj

    oid = TEST_PAYMENT[4:-2]
    with psycopg.connect(settings.database_url, prepare_threshold=None) as conn:
        conn.execute("""INSERT INTO orders (id, status, currency, customer_name, customer_email, customer_phone,
                            ship_line1, ship_city, ship_state, ship_zip, subtotal_cents, tax_cents, delivery_fee_cents,
                            estimated_total_cents, hold_amount_cents, final_amount_cents, captured_amount_cents,
                            weight_buffer_percent)
                        VALUES (%s, 'captured', 'USD', 'Test', 't@example.com', '5555550100', '1 Test St', 'Austin',
                                'TX', '78701', 1000, 0, 399, 1399, 1500, 1399, 1399, 15)""", (oid,))
        conn.execute("""INSERT INTO payments (payment_id, order_id, attempt_no, amount_cents, status)
                        VALUES (%s, %s, 1, 1500, 'partially_captured')""", (TEST_PAYMENT, oid))
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(hs, "retrieve_payment", fake_retrieve_payment)
            yield app_client
    finally:
        with psycopg.connect(settings.database_url, prepare_threshold=None) as conn:
            conn.execute("DELETE FROM payments WHERE order_id = %s", (oid,))
            conn.execute("DELETE FROM orders WHERE id = %s", (oid,))


@pytest.fixture
def event_ids():
    ids = []
    yield ids
    import psycopg

    from app.config import settings
    with psycopg.connect(settings.database_url, prepare_threshold=None) as conn:
        conn.execute("DELETE FROM webhook_events WHERE event_id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM webhook_failures WHERE event_id = ANY(%s) OR raw_body LIKE '%%evt_test_%%' "
                     "OR (stage = 'signature' AND raw_body LIKE '%%evt_fixture_payment_authorized%%')", (ids,))


def post(client, body: bytes, signature: str | None = "valid"):
    headers = {"Content-Type": "application/json"}
    if signature == "valid":
        headers["X-Webhook-Signature-512"] = sign(body)
    elif signature:
        headers["X-Webhook-Signature-512"] = signature
    return client.post("/webhooks/hyperswitch", content=body, headers=headers)


def test_real_payload_is_processed_then_deduped(client, event_ids):
    event_ids.append(eid := f"evt_test_{uuid.uuid4().hex}")
    body = with_event_id(SYNTHETIC, eid)
    first = post(client, body)
    assert first.status_code == 200, first.text
    assert first.json()["status"].startswith("synced payment")
    second = post(client, body)
    assert second.status_code == 200 and second.json() == {"status": "duplicate"}


def test_nul_escape_in_payload_is_stored_not_500(client, event_ids):
    # Old handler: JSONB insert raised UntranslatableCharacter -> 500.
    event_ids.append(eid := f"evt_test_{uuid.uuid4().hex}")
    body = with_event_id(SYNTHETIC, eid).replace(b'"description":"', b'"description":"\\u0000', 1)
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


def test_unsigned_is_401_and_recorded_with_diagnosis(client, event_ids):
    assert post(client, REAL, signature=None).status_code == 401
    assert post(client, REAL, signature="00" * 64).status_code == 401

    import psycopg

    from app.config import settings
    with psycopg.connect(settings.database_url, prepare_threshold=None) as conn:
        errors = [r[0] for r in conn.execute(
            "SELECT error FROM webhook_failures WHERE stage = 'signature' AND raw_body LIKE %s "
            "AND received_at > now() - interval '1 minute' ORDER BY id", ("%evt_fixture_payment_authorized%",))]
    assert any("no X-Webhook-Signature-512 header" in e for e in errors)
    assert any("received len=128" in e and "matches: none of" in e for e in errors)


# ── A real delivery captured from Hyperswitch (webhook.site, 2026-10-11) ─────
# refund_failed from Authorize.net, error 54 (capture not settled yet). Byte-for-byte
# as sent; the signature is the one Hyperswitch actually sent with it.
REFUND_54 = (Path(__file__).parent / "fixtures" / "hyperswitch_refund_failed_54.json").read_bytes()
REFUND_54_SIGNATURE = ("d3c112df7387e3ee92762d6b6146219e33111f2bc2850a49e27eb6626ac908d3"
                       "abcf1ee444bfb91e1510a82eb310f33d567f756bfa64d78f38e80f81c4b0c22e")
REAL_HEADERS = {"accept-encoding": "gzip", "accept": "*/*", "user-agent": "Hyperswitch-Backend-Server",
                "via": "HyperSwitch", "content-type": "application/json"}


def test_real_hyperswitch_signature_verifies():
    # Requires HYPERSWITCH_WEBHOOK_SECRET to be the profile's payment response hash key.
    assert len(REFUND_54) == 996
    assert signature_valid(REFUND_54, REFUND_54_SIGNATURE)


def test_parses_real_refund_failed_54():
    assert parse_event(REFUND_54) == {"event_id": "evt_01a12d22c2287d72a9a3927486d1e264",
                                      "event_type": "refund_failed", "kind": "refund_details",
                                      "object_id": "rf_ZKQ2GA3EEHYJ_48_damaged_r12_a8"}
    obj = json.loads(REFUND_54)["content"]["object"]
    assert obj["error_code"] == "54" and obj["unified_code"] == "UE_4000" and obj["connector"] == "authorizedotnet"


def test_real_delivery_with_real_headers_through_handler(client, monkeypatch, event_ids):
    from app import hyperswitch as hs

    async def fake_retrieve_refund(refund_id):
        return json.loads(REFUND_54)["content"]["object"]
    monkeypatch.setattr(hs, "retrieve_refund", fake_retrieve_refund)
    r = client.post("/webhooks/hyperswitch", content=REFUND_54,
                    headers={**REAL_HEADERS, "x-webhook-signature-512": REFUND_54_SIGNATURE})
    # The real headers and signature are accepted. The refund belongs to production, not to
    # this (dev) database, so the deliberate answer is "unknown_object", never an error.
    event_ids.append("evt_01a12d22c2287d72a9a3927486d1e264")
    assert r.status_code == 200 and r.json()["status"] == "unknown_object"
