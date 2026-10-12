"""Refund scheduling, budget and state machine.

Pure tests cover the settlement gate (incl. the Nov 1 2026 PDT->PST change) and the
outcome mapping. DB tests build throwaway orders (ids ZKT...), replace Hyperswitch's
refund calls with fakes (no network), and delete everything they created.
"""
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import hyperswitch as hs
from app.refunds import remedy_outcome
from app.settlement import first_cutoff_after, refund_gate

UTC = timezone.utc
LA = ZoneInfo("America/Los_Angeles")


# ── Settlement gate (pure) ───────────────────────────────────────────────────
@pytest.mark.parametrize("captured_utc, cutoff_utc", [
    # before the 16:00 PDT cut-off -> same day's cut-off (23:00Z)
    (datetime(2026, 10, 11, 21, 34, tzinfo=UTC), datetime(2026, 10, 11, 23, 0, tzinfo=UTC)),
    # after it -> next day's
    (datetime(2026, 10, 11, 3, 54, tzinfo=UTC), datetime(2026, 10, 11, 23, 0, tzinfo=UTC)),
    # exactly at the cut-off -> next day's (strictly after)
    (datetime(2026, 10, 11, 23, 0, tzinfo=UTC), datetime(2026, 10, 12, 23, 0, tzinfo=UTC)),
    # Oct 31 17:00 PDT -> Nov 1 16:00 PST, which is 00:00Z on Nov 2 (not 23:00Z)
    (datetime(2026, 11, 1, 0, 0, tzinfo=UTC), datetime(2026, 11, 2, 0, 0, tzinfo=UTC)),
    # Oct 31 15:00 PDT -> Oct 31 16:00 PDT = 23:00Z
    (datetime(2026, 10, 31, 22, 0, tzinfo=UTC), datetime(2026, 10, 31, 23, 0, tzinfo=UTC)),
    # Nov 2 10:00 PST -> Nov 2 16:00 PST = 00:00Z Nov 3
    (datetime(2026, 11, 2, 18, 0, tzinfo=UTC), datetime(2026, 11, 3, 0, 0, tzinfo=UTC)),
])
def test_cutoff_is_16_00_los_angeles_across_dst(captured_utc, cutoff_utc):
    got = first_cutoff_after(captured_utc, cutoff="16:00", tz_name="America/Los_Angeles")
    assert got == cutoff_utc
    assert got.astimezone(LA).strftime("%H:%M") == "16:00"


def test_margin_is_elapsed_time_in_utc():
    t = datetime(2026, 10, 31, 23, 30, tzinfo=UTC)          # after Oct 31's cut-off
    gate = refund_gate(t, margin_hours=6, cutoff="16:00", tz_name="America/Los_Angeles")
    assert gate == datetime(2026, 11, 2, 6, 0, tzinfo=UTC)    # Nov 1 16:00 PST + 6h
    assert gate.astimezone(LA).strftime("%b %d %H:%M %Z") == "Nov 01 22:00 PST"


@pytest.mark.parametrize("status, code, expected", [
    ("succeeded", None, "refunded"),
    ("failed", "54", "awaiting_settlement"),
    ("failed", "IR_12", "needs_manual_refund"),
    ("failed", "E00027", "needs_manual_refund"),
    ("failed", None, "needs_manual_refund"),
    ("pending", None, None),
])
def test_outcome_mapping(status, code, expected):
    assert remedy_outcome(status, code) == expected


# ── DB-backed: throwaway orders, fake Hyperswitch ───────────────────────────
class FakeHyperswitch:
    """Records calls; answers create_refund with a scripted response."""

    def __init__(self):
        self.creates, self.gets = [], []
        self.respond = lambda payload: {"refund_id": payload["refund_id"], "status": "succeeded"}
        self.get_status = "pending"

    async def create_refund(self, payload):
        self.creates.append(payload["refund_id"])
        r = self.respond(payload)
        if isinstance(r, Exception):
            raise r
        return r

    async def retrieve_refund(self, refund_id):
        self.gets.append(refund_id)
        return {"refund_id": refund_id, "status": self.get_status}


