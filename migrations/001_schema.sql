-- ZipKart core schema. Money is always BIGINT cents; weights are exact NUMERIC pounds.
-- Statuses are TEXT + CHECK (not Postgres ENUMs) so adding a state later is a
-- one-line constraint change instead of an ALTER TYPE.

-- ── Catalog ────────────────────────────────────────────────────────────────
CREATE TABLE products (
    sku               TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    category          TEXT NOT NULL,
    pricing_type      TEXT NOT NULL CHECK (pricing_type IN ('fixed', 'weighed')),
    -- fixed: price per unit; weighed: price per lb
    unit_price_cents  BIGINT NOT NULL CHECK (unit_price_cents > 0),
    unit_label        TEXT NOT NULL,              -- "each", "12 pk", "lb"
    taxable           BOOLEAN NOT NULL DEFAULT FALSE,
    in_stock          BOOLEAN NOT NULL DEFAULT TRUE,
    emoji             TEXT NOT NULL DEFAULT '🛒',
    sort_order        INT NOT NULL DEFAULT 0
);

-- ── Orders ─────────────────────────────────────────────────────────────────
CREATE TABLE orders (
    id                    TEXT PRIMARY KEY,       -- e.g. ZK7F3K9Q2M; also merchant_order_reference_id
    status                TEXT NOT NULL DEFAULT 'pending_payment' CHECK (status IN (
                              'pending_payment',   -- waiting for an authorized hold
                              'authorized',        -- hold confirmed by Hyperswitch (requires_capture)
                              'picking',           -- shopper working the order
                              'finalizing',        -- frozen: no item edits, capture amount fixed
                              'capturing',         -- capture call in flight
                              'captured',          -- final amount charged, remainder released
                              'delivered',
                              'cancelling',        -- void call in flight
                              'cancelled',
                              'needs_review'       -- money state we didn't expect; human looks
                          )),
    currency              CHAR(3) NOT NULL,
    customer_name         TEXT NOT NULL,
    customer_email        TEXT NOT NULL,
    customer_phone        TEXT NOT NULL,
    ship_line1            TEXT NOT NULL,
    ship_line2            TEXT,
    ship_city             TEXT NOT NULL,
    ship_state            CHAR(2) NOT NULL,
    ship_zip              TEXT NOT NULL,
    -- Pricing snapshot at checkout, computed server-side.
    subtotal_cents        BIGINT NOT NULL CHECK (subtotal_cents >= 0),   -- estimate
    tax_cents             BIGINT NOT NULL CHECK (tax_cents >= 0),        -- estimate
    delivery_fee_cents    BIGINT NOT NULL CHECK (delivery_fee_cents >= 0),
    estimated_total_cents BIGINT NOT NULL CHECK (estimated_total_cents >= 0),
    hold_amount_cents     BIGINT NOT NULL,
    -- Set at finalize/capture time.
    final_amount_cents    BIGINT CHECK (final_amount_cents >= 0),
    captured_amount_cents BIGINT CHECK (captured_amount_cents >= 0),
    review_reason         TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The hold must cover the estimate; we can never capture more than we hold.
    CHECK (hold_amount_cents >= estimated_total_cents),
    CHECK (final_amount_cents IS NULL OR final_amount_cents <= hold_amount_cents),
    CHECK (captured_amount_cents IS NULL OR captured_amount_cents <= hold_amount_cents)
);
-- Store console queue and the reconciliation sweep both ask
-- "orders in status X, oldest first".
CREATE INDEX orders_status_updated_idx ON orders (status, updated_at);

-- ── Order items ────────────────────────────────────────────────────────────
CREATE TABLE order_items (
    id                       BIGSERIAL PRIMARY KEY,
    order_id                 TEXT NOT NULL REFERENCES orders(id),
    sku                      TEXT NOT NULL REFERENCES products(sku),
    name                     TEXT NOT NULL,          -- snapshot: catalog can change later
    pricing_type             TEXT NOT NULL CHECK (pricing_type IN ('fixed', 'weighed')),
    unit_price_cents         BIGINT NOT NULL CHECK (unit_price_cents > 0),
    taxable                  BOOLEAN NOT NULL,
    quantity                 INT,                    -- fixed items only
    estimated_weight_lb      NUMERIC(7,2),           -- weighed items only
    actual_weight_lb         NUMERIC(7,2),           -- recorded at the scale while picking
    estimated_line_cents     BIGINT NOT NULL CHECK (estimated_line_cents >= 0),
    hold_line_cents          BIGINT NOT NULL,        -- estimate + weight buffer (weighed only)
    final_line_cents         BIGINT CHECK (final_line_cents >= 0),
    status                   TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                                 'pending',                -- not picked yet
                                 'picked',
                                 'substitution_proposed',  -- waiting for the customer
                                 'substituted',            -- original replaced by substitute line
                                 'removed'                 -- out of stock / declined substitute
                             )),
    substitute_for_item_id   BIGINT REFERENCES order_items(id),  -- set on a substitute line
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Shape rules: a fixed line has a quantity and no weights; a weighed line the opposite.
    CHECK (
        (pricing_type = 'fixed'   AND quantity > 0 AND estimated_weight_lb IS NULL AND actual_weight_lb IS NULL)
     OR (pricing_type = 'weighed' AND quantity IS NULL AND estimated_weight_lb > 0)
    ),
    CHECK (hold_line_cents >= estimated_line_cents)
);
CREATE INDEX order_items_order_idx ON order_items (order_id);
-- One substitute per original line; a second proposal for the same item is a bug.
CREATE UNIQUE INDEX order_items_one_substitute_idx
    ON order_items (substitute_for_item_id) WHERE substitute_for_item_id IS NOT NULL;

