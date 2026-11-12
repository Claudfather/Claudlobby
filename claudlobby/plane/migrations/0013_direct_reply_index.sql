-- wait_for_reply selects the first direct reply by parent message ID. The
-- sender index scans unrelated messages from a busy peer on every poll; this
-- partial index seeks only rows that can be replies, in ingest order.
BEGIN IMMEDIATE;

CREATE INDEX idx_intents_reply ON communications (reply_to_msg_id, ingest_seq)
    WHERE reply_to_msg_id IS NOT NULL;

PRAGMA user_version = 13;
COMMIT;
