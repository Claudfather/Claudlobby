### Changed — `task show` prints each event once (#2771)

`claudlobby --json task show` printed the task's history, then each assignment with its own history and closing event, then the current assignment again. So each event appeared two to four times. Each event now appears once, in `data.events`, in ingest order: the task's events, each of its assignments' events and every closing event. This is the shape `assignment show` has printed since #2768, so the two commands now print a task the same way. Every other place that held an event names it by ID:

- `data.task`: `history`, `terminal_event` and `current_assignment` are gone, and `terminal_event_id` and `current_assignment_id` name them. The task's history is the entries of `data.events` with its `task_id`; the current assignment's record is its entry in `data.task.assignments`.
- Each record in `data.task.assignments`: `history` and `terminal_event` are gone, and `terminal_event_id` names the closing event (null while the assignment is open). An assignment's history is the entries of `data.events` with its `assignment_id`.
- Schema-1's top-level keys are unchanged, as are every other field, `resolved_from_prefix`, the plain-text lines, `task list` and `fleet inbox`.
- A jq read of a removed key now gets null. That is also what an open task's `terminal_event` and a task with no current assignment's `current_assignment` read before, so to tell them apart, read `state`, `terminal_event_id` or `current_assignment_id`.

On one worker's three tasks with the most events (17, 15 and 11), a read is 47% to 49% smaller in tokens: 18,932 to 9,684 on the largest, counted exactly with Claude Code's `/context`. Whether a reader acts as well on the shorter read is not measured.
