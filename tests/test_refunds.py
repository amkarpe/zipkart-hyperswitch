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
        DB().sql("DELETE FROM remedy_schedule_changes WHERE remedy_id IN (SELECT id FROM remedies WHERE order_id = %s)", oid)
        DB().sql("DELETE FROM remedy_txn_corrections WHERE remedy_id IN (SELECT id FROM remedies WHERE order_id = %s)", oid)
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


def test_migration_008_moves_only_remedies_on_payments_with_ir12(db):
    """Runs migration 008's UPDATE on synthetic rows. (On production, on 2026-10-11, it moved
    exactly remedies 8, 10, 12, 13, 14, 15, 16, 17, 19; dev no longer holds those copies.)"""
    from pathlib import Path
    sql = Path("migrations/008_refund_settlement_gate.sql").read_text()
    update = sql[sql.index("UPDATE remedies m"):]

    def failed_remedy(oid, item, codes):
        rid = db.sql("""INSERT INTO remedies (order_id, order_item_id, pricing_type, quantity, reason, resolution,
                            amount_cents, status) VALUES (%s, %s, 'fixed', 1, 'missing', 'refund', 429, 'failed')
                        RETURNING id""", oid, item)[0]["id"]
        for n, code in enumerate(codes, 1):
            db.sql("""INSERT INTO refunds (refund_id, remedy_id, payment_id, attempt_no, amount_cents, status, error_code)
                      VALUES (%s, %s, %s, %s, 429, 'failed', %s)""",
                   f"rf_{oid}_{item}_missing_r{rid}_a{n}", rid, f"pay_{oid}_1", n, code)
        return rid

    hit_oid, hit_item = db.order(captured_at=now() - timedelta(days=3))
    hit_a = failed_remedy(hit_oid, hit_item, ["54", "54", "IR_12"])
    hit_b = failed_remedy(hit_oid, hit_item, ["54"])               # same order as an IR_12: moves too
    miss_oid, miss_item = db.order(captured_at=now() - timedelta(days=3))
    miss = failed_remedy(miss_oid, miss_item, ["54", "54"])        # never hit IR_12: stays
    replacement = db.sql("""INSERT INTO remedies (order_id, order_item_id, pricing_type, quantity, reason, resolution,
                                amount_cents, status) VALUES (%s, %s, 'fixed', 1, 'damaged', 'replacement', 0,
                                'replacement_scheduled') RETURNING id""", hit_oid, hit_item)[0]["id"]
    history_before = db.sql("SELECT count(*) AS n FROM refunds WHERE payment_id IN (%s, %s)",
                            f"pay_{hit_oid}_1", f"pay_{miss_oid}_1")[0]["n"]

    db.sql(update)
    statuses = {rid: db.remedy_row(rid)["status"] for rid in (hit_a, hit_b, miss, replacement)}
    assert statuses == {hit_a: "needs_manual_refund", hit_b: "needs_manual_refund",
                        miss: "failed", replacement: "replacement_scheduled"}
    assert db.remedy_row(hit_a)["manual_reason"] == "refund attempt limit reached before settlement"
    assert db.remedy_row(hit_a)["last_error_code"] == "IR_12"
    assert db.sql("SELECT count(*) AS n FROM refunds WHERE payment_id IN (%s, %s)",
                  f"pay_{hit_oid}_1", f"pay_{miss_oid}_1")[0]["n"] == history_before   # history untouched


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


# ── Manual resolution: needs_manual_refund -> refunded_manually ─────────────
@pytest.fixture
def store(app_client):
    from app.config import settings
    assert app_client.post("/store/api/login", json={"password": settings.store_console_password}).status_code == 200
    yield app_client
    app_client.post("/store/api/logout")


def _needs_manual(db, fake_hs):
    from app import refunds
    fake_hs.respond = lambda p: hs.HyperswitchError(400, "IR_12", "Reached maximum refund attempts")
    oid, item = db.order(captured_at=now() - timedelta(days=3))
    rid = db.remedy(oid, item)
    db.call(refunds.start_refund, rid)
    assert db.remedy_row(rid)["status"] == "needs_manual_refund"
    return oid, rid


