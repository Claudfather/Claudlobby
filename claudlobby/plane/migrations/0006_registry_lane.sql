-- 0006: the registry lane (Phase 2b — spec §9b FINAL field lists, §18).
-- Two construct tables, F16 pattern: full envelope + ledger FK.
--
-- registry_snapshots: entity keyframes × observed change. The SCD partition
-- is (host_uid, entity_type, entity_uid); consecutive rows in a partition
-- ARE the diff view. tombstone is the ONLY stored operation (deletion is
-- the one underivable fact); payload/payload_hash are null iff tombstone
-- (CHECK-enforced, NULL-safe). payload_hash is the write gate: ingest
-- suppresses a row whose hash equals the partition's latest — partial
-- scans self-heal, and provenance survives via declaration events, which
-- are never hash-gated. Bot keyframes key on the INSTANCE uid (§9b).
--
-- metric_samples: the volume/retention lane (F20) — subject pair + a
-- registry-governed metric name + JSON value + the emitter's judgment.
-- No alias column BY DESIGN (rows are aggregated, never read singly;
-- display joins identity_registry once). 30d retention arrives with the
-- retention policy, not here.
BEGIN IMMEDIATE;

CREATE TABLE registry_snapshots (
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
    origin            TEXT NOT NULL DEFAULT 'live'
                        CHECK (origin IN ('live','legacy')),
    import_batch      TEXT,
    confidence        TEXT,
    entity_type       TEXT NOT NULL CHECK (entity_type IN
                        ('host','vault','fleet','bot','project','library_item')),
    entity_uid        TEXT NOT NULL,
    entity_alias      TEXT NOT NULL,
    tombstone         INTEGER NOT NULL DEFAULT 0 CHECK (tombstone IN (0,1)),
    payload           TEXT,
    payload_hash      TEXT,
    cause             TEXT NOT NULL CHECK (cause IN
                        ('generate','probe','equip','migration')),
    scan_id           TEXT NOT NULL,
    vault_rev         TEXT,
    CHECK ((tombstone = 1 AND payload IS NULL AND payload_hash IS NULL)
        OR (tombstone = 0 AND payload IS NOT NULL AND payload_hash IS NOT NULL)),
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);

-- the partition-latest lookup (hash gate) and the SCD chain read
CREATE INDEX idx_registry_partition
  ON registry_snapshots (host_uid, entity_type, entity_uid, ingest_seq);
CREATE INDEX idx_registry_scan ON registry_snapshots (scan_id);

CREATE TABLE metric_samples (
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
    origin            TEXT NOT NULL DEFAULT 'live'
                        CHECK (origin IN ('live','legacy')),
    import_batch      TEXT,
    confidence        TEXT,
    subject_kind      TEXT NOT NULL CHECK (subject_kind IN
                        ('host','vault','fleet','actor','bot_instance','session')),
    subject_uid       TEXT NOT NULL,
    metric            TEXT NOT NULL,
    value             TEXT NOT NULL,
    status            TEXT CHECK (status IN ('ok','warn','alert')),
    FOREIGN KEY (ingest_seq) REFERENCES ingest_ledger (ingest_seq)
);

CREATE INDEX idx_samples_subject
  ON metric_samples (subject_uid, metric, ingest_seq);

PRAGMA user_version = 6;
COMMIT;
