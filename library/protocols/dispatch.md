---
title: Dispatch Protocol
---

# Dispatch Protocol

Manager → worker via `tmux send-keys -t <worker> '<task prompt>' Enter`.

Dispatch prompt names: **what** (concrete deliverable), **target** (`--repo <name>`), **constraints** (scope, deadlines, hand-off rules), **reporting expectation** (what `[BOTREPORT]` payload back).

**Use two-step send-keys** — split text and Enter into separate calls with a short pause to prevent a race where Claude Code's TUI swallows keystrokes during render:

```bash
tmux send-keys -t <worker> "set +H; <task prompt>"
sleep 0.3
tmux send-keys -t <worker> Enter
```

Example:

```bash
tmux send-keys -t eng-1 "set +H; /lifecycle 'Add rate-limit middleware' --repo backend"
sleep 0.3
tmux send-keys -t eng-1 Enter
```

After dispatch, monitor: capture the worker's pane after ~2-3 min if you haven't heard back. Workers acknowledge in Telegram, go quiet during work, post completion.

## Tracked dispatch & the overdue watchdog

For tasks you want tracked, dispatch via `lib/dispatch-task.sh <worker> <task…>` instead of raw `send-keys`. It records the dispatch (with a deadline from `OBSERVABILITY_DISPATCH_DEADLINE`, or `--deadline-min N`) to `state/dispatch-log.jsonl`, then sends. The fleet pulse then watches it: if the deadline passes with no terminal `[BOTREPORT]` (completed/failed/blocked), it emits `overdue_dispatch` and pushes a debounced `[FLEET-PULSE]` note into **your** session. So you don't have to remember to poll — an unanswered task surfaces itself.

When you get an `overdue_dispatch` alert: check the worker (cross-reference `activity_stuck` — it may be hung, see `fleet-observability`). Then recover it, re-dispatch/reassign if it's wedged or mis-scoped, or escalate to the human. The watchdog tells you *something is overdue*; the call on what to do is yours. A worker's terminal report closes the dispatch automatically — no manual bookkeeping.

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