def test_mark_refunded_manually_records_and_is_final(db, fake_hs, store):
    oid, rid = _needs_manual(db, fake_hs)
    url = f"/store/api/orders/{oid}/remedies/{rid}/mark-refunded"
    r = store.post(url, json={"processor_transaction_id": "120091999999", "note": "Refunded in Authorize.net by J."})
    assert r.status_code == 200, r.text
    row = db.remedy_row(rid)
    assert row["status"] == "refunded_manually"
    assert row["manual_refund_txn_id"] == "120091999999" and row["manual_refund_note"].startswith("Refunded in")
    assert row["manual_refunded_at"] is not None
    # final: a second click does nothing
    again = store.post(url, json={"processor_transaction_id": "x", "note": "y"})
    assert again.status_code == 409 and db.remedy_row(rid)["manual_refund_txn_id"] == "120091999999"
    # the customer now sees it as sent, without any internal detail
    view = store.get(f"/api/orders/{oid}").json()
    msg = next(x["message"] for x in view["remedies"] if x["id"] == rid)
    assert msg.startswith("Refund of $4.29 has been sent to your bank.")
    assert "120091999999" not in str(view["remedies"]) and "IR_12" not in str(view["remedies"])


def test_mark_refunded_manually_requires_both_fields(db, fake_hs, store):
    oid, rid = _needs_manual(db, fake_hs)
    url = f"/store/api/orders/{oid}/remedies/{rid}/mark-refunded"
    assert store.post(url, json={"processor_transaction_id": "", "note": "n"}).status_code == 422
    assert store.post(url, json={"processor_transaction_id": "t", "note": ""}).status_code == 422
    assert store.post(url, json={"note": "n"}).status_code == 422
    assert store.post(url, json={"processor_transaction_id": "   ", "note": "n"}).status_code == 400   # blank after trim
    assert db.remedy_row(rid)["status"] == "needs_manual_refund"


def test_mark_refunded_manually_only_from_needs_manual(db, fake_hs, store):
    oid, item = db.order(captured_at=now())          # scheduled, not due: awaiting_settlement
    rid = db.remedy(oid, item)
    r = store.post(f"/store/api/orders/{oid}/remedies/{rid}/mark-refunded",
                   json={"processor_transaction_id": "t", "note": "n"})
    assert r.status_code == 409 and db.remedy_row(rid)["status"] == "awaiting_settlement"


def test_mark_refunded_manually_needs_store_login(db, fake_hs, app_client):
    oid, rid = _needs_manual(db, fake_hs)
    app_client.post("/store/api/logout")
    r = app_client.post(f"/store/api/orders/{oid}/remedies/{rid}/mark-refunded",
                        json={"processor_transaction_id": "t", "note": "n"})
    assert r.status_code == 401 and db.remedy_row(rid)["status"] == "needs_manual_refund"


def test_database_rejects_incomplete_manual_refund(db, fake_hs):
    import psycopg
    oid, rid = _needs_manual(db, fake_hs)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.sql("UPDATE remedies SET status = 'refunded_manually' WHERE id = %s", rid)


def test_refund_copy_has_statement_timing():
    from app.views import REMEDY_MESSAGES, STATEMENT_NOTE
    assert STATEMENT_NOTE == "After it's sent, it usually appears on your statement within 5–10 business days."
    for status, text in REMEDY_MESSAGES.items():
        if status != "replacement_scheduled":
            assert text.endswith(STATEMENT_NOTE), status


# ── Effective settlement config ─────────────────────────────────────────────
ZK6_CAPTURE = datetime(2026, 10, 12, 0, 10, tzinfo=LA)   # the production case: ~12:10 AM PDT


def test_config_override_changes_the_gate(monkeypatch):
    import dataclasses

    from app import settlement
    for cutoff, expected in (("15:00", datetime(2026, 10, 13, 4, 0, tzinfo=UTC)),    # Oct 12 11 PM CDT
                             ("16:00", datetime(2026, 10, 13, 5, 0, tzinfo=UTC))):   # Oct 13 12 AM CDT
        monkeypatch.setattr(settlement, "settings",
                            dataclasses.replace(settlement.settings, settlement_cutoff=cutoff,
                                                settlement_timezone="America/Los_Angeles",
                                                settlement_margin_hours=6.0))
        assert settlement.refund_gate(ZK6_CAPTURE) == expected


