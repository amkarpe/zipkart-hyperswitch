-- Refunds wait for settlement, are scheduled (not retried every sweep), and are
-- capped per payment. See app/refunds.py for the state machine.
--
-- Why: Authorize.net only credits settled transactions (error 54 before that), and
-- Hyperswitch rejects a refund with IR_12 once a payment already has more than
-- [refund] max_attempts = 10 refunds on record, failed ones included
-- (crates/router/src/core/utils/refunds_validator.rs). Retrying every sweep run burned
-- that budget on six payments before any of them settled.

ALTER TABLE remedies DROP CONSTRAINT remedies_status_check;
ALTER TABLE remedies ADD CONSTRAINT remedies_status_check CHECK (status IN (
    'approved', 'failed',            -- legacy; kept so old rows stay valid, never written by new code
    'awaiting_settlement',           -- scheduled: next_attempt_at is when the sweep may send it
    'refund_pending',                -- an attempt is in flight at Hyperswitch
    'refunded',
    'needs_manual_refund',           -- automation stopped; a person refunds it
    'replacement_scheduled'
));

ALTER TABLE remedies
    ADD COLUMN next_attempt_at    TIMESTAMPTZ,
    ADD COLUMN manual_reason      TEXT,
    ADD COLUMN last_error_code    TEXT,
    ADD COLUMN last_error_message TEXT,
    ADD CONSTRAINT remedies_waiting_has_time
        CHECK (status <> 'awaiting_settlement' OR next_attempt_at IS NOT NULL),
    ADD CONSTRAINT remedies_manual_has_reason
        CHECK (status <> 'needs_manual_refund' OR manual_reason IS NOT NULL);

-- The sweep's only refund query: due, scheduled remedies.
CREATE INDEX remedies_due_idx ON remedies (next_attempt_at) WHERE status = 'awaiting_settlement';

-- The remedies on payments that already hit IR_12 (Hyperswitch's attempt limit) before
-- their captures settled. Status only: every refunds row (each attempt and its error)
-- stays exactly as it was.
UPDATE remedies m
   SET status             = 'needs_manual_refund',
       manual_reason      = 'refund attempt limit reached before settlement',
       last_error_code    = 'IR_12',
       last_error_message = 'Reached maximum refund attempts',
       updated_at         = now()
 WHERE m.resolution = 'refund'
   AND m.status = 'failed'
   AND EXISTS (SELECT 1 FROM refunds f JOIN remedies m2 ON m2.id = f.remedy_id
               WHERE m2.order_id = m.order_id AND f.error_code = 'IR_12');
