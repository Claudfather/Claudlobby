-- 0010: the task loop's two HUMAN acts join the task vocabulary (chunk M-A,
-- #1481) -- `escalated` (a manager raises a task for human guidance;
-- NON-terminal by ruling, the task stays open while the human decides) and
-- `nudged` (the operator asks the manager to act on one row). The events
-- table CHECK enumerates the task tokens, and SQLite cannot ALTER a CHECK,
-- so this is the documented 12-step rebuild: a new table with the widened
-- list, the rows copied, the old one dropped, every index recreated.
--
-- IT IS A FULL COPY OF `events`, said plainly because it is the cost: the
-- first door to open the plane after this ships pays it once, holding the
-- write lock, and needs the table's size again in free space while it runs.
-- The alternative (PRAGMA writable_schema, an O(1) edit of sqlite_master) is
-- documented for expert use and corrupts the schema outright when the SQL
-- string is wrong -- a worse failure than a slow start, on the one database
-- the estate keeps its history in.
--
-- foreign_keys is toggled OUTSIDE the transaction (it is a no-op inside one),
-- per the same procedure: `events` is a CHILD of ingest_ledger and has no
-- children of its own, so nothing cascades either way, but DROP TABLE under
-- enforcement is exactly the step the procedure guards.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TABLE events_m10 (
    ingest_seq      INTEGER NOT NULL UNIQUE,
    event_id        TEXT NOT NULL UNIQUE,
    schema_version  TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    observed_at     TEXT,
    ingested_at     TEXT NOT NULL,
    host_uid        TEXT NOT NULL,
    fleet_uid       TEXT,
    emitter         TEXT NOT NULL,
    source_ref      TEXT,
    correlation_id  TEXT,
    causation_id    TEXT,
    trace_id        TEXT,
    span_id         TEXT,
    origin          TEXT NOT NULL DEFAULT 'live' CHECK (origin IN ('live','legacy')),
    import_batch    TEXT,
    confidence      TEXT,
    kind            TEXT NOT NULL CHECK (kind IN
                      ('transmission','task','workstream','system','declaration')),
    event           TEXT,
    carrier         TEXT,
    attempt_no      INTEGER,
    carrier_ref     TEXT,
    msg_id          TEXT,
    work_item_id    TEXT,
    assignment_id   TEXT,
    workstream_id   TEXT,
    subject_kind    TEXT,
    subject_uid     TEXT,
    subject_alias   TEXT,
    actor_uid       TEXT,
    session_uid     TEXT,
    severity        TEXT,
    deadline        TEXT,
    successor_id    TEXT,
    renewed_until   TEXT,
    detail          TEXT,
    detail_truncated INTEGER NOT NULL DEFAULT 0,
    CHECK (
        (kind = 'transmission'
            AND event IS NOT NULL AND event IN ('send_attempted','carrier_accepted','carrier_queued',
                          'pane_submitted',
                          'failed','unknown','recipient_acknowledged',
                          'duplicate_suppressed')
            AND msg_id IS NOT NULL AND carrier IS NOT NULL
            AND carrier IN ('tmux','telegram-tgpost','telegram-bridge')
            AND (event NOT IN ('pane_submitted','carrier_queued')
                 OR carrier = 'tmux')
            AND (event <> 'carrier_accepted'
                 OR carrier IN ('telegram-tgpost','telegram-bridge'))
            AND attempt_no IS NOT NULL
            AND work_item_id IS NULL AND assignment_id IS NULL
            AND workstream_id IS NULL AND subject_kind IS NULL
            AND subject_uid IS NULL AND subject_alias IS NULL
            AND severity IS NULL AND deadline IS NULL
            AND successor_id IS NULL AND renewed_until IS NULL
            AND actor_uid IS NULL AND session_uid IS NULL)
     OR (kind = 'task'
            AND event IS NOT NULL AND event IN ('dispatch_intended','transmission_failed',
                          'dispatch_submitted','accepted','rejected','progress',
                          'blocked_waiting','returned_blocked','resumed',
                          'completed','failed','cancelled','deadline_changed',
                          'superseded','reassigned','retry_created',
                          'orphaned_by_session_loss','recovered_after_restart',
                          'expired','supplied_id_not_open',
                          'escalated','nudged')
            AND work_item_id IS NOT NULL
            AND msg_id IS NULL AND carrier IS NULL AND attempt_no IS NULL
            AND carrier_ref IS NULL AND workstream_id IS NULL
            AND subject_kind IS NULL AND subject_uid IS NULL
            AND subject_alias IS NULL AND severity IS NULL
            AND renewed_until IS NULL)
     OR (kind = 'workstream'
            AND event IS NOT NULL AND event IN ('progressed','renewed','blocked','unblocked','closed',
                          'archived','plan_linked','plan_unlinked')
            AND workstream_id IS NOT NULL
            AND msg_id IS NULL AND carrier IS NULL AND attempt_no IS NULL
            AND carrier_ref IS NULL AND work_item_id IS NULL
            AND assignment_id IS NULL AND subject_kind IS NULL
            AND subject_uid IS NULL AND subject_alias IS NULL
            AND severity IS NULL AND deadline IS NULL AND successor_id IS NULL
            AND session_uid IS NULL)
     OR (kind = 'system'
            AND event IS NOT NULL
            AND msg_id IS NULL AND carrier IS NULL AND attempt_no IS NULL
            AND carrier_ref IS NULL AND work_item_id IS NULL
            AND assignment_id IS NULL AND workstream_id IS NULL
            AND deadline IS NULL AND successor_id IS NULL
            AND renewed_until IS NULL
            AND actor_uid IS NULL AND session_uid IS NULL
            AND (severity IS NULL OR severity IN ('critical','notice'))
            AND ((subject_uid IS NULL AND subject_kind IS NULL
                  AND subject_alias IS NULL)
                 OR (subject_uid IS NOT NULL AND subject_kind IS NOT NULL
                     AND subject_kind IN ('host','vault','fleet','actor',
                                          'bot_instance','session'))))
     OR (kind = 'declaration'
            AND event IS NOT NULL AND event IN ('revision_seen','scan_completed')
            AND subject_kind IS NOT NULL
            AND subject_kind IN ('vault','host') AND subject_uid IS NOT NULL
            AND msg_id IS NULL AND carrier IS NULL AND attempt_no IS NULL
            AND carrier_ref IS NULL AND work_item_id IS NULL
            AND assignment_id IS NULL AND workstream_id IS NULL
            AND severity IS NULL AND deadline IS NULL
            AND successor_id IS NULL AND renewed_until IS NULL
            AND actor_uid IS NULL AND session_uid IS NULL)
    ),
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);

