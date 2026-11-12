---
title: Fleet Observability Protocol
description: Manager protocol for consuming bot-local observability events and making routing/restart decisions
requires:
  skills: [fleet-pulse]
---

# Fleet Observability Protocol

Pull-based observability for fleet managers. Two writers produce events; managers consume them at natural decision points.

This protocol's reference text (the event writers and their marker files, payload fields, detection mechanisms, history, reasons, and the critical-types table) is in `.claude/skills/fleet-pulse/fleet-observability-reference.md`, word for word under the same section names; it adds no rule.

## Event Sources

Every writer lands on the plane through `emit_fleet_event`; managers read one door, `claudlobby event list`, regardless of writer.

## Where to Read

Every bot's events are recorded on the plane — the host's flight recorder,
`$CLAUDLOBBY_ROOT/state/plane/plane.db` — and nowhere else (F18 closure: the
per-bot `data/events/*.jsonl` files are gone). Read them through
`claudlobby --json event list --since 24h` with the fleet selected.
Never open the database by hand from a session; `resolve_bots_dir` stays the
right tool for cases that only need bot *names* (e.g. enumerating who exists).

## When to Read

Read bot event logs at these natural decision points — not continuously, not on interrupt:

| Moment | Why |
|--------|-----|
| **Before dispatch** | Check target worker health before sending work |
| **Review routing** | Pick the healthiest available reviewer |
| **Idle / between tasks** | Proactive fleet health scan |
| **On linked-report receipt** | Cross-reference report with recent events for context |

## Decision Table

