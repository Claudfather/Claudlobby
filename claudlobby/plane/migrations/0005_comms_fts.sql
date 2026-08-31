-- 0005: FTS over the channel (Phase-4 walk: "FTS lands with the channel once
-- bodies exist; index only permitted content per §11"). External-content
-- FTS5 over communications.body — §11 falls out STRUCTURALLY: a metadata-
-- capture row stores body NULL (redaction happens at the EMIT door,
-- emit_api), so there are no words to index and the WHEN guard makes it
-- explicit. The lane is append-only (communications never UPDATE/DELETE),
-- so one AFTER INSERT trigger is the whole maintenance story; the backfill
-- covers rows ingested before this migration.
--
-- content_rowid is INGEST_SEQ, deliberately never the implicit rowid: the
-- table's PK is TEXT (msg_id), so its rowid is unowned — a delete+insert
-- REUSES it (probed: a search returned the wrong message verbatim), and
-- VACUUM is documented-permitted to renumber it on any build. ingest_seq
-- is ledger-AUTOINCREMENT-derived: never reused, never renumbered — the
-- schema's own ordering authority as the join key. Retention pruning
-- (future DELETEs) must add a delete trigger + rebuild; that lands with
-- retention, not speculatively here.
BEGIN IMMEDIATE;

CREATE VIRTUAL TABLE comms_fts USING fts5(
  body,
  content='communications',
  content_rowid='ingest_seq',
  tokenize='porter unicode61'
);

INSERT INTO comms_fts(rowid, body)
  SELECT ingest_seq, body FROM communications WHERE body IS NOT NULL;

CREATE TRIGGER comms_fts_ai AFTER INSERT ON communications
  WHEN new.body IS NOT NULL
BEGIN
  INSERT INTO comms_fts(rowid, body) VALUES (new.ingest_seq, new.body);
END;

PRAGMA user_version = 5;
COMMIT;
