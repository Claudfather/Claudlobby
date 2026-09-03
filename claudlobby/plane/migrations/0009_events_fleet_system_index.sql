-- 0009: the bot-events ledger's readers (cutover Phase B) ask the fleet's
-- system events by provenance and instant — brief for one bot's day,
-- fleet-pulse for the critical set inside a window, `claudlobby events` for
-- the tail — and the system rows carry the shadow's comparisons too (27 per
-- ten minutes on a 9-bot fleet), so an unindexed (fleet, instant) read scans
-- every system event the plane ever recorded (measured plan: the kind index,
-- then a temp B-tree). One partial index makes each a seek.
BEGIN IMMEDIATE;

CREATE INDEX idx_events_fleet_system ON events (fleet_uid, occurred_at) WHERE kind = 'system';

PRAGMA user_version = 9;
COMMIT;