INSERT INTO events_m10 (
    ingest_seq,
    event_id,
    schema_version,
    occurred_at,
    observed_at,
    ingested_at,
    host_uid,
    fleet_uid,
    emitter,
    source_ref,
    correlation_id,
    causation_id,
    trace_id,
    span_id,
    origin,
    import_batch,
    confidence,
    kind,
    event,
    carrier,
    attempt_no,
    carrier_ref,
    msg_id,
    work_item_id,
    assignment_id,
    workstream_id,
    subject_kind,
    subject_uid,
    subject_alias,
    actor_uid,
    session_uid,
    severity,
    deadline,
    successor_id,
    renewed_until,
    detail,
    detail_truncated
) SELECT
    ingest_seq,
    event_id,
    schema_version,
    occurred_at,
    observed_at,
    ingested_at,
    host_uid,
    fleet_uid,
    emitter,
    source_ref,
    correlation_id,
    causation_id,
    trace_id,
    span_id,
    origin,
    import_batch,
    confidence,
    kind,
    event,
    carrier,
    attempt_no,
    carrier_ref,
    msg_id,
    work_item_id,
    assignment_id,
    workstream_id,
    subject_kind,
    subject_uid,
    subject_alias,
    actor_uid,
    session_uid,
    severity,
    deadline,
    successor_id,
    renewed_until,
    detail,
    detail_truncated
FROM events;

DROP TABLE events;
ALTER TABLE events_m10 RENAME TO events;

CREATE INDEX idx_events_actor ON events (actor_uid, occurred_at) WHERE actor_uid IS NOT NULL;
CREATE INDEX idx_events_assignment ON events (assignment_id) WHERE assignment_id IS NOT NULL;
CREATE INDEX idx_events_carrier_ref ON events (carrier_ref) WHERE carrier_ref IS NOT NULL;
CREATE INDEX idx_events_fleet_system ON events (fleet_uid, occurred_at) WHERE kind = 'system';
CREATE INDEX idx_events_item ON events (work_item_id, ingest_seq) WHERE kind = 'task';
CREATE INDEX idx_events_kind_seq ON events (kind, ingest_seq);
CREATE INDEX idx_events_msg ON events (msg_id, ingest_seq) WHERE kind = 'transmission';
CREATE INDEX idx_events_subject ON events (subject_uid, ingest_seq) WHERE kind = 'system';
CREATE INDEX idx_events_task_assignment
  ON events (assignment_id, ingest_seq) WHERE kind = 'task';
CREATE INDEX idx_events_ws ON events (workstream_id, ingest_seq) WHERE kind = 'workstream';

PRAGMA user_version = 10;
COMMIT;
PRAGMA foreign_keys = ON;
