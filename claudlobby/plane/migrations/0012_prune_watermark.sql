-- #1659: the retention lane deletes family rows and (correctly) leaves their
-- ingest_ledger rows, which is what makes a REPLAY of a pruned event reach
-- `_verify_duplicates` with a ledger row and no family row -- the exact state
-- that raised "ledger/family divergence ... (integrity, not idempotency)" and
-- lost the whole batch with it.
--
-- A watermark rather than a per-event record, because a row per pruned event
-- would store as much as the prune deleted and save nothing.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS prune_watermarks (
    family         TEXT NOT NULL,
    pruned_before  TEXT NOT NULL,   -- the cutoff the prune used (ingested_at)
    pruned_at      TEXT NOT NULL,   -- when the prune ran, for the operator
    PRIMARY KEY (family)
);

PRAGMA user_version = 12;
COMMIT;
PRAGMA foreign_keys = ON;
