-- 0001_kernel -- the script OWNS its transaction (round-2 F2): executescript
-- runs in autocommit; BEGIN IMMEDIATE serializes concurrent first emitters,
-- and the version stamp commits WITH the DDL or not at all.
BEGIN IMMEDIATE;
-- 0001_kernel: ingest ledger, identity registry, constructs + events stream.
-- Envelope columns are identical on every family table by design (F16):
--   ingest_seq, event_id, schema_version, occurred_at, observed_at,
--   ingested_at, host_uid, fleet_uid, emitter, source_ref,
--   correlation_id, causation_id, trace_id, span_id
-- Ordering authority is ingest_ledger.ingest_seq (AUTOINCREMENT), copied
-- into each family row in the same transaction. rowid is never a cursor.

CREATE TABLE ingest_ledger (
    ingest_seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    family      TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

-- Registry, not observed lane: last_seen/provisional may UPDATE (the one
-- sanctioned mutation — spec §5). provisional=1 marks a lazily-minted
-- identity awaiting confirmation by a generate-time registry pass (Phase 2+);
-- doctor surfaces provisional actors so a typo'd alias cannot silently
-- become a phantom colleague.
CREATE TABLE identity_registry (
    uid         TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN
                  ('host','fleet','actor','bot_instance','session','vault','project','library_item')),
    alias       TEXT NOT NULL,
    parent_uid  TEXT,
    provisional INTEGER NOT NULL DEFAULT 1,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    UNIQUE (kind, alias)
);

CREATE TABLE communications (
    ingest_seq        INTEGER NOT NULL UNIQUE,
    event_id          TEXT NOT NULL UNIQUE,
    schema_version    TEXT NOT NULL,
    occurred_at       TEXT NOT NULL,
    observed_at       TEXT,
    ingested_at       TEXT NOT NULL,
    host_uid          TEXT NOT NULL,
    fleet_uid         TEXT,
    emitter           TEXT NOT NULL,
    source_ref        TEXT,
    correlation_id    TEXT,
    causation_id      TEXT,
    trace_id          TEXT,
    span_id           TEXT,
    origin          TEXT NOT NULL DEFAULT 'live' CHECK (origin IN ('live','legacy')),
    import_batch    TEXT,
    confidence      TEXT,
    msg_id            TEXT PRIMARY KEY NOT NULL,   -- the communication id
    sender_uid        TEXT NOT NULL,
    sender_alias      TEXT NOT NULL,
    sender_session_uid TEXT,
    recipient_uid     TEXT,
    recipient_alias   TEXT,
    recipient_raw     TEXT,
    message_class     TEXT NOT NULL CHECK (message_class IN
        ('task_request','report','question','answer','alert','notice',
         'briefing','nudge','acknowledgement','chat','config_change',
         'raw_control')),
    command_type      TEXT CHECK (command_type IN
        ('task','cancel','compact','restart','query')),
    work_item_id      TEXT,
    assignment_id   TEXT,
    workstream_id     TEXT,
    deliberation_id   TEXT,
    reply_to_msg_id   TEXT,
    supersedes_msg_id TEXT,
    body              TEXT,
    body_bytes        INTEGER NOT NULL DEFAULT 0,
    body_sha256       TEXT,
    truncated         INTEGER NOT NULL DEFAULT 0,
    privacy           TEXT NOT NULL CHECK (privacy IN ('metadata','preview','full')),
    idempotency_key   TEXT,
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);
CREATE INDEX idx_intents_msg       ON communications (msg_id);
CREATE INDEX idx_intents_sender    ON communications (sender_uid, ingest_seq);
CREATE INDEX idx_intents_work_item ON communications (work_item_id)
    WHERE work_item_id IS NOT NULL;


CREATE TABLE work_items (
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
    work_item_id    TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    created_by_uid  TEXT NOT NULL,
    workstream_id   TEXT,
    repo            TEXT,
    project_key     TEXT,
    body            TEXT,
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);

CREATE TABLE assignments (
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
    assignment_id TEXT NOT NULL UNIQUE,
    work_item_id    TEXT NOT NULL,
    assignee_uid    TEXT NOT NULL,
    assigned_by_uid TEXT NOT NULL,
    expected_by     TEXT,
    dispatch_msg_id TEXT,
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);
CREATE INDEX idx_assignments_item ON assignments (work_item_id);
CREATE UNIQUE INDEX idx_assignments_dispatch ON assignments (dispatch_msg_id)
    WHERE dispatch_msg_id IS NOT NULL;

-- workstreams construct pulled into 0001 (round-5 F7): the events stream
-- already declares the workstream KIND here, and the workstream-status
-- reducer (a required §14 bench query) needs the construct to exist. The
-- DOOR and Pydantic contract remain Phase 2b — Phase 1 rows arrive only
-- from the bench seed and tests, via direct SQL.
CREATE TABLE workstreams (
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
    workstream_id   TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    goal            TEXT,
    owner_uid       TEXT,
    opened_by_uid   TEXT NOT NULL,
    project_key     TEXT,
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);
CREATE INDEX idx_assignments_assignee ON assignments (assignee_uid, ingest_seq);

-- The ONE events stream (F16-v2.1): everything that HAPPENS to a construct.
-- The CHECK is NULL-safe require-AND-forbid per kind (round-2 F3: SQLite
-- passes NULL CHECK results, so every branch requires its columns NOT NULL
-- and forbids off-kind columns IS NULL). Kinds/vocabularies mirror
-- contracts.KIND_MANIFEST — the INSERT-matrix test executes both sides.
CREATE TABLE events (
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
            AND event IS NOT NULL AND event IN ('send_attempted','carrier_accepted','pane_submitted',
                          'failed','unknown','recipient_acknowledged',
                          'duplicate_suppressed')
            AND msg_id IS NOT NULL AND carrier IS NOT NULL
            AND carrier IN ('tmux','telegram-tgpost','telegram-bridge')
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
                          'expired')
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
CREATE INDEX idx_events_kind_seq ON events (kind, ingest_seq);
CREATE INDEX idx_events_msg ON events (msg_id, ingest_seq) WHERE kind = 'transmission';
CREATE INDEX idx_events_item ON events (work_item_id, ingest_seq) WHERE kind = 'task';
CREATE INDEX idx_events_ws ON events (workstream_id, ingest_seq) WHERE kind = 'workstream';
CREATE INDEX idx_events_subject ON events (subject_uid, ingest_seq) WHERE kind = 'system';
CREATE INDEX idx_events_carrier_ref ON events (carrier_ref) WHERE carrier_ref IS NOT NULL;
CREATE INDEX idx_events_assignment ON events (assignment_id) WHERE assignment_id IS NOT NULL;

PRAGMA user_version = 1;
COMMIT;

