-- Task boards discover permission candidates before canonical reduction. Avoid
-- scanning a fleet's entire system history when permission requests are rare.
-- Keep JSON validation in the reader, not in an index over untrusted detail.
BEGIN IMMEDIATE;

CREATE INDEX idx_events_permission_requests
    ON events (fleet_uid, emitter, host_uid, ingest_seq)
    WHERE kind='system' AND event='permission_requested' AND detail_truncated=0;

-- Keep the existing actor-or-subject acknowledgement query and ingest order.
-- Equality keys let the planner prefer this small subset over all system rows.
CREATE INDEX idx_events_reports_acked
    ON events (kind, event, ingest_seq DESC)
    WHERE kind='system' AND event='reports_acked';

PRAGMA user_version = 14;
COMMIT;