def test_effective_config_reports_value_and_source(monkeypatch):
    import dataclasses

    from app import settlement
    monkeypatch.delenv("SETTLEMENT_CUTOFF", raising=False)
    assert settlement.effective_config()["settlement_cutoff"]["source"] == "default"
    monkeypatch.setenv("SETTLEMENT_CUTOFF", "15:00")
    monkeypatch.setattr(settlement, "settings", dataclasses.replace(settlement.settings, settlement_cutoff="15:00"))
    cfg = settlement.effective_config()
    assert cfg["settlement_cutoff"] == {"value": "15:00", "source": "env"}
    assert set(cfg) == {"settlement_cutoff", "settlement_timezone", "settlement_margin_hours",
                        "refund_max_auto_attempts_per_payment", "store_timezone"}


def test_startup_log_shows_effective_values(monkeypatch, caplog):
    import dataclasses
    import logging

    from app import main, settlement
    monkeypatch.setenv("SETTLEMENT_CUTOFF", "15:00")
    monkeypatch.setattr(settlement, "settings", dataclasses.replace(settlement.settings, settlement_cutoff="15:00"))
    with caplog.at_level(logging.INFO, logger="zipkart"):
        main.log_settlement_config()
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("settlement config:"))
    assert "settlement_cutoff=15:00 (env)" in line
    assert "settlement_timezone=America/Los_Angeles" in line and "settlement_margin_hours=" in line
    assert "refund_max_auto_attempts_per_payment=" in line


def test_health_shows_effective_settlement_config(app_client):
    body = app_client.get("/health").json()
    s = body["settlement"]
    assert s["settlement_cutoff"]["value"] == "16:00" and s["settlement_cutoff"]["source"] in ("env", "default")
    assert s["settlement_timezone"]["value"] == "America/Los_Angeles"
    assert s["settlement_margin_hours"]["value"] == 6.0
    assert s["refund_max_auto_attempts_per_payment"]["value"] == 4
    assert "latest_migration" in body and "commit" in body



# ── Rescheduling first attempts after a settlement-config change ────────────
def _set_next(db, rid, when):
    db.sql("UPDATE remedies SET next_attempt_at = %s WHERE id = %s", when, rid)


def _changes(db, rid):
    return db.sql("SELECT old_next_attempt_at, new_next_attempt_at, reason FROM remedy_schedule_changes "
                  "WHERE remedy_id = %s ORDER BY id", rid)


def test_reschedule_moves_only_zero_attempt_awaiting_remedies(db, fake_hs):
    from app import sweep
    from app.settlement import refund_gate
    wrong = datetime(2026, 1, 1, tzinfo=UTC)      # a stale stored first-attempt time

    ok_oid, ok_item = db.order(captured_at=now())              # zero attempts: should move
    ok = db.remedy(ok_oid, ok_item)
    _set_next(db, ok, wrong)

    manual_oid, manual_item = _needs_manual(db, fake_hs)[0], None   # needs_manual_refund: untouched
    manual = db.sql("SELECT id FROM remedies WHERE order_id = %s", manual_oid)[0]["id"]

    repl_oid, repl_item = db.order(captured_at=now())          # replacement: never scheduled
    from app.remedies import create_remedy
    repl = db.call(create_remedy, repl_oid, repl_item, "damaged", "replacement", 1, "t")["id"]

    moved = db.call(sweep._reschedule_first_attempts)
    assert [int(m["remedy_id"]) for m in moved] == [ok]
    assert db.remedy_row(ok)["next_attempt_at"] == refund_gate(db.sql("SELECT captured_at FROM orders WHERE id = %s", ok_oid)[0]["captured_at"])
    assert db.remedy_row(manual)["status"] == "needs_manual_refund" and _changes(db, manual) == []
    assert db.remedy_row(repl)["next_attempt_at"] is None and _changes(db, repl) == []


def test_remedy_with_attempts_keeps_its_retry_time(db, fake_hs):
    from app import refunds, sweep
    fake_hs.respond = lambda p: {"refund_id": p["refund_id"], "status": "failed", "error_code": "54"}
    oid, item = db.order(captured_at=now() - timedelta(days=3))
    rid = db.remedy(oid, item)
    db.call(refunds.start_refund, rid)                      # attempt 1 -> 54 -> retry time stored
    retry_at = db.remedy_row(rid)["next_attempt_at"]
    other = db.remedy(oid, item)                            # a second remedy on the SAME payment
    other_at = db.remedy_row(other)["next_attempt_at"]
    _set_next(db, other, other_at + timedelta(hours=1))      # would differ from the gate
    db.call(sweep._reschedule_first_attempts)
    assert db.remedy_row(rid)["next_attempt_at"] == retry_at             # retry time kept
    assert db.remedy_row(other)["next_attempt_at"] == other_at + timedelta(hours=1)   # payment has attempts
    assert _changes(db, rid) == [] and _changes(db, other) == []


