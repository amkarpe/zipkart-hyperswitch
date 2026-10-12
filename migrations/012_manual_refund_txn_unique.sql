-- One processor refund belongs to one remedy: manual_refund_txn_id is unique where set.
--
-- Deploy only after every existing duplicate has been corrected with "Correct processor
-- reference" (migration 011). If any duplicate remains, this stops with a message naming it,
-- before changing anything: the deploy fails and Render keeps serving the previous version.

DO $$
DECLARE dups TEXT;
BEGIN
    SELECT string_agg(format('%s (remedies %s)', manual_refund_txn_id, ids), '; ')
      INTO dups
      FROM (SELECT manual_refund_txn_id, string_agg(id::text, ', ' ORDER BY id) AS ids
              FROM remedies WHERE manual_refund_txn_id IS NOT NULL
             GROUP BY manual_refund_txn_id HAVING count(*) > 1) d;
    IF dups IS NOT NULL THEN
        RAISE EXCEPTION 'Duplicate processor transaction IDs on remedies: %. Correct them with "Correct processor reference" first.', dups;
    END IF;
END $$;

CREATE UNIQUE INDEX remedies_manual_refund_txn_unique
    ON remedies (manual_refund_txn_id) WHERE manual_refund_txn_id IS NOT NULL;