-- ── Payments (one order → many attempts, at most one active) ───────────────
CREATE TABLE payments (
    payment_id                TEXT PRIMARY KEY,      -- ours, deterministic: pay_{order_id}_{attempt}
    order_id                  TEXT NOT NULL REFERENCES orders(id),
    attempt_no                INT NOT NULL CHECK (attempt_no > 0),
    is_active                 BOOLEAN NOT NULL DEFAULT TRUE,
    status                    TEXT NOT NULL DEFAULT 'creating' CHECK (status IN (
                                  'creating',        -- intent written, create call not confirmed yet
                                  'create_failed',   -- Hyperswitch never created it
                                  -- Hyperswitch statuses below
                                  'requires_payment_method',
                                  'requires_confirmation',
                                  'requires_customer_action',
                                  'processing',
                                  'requires_capture',
                                  'partially_captured',
                                  'partially_captured_and_capturable',
                                  'succeeded',
                                  'cancelled',
                                  'failed'
                              )),
    hs_status_raw             TEXT,                  -- exactly what Hyperswitch last said
    amount_cents              BIGINT NOT NULL CHECK (amount_cents > 0),  -- hold we asked for (net_amount)
    amount_capturable_cents   BIGINT,
    amount_received_cents     BIGINT,
    client_secret             TEXT,                  -- short-lived; re-served only while unexpired
    expires_at                TIMESTAMPTZ,
    connector                 TEXT,
    connector_transaction_id  TEXT,                  -- Authorize.net transId
    error_code                TEXT,
    unified_code              TEXT,
    error_message             TEXT,                  -- processor's raw message (logs/support only)
    unified_message           TEXT,                  -- Hyperswitch's processor-agnostic message
    last_synced_at            TIMESTAMPTZ,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (order_id, attempt_no)
);
-- The "one active payment per order" rule, enforced by the database, not by hope.
CREATE UNIQUE INDEX payments_one_active_per_order_idx ON payments (order_id) WHERE is_active;
-- Sweep: find payments stuck in non-final states.
CREATE INDEX payments_status_updated_idx ON payments (status, updated_at);

-- ── Remedies (post-delivery problems) ──────────────────────────────────────
CREATE TABLE remedies (
    id               BIGSERIAL PRIMARY KEY,
    order_id         TEXT NOT NULL REFERENCES orders(id),
    order_item_id    BIGINT NOT NULL REFERENCES order_items(id),
    reason           TEXT NOT NULL CHECK (reason IN ('missing', 'damaged', 'expired')),
    resolution       TEXT NOT NULL CHECK (resolution IN ('refund', 'replacement')),
    amount_cents     BIGINT NOT NULL CHECK (amount_cents >= 0),  -- refund value; 0 for replacement
    status           TEXT NOT NULL CHECK (status IN (
                         'approved',               -- recorded; money not moved yet
                         'refund_pending',         -- refund call in flight
                         'awaiting_settlement',    -- Authorize.net error 54: retry after batch settles
                         'refunded',
                         'replacement_scheduled',
                         'failed'                  -- real failure; human looks
                     )),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A missing item can only be refunded (nothing to replace from).
    CHECK (reason <> 'missing' OR resolution = 'refund'),
    CHECK ((resolution = 'refund' AND amount_cents > 0) OR (resolution = 'replacement' AND amount_cents = 0))
);
-- One remedy per line: no refund AND replacement, no double refund for the same item.
CREATE UNIQUE INDEX remedies_one_per_item_idx ON remedies (order_item_id);
CREATE INDEX remedies_order_idx ON remedies (order_id);
CREATE INDEX remedies_status_idx ON remedies (status, updated_at);

-- ── Refunds (attempts against Hyperswitch) ─────────────────────────────────
CREATE TABLE refunds (
    refund_id            TEXT PRIMARY KEY,        -- ours: rf_{order}_{item}_{reason}_a{n}
    remedy_id            BIGINT NOT NULL REFERENCES remedies(id),
    payment_id           TEXT NOT NULL REFERENCES payments(payment_id),
    attempt_no           INT NOT NULL CHECK (attempt_no > 0),
    amount_cents         BIGINT NOT NULL CHECK (amount_cents > 0),
    status               TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'succeeded', 'failed')),
    connector_refund_id  TEXT,                    -- Authorize.net's id for the same refund
    error_code           TEXT,                    -- "54" = transaction not settled yet
    unified_code         TEXT,
    error_message        TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (remedy_id, attempt_no)
);
-- At most one live (pending or succeeded) refund per remedy. Failed attempts
-- stay as history; only after a confirmed failure may attempt n+1 be created.
CREATE UNIQUE INDEX refunds_one_live_per_remedy_idx ON refunds (remedy_id) WHERE status <> 'failed';
CREATE INDEX refunds_payment_idx ON refunds (payment_id);

-- ── Webhook inbox ──────────────────────────────────────────────────────────
CREATE TABLE webhook_events (
    event_id      TEXT PRIMARY KEY,             -- Hyperswitch event_id: duplicates hit this and stop
    event_type    TEXT NOT NULL,
    object_id     TEXT,                         -- payment_id or refund_id
    payload       JSONB NOT NULL,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ
);
CREATE INDEX webhook_events_unprocessed_idx ON webhook_events (received_at) WHERE processed_at IS NULL;