def test_reschedule_then_select_never_sends_at_the_old_time(db, fake_hs):
    from app import sweep
    oid, item = db.order(captured_at=now())        # correct gate is in the future
    rid = db.remedy(oid, item)
    _set_next(db, rid, now() - timedelta(hours=1))   # stale stored time: already "due"
    result = db.call(sweep.run_sweep, "test_reschedule")
    assert result is not None, "another sweep held the lease"
    assert [int(r["remedy_id"]) for r in result["d1_rescheduled"]] == [rid]
    assert fake_hs.creates == [] and result["d2_refunds_sent"] == []
    assert db.remedy_row(rid)["next_attempt_at"] > now() and db.remedy_row(rid)["status"] == "awaiting_settlement"
    assert list(result).index("d1_rescheduled") < list(result).index("d2_refunds_sent")


def test_audit_entry_written_exactly_once_per_change(db, fake_hs, monkeypatch):
    import dataclasses

    from app import settlement, sweep
    oid, item = db.order(captured_at=datetime(2026, 10, 12, 0, 10, tzinfo=LA))   # the ZK6 case
    rid = db.remedy(oid, item)
    wrong = datetime(2026, 10, 13, 4, 0, tzinfo=UTC)          # what the 15:00 cut-off produced
    _set_next(db, rid, wrong)
    for _ in range(3):                                        # repeated runs: one change, one row
        db.call(sweep._reschedule_first_attempts)
    rows = _changes(db, rid)
    assert len(rows) == 1
    assert rows[0]["old_next_attempt_at"] == wrong
    assert rows[0]["new_next_attempt_at"] == datetime(2026, 10, 13, 5, 0, tzinfo=UTC)   # Oct 13 12:00 AM CDT
    assert rows[0]["reason"].startswith("settlement config changed: cutoff 16:00 America/Los_Angeles")

    # a later, different config change is a second change -> exactly one more row
    monkeypatch.setattr(settlement, "settings", dataclasses.replace(settlement.settings, settlement_margin_hours=7.0))
    db.call(sweep._reschedule_first_attempts)
    db.call(sweep._reschedule_first_attempts)
    rows = _changes(db, rid)
    assert len(rows) == 2 and rows[1]["new_next_attempt_at"] == datetime(2026, 10, 13, 6, 0, tzinfo=UTC)
    assert "margin 7h" in rows[1]["reason"]


def test_store_console_shows_schedule_change(db, fake_hs, store):
    from app import sweep
    oid, item = db.order(captured_at=datetime(2026, 10, 12, 0, 10, tzinfo=LA))
    rid = db.remedy(oid, item)
    _set_next(db, rid, datetime(2026, 10, 13, 4, 0, tzinfo=UTC))
    db.call(sweep._reschedule_first_attempts)
    view = store.get(f"/store/api/orders/{oid}").json()
    r = next(x for x in view["remedies"] if x["id"] == rid)
    assert r["next_attempt"] == "Oct 13, 12:00 AM CDT"
    assert r["schedule_changes"][0]["old"] == "Oct 12, 11:00 PM CDT"
    assert r["schedule_changes"][0]["new"] == "Oct 13, 12:00 AM CDT"
    cust = store.get(f"/api/orders/{oid}").json()
    assert "by Oct 13" in next(x["message"] for x in cust["remedies"] if x["id"] == rid)



# ── Processor references on manual refunds: unique, and correctable with history ──
def _manually_refunded(db, fake_hs, store, txn):
    oid, rid = _needs_manual(db, fake_hs)
    r = store.post(f"/store/api/orders/{oid}/remedies/{rid}/mark-refunded",
                   json={"processor_transaction_id": txn, "note": "refunded in Authorize.net"})
    assert r.status_code == 200, r.text
    return oid, rid


def _txn(prefix="8006"):
    return prefix + secrets.token_hex(4)