@pytest.fixture
def fake_hs(monkeypatch):
    fake = FakeHyperswitch()
    monkeypatch.setattr(hs, "create_refund", fake.create_refund)
    monkeypatch.setattr(hs, "retrieve_refund", fake.retrieve_refund)
    return fake


@pytest.fixture
def db(app_client):
    """Run SQL / app coroutines inside the app's event loop; clean up test orders."""
    from app.db import pool
    made = []

    class DB:
        def call(self, fn, *args):
            return app_client.portal.call(fn, *args)

        def sql(self, query, *params):
            async def run():
                async with pool.connection() as conn:
                    cur = await conn.execute(query, params)
                    return await cur.fetchall() if cur.description else None
            return self.call(run)

        def order(self, captured_at: datetime, qty: int = 3) -> tuple[str, int]:
            """A captured order with one picked fixed-price line of `qty` units (milk)."""
            oid = "ZKT" + secrets.token_hex(4).upper()
            made.append(oid)
            self.sql("""INSERT INTO orders (id, status, currency, customer_name, customer_email, customer_phone,
                            ship_line1, ship_city, ship_state, ship_zip, subtotal_cents, tax_cents, delivery_fee_cents,
                            estimated_total_cents, hold_amount_cents, final_amount_cents, captured_amount_cents,
                            captured_at, weight_buffer_percent)
                        VALUES (%s,'captured','USD','Test','t@example.com','5555550100','1 Test St','Austin','TX',
                                '78701', %s, 0, 399, %s, %s, %s, %s, %s, 15)""",
                     oid, 429 * qty, 429 * qty + 399, 429 * qty + 399, 429 * qty + 399, 429 * qty + 399, captured_at)
            self.sql("""INSERT INTO payments (payment_id, order_id, attempt_no, amount_cents, status)
                        VALUES (%s, %s, 1, %s, 'partially_captured')""", f"pay_{oid}_1", oid, 429 * qty + 399)
            item = self.sql("""INSERT INTO order_items (order_id, sku, name, pricing_type, unit_price_cents, taxable,
                                   quantity, picked_quantity, estimated_line_cents, hold_line_cents, final_line_cents, status)
                               VALUES (%s, 'DAI-MILK-GAL', 'Whole Milk, 1 gal', 'fixed', 429, false, %s, %s, %s, %s, %s,
                                       'picked') RETURNING id""",
                            oid, qty, qty, 429 * qty, 429 * qty, 429 * qty)[0]["id"]
            return oid, item

        def remedy(self, oid, item, qty=1) -> int:
            from app.remedies import create_remedy
            return self.call(create_remedy, oid, item, "missing", "refund", qty, "test")["id"]

        def remedy_row(self, rid):
            return self.sql("SELECT * FROM remedies WHERE id = %s", rid)[0]

        def attempts(self, oid):
            return self.sql("SELECT count(*) AS n FROM refunds WHERE payment_id = %s", f"pay_{oid}_1")[0]["n"]

    yield DB()
    for oid in made:
        for q in ("DELETE FROM refunds WHERE payment_id = %s",):
            DB().sql(q, f"pay_{oid}_1")
        DB().sql("DELETE FROM remedies WHERE order_id = %s", oid)
        DB().sql("DELETE FROM order_items WHERE order_id = %s", oid)
        DB().sql("DELETE FROM payments WHERE order_id = %s", oid)
        DB().sql("DELETE FROM orders WHERE id = %s", oid)


def now():
    return datetime.now(UTC)