| Event type | Source | Manager action |
|------------|--------|---------------|
| `activity_stuck` | pulse | Bot has made **no tool call** for longer than its threshold, counted from its last tool call or the last tick keepalive saw no turn running, whichever is later, AND keepalive has not classified it as idle (no recent `data/.idle` marker). Investigate; restart only if `safe-worker-restart` guards pass. A bot whose input box holds text it never submitted, with no turn running, gets `input_held` instead (below). |
| `input_held` | pulse | The bot's **input box holds text that was never submitted**, held past `OBSERVABILITY_INPUT_HELD_THRESHOLD` (default 300 s; `0` turns this page off), whether or not a turn is running: no message sent to the bot lands until the box is clear (`turn` in the payload says which). A held box is not a hang, and **do not restart it**: a restart discards the text. **Look at its pane first**: where a menu shows (numbered options, "Enter to confirm"), an Enter picks an option, so press nothing. Otherwise ask an operator to press **one Enter** in its pane, wait about 10 s, and press **one more** only if the text is still there. Under a running turn that Enter queues the text behind the turn. If it is still there after that, **stop and look**: a menu or a modal (a safeguards pause) may be taking the Enter. Never typed text, and do not message the bot first: a message typed into a held box joins the held text. |
| `usage_limit_held` | pulse | A claude.ai **usage limit** stopped the bot and it is **still held after the limit's printed reset** (keepalive's `LIMIT` verdict: the bot's own `StopFailure` record, and the limit line still its last word on screen; #996). If its limit menu is up (the usual case on a bot without `--remote-control`, every worker by default), first choose **"Stop and wait for limit to reset" by its label, never by position**: a server flag can put usage credits first, and **credits are never chosen**. Then send the bot **any prompt** once the reset has passed, or arm `KEEPALIVE_LIMIT_RESUME_ENABLED=1` in its `fleet.yaml` env so keepalive does both once per limit. No restart is needed. `resume: tried` means it was resumed once and stopped again: **look before sending more**. |
| `pane_unknown` | pulse | keepalive **cannot read the bot's pane**: no running turn, no prompt, no held box and no usage limit (its `UNKNOWN` verdict), for longer than `OBSERVABILITY_PANE_UNKNOWN_THRESHOLD` (default 1800 s) with no tool call since. After an idle tick such a pane keeps its idle marker, so `activity_stuck` never fires for it (#997). **Look at its pane before sending it anything**: a menu or a dialog may be waiting for a key. Choose an option by its label, never by its position, and never usage credits. Do not restart it first: a restart discards what the pane holds. |
| `overdue_dispatch` | pulse | A task you dispatched to this bot passed its deadline with no terminal linked report. Check the bot (cross-reference `activity_stuck`): if hung, recover it; if mis-scoped or wedged, re-dispatch or reassign; if it waits on something outside the bot, move its deadline with `assignment reschedule` (the bot keeps its assignment); if it needs a human, escalate. Don't silently wait. |
| `pane_stuck` (>5 min) | pulse | Investigate pane content, restart if confirmed stuck. |
| `crash_loop` | pulse | The unit is **failing its start over and over** and systemd is already restarting it (`restarts` in the payload is how many times running). **Do NOT restart it** — another restart only zeroes the counter; the unit is enrolled, so `bot start` is not the fix either. The cause is in the bot's `logs/startup.log` (on 2026-09-23 it was a broken `claude` install, printed on every attempt). Fix the cause, or escalate to the human. |
| `channel_refused` | alert | Claude Code is **dropping the bot's inbound Telegram messages**: it refused the channel plugin at connect, while the poller runs and the bot's replies still go out (#2423; the reason is Claude Code's own, in the alert). **Do NOT restart it**: the refusal comes from the login (its account, or its org's managed settings), and a restart on the same login is refused again. Reach the bot by tmux dispatch, and escalate to the human: the login's channel setting is theirs. |
| `service_down` | pulse | The unit is installed and down. Its payload carries the session too (`session`: `up` or `missing`): a down unit is one alert, never a `session_missing` as well (#2243). If the bot is meant to run, the selected manager calls `claudlobby --json bot start BOT_ID` with its literal declared ID; inspect state and readiness. |
| `session_missing` | pulse | The tmux session is gone while the unit is active (keepalive restarts it within a minute), or on a bot with no supervised service configured. If the bot is meant to run, the selected manager calls `claudlobby --json bot start BOT_ID` with its literal declared ID; inspect state and readiness. |
| `unit_missing` | pulse | The bot names a service, its unit file is **not installed**, and **no stop was recorded**: nobody stopped it through `bot stop` (a botched disenroll, a broken activation, a deleted unit directory). It replaces `service_down` and `session_missing` for that bot and escalates like them (#2243). If the bot is meant to run, the selected manager calls `claudlobby --json bot start BOT_ID`; if it was stopped on purpose, `claudlobby --json bot stop BOT_ID` (with `--reason TEXT`) records that, and the alert ends. |
| `wip_uncommitted` | pulse | Do NOT restart — task is in flight. **Decide on the payload's `paths`, never on `dirty_files`**: a count cannot separate `M lib/foo.py` from `?? .venv/`, and reading it as a count is what made this alert fire forever and get skipped (#1728). `dirty_tracked`/`dirty_untracked` are facts to read, not a filter — an unadded new source file is untracked and is the unrecoverable case. `unchanged_for_s` is a floor measured from the sweep's first sighting; past ~2h on a source path, check for staleness. |
| `browser_outside_render` | pulse | A browser has run for **more than 15 minutes outside a heavy-slot render scope** (#2332). **The pulse never stops it** and pushes it once. Find what opened it and close it there, through the bot that started it or the operator at the desktop; never kill a browser by name, because every bot shares the user. Bot sessions have had no display since #2332, so a browser one opened anyway took a path the cut does not cover: report it. |
| `unit_failed` | pulse | An owned **unit is in the failed state** (#2532): one of the fleet's jobs, or an instance of its failure template, which means the alert a failed job sends did not go out. The pulse records it once an episode and pages the fleet's escalation chat once. Read the unit's log (`journalctl --user -u <unit>`) and tell the operator what failed; fixing or restarting a unit is the operator's. |
| `memory_thrash` | alert | The host is **swapping hard**: swap-out stayed over its line for its minutes in a row (once an episode). The alert names the largest bots by memory. **Read those bots first**, then act on the one that grew through your own doors: `bot compact`, `bot restart --fresh` or `bot stop`, once its work is committed. |
| `memory_thrash_unarmed` | alert | The host's **swap thrash alert is off**: its line file was refused, and the alert says why (once a bad edit). The file is the operator's: tell the operator what the alert says. |
| `compaction` | compactions | Informational — one of the bot's Claude Code compactions, recorded once by the pulse from its `compact_boundary` transcript row: `trigger` (`manual` or `auto`), `pre_tokens` to `post_tokens` (null until it ends). Verify a `bot compact` here instead of opening a transcript; `claudlobby --json fleet compactions record` records it at once. The bot's live context is `context` in `fleet status`. |
| `compaction_sent` | keepalive | Informational — keepalive typed `/compact` for the bot's own compaction request (`bot compact` on itself) at an idle, empty prompt, every guard holding (#1053). `compaction_proven` follows. |
| `compaction_proven` | keepalive | Informational — the bot's `compact_boundary` row newer than that `/compact`: `pre_tokens` to `post_tokens`. |
| `compaction_not_proven` | keepalive | No `compact_boundary` row came within 10 minutes of the `/compact` keepalive typed. **Nothing resends and nothing restarts**: look at the bot's pane and its `compaction` events before anything else, since a PreCompact hook may have refused it or the session may have restarted. The request is over; the bot asks again for another. |
| `compaction_skipped` | keepalive | Informational — a compaction request keepalive did not act on, once per `reason` per request. A bot skipped for `precompact_hook_refuses` is compacted by hand until its Claudron engine is 0.10.0 or later. |
| `audit_selected` | audit | Informational — the rolling sweep picked this repo as stalest. |
| `audit_dispatched` | audit | Informational — the audit was dispatched into the owner bot's session. |
| `audit_deferred` | audit | Owner was busy; the sweep skipped this tick and retries next run. No action. |
| `sweep_repo_unreachable` | audit | A `gh` query failed (auth/network); that repo was skipped, not mis-ranked. Check fleet GitHub auth if it persists. |
| `audit_completed` | audit | Informational — the audit finished and filed `auto-audit`-labelled issues. |
| `audit_failed` | audit | The audit could not dispatch or run. Investigate the owner bot / `gh` auth. |

For `bot start`, `ok: true`, `data.state: "running"`, and
`data.native_outcome: "observed"` confirm the supervised result;
`data.readiness` distinguishes an existing session (`current_session_ready`),
new bridge readiness (`bridge_ready`), and non-channel or intentionally
tokenless session readiness (`session_ready`). If the CLI refuses or readiness
is unknown, report the failure and inspect the selected unit and private
session. Do not fall back to a raw launcher.

**A bot stopped through `bot stop` raises no alert (#2243).** `fleet status`, `fleet reconcile`
and the pulse summary show the bot as `stopped`, with who and since.

## Active Notifications (push)

Treat a `[FLEET-PULSE]` line like a linked report: look up the event in the table above and act. The push tells you *something needs attention*; the decision (investigate, restart, escalate to the human via Telegram) is still yours.

Managers must use live checks for these until the hook schema exposes them:

- **`rate_limit`** — **Position against the ceiling is still not reported**: no hook carries it, and `capture-pane` is not an instrument
  for it, whatever the TUI draws: Claude Code runs in the tmux **alternate
  screen**, which retains no scrollback, so a capture can only ever describe
  the present frame. `claudlobby --json fleet usage --since 24h` reads
  covered transcript token counts, not position against a provider ceiling. A
  warning short of a trip is a *sighting*: label it as one, use it to raise a
  question, never as a measurement.
- **`context_warning`** — not present in the payload. It is, however, **sometimes
  visible in the pane**: above an undocumented threshold a figure is rendered,
  and below it the same slot renders empty. It reads `N% until auto-compact`,
  the share left before Claude Code compacts, not the share used, wherever
  Claude Code holds the conversation to an autocompact window, and
  `NN% context used` where it holds it to none (the `context-management`
  protocol says when each shows). In August 2026, when it read
  `NN% context used`, it was a **live gauge, not a one-time alarm** — the same
  bot read `97%` then `98%` minutes later — and it was **not a latch**: a bot
  observed at `100%` rendered nothing later in the same session, with no
  restart *(cause not established;
  do not assert one)*. **A rendered figure is real and worth acting on. A blank
  slot means nothing**, and specifically it has **two causes this instrument
  cannot separate** — never reached the threshold, or reached it and came back
  down. So the pane is a **positive detector only**: it can tell you to
  act, never that all is well. See the sweep rule two paragraphs down, which
  already states the general form of this correctly. The standing signal remains
  the worker's own `context-degraded` report plus completed-work counts — see
  the `context-management` protocol.
- **`mcp_error`** — Detecting dead or erroring MCP servers requires a dedicated mechanism (e.g. a `PostToolUseFailure` hook or an out-of-band liveness probe).

**Never sweep panes to establish absence — for `rate_limit` or `context_warning`.**
**Absence of a rendered warning is not evidence of headroom**, and the two fail for
*independent* reasons, neither of which depends on what the TUI renders:

- `rate_limit` — the frame is momentary. A quiet pane is equally consistent with a
  warning that was drawn and has already been overwritten.
- `context_warning` — a threshold warning is absent *below* the threshold too. Quiet
  means "not over the line", which is also what "nowhere near the line" looks like.

Act on a limit that trips; treat everything else as a sighting.

## Reading Events

Use `claudlobby event list` — never a hand-rolled loop over bot directories.

The event schema, the everyday read recipes, retention, configuration and why that loop misleads are in `.claude/skills/fleet-pulse/SKILL.md`: read that file; invoking `/fleet-pulse` starts a pulse run.

**Always inspect `data.next_cursor` before claiming a complete sweep.** `--limit`
defaults to 50; a nonempty cursor means more matching events remain. Continue
with `--cursor TOKEN` and the same filters. Set an explicit limit when scanning
a large window.

**`--critical` reads the severity the plane stamped from its registry** (`SYSTEM_EVENT_SEVERITY` in
`claudlobby/plane/registries.py`), and these are the registry's critical types:

They are listed under Critical types in `.claude/skills/fleet-pulse/fleet-observability-reference.md`, with what raises each and where each is recorded.

**Where an alert is recorded decides who can read it (#2109).** Fleet-pulse's escalation and a bot's
`brief` read bot rows only, so neither shows a fleet- or host-recorded alert; this fleet's `event
list` shows the fleet's but never the host's. Each FLEET ALERT still reaches a manager's pane and
Telegram when it is raised: treat a `[FLEET-ALERT]` line like a linked report.

**`--critical` does not cover every actionable type in the decision table above.** `pane_stuck`,
`wip_uncommitted`, `sweep_repo_unreachable` and `audit_failed` are registered `notice`, and each is
actionable per the table. Pair `--critical` with either a periodic unfiltered `--limit N` sweep, or
explicit per-type calls:

```bash
for t in pane_stuck wip_uncommitted sweep_repo_unreachable audit_failed; do
    claudlobby --json event list --type "$t" --limit 10
done
```

## Cross-Fleet Reads

A cross-fleet event read requires an operator shell with an explicit `--fleet` selector;
the generated manager grant is scoped to its own fleet. Use `claudlobby event list` rather
than reading the sibling fleet's bot directories directly.

```bash
# Operator shell: read events for a bot in a different fleet
claudlobby --fleet "other-fleet" --json event list --bot "some-bot" --limit 20
```
