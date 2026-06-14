---
title: Fleet Observability Protocol
description: Manager protocol for consuming bot-local observability events and making routing/restart decisions
---

# Fleet Observability Protocol

Pull-based observability for fleet managers. Two writers produce events; managers consume them at natural decision points.

## Event Sources

| Writer | Script | Runs when | Source field |
|--------|--------|-----------|--------------|
| Bot vitals | `lib/bot-vitals.sh` | Every tool call (Claude Code hook) | `vitals` |
| Fleet pulse | `lib/fleet-pulse.sh` | Cron (every 5 min) | `pulse` |
| Keepalive idle marker | `lib/keepalive.sh` | Every keepalive run (60s timer) | Marker file (`data/.idle`), not JSONL |

Both write the same JSONL schema to the same bot-local directory. Managers read one path per bot regardless of writer. The idle marker is a special case: keepalive touches `data/.idle` when it classifies a pane as IDLE and removes it on BUSY. Fleet-pulse compares `.idle` mtime vs `.last-tool-call` mtime to determine idle state without parsing panes.

## Where to Read

Each bot's events live at:

```
<bot-dir>/data/events/fleet-YYYY-MM-DD.jsonl
```

All paths are derivable from fleet.yaml. For a fleet named `<fleet>`:

```
$CLAUDLOBBY_ROOT/local/<fleet>/runtime/bots/<bot>/data/events/fleet-$(date +%Y-%m-%d).jsonl
```

Use `resolve_bots_dir` from `lib-common.sh` to find the bots directory, then iterate.

## Event Schema

```json
{"ts": "...", "bot": "...", "type": "...", "source": "vitals|pulse|audit", "data": {...}}
```

- **ts** — ISO 8601 timestamp
- **bot** — bot identifier
- **type** — event classification (open-ended, match on what you care about)
- **source** — `vitals` (bot-emitted), `pulse` (external check), or `audit` (rolling code-audit sweep)
- **data** — type-specific payload (open object)

## When to Read

Read bot event logs at these natural decision points — not continuously, not on interrupt:

| Moment | Why |
|--------|-----|
| **Before dispatch** | Check target worker health before sending work |
| **Review routing** | Pick the healthiest available reviewer |
| **Idle / between tasks** | Proactive fleet health scan |
| **On BOTREPORT receipt** | Cross-reference report with recent events for context |

## Decision Table

| Event type | Source | Manager action |
|------------|--------|---------------|
| `activity_stuck` | pulse | Bot has made **no tool call** for longer than its threshold AND keepalive has not classified it as idle (no recent `data/.idle` marker). Uses marker-file mtime comparison, not pane regex. Investigate; restart only if `safe-worker-restart` guards pass. |
| `overdue_dispatch` | pulse | A task you dispatched to this bot passed its deadline with no terminal `[BOTREPORT]`. Check the bot (cross-reference `activity_stuck`): if hung, recover it; if mis-scoped or wedged, re-dispatch or reassign; if it needs a human, escalate. Don't silently wait. |
| `pane_stuck` (>5 min) | pulse | Investigate pane content, restart if confirmed stuck. Note: a live spinner animates the pane, so an animated-but-hung bot shows up as `activity_stuck`, not `pane_stuck`. |
| `service_down` | pulse | Re-enroll via `lib/spin-up-bot.sh <bot-dir>` |
| `session_missing` | pulse | Re-enroll via `lib/spin-up-bot.sh <bot-dir>` |
| `wip_uncommitted` | pulse | Do NOT restart — task is in flight. Check for staleness instead. |
| `session_event` | vitals | Informational — log awareness of session lifecycle |
| `audit_selected` | audit | Informational — the rolling sweep picked this repo as stalest. |
| `audit_dispatched` | audit | Informational — the audit was dispatched into the owner bot's session. |
| `audit_deferred` | audit | Owner was busy; the sweep skipped this tick and retries next run. No action. |
| `sweep_repo_unreachable` | audit | A `gh` query failed (auth/network); that repo was skipped, not mis-ranked. Check fleet GitHub auth if it persists. |
| `audit_completed` | audit | Informational — the audit finished and filed `auto-audit`-labelled issues. |
| `audit_failed` | audit | The audit could not dispatch or run. Investigate the owner bot / `gh` auth. |

## Active Notifications (push)

Reading events at decision points is the default, but silent stalls — the reason `activity_stuck` exists — are exactly the case where a manager *can't* rely on remembering to poll. So `fleet-pulse.sh` also **pushes** a one-line note into your tmux session for high-severity events (`activity_stuck`, `session_missing`, `service_down`), debounced to once per episode:

```
[FLEET-PULSE] <bot> activity_stuck — no tool calls for 11400s while not idle (likely hung mid-task)
```

Treat a `[FLEET-PULSE]` line like a `[BOTREPORT]`: look up the event in the table above and act. The push tells you *something needs attention*; the decision (investigate, restart, escalate to the human via Telegram) is still yours.

**Not yet captured via hooks:** several fleet-health signals are not derivable from the Claude Code PreToolUse/PostToolUse hook payload. Managers must use live checks for these until the hook schema exposes them:

- **`context_warning` / `rate_limit`** — not present in the payload at all. Use live checks (capture-pane, direct query) for context percentage and rate-limit status.
- **`mcp_error`** — a failing tool call (including an MCP server returning `isError`) fires the `PostToolUseFailure` hook event rather than `PostToolUse`, and only that event carries an error field. The `bot-vitals.sh` hook is wired to Pre/PostToolUse, so no `mcp_error` event is produced. Detecting dead or erroring MCP servers requires a dedicated mechanism (e.g. a `PostToolUseFailure` hook or an out-of-band liveness probe).

## Reading Events

Tail today's file for recent events:

```bash
BOTS_DIR=$(resolve_bots_dir "$FLEET_NAME")
today=$(date +%Y-%m-%d)
for bot_dir in "$BOTS_DIR"/*/; do
    bot=$(basename "$bot_dir")
    f="$bot_dir/data/events/fleet-${today}.jsonl"
    [ -f "$f" ] && echo "=== $bot ===" && tail -20 "$f"
done
```

Filter for actionable events:

```bash
grep -Eh '"type":"pane_stuck"|"type":"service_down"|"type":"session_missing"' \
    "$bot_dir/data/events/fleet-${today}.jsonl" 2>/dev/null
```

## Cross-Fleet Reads

A top-level manager can read any bot's events across sub-fleets since all paths are filesystem-based and derivable from fleet.yaml. No push mechanism needed — just read the bot directories for any fleet in scope.

```bash
# Read events for a bot in a different fleet
other_bots=$(resolve_bots_dir "other-fleet")
cat "$other_bots/some-bot/data/events/fleet-$(date +%Y-%m-%d).jsonl"
```

## Retention

Event files older than 7 days are automatically reaped by both `bot-vitals.sh` (on each hook invocation) and `fleet-pulse.sh` (on each cron run). No archiving — build archive-to-claudron if trend analysis proves valuable later.

## Configuration

Opt-in per bot via fleet.yaml. Hooks go on all bots; this protocol goes on managers only.

```yaml
bots:
  manager-bot:
    protocols:
      - fleet-observability    # manager reads event logs
  worker-bot:
    hooks:
      PreToolUse:
        - command: "$CLAUDLOBBY_ROOT/lib/bot-vitals.sh"
      PostToolUse:
        - command: "$CLAUDLOBBY_ROOT/lib/bot-vitals.sh"
```

Workers emit events but never read them. Managers read events but hooks are optional on them (useful if the manager also does tool work).
