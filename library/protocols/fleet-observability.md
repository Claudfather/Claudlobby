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

Every bot's events are recorded on the plane — the host's flight recorder,
`$CLAUDLOBBY_ROOT/state/plane/plane.db` — and nowhere else (F18 closure: the
per-bot `data/events/*.jsonl` files are gone). Read them through
`claudlobby events` (`--fleet <fleet> events --since 24h [--bot <bot>] [--json]`),
which renders the same `{ts, bot, type, source, data}` rows the ledgers had.
Never open the database by hand from a session; `resolve_bots_dir` stays the
right tool for cases that only need bot *names* (e.g. enumerating who exists).

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

- **`rate_limit`** — not present in the payload, and **no instrument reports it.**
  `capture-pane` is not one, and this holds whatever the TUI does or does not
  draw: Claude Code runs in the tmux **alternate screen**, which retains no
  scrollback, so a capture can only ever describe the present frame. Measured on
  this host: `history_size` is **0** on every bot pane while `history-limit`
  reads `2000`, and `capture-pane -S -` returns exactly `pane_height` lines —
  the current frame, with nothing behind it. There is no durable trace either;
  `lib/transcript-usage.py` measures spend, not position against a ceiling.
  **A limit that actually trips announces itself** and needs no instrument —
  that is the signal to act on. Anything short of it is a *sighting*: label it
  as one, use it to raise a question, never as a measurement — the frame is
  gone, so neither you nor anyone else can re-verify it afterwards.
- **`context_warning`** — not present in the payload. It is, however, **sometimes
  visible in the pane**: above an undocumented threshold a `NN% context used`
  figure is rendered, and below it the same slot renders empty. Measured across
  all 21 bots on this host: one rendered `98% context used` while twenty
  rendered nothing, panes structurally identical. Above the threshold it is a
  **live gauge, not a one-time alarm** — the same bot read `97%` then `98%`
  minutes later — and it is **not a latch**: a bot observed at `100%` rendered
  nothing later in the same session, with no restart *(cause not established;
  do not assert one)*. **A rendered figure is real and worth acting on. A blank
  slot means nothing**, and specifically it has **two causes this instrument
  cannot separate** — never reached the threshold, or reached it and came back
  down. A bot under no pressure and one that was at `100%` minutes ago present
  identically. So the pane is a **positive detector only**: it can tell you to
  act, never that all is well. See the sweep rule two paragraphs down, which
  already states the general form of this correctly. The standing signal remains
  the worker's own `context-degraded` report plus completed-work counts — see
  the `context-management` protocol.
- **`mcp_error`** — a failing tool call (including an MCP server returning `isError`) fires the `PostToolUseFailure` hook event rather than `PostToolUse`, and only that event carries an error field. The `bot-vitals.sh` hook is wired to Pre/PostToolUse, so no `mcp_error` event is produced. Detecting dead or erroring MCP servers requires a dedicated mechanism (e.g. a `PostToolUseFailure` hook or an out-of-band liveness probe).

**Never sweep panes to establish absence — for `rate_limit` or `context_warning`.**
A quiet pane is one instant for one bot, not a history, and "checked all N panes,
nothing there" reads as authoritative when it is not. **Absence of a rendered
warning is not evidence of headroom**, and the two fail for *independent* reasons,
neither of which depends on what the TUI renders:

- `rate_limit` — the frame is momentary. A quiet pane is equally consistent with a
  warning that was drawn and has already been overwritten.
- `context_warning` — a threshold warning is absent *below* the threshold too. Quiet
  means "not over the line", which is also what "nowhere near the line" looks like.

So a clean sweep can never distinguish *fine* from *already past it*, and it is the
reading that gets acted on, because absence looks like an all-clear. Act on a limit
that trips; treat everything else as a sighting.

## Reading Events

Use `claudlobby events` — never a hand-rolled loop over bot directories. This is **not** a fix
for a live break: run verbatim on an armed bot, the loop below still works, including for denied
siblings — confirmed directly, independently, by two different bots on two different sessions.
The mechanism (otis, `shared/planning/active/2026-08-27-deny-bypass-probe.md`): the permission
matcher resolves a variable **only if its value is assigned in the same command as a literal** —
`b=ravi; cat .../$b/...` is denied correctly (and `b=otis; cat .../$b/...` allowed correctly, both
resolved against the right bot). An expansion the matcher **cannot** statically resolve from the
command text — a glob-bound loop variable, `${RANDOM}`, `${PIPESTATUS[0]}` — makes it fail open,
regardless of where that expansion sits or whether the path beside it is literal. The loop below
binds `"$bot_dir"` from `"$BOTS_DIR"/*/`, a glob expansion depending on filesystem state at
runtime — unresolvable from the command text alone — which is why it evades where a same-command
literal assignment would not.

That is exactly the problem. **It works by accident**, on some permission-matcher blind spot, not
by design — and the same evasion is why it's silent in the reassuring direction if the matcher
is ever tightened: an event sweep that stops resolving would return no events, indistinguishable
from a healthy fleet, with no warning that anything changed. Separately from permissions entirely,
`claudlobby events` is also just the better tool for this: it's the same door `brief.py`'s alerts
section already consumes, and it adds type/critical filtering and coverage-honesty disclosure a
hand-rolled loop doesn't have.

Tail today's events across the fleet:

```bash
claudlobby --fleet "$FLEET_NAME" events --tail 50
```

Scope to one bot — e.g. before dispatch, or cross-referencing a `[BOTREPORT]`:

```bash
claudlobby --fleet "$FLEET_NAME" events --bot "$BOT_NAME" --tail 20
```

Filter for actionable events:

```bash
claudlobby --fleet "$FLEET_NAME" events --critical --tail 200
```

**Always pass an explicit `--tail` with `--critical`.** `--tail` defaults to 50 and `--critical`
inherits that default silently — the output states no bound and gives no hint that anything was
dropped. Measured: the same query returned 50 rows at the default and 500 at `--tail 500`, with
no disclosure either way. That is the exact silent-cap shape this codebase's own coverage-honesty
discipline forbids, sitting in a shipped door; treat the default as unsafe until it's fixed
upstream and always size `--tail` explicitly instead of relying on it.

**`--critical` also does not cover every actionable type in the decision table above.** It matches a
fixed, hand-maintained set (`session_missing`, `service_down`, `activity_stuck`, `script_error`,
`overdue_dispatch`, `bridge_down`, `reload_failed`, `restart_failed`, `rc_timeout`) that omits
`pane_stuck`, `wip_uncommitted`, `sweep_repo_unreachable`, and `audit_failed` — all actionable per
the table above. Same hand-maintained-list gap `brief.py`'s alerts section already discloses
(#903); this protocol inherits it rather than reintroducing it. Until #903 closes, pair
`--critical` with either a periodic unfiltered `--tail N` sweep, or explicit per-type calls:

```bash
for t in pane_stuck wip_uncommitted sweep_repo_unreachable audit_failed; do
    claudlobby --fleet "$FLEET_NAME" events --type "$t" --tail 10
done
```

(That loop is over **type strings**, never bot paths — the command text names no bot directory,
so it is unaffected by path-scoped deny rules regardless of arming.)

## Cross-Fleet Reads

A top-level manager can read any bot's events across sub-fleets. Use `claudlobby events` with
`--fleet` rather than reading the sibling fleet's bot directories directly — same reasoning as
above, and it works the same way whether or not the target fleet has armed.

```bash
# Read events for a bot in a different fleet
claudlobby --fleet "other-fleet" events --bot "some-bot" --tail 20
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
