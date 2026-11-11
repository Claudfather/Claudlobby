### Changed — task rows and messages about a task name its workstream (#1053)

Each task verb's row now names the task's workstream at the moment of the act, in the row's `detail` as `workstream_id`: accept, every report (progress, block, return, complete, fail), withdraw, escalate, reschedule, nudge and reassign. After a `task move`, a row names the workstream the task moved to. A workstream's close names itself on each withdraw row it records. A communication about one task names it in its own `workstream_id` column: a report, an assignment delivery, a nudge's ask to the manager and a comment on a task. A task in no workstream, admitted before every task had one and not yet filed by `migration workstream-backfill`, names none, and so does a report that names no task. `task show` prints each row's `detail`, the field included. The field is metadata, so a metadata capture keeps it. Nothing migrates: rows recorded before the update name none.

A delivery retried with its request ID still matches the communication it first recorded when the task has moved since, or when a release before this one recorded it. The workstream is the only field that retry check relaxes.

### Changed — every workstream verb's JSON result names `filed_tasks` (#1053)

Since `migration workstream-backfill` (#2869), the JSON result of each `workstream` verb that records (`open`, `progress`, `renew`, `block`, `unblock`, `close`, `prune`) and of the backfill's `--apply` carries `filed_tasks`: each task the backfill filed, with the workstream it left (none, or the closed one) and the one it was filed into. It is empty for every verb but the backfill. The field is additive; the backfill's own entry did not name it.
