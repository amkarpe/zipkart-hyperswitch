-- A person resolves a needs_manual_refund remedy by refunding it directly with the
-- processor (Authorize.net dashboard), then records it here: refunded_manually (final),
-- with the processor's transaction id, a note, and when it was recorded.

ALTER TABLE remedies DROP CONSTRAINT remedies_status_check;
ALTER TABLE remedies ADD CONSTRAINT remedies_status_check CHECK (status IN (
    'approved', 'failed',            -- legacy; kept so old rows stay valid, never written by new code
    'awaiting_settlement',
    'refund_pending',
    'refunded',
    'needs_manual_refund',
    'refunded_manually',             -- final: refunded outside Hyperswitch, recorded by the store
    'replacement_scheduled'
));

ALTER TABLE remedies
    ADD COLUMN manual_refund_txn_id TEXT CHECK (char_length(manual_refund_txn_id) BETWEEN 1 AND 100),
    ADD COLUMN manual_refund_note   TEXT CHECK (char_length(manual_refund_note) BETWEEN 1 AND 500),
    ADD COLUMN manual_refunded_at   TIMESTAMPTZ,
    ADD CONSTRAINT remedies_manual_refund_complete CHECK (
        status <> 'refunded_manually'
        OR (manual_refund_txn_id IS NOT NULL AND manual_refund_note IS NOT NULL AND manual_refunded_at IS NOT NULL)
    );
