-- Remedies carry a quantity, so one fixed-price line can get several remedies
-- (e.g. 1 of 3 cartons missing at the door, 1 more found damaged later).
--
-- What the database enforces:
--   * shape: a fixed-line remedy has quantity > 0; a weighed-line remedy has none
--   * weighed lines: still one remedy per line (a partial unique index)
-- What the insert transaction enforces (it needs a SUM across rows, which a CHECK can't do):
--   * fixed lines: SUM(remedies.quantity) <= order_items.picked_quantity
-- See app/remedies.py.

-- The old one-remedy-per-line rule.
DROP INDEX remedies_one_per_item_idx;

-- How many units were actually delivered (set while picking). NULL for weighed lines,
-- which use actual_weight_lb instead.
ALTER TABLE order_items
    ADD COLUMN picked_quantity INT,
    ADD CONSTRAINT order_items_picked_quantity_chk
        CHECK (picked_quantity IS NULL OR (pricing_type = 'fixed' AND picked_quantity BETWEEN 0 AND quantity)),
    -- Target for the composite FK below.
    ADD CONSTRAINT order_items_id_pricing_type_key UNIQUE (id, pricing_type);

-- remedies is empty at this point (no remedy flow exists yet), so NOT NULL needs no default.
ALTER TABLE remedies
    ADD COLUMN pricing_type TEXT NOT NULL,
    ADD COLUMN quantity INT,
    -- Copying pricing_type into remedies and binding it to the item's real type with
    -- a composite FK lets the per-type rules below be plain constraints.
    ADD CONSTRAINT remedies_item_pricing_type_fkey
        FOREIGN KEY (order_item_id, pricing_type) REFERENCES order_items (id, pricing_type),
    ADD CONSTRAINT remedies_quantity_shape_chk CHECK (
        (pricing_type = 'fixed'   AND quantity > 0)
     OR (pricing_type = 'weighed' AND quantity IS NULL)
    );

CREATE UNIQUE INDEX remedies_one_per_weighed_item_idx
    ON remedies (order_item_id) WHERE pricing_type = 'weighed';
-- Serves the SUM(quantity) check for fixed lines.
CREATE INDEX remedies_item_idx ON remedies (order_item_id);
