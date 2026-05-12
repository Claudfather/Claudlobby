---
title: Dispatch Protocol
---

# Dispatch Protocol

Manager → worker via `tmux send-keys -t <worker> '<task prompt>' Enter`.

Dispatch prompt names: **what** (concrete deliverable), **target** (`--repo <name>`), **constraints** (scope, deadlines, hand-off rules), **reporting expectation** (what `[BOTREPORT]` payload back).

Example: `tmux send-keys -t eng-1 '/lifecycle "Add rate-limit middleware to /api/login" --repo backend' Enter`

After dispatch, monitor: capture the worker's pane after ~2-3 min if you haven't heard back. Workers acknowledge in Telegram, go quiet during work, post completion.

## Preflight: ensure the worker is up under proper supervision

Before dispatch, verify the target session exists. If it doesn't, **always bring it up via `lib/spin-up-bot.sh <bot-dir>`** — never `start-bot.sh` directly. `spin-up-bot.sh` is host-aware: it enrolls the bot as a systemd-user service on Linux or a launchd LaunchAgent on macOS, so the bot is supervised (auto-restart on crash, picked up by the fleet keepalive timer). `start-bot.sh` only spawns a raw tmux session — bots launched that way are invisible to the keepalive scope and won't survive a crash.

```bash
# Idiomatic worker spin-up (idempotent: restarts if already enrolled):
$CLAUDLOBBY_ROOT/lib/spin-up-bot.sh $CLAUDLOBBY_ROOT/local/<fleet>/runtime/bots/<bot>
```

To audit/repair an entire fleet's supervision state in one shot:

```bash
$CLAUDLOBBY_ROOT/lib/reconcile-fleet.sh <fleet>          # report only
$CLAUDLOBBY_ROOT/lib/reconcile-fleet.sh <fleet> --enroll  # enroll any orphans
```

`reconcile-fleet.sh` reports four buckets: healthy (tmux + unit), orphan (tmux but no unit — unsupervised), missing (unit but no tmux — down), unbound (running but not in any fleet.yaml — investigate before killing).

## Preflight: check shared knowledge before dispatch

Before dispatching to a repo, check shared docs for relevant context:

1. Scan `shared/planning/active/INDEX.md` — is there an active plan for the target repo?
2. Scan `shared/knowledge/<repo>/INDEX.md` — are there existing learnings the worker should know?

If relevant docs exist, **include the key context in the dispatch prompt** so the engineer doesn't duplicate work, contradict an in-flight plan, or re-discover something the fleet already knows.

Example: if an active plan covers auth refactoring in `backend`, and you're dispatching a login endpoint task to the same repo, reference the plan in the dispatch: "See shared/planning/active/backend-auth-rework.md — your task aligns with phase 2."

## Manager: INDEX.md monitoring

The orchestrator periodically scans `shared/planning/active/INDEX.md` to:

- **Surface stale plans** — status: active but `updated:` older than 7 days. Ping the owner.
- **Detect conflicts** — two active plans touching the same repo. Flag for human resolution.
- **Catch forgotten transitions** — a completed task whose plan still says status: active. Nudge the owner to update status and run `/index`.
