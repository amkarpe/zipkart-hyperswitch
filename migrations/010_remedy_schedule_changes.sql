-- Audit trail for refund schedule corrections.
--
-- The sweep recomputes the first refund attempt time (gate(captured_at) with the current
-- settlement config) for scheduled remedies on payments with no refund attempts yet, and
-- rewrites next_attempt_at when it differs (e.g. after SETTLEMENT_CUTOFF was corrected from
-- 15:00 to 16:00). Every such change is recorded here, append-only: one row per change, with
-- the old and new time and why. A table rather than columns on remedies, so a remedy that is
-- corrected more than once keeps its whole history.

CREATE TABLE remedy_schedule_changes (
    id                   BIGSERIAL PRIMARY KEY,
    remedy_id            BIGINT NOT NULL REFERENCES remedies(id),
    old_next_attempt_at  TIMESTAMPTZ NOT NULL,
    new_next_attempt_at  TIMESTAMPTZ NOT NULL,
    reason               TEXT NOT NULL,
    changed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (old_next_attempt_at <> new_next_attempt_at)
);
CREATE INDEX remedy_schedule_changes_remedy_idx ON remedy_schedule_changes (remedy_id, changed_at);
