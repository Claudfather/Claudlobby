-- 0008: the flipped readers ask "the bot's own progress inside the grace" and
-- "a terminal event by this actor" per bot on every watchdog tick and every
-- worker report (chunks 4-6a: LAST_PROGRESS_SQL, the resolver's guard). The
-- events table indexes kind/msg/item/ws/subject/assignment but never the
-- ACTOR, so those reads narrowed by kind and then scanned every task event
-- the fleet ever recorded — cost growing with history, not with one bot's rows.
BEGIN IMMEDIATE;

CREATE INDEX idx_events_actor ON events (actor_uid, occurred_at) WHERE actor_uid IS NOT NULL;

PRAGMA user_version = 8;
COMMIT;