def test_duplicate_processor_txn_is_rejected(db, fake_hs, store):
    txn = _txn()
    _oid1, rid1 = _manually_refunded(db, fake_hs, store, txn)
    oid2, rid2 = _needs_manual(db, fake_hs)
    r = store.post(f"/store/api/orders/{oid2}/remedies/{rid2}/mark-refunded",
                   json={"processor_transaction_id": f"  {txn} ", "note": "oops, same id"})
    assert r.status_code == 409
    assert r.json()["detail"] == f"This processor transaction is already recorded on remedy {rid1}."
    assert db.remedy_row(rid2)["status"] == "needs_manual_refund" and db.remedy_row(rid2)["manual_refund_txn_id"] is None


def test_correction_writes_one_history_row_and_updates_reference(db, fake_hs, store):
    wrong, right = _txn(), _txn()
    oid, rid = _manually_refunded(db, fake_hs, store, wrong)
    url = f"/store/api/orders/{oid}/remedies/{rid}/correct-txn"
    r = store.post(url, json={"processor_transaction_id": right, "reason": "entered remedy 15's refund by mistake"})
    assert r.status_code == 200, r.text
    row = db.remedy_row(rid)
    assert (row["status"], row["manual_refund_txn_id"]) == ("refunded_manually", right)   # status unchanged
    hist = db.sql("SELECT old_txn_id, new_txn_id, reason FROM remedy_txn_corrections WHERE remedy_id = %s", rid)
    assert hist == [{"old_txn_id": wrong, "new_txn_id": right, "reason": "entered remedy 15's refund by mistake"}]
    # repeating the same correction is refused and writes nothing more
    again = store.post(url, json={"processor_transaction_id": right, "reason": "again"})
    assert again.status_code == 409
    assert db.sql("SELECT count(*) AS n FROM remedy_txn_corrections WHERE remedy_id = %s", rid)[0]["n"] == 1
    # shown on the console row
    view = store.get(f"/store/api/orders/{oid}").json()
    shown = next(x for x in view["remedies"] if x["id"] == rid)["txn_corrections"]
    assert [(t["old"], t["new"]) for t in shown] == [(wrong, right)]


def test_correction_cannot_create_a_duplicate(db, fake_hs, store):
    taken, mine = _txn(), _txn()
    _oid1, rid1 = _manually_refunded(db, fake_hs, store, taken)
    oid2, rid2 = _manually_refunded(db, fake_hs, store, mine)
    r = store.post(f"/store/api/orders/{oid2}/remedies/{rid2}/correct-txn",
                   json={"processor_transaction_id": taken, "reason": "wrong one"})
    assert r.status_code == 409 and r.json()["detail"] == f"This processor transaction is already recorded on remedy {rid1}."
    assert db.remedy_row(rid2)["manual_refund_txn_id"] == mine
    assert db.sql("SELECT count(*) AS n FROM remedy_txn_corrections WHERE remedy_id = %s", rid2)[0]["n"] == 0


def test_correction_requires_id_and_reason_and_a_manual_refund(db, fake_hs, store):
    oid, rid = _manually_refunded(db, fake_hs, store, _txn())
    url = f"/store/api/orders/{oid}/remedies/{rid}/correct-txn"
    assert store.post(url, json={"processor_transaction_id": "", "reason": "r"}).status_code == 422
    assert store.post(url, json={"processor_transaction_id": "x", "reason": ""}).status_code == 422
    oid2, rid2 = _needs_manual(db, fake_hs)                     # not refunded_manually yet
    r = store.post(f"/store/api/orders/{oid2}/remedies/{rid2}/correct-txn",
                   json={"processor_transaction_id": "x1", "reason": "r"})
    assert r.status_code == 409


def test_concurrent_records_of_the_same_txn_only_one_wins(db, fake_hs):
    import asyncio

    from app import refunds
    from app.refunds import RefundError
    txn = _txn()
    (oid1, rid1), (oid2, rid2) = _needs_manual(db, fake_hs), _needs_manual(db, fake_hs)

    async def both():
        async def one(oid, rid):
            try:
                return await refunds.mark_refunded_manually(oid, rid, txn, "n")
            except RefundError as exc:
                return exc.message
        return await asyncio.gather(one(oid1, rid1), one(oid2, rid2))
    results = db.call(both)
    assert sorted(map(str, results))[0].startswith("This processor transaction is already recorded on remedy")
    assert results.count(True) == 1
    assert db.sql("SELECT count(*) AS n FROM remedies WHERE manual_refund_txn_id = %s", txn)[0]["n"] == 1
