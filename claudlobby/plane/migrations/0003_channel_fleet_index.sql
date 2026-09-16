-- 0003: the fleet-scoped channel (per-team ROOMS are the default view — the
-- operator ruling 2026-08-29). Without a (fleet_uid, ingest_seq) index the
-- planner serves /api/channel?fleet= from the msg_id autoindex and SCANs the
-- whole communications table per poll (EXPLAIN QUERY PLAN confirmed), and a
-- quiet room scans everything for nothing — linear in ledger growth on the
-- Pi's SD, paid on every SSE-triggered + 60s-safety refresh, per viewer.
BEGIN IMMEDIATE;

CREATE INDEX idx_intents_fleet_seq
  ON communications (fleet_uid, ingest_seq) WHERE fleet_uid IS NOT NULL;

PRAGMA user_version = 3;
COMMIT;
