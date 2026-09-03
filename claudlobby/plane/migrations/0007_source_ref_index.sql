-- 0007: the cutover's first door reads the plane BY LEGACY TASK ID — every
-- dispatch stamps source_ref = "dispatch-log:<task_id>" on its work_item and
-- assignment envelopes, and report-back / --supersedes recover the plane ids
-- through it instead of grepping the JSONL ledger. Index the join column so
-- the lookup is a seek, not a scan of the whole assignments table.
BEGIN IMMEDIATE;

CREATE INDEX idx_assignments_source_ref ON assignments (source_ref);

PRAGMA user_version = 7;
COMMIT;
