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

Both write the same schema to the same bot-local directory. Managers read one path per bot regardless of writer.

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
{"ts": "...", "bot": "...", "type": "...", "source": "vitals|pulse", "data": {...}}
```

- **ts** — ISO 8601 timestamp
- **bot** — bot identifier
- **type** — event classification (open-ended, match on what you care about)
- **source** — `vitals` (bot-emitted) or `pulse` (external check)
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
| `pane_stuck` (>5 min) | pulse | Investigate pane content, restart if confirmed stuck |
| `mcp_error` | vitals | Attempt MCP server reconnect; flag human if persistent (>3 in 30 min) |
| `service_down` | pulse | Re-enroll via `lib/spin-up-bot.sh <bot-dir>` |
| `session_missing` | pulse | Re-enroll via `lib/spin-up-bot.sh <bot-dir>` |
| `wip_uncommitted` | pulse | Do NOT restart — task is in flight. Check for staleness instead. |
| `session_event` | vitals | Informational — log awareness of session lifecycle |

**Not yet captured via hooks:** `context_warning` and `rate_limit` are not available in the Claude Code PreToolUse/PostToolUse hook payload. Managers must continue using live checks (capture-pane, direct query) for context percentage and rate limit status until these signals become available in the hook schema.

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
grep -Eh '"type":"pane_stuck"|"type":"service_down"|"type":"session_missing"|"type":"mcp_error"' \
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
