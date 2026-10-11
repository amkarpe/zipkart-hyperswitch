-- Phase 3: remedy notes, refund bookkeeping, reconciliation sweep lease.

ALTER TABLE remedies
    ADD COLUMN note TEXT CHECK (char_length(note) <= 500);

ALTER TABLE refunds
    -- Exactly what Hyperswitch last said ('review' is stored as our 'pending').
    ADD COLUMN hs_status_raw  TEXT,
    ADD COLUMN last_synced_at TIMESTAMPTZ;
-- Sweep (e): pending refunds to re-check.
CREATE INDEX refunds_pending_idx ON refunds (updated_at) WHERE status = 'pending';

-- One sweep at a time across all app instances, without holding a transaction
-- (or a session advisory lock, which PgBouncer transaction pooling can't keep)
-- open across Hyperswitch calls: a lease taken with a compare-and-set.
CREATE TABLE sweep_lease (
    id            INT PRIMARY KEY CHECK (id = 1),
    locked_until  TIMESTAMPTZ NOT NULL DEFAULT '-infinity',
    owner         TEXT,
    last_run_at   TIMESTAMPTZ,
    last_result   JSONB
);
INSERT INTO sweep_lease (id) VALUES (1);
