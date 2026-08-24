-- 0002: per-assignment ordered probes on task events. Without this partial
-- composite the planner serves TASK_STATUS_SQL's correlated subqueries from
-- idx_events_kind_seq (kind=?) and walks the whole task slice once per
-- assignment: p50 318ms on Pi-class hardware, 1.2ms with it (both measured
-- on the Pi; independently reproduced at 42.3ms -> 0.36ms elsewhere).
BEGIN IMMEDIATE;

CREATE INDEX idx_events_task_assignment
  ON events (assignment_id, ingest_seq) WHERE kind = 'task';

PRAGMA user_version = 2;
COMMIT;
