### Added — the workstream registry reads its tasks (#1053)

Every reader of the workstream registry (`workstream show` and `list`, the brief, the manager's `workstream` verbs, the check-in and the inbox) now sees each workstream's tasks:
- `task_ids` lists them in admission order and `open_task_ids` the open ones, and a task's newest row counts as its workstream's progress;
- a workstream that holds a task and no open one reads closed: `done` if any of its tasks completed, else `abandoned`, with `closed_ts` and `closed_by: task:<ID>` taken from the row that ended its last open task;
- the registry reads the same terminal facts as admit's join check, so the two agree on which workstreams are closed;
- a recorded `workstream close` wins, and its `closed_by` names who closed it.

A closed workstream refuses `progress`, `renew`, `block`, `unblock` and `close`, and `workstream prune` archives it. That holds for a blocked workstream too: when its last open task ends, it reads closed, and it leaves the blocked waits. So a workstream left waiting with no open task reads closed after the update; a wait that still stands needs an open task, and admitting one opens a workstream of its own.

In the brief, a workstream that holds an open task is never `stalled` or `lease_expired`: its tasks carry their own deadline watch, and its entry names them in `open_task_ids`. A workstream that holds no task keeps its lease. Each of the brief's workstream lists (`active`, `stalled`, `blocked`) keeps its oldest 50 rows, and a cut is labeled in `degraded[]` with the count and `claudlobby --json workstream list`. The text view counts its "showing the oldest 10" line against the whole list.
