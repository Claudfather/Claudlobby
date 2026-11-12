# Worker lifecycle reference

Reference that the `worker-lifecycle` protocol points to: what the manager, the framework and the fleet pulse do around a worker. The protocol keeps every rule; this file adds none.

## Inbound

The manager admits fleet work, creates an assignment, then delivers it.

## Outbound

Telegram is prose; the CLI records
structured facts.

## You are monitored

Your tool-call activity is observed by the fleet pulse. If your session is alive and not at an idle prompt but you make **no tool call for a long stretch** (the configured `activity_stuck` threshold — e.g. a main thread that stalled after a subagent returned), the pulse flags `activity_stuck` and notifies your manager. This is a safety net, not surveillance: it exists so a silent hang gets noticed in minutes instead of hours.

In the last sentence below, "the discipline above" means the protocol's rule to keep work committed and report at lifecycle boundaries.

A plain restart continues your exact conversation. A fresh session (`bot restart BOT --fresh`) preserves context at **best-achievable fidelity, not zero loss**: it starts empty and resumes from your saved `session.md` handoff, but that handoff is a *summary*, not the live conversation. Committed work and reported state are what survive a fresh session intact — so the discipline above is exactly what makes the resume reliable.

## Step 1

On the leading `set +H; `:

The framework's pane delivery adds it to every message except one that starts with a command word and has no `!`, which means a message that begins with `/`, such as a file path, is not run as a slash command.
