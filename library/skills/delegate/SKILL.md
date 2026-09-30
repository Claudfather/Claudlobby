---
name: delegate
description: "Route fleet work and inspect or manage declared bots through the public CLI."
argument-hint: "[dispatch|status|restart|fleet] <bot> [task description]"
---

# Delegate

Use `fleet-ops` for canonical command syntax and the `dispatch` skill for the
admit → assign → deliver sequence. Task ownership belongs to the fleet; the
selected fleet manager routes it to a declared worker. Keep admission,
assignment, delivery and the worker's acceptance as separate confirmed steps.

For `status BOT`, read `claudlobby --json bot status BOT` and the bot's current
tasks. For `fleet`, read `claudlobby --json fleet status` and
`claudlobby --json fleet inbox`. Unavailable observations are unknown, not idle.

For `restart BOT`, first follow safe-worker-restart and honor all operator
holds. Request `claudlobby --json bot handoff BOT` and require
`data.handoff=saved` before `claudlobby --json bot restart BOT`. Inspect the
returned readiness and native outcome before claiming the bot is back.
Uncertain handoff/control outcomes require inspection, never automatic retries.

Use `claudlobby --json message send --to BOT --text "QUESTION" --request-id UUID`
for an ordinary question. Use linked assignment reports to determine task
progress and completion. Silence alone is not failure or restart authority.
Report verified outcomes to the original human thread.
