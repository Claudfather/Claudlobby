-- 0004: the recipient arm of the fleet ROOM query, made indexable. 0003
-- indexed the sender arm (fleet_uid), but the room predicate matches sender
-- OR recipient, and an OR across two columns forced the planner back to a
-- full reverse scan — 0003 shipped dead-on-arrival against the only query
-- it serves (EXPLAIN-confirmed in the #1393 gauntlet; 78ms quiet room /
-- 123ms nonexistent fleet at 200k rows, vs 0.05ms fixed). The fix is a
-- VIRTUAL generated column parsing the fleet out of recipient_alias
-- ('bot:<fleet>/<name>'), a partial index over it, and the query rewritten
-- as a UNION of two EQUALITY arms (view.py) — each arm SEARCHes its index
-- and early-exits on LIMIT. Deliberately NOT a plain (recipient_alias,
-- ingest_seq) index with a LIKE-prefix arm: the range spans many alias
-- values so that index cannot yield ingest_seq order, and the arm
-- temp-B-tree-sorts its whole matching set — measured WORSE (283ms busy
-- room) than the scan it replaces. Equality also retires the LIKE
-- metacharacter class (a fleet named 'en_' absorbing 'eng''s room).
BEGIN IMMEDIATE;

ALTER TABLE communications ADD COLUMN recipient_fleet TEXT
  GENERATED ALWAYS AS (
    CASE WHEN recipient_alias LIKE 'bot:%/%'
         THEN substr(substr(recipient_alias, 5), 1,
                     instr(substr(recipient_alias, 5), '/') - 1)
    END) VIRTUAL;

CREATE INDEX idx_intents_recipient_fleet
  ON communications (recipient_fleet, ingest_seq)
  WHERE recipient_fleet IS NOT NULL;

PRAGMA user_version = 4;
COMMIT;
