-- 0005: FTS over the channel (Phase-4 walk: "FTS lands with the channel once
-- bodies exist; index only permitted content per §11"). External-content
-- FTS5 over communications.body — §11 falls out STRUCTURALLY: a metadata-
-- capture row stores body NULL (redaction happens at the EMIT door,
-- emit_api), so there are no words to index and the WHEN guard makes it
-- explicit. The lane is append-only (communications never UPDATE/DELETE),
-- so one AFTER INSERT trigger is the whole maintenance story; the backfill
-- covers rows ingested before this migration.
BEGIN IMMEDIATE;

CREATE VIRTUAL TABLE comms_fts USING fts5(
  body,
  content='communications',
  content_rowid='rowid',
  tokenize='porter unicode61'
);

INSERT INTO comms_fts(rowid, body)
  SELECT rowid, body FROM communications WHERE body IS NOT NULL;

CREATE TRIGGER comms_fts_ai AFTER INSERT ON communications
  WHEN new.body IS NOT NULL
BEGIN
  INSERT INTO comms_fts(rowid, body) VALUES (new.rowid, new.body);
END;

PRAGMA user_version = 5;
COMMIT;