def test_refund_before_settlement_is_never_sent(db, fake_hs):
    from app import refunds
    oid, item = db.order(captured_at=now())
    rid = db.remedy(oid, item)
    r = db.remedy_row(rid)
    assert r["status"] == "awaiting_settlement" and r["next_attempt_at"] > now()
    assert db.call(refunds.start_refund, rid) is None          # not due
    assert fake_hs.creates == [] and db.attempts(oid) == 0


def test_capture_already_past_the_gate_is_sent_on_next_sweep(db, fake_hs):
    from app import sweep
    oid, item = db.order(captured_at=now() - timedelta(days=3))
    rid = db.remedy(oid, item)
    assert db.remedy_row(rid)["next_attempt_at"] <= now()      # already due at report time
    assert fake_hs.creates == []                                 # reporting doesn't send
    db.call(sweep._send_due_refunds)
    assert fake_hs.creates == [f"rf_{oid}_{item}_missing_r{rid}_a1"]
    assert db.remedy_row(rid)["status"] == "refunded"


def test_error_54_schedules_the_next_window(db, fake_hs):
    from app import refunds
    from app.settlement import refund_gate
    fake_hs.respond = lambda p: {"refund_id": p["refund_id"], "status": "failed", "error_code": "54",
                                 "error_message": "The referenced transaction does not meet the criteria"}
    oid, item = db.order(captured_at=now() - timedelta(days=3))
    rid = db.remedy(oid, item)
    before = now()
    assert db.call(refunds.start_refund, rid)
    r = db.remedy_row(rid)
    assert r["status"] == "awaiting_settlement"
    assert r["next_attempt_at"] == refund_gate(before)           # next settlement window, not "now"
    assert r["next_attempt_at"] > now()
    assert db.call(refunds.start_refund, rid) is None           # not retried until then
    assert len(fake_hs.creates) == 1 and db.attempts(oid) == 1


def test_cap_holds_across_two_remedies_on_one_payment(db, fake_hs):
    from app import refunds
    from app.config import settings
    cap = settings.refund_max_auto_attempts_per_payment
    oid, item = db.order(captured_at=now() - timedelta(days=3))
    a, b = db.remedy(oid, item), db.remedy(oid, item)
    for n in range(cap - 1):   # earlier failed attempts on the same payment (remedy a)
        db.sql("""INSERT INTO refunds (refund_id, remedy_id, payment_id, attempt_no, amount_cents, status, error_code)
                  VALUES (%s, %s, %s, %s, 429, 'failed', '54')""",
               f"rf_{oid}_{item}_missing_r{a}_old{n}", a, f"pay_{oid}_1", 100 + n)
    fake_hs.respond = lambda p: {"refund_id": p["refund_id"], "status": "pending"}

    async def both():   # overlapping: both remedies try to take the last attempt at once
        import asyncio
        return await asyncio.gather(refunds.start_refund(a), refunds.start_refund(b))
    sent = [x for x in db.call(both) if x]
    assert len(sent) == 1 and len(fake_hs.creates) == 1
    assert db.attempts(oid) == cap
    statuses = {db.remedy_row(a)["status"], db.remedy_row(b)["status"]}
    assert statuses == {"refund_pending", "needs_manual_refund"}
    manual = next(r for r in (db.remedy_row(a), db.remedy_row(b)) if r["status"] == "needs_manual_refund")
    assert f"limit ({cap})" in manual["manual_reason"]


def test_ir_12_goes_to_needs_manual_refund(db, fake_hs):
    from app import refunds
    fake_hs.respond = lambda p: hs.HyperswitchError(400, "IR_12", "Reached maximum refund attempts")
    oid, item = db.order(captured_at=now() - timedelta(days=3))
    rid = db.remedy(oid, item)
    db.call(refunds.start_refund, rid)
    r = db.remedy_row(rid)
    assert (r["status"], r["last_error_code"], r["next_attempt_at"]) == ("needs_manual_refund", "IR_12", None)
    assert db.call(refunds.start_refund, rid) is None and len(fake_hs.creates) == 1   # never retried


