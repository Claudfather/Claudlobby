### Changed — `assignment show` prints each event once (#2273)

`claudlobby --json assignment show` printed the assignment and then its whole task, which carried the assignment again. So each event of the assignment appeared three times, four while it was the task's current assignment, and a closing event up to six times. Each event now appears once, in `data.events`, in ingest order: the task's events, each of its assignments' events and every closing event. Every other place that held an event names it by ID:

- `data.assignment`, and each record in `data.task.assignments`: `history` and `terminal_event` are gone, and `terminal_event_id` names the closing event (null while the assignment is open). An assignment's history is the entries of `data.events` with its `assignment_id`.
- `data.task`: `history`, `terminal_event` and `current_assignment` are gone, and `terminal_event_id` and `current_assignment_id` name them. The task's history is the entries of `data.events` with its `task_id`.
- Schema-1's top-level keys are unchanged, as are every other field, the plain-text lines, `task show` and `task list`.
- A jq read of a removed key now gets null, which is also what an open assignment's `terminal_event` read before. To tell open from closed, read `state` or `terminal_event_id`.

On three of one worker's closed assignments (13, 10 and 9 events), a read is 60% to 63% smaller in tokens: 21,246 to 7,813 on the largest, counted exactly with Claude Code's `/context`. Whether a reader acts as well on the shorter read is not measured.
