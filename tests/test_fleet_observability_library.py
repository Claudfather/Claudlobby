"""tests/test_fleet_observability_library.py — the fleet-observability protocol
keeps its guards in the text a manager composes on every call. #2755 moved the
protocol's event reference into the fleet-pulse skill and kept every guard in
place; its body lists 22 of them, the Decision Table's 21 rows among them, and
its review checked each one. One `in` check per kept rule sentence: a later trim
that drops one fails here, and a rule reworded on purpose changes its line here
in the same PR (#2761). The critical-types table and the per-type loop are held
to the event registry by tests/test_event_type_registry.py."""

from __future__ import annotations

import re
from pathlib import Path

from claudlobby.loader import parse_frontmatter

PROTOCOL = Path(__file__).resolve().parent.parent / "library" / "protocols" / "fleet-observability.md"


def _flat(text: str) -> str:
    """Whitespace-collapsed: the notes wrap mid-sentence, and once inside a code span."""
    return " ".join(text.split())


def _body() -> str:
    return parse_frontmatter(PROTOCOL.read_text())[1]


# The guards outside the Decision Table, in the protocol's order, each under the
# name #2755's table gives it.
KEPT = (
    # Managers read one door, `claudlobby event list`, whatever the writer
    "Every writer lands on the plane through `emit_fleet_event`; managers read one door, `claudlobby event list`, regardless of writer.",
    # Events are read through `event list`, with the fleet selected
    "Read them through `claudlobby --json event list --since 24h` with the fleet selected.",
    # Never open the plane database by hand
    "Never open the database by hand from a session; `resolve_bots_dir` stays the right tool for cases that only need bot *names* (e.g. enumerating who exists).",
    # Read events at four decision points (When to Read)
    "Read bot event logs at these natural decision points — not continuously, not on interrupt:",
    "| **Before dispatch** | Check target worker health before sending work |",
    "| **Review routing** | Pick the healthiest available reviewer |",
    "| **Idle / between tasks** | Proactive fleet health scan |",
    "| **On linked-report receipt** | Cross-reference report with recent events for context |",
    # A `bot start` counts only on ok, running, observed and a readiness; never a raw launcher
    'For `bot start`, `ok: true`, `data.state: "running"`, and `data.native_outcome: "observed"` confirm the supervised result; `data.readiness` distinguishes an existing session (`current_session_ready`), new bridge readiness (`bridge_ready`), and non-channel or intentionally tokenless session readiness (`session_ready`).',
    "If the CLI refuses or readiness is unknown, report the failure and inspect the selected unit and private session.",
    "Do not fall back to a raw launcher.",
    # A bot stopped through `bot stop` raises no alert
    "**A bot stopped through `bot stop` raises no alert (#2243).**",
    # A pushed `[FLEET-PULSE]` line is acted on like a linked report
    "Treat a `[FLEET-PULSE]` line like a linked report: look up the event in the table above and act.",
    "The push tells you *something needs attention*; the decision (investigate, restart, escalate to the human via Telegram) is still yours.",
    # Signals no hook captures need live checks
    "Managers must use live checks for these until the hook schema exposes them:",
    # (not a row of #2755's table: an instruction in the same `rate_limit` note)
    "**Position against the ceiling is still not reported**: no hook carries it, and `capture-pane` is not an instrument for it",
    # A limit warning short of a trip is a sighting, never a measurement
    "A warning short of a trip is a *sighting*: label it as one, use it to raise a question, never as a measurement.",
    # (not a row of #2755's table: an instruction in the same `context_warning` note)
    "*(cause not established; do not assert one)*",
    # A rendered context figure is real; a blank slot means nothing
    "**A rendered figure is real and worth acting on. A blank slot means nothing**, and specifically it has **two causes this instrument cannot separate** — never reached the threshold, or reached it and came back down.",
    # The pane can tell you to act, never that all is well
    "So the pane is a **positive detector only**: it can tell you to act, never that all is well.",
    # (not a row of #2755's table: the same note names the standing signal)
    "The standing signal remains the worker's own `context-degraded` report plus completed-work counts",
    # A dead MCP server needs its own detector
    "Detecting dead or erroring MCP servers requires a dedicated mechanism (e.g. a `PostToolUseFailure` hook or an out-of-band liveness probe).",
    # A quiet pane is never an all-clear (never sweep panes to establish absence)
    "**Never sweep panes to establish absence — for `rate_limit` or `context_warning`.**",
    "**Absence of a rendered warning is not evidence of headroom**",
    # Act on a limit that trips; everything else is a sighting
    "Act on a limit that trips; treat everything else as a sighting.",
    # Never a hand-rolled loop over bot directories
    "Use `claudlobby event list` — never a hand-rolled loop over bot directories.",
    # Inspect `data.next_cursor` before claiming a complete sweep
    "**Always inspect `data.next_cursor` before claiming a complete sweep.**",
    "Continue with `--cursor TOKEN` and the same filters.",
    "Set an explicit limit when scanning a large window.",
    # What `--critical` covers: the critical-types table
    "**`--critical` reads the severity the plane stamped from its registry**",
    # Where an alert is recorded decides who can read it
    "**Where an alert is recorded decides who can read it (#2109).**",
    "Fleet-pulse's escalation and a bot's `brief` read bot rows only, so neither shows a fleet- or host-recorded alert; this fleet's `event list` shows the fleet's but never the host's.",
    # A pushed `[FLEET-ALERT]` line is acted on like a linked report
    "Each FLEET ALERT still reaches a manager's pane and Telegram when it is raised: treat a `[FLEET-ALERT]` line like a linked report.",
    # `--critical` misses actionable types: pair it with the per-type loop
    "**`--critical` does not cover every actionable type in the decision table above.**",
    "Pair `--critical` with either a periodic unfiltered `--limit N` sweep, or explicit per-type calls:",
    # A cross-fleet read needs an operator shell, never a sibling fleet's bot directories
    "A cross-fleet event read requires an operator shell with an explicit `--fleet` selector; the generated manager grant is scoped to its own fleet.",
    "Use `claudlobby event list` rather than reading the sibling fleet's bot directories directly.",
)

