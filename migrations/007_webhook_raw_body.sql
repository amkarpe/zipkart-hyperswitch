-- Webhooks: keep the exact bytes Hyperswitch sent, as TEXT.
-- JSONB was the wrong type for an audit copy: Postgres JSONB rejects input that
-- Python's json module accepts (e.g. a \u0000 escape or a lone surrogate), so a
-- valid, correctly signed delivery could fail on INSERT. TEXT stores it verbatim.
ALTER TABLE webhook_events ALTER COLUMN payload DROP NOT NULL;
ALTER TABLE webhook_events ADD COLUMN raw_body TEXT;

-- Deliveries we couldn't process: what arrived and where it failed, so an
-- unexpected payload is diagnosable from the database (no access to host logs needed).
CREATE TABLE webhook_failures (
    id           BIGSERIAL PRIMARY KEY,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_id     TEXT,
    stage        TEXT NOT NULL,       -- signature (capped per hour) | parse | store | process
    status_code  INT NOT NULL,        -- what we answered
    error        TEXT NOT NULL,
    headers      TEXT,                -- signature value redacted
    raw_body     TEXT
);
CREATE INDEX webhook_failures_received_idx ON webhook_failures (received_at DESC);
