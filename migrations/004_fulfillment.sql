-- Phase 2: substitution preference, picking, substitutions, capture, cancel.

-- ── Orders ─────────────────────────────────────────────────────────────────
ALTER TABLE orders
    ADD COLUMN substitution_preference TEXT NOT NULL DEFAULT 'best_match'
        CHECK (substitution_preference IN ('best_match', 'remove')),
    -- The buffer % used for THIS order's hold, so the hold explanation can never
    -- drift if WEIGHT_BUFFER_PERCENT changes later. Existing orders were made at 15.
    ADD COLUMN weight_buffer_percent INT NOT NULL DEFAULT 15 CHECK (weight_buffer_percent BETWEEN 0 AND 100),
    -- Final breakdown, stored in the same transaction that fixes final_amount_cents.
    ADD COLUMN final_subtotal_cents BIGINT CHECK (final_subtotal_cents >= 0),
    ADD COLUMN final_tax_cents      BIGINT CHECK (final_tax_cents >= 0),
    ADD COLUMN picking_started_at   TIMESTAMPTZ,
    ADD COLUMN captured_at          TIMESTAMPTZ,   -- refunds later need this (Authorize.net settles nightly)
    ADD COLUMN cancelled_at         TIMESTAMPTZ,
    ADD COLUMN cancelled_by         TEXT CHECK (cancelled_by IN ('customer', 'store'));
ALTER TABLE orders ALTER COLUMN weight_buffer_percent DROP DEFAULT;

-- ── Order items ────────────────────────────────────────────────────────────
-- Substitute lines are separate rows pointing at the line they replace, with
-- their own small lifecycle: proposed -> picked (accepted) | declined.
ALTER TABLE order_items DROP CONSTRAINT order_items_status_check;
ALTER TABLE order_items
    ADD COLUMN substitution_expires_at TIMESTAMPTZ,   -- on a proposed substitute line
    ADD CONSTRAINT order_items_status_check CHECK (
        (substitute_for_item_id IS NULL
            AND status IN ('pending', 'picked', 'substitution_proposed', 'substituted', 'removed'))
     OR (substitute_for_item_id IS NOT NULL
            AND status IN ('proposed', 'picked', 'declined')
            AND substitution_expires_at IS NOT NULL)
    ),
    -- A picked line always knows exactly what it costs and what was picked.
    ADD CONSTRAINT order_items_picked_complete_chk CHECK (
        status <> 'picked' OR (
            final_line_cents IS NOT NULL
            AND (pricing_type = 'weighed' OR picked_quantity >= 1)
            AND (pricing_type = 'fixed'   OR actual_weight_lb > 0)
        )
    ),
    -- A proposed substitute is priced up front (the shopper has it in hand),
    -- because that price is what gets reserved against the hold.
    ADD CONSTRAINT order_items_proposed_priced_chk CHECK (
        status <> 'proposed' OR final_line_cents IS NOT NULL
    );

-- Lazy expiry of unanswered substitutions looks for these on every poll.
CREATE INDEX order_items_proposed_idx ON order_items (order_id, substitution_expires_at)
    WHERE status = 'proposed';