# Each Decision Table row keeps what the event means and what the manager does,
# checked inside that row: every sentence but its payload field lists, its history,
# and its notes on mechanism.
DECISION = {
    "activity_stuck": (
        "Bot has made **no tool call** for longer than its threshold, counted from its last tool call or the last tick keepalive saw no turn running, whichever is later, AND keepalive has not classified it as idle (no recent `data/.idle` marker).",
        "Investigate; restart only if `safe-worker-restart` guards pass.",
        "A bot whose input box holds text it never submitted gets `input_held` instead (below).",
    ),
    "input_held": (
        "The bot's **input box holds text that was never submitted** and no turn is running (keepalive's `HELD` verdict, held past `OBSERVABILITY_INPUT_HELD_THRESHOLD`, default 300 s).",
        "It is not hung, and **do not restart it**: a restart discards the text.",
        '**Look at its pane first**: where a menu shows (numbered options, "Enter to confirm"), an Enter picks an option, so press nothing.',
        "Otherwise ask an operator to press **one Enter** in its pane, wait about 10 s, and press **one more** only if the text is still there.",
        "If it is still there after that, **stop and look**: a menu or a modal (a safeguards pause) may be taking the Enter.",
        "Never typed text, and do not message the bot first: a message typed into a held box joins the held text.",
    ),
    "usage_limit_held": (
        "A claude.ai **usage limit** stopped the bot and it is **still held after the limit's printed reset** (keepalive's `LIMIT` verdict: the bot's own `StopFailure` record, and the limit line still its last word on screen; #996).",
        'If its limit menu is up (the usual case on a bot without `--remote-control`, every worker by default), first choose **"Stop and wait for limit to reset" by its label, never by position**: a server flag can put usage credits first, and **credits are never chosen**.',
        "Then send the bot **any prompt** once the reset has passed, or arm `KEEPALIVE_LIMIT_RESUME_ENABLED=1` in its `fleet.yaml` env so keepalive does both once per limit.",
        "No restart is needed.",
        "`resume: tried` means it was resumed once and stopped again: **look before sending more**.",
    ),
    "pane_unknown": (
        "keepalive **cannot read the bot's pane**: no running turn, no prompt, no held box and no usage limit (its `UNKNOWN` verdict), for longer than `OBSERVABILITY_PANE_UNKNOWN_THRESHOLD` (default 1800 s) with no tool call since.",
        "After an idle tick such a pane keeps its idle marker, so `activity_stuck` never fires for it (#997).",
        "**Look at its pane before sending it anything**: a menu or a dialog may be waiting for a key.",
        "Choose an option by its label, never by its position, and never usage credits.",
        "Do not restart it first: a restart discards what the pane holds.",
    ),
    "overdue_dispatch": (
        "A task you dispatched to this bot passed its deadline with no terminal linked report.",
        "Check the bot (cross-reference `activity_stuck`): if hung, recover it; if mis-scoped or wedged, re-dispatch or reassign; if it waits on something outside the bot, move its deadline with `assignment reschedule` (the bot keeps its assignment); if it needs a human, escalate.",
        "Don't silently wait.",
    ),
    "pane_stuck": (
        "Investigate pane content, restart if confirmed stuck.",
    ),
    "crash_loop": (
        "The unit is **failing its start over and over** and systemd is already restarting it (`restarts` in the payload is how many times running).",
        "**Do NOT restart it** — another restart only zeroes the counter; the unit is enrolled, so `bot start` is not the fix either.",
        "The cause is in the bot's `logs/startup.log` (on 2026-09-23 it was a broken `claude` install, printed on every attempt).",
        "Fix the cause, or escalate to the human.",
    ),
    "channel_refused": (
        "Claude Code is **dropping the bot's inbound Telegram messages**: it refused the channel plugin at connect, while the poller runs and the bot's replies still go out (#2423; the reason is Claude Code's own, in the alert).",
        "**Do NOT restart it**: the refusal comes from the login (its account, or its org's managed settings), and a restart on the same login is refused again.",
        "Reach the bot by tmux dispatch, and escalate to the human: the login's channel setting is theirs.",
    ),
    "service_down": (
        "The unit is installed and down.",
        "Its payload carries the session too (`session`: `up` or `missing`): a down unit is one alert, never a `session_missing` as well (#2243).",
        "If the bot is meant to run, the selected manager calls `claudlobby --json bot start BOT_ID` with its literal declared ID; inspect state and readiness.",
    ),
    "session_missing": (
        "The tmux session is gone while the unit is active (keepalive restarts it within a minute), or on a bot with no supervised service configured.",
        "If the bot is meant to run, the selected manager calls `claudlobby --json bot start BOT_ID` with its literal declared ID; inspect state and readiness.",
    ),
    "unit_missing": (
        "The bot names a service, its unit file is **not installed**, and **no stop was recorded**: nobody stopped it through `bot stop` (a botched disenroll, a broken activation, a deleted unit directory).",
        "It replaces `service_down` and `session_missing` for that bot and escalates like them (#2243).",
        "If the bot is meant to run, the selected manager calls `claudlobby --json bot start BOT_ID`; if it was stopped on purpose, `claudlobby --json bot stop BOT_ID` (with `--reason TEXT`) records that, and the alert ends.",
    ),
    "wip_uncommitted": (
        "Do NOT restart — task is in flight.",
        "**Decide on the payload's `paths`, never on `dirty_files`**: a count cannot separate `M lib/foo.py` from `?? .venv/`, and reading it as a count is what made this alert fire forever and get skipped (#1728).",
        "`dirty_tracked`/`dirty_untracked` are facts to read, not a filter — an unadded new source file is untracked and is the unrecoverable case.",
        "`unchanged_for_s` is a floor measured from the sweep's first sighting; past ~2h on a source path, check for staleness.",
    ),
    "browser_outside_render": (
        "A browser has run for **more than 15 minutes outside a heavy-slot render scope** (#2332).",
        "**The pulse never stops it** and pushes it once.",
        "Find what opened it and close it there, through the bot that started it or the operator at the desktop; never kill a browser by name, because every bot shares the user.",
        "Bot sessions have had no display since #2332, so a browser one opened anyway took a path the cut does not cover: report it.",
    ),
    "unit_failed": (
        "An owned **unit is in the failed state** (#2532): one of the fleet's jobs, or an instance of its failure template, which means the alert a failed job sends did not go out.",
        "The pulse records it once an episode and pages the fleet's escalation chat once.",
        "Read the unit's log (`journalctl --user -u <unit>`) and tell the operator what failed; fixing or restarting a unit is the operator's.",
    ),
    "compaction": (
        "Informational — one of the bot's Claude Code compactions, recorded once by the pulse from its `compact_boundary` transcript row: `trigger` (`manual` or `auto`), `pre_tokens` to `post_tokens` (null until it ends).",
        "Verify a `bot compact` here instead of opening a transcript; `claudlobby --json fleet compactions record` records it at once.",
    ),
    "audit_selected": (
        "Informational — the rolling sweep picked this repo as stalest.",
    ),
    "audit_dispatched": (
        "Informational — the audit was dispatched into the owner bot's session.",
    ),
    "audit_deferred": (
        "Owner was busy; the sweep skipped this tick and retries next run.",
        "No action.",
    ),
    "sweep_repo_unreachable": (
        "A `gh` query failed (auth/network); that repo was skipped, not mis-ranked.",
        "Check fleet GitHub auth if it persists.",
    ),
    "audit_completed": (
        "Informational — the audit finished and filed `auto-audit`-labelled issues.",
    ),
    "audit_failed": (
        "The audit could not dispatch or run.",
        "Investigate the owner bot / `gh` auth.",
    ),
}


def _decision_rows() -> dict[str, str]:
    """Each Decision Table row by its event type, whitespace-collapsed."""
    _head, sep, rest = _body().partition("\n## Decision Table\n")
    assert sep, "the protocol has no Decision Table"
    rows = {}
    for line in rest.split("\n## ", 1)[0].splitlines():
        m = re.match(r"\| `([a-z_]+)`", line)
        if m:
            rows[m.group(1)] = _flat(line)
    return rows


def test_the_protocol_keeps_its_rule_sentences():
    body = _flat(_body())
    missing = [s for s in KEPT if _flat(s) not in body]
    assert not missing, missing


def test_each_decision_table_row_keeps_its_rules():
    rows = _decision_rows()
    missing = [(event, s) for event, kept in DECISION.items() for s in kept
               if _flat(s) not in rows.get(event, "")]
    assert not missing, missing