def test_unknown_processor_error_goes_to_needs_manual_refund(db, fake_hs):
    from app import refunds
    fake_hs.respond = lambda p: {"refund_id": p["refund_id"], "status": "failed", "error_code": "E00027",
                                 "error_message": "The transaction was unsuccessful."}
    oid, item = db.order(captured_at=now() - timedelta(days=3))
    rid = db.remedy(oid, item)
    db.call(refunds.start_refund, rid)
    r = db.remedy_row(rid)
    assert (r["status"], r["last_error_code"]) == ("needs_manual_refund", "E00027")


def test_get_poll_of_pending_refund_is_not_an_attempt(db, fake_hs):
    from app import refunds
    fake_hs.respond = lambda p: {"refund_id": p["refund_id"], "status": "pending"}
    oid, item = db.order(captured_at=now() - timedelta(days=3))
    rid = db.remedy(oid, item)
    refund_id = db.call(refunds.start_refund, rid)
    for _ in range(3):
        db.call(refunds.check_pending, refund_id)
    assert len(fake_hs.gets) == 3 and len(fake_hs.creates) == 1 and db.attempts(oid) == 1
    fake_hs.get_status = "succeeded"
    db.call(refunds.check_pending, refund_id)
    assert db.remedy_row(rid)["status"] == "refunded" and db.attempts(oid) == 1


def test_customer_never_sees_error_codes(db, fake_hs):
    from app.views import order_view
    fake_hs.respond = lambda p: hs.HyperswitchError(400, "IR_12", "Reached maximum refund attempts")
    from app import refunds
    oid, item = db.order(captured_at=now() - timedelta(days=3))
    db.call(refunds.start_refund, db.remedy(oid, item))
    view = db.call(order_view, oid)
    text = str(view["remedies"])
    assert "Our team is processing your refund" in text
    assert "IR_12" not in text and "maximum refund attempts" not in text


def test_migration_moved_exactly_the_ir12_remedies(db):
    moved = [r["id"] for r in db.sql(
        "SELECT id FROM remedies WHERE manual_reason = 'refund attempt limit reached before settlement' ORDER BY id")]
    assert moved == [8, 10, 12, 13, 14, 15, 16, 17, 19]
    assert db.sql("SELECT count(*) AS n FROM remedies WHERE resolution = 'refund' "
                  "AND status IN ('approved', 'failed')")[0]["n"] == 0
    # history untouched: every attempt of those remedies is still there
    assert db.sql("SELECT count(*) AS n FROM refunds WHERE remedy_id = ANY(%s)", moved)[0]["n"] == 75


# ── POST /internal/sweep ────────────────────────────────────────────────────
@pytest.fixture
def sweep_secret(monkeypatch):
    import dataclasses

    from app import main, sweep
    secret = "test-" + secrets.token_urlsafe(16)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, internal_sweep_secret=secret))

    async def fake_run(trigger):
        return {"trigger": trigger, "seconds": 0.0, "a_payments_synced": []}
    monkeypatch.setattr(sweep, "run_sweep", fake_run)
    return secret


def test_sweep_endpoint_rejects_missing_or_wrong_secret(app_client, sweep_secret):
    assert app_client.post("/internal/sweep").status_code == 401
    assert app_client.post("/internal/sweep", headers={"x-sweep-secret": "wrong"}).status_code == 401
    assert app_client.post("/internal/sweep", headers={"x-sweep-secret": sweep_secret + "x"}).status_code == 401
    ok = app_client.post("/internal/sweep", headers={"x-sweep-secret": sweep_secret})
    assert ok.status_code == 200 and ok.json()["status"] == "ok"


def test_sweep_endpoint_disabled_without_configured_secret(app_client, monkeypatch):
    import dataclasses

    from app import main
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, internal_sweep_secret=""))
    assert app_client.post("/internal/sweep", headers={"x-sweep-secret": ""}).status_code == 404
