-- Corrections to a recorded manual refund's processor transaction ID.
--
-- A refunded_manually remedy records the processor's (Authorize.net) refund transaction ID.
-- If it was entered wrong, the store corrects it with a reason; the status doesn't change.
-- Every correction is kept here, append-only (same pattern as remedy_schedule_changes):
-- one row per change, old and new ID, why, and when. remedies holds only the current value.

CREATE TABLE remedy_txn_corrections (
    id            BIGSERIAL PRIMARY KEY,
    remedy_id     BIGINT NOT NULL REFERENCES remedies(id),
    old_txn_id    TEXT NOT NULL,
    new_txn_id    TEXT NOT NULL CHECK (char_length(new_txn_id) BETWEEN 1 AND 100),
    reason        TEXT NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    corrected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (old_txn_id <> new_txn_id)
);
CREATE INDEX remedy_txn_corrections_remedy_idx ON remedy_txn_corrections (remedy_id, corrected_at);

-- The uniqueness rule (one processor refund belongs to one remedy) is added in a later
-- migration, after the one known duplicate in production has been corrected with this.
