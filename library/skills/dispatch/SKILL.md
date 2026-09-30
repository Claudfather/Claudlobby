---
name: dispatch
description: "Admit fleet work, assign a declared worker, and deliver the recorded assignment through the public CLI."
argument-hint: "<bot> <task description> [--repo <repo>]"
---

# Dispatch

Use the universally composed `fleet-ops` skill for the full contract. Only the
selected fleet manager assigns and delivers work. Read `claudlobby --json fleet
status` and `claudlobby --json task list` before choosing a declared worker;
unavailable evidence does not mean idle.

1. Admit the work with `claudlobby --json task admit --title "TASK" --request-id UUID`.
   Retain the returned `data.task_id`. Put longer context in a UTF-8 file and
   supply `--body-file FILE`; add `--repo OWNER/REPO` when appropriate.
2. Assign it with `claudlobby --json task assign TASK_ID --bot BOT --request-id UUID`.
   Retain `data.assignment_id`. This records ownership without sending a prompt.
3. Write the bounded worker instructions to a UTF-8 file, then run
   `claudlobby --json assignment deliver ASSIGNMENT_ID --file FILE --request-id UUID`.
4. Inspect the result and its message receipt before claiming delivery. The
   assigned bot accepts the canonical assignment before acting and records its
   linked progress or terminal report through the assignment commands.

Use a separate UUID for each distinct operation; retain it for inspection or
replay of that same operation. Unknown delivery never authorizes an automatic
resend. An ordinary question uses `message send`, not a new task assignment.
Summarize the verified outcome in the original human thread. A busy, silent or
unavailable worker is not permission to restart it; respect explicit holds and
use the safe-worker-restart protocol when recovery is needed.
