---
title: Alert Routing Protocol
description: Background watcher pattern — poll an alert source, classify, file durable GitHub issues, notify the manager selectively, dedupe over 24h, sleep, repeat.
---

# Alert Routing Protocol

For a background watcher bot whose job is to keep the fleet informed of in-flight failures **without dragging workers off their current sprint tasks**. Source-agnostic (Slack channel, Dagster sensor, Sentry, PagerDuty, Cron job log, anything pollable). Output is *durable* (a GitHub issue) plus a *targeted* Telegram poke; never a dispatch.

## When to use this protocol

A bot fits this pattern when:

- It polls an alert source continuously (interval, not a tight loop)
- It does **not** dispatch other workers, author PRs, or remediate
- It produces a durable, re-readable record (GitHub issue) that auto-promotes to your tracker (e.g. Linear) on close
- It notifies a manager only when the alert needs sprint-level triage, not for every blip

Examples: `Sorin` (Quintorius fleet, Slack `#data-alerts`), `Tom Smykowski` (Artemis Data Platform fleet, same source). The same shape works for a Sentry-watcher bot, a Dagster freshness-test watcher, a CI-failure scribe.

## The loop

On each tick (default cadence: **60s** via `ScheduleWakeup(60)` — never `/loop` or tight polling; you must yield so Telegram `@<your-handle>` mentions can preempt):

1. **Poll** the alert source for messages newer than `last_seen_ts` (state file in bot's `memory/.last_seen_ts`).
2. **Filter** out:
   - Bot-to-bot replies (other watchers responding to the same source)
   - Thread replies unless the thread has a new parent alert
   - Duplicate alerts (same `(job, step, error_signature)` within the dedupe window — see below)
3. **Classify** each new alert by the fleet's domain model. Pick the model that fits — examples:
   - **Layered staleness model** for data pipelines (extraction → transform → sync)
   - **Severity model** for SRE (SEV-1 / SEV-2 / SEV-3)
   - **Owner-component model** for monorepos (team_a / team_b / shared)
   - Each layer/severity maps to: (a) the owning repo, (b) the owning persona, (c) a default priority label
4. **Dedupe.** Hash `(source_id, job, step, first_line_of_error)`. If the hash exists in `memory/.dedupe.json` within the dedupe window (default 24h):
   - **Do NOT** file a new issue
   - **Add a comment** to the existing issue with the new timestamp + occurrence count
   - **Do NOT** post to Telegram (silent update)
   - Reset the hash after the dedupe window so a still-firing alert re-escalates
5. **File a GitHub issue** on the owning repo with this body:
   ```markdown
   ## Alert
   - Source: <slack channel / dagster job / sentry project>
   - Job / asset / event: <name>
   - Step / step failed: <name>
   - Classification: <layer or severity>
   - First seen: <source timestamp>
   - Permalink: <source URL if available>

   ## Error excerpt
   ```
   <relevant error text, truncated to ~30 lines>
   ```

   ## Classification rationale
   <why this layer/severity, based on the error pattern + step context>

   ## Owner
   <persona name per the classification model>

   ## Not dispatching
   Per alert-routing protocol — durable issue only; manager sequences dispatch.
   ```
   **Labels:** `alert` (always) + `claude-code-assisted` (always) + `priority:<low|medium|high>` (based on customer-facing impact: customer-facing prod = `high`).
6. **Post a Telegram summary** to the fleet group via `mcp__plugin_telegram_telegram__reply`:
   ```
   [<bot>] Alert — <job> — <classification> — issue <#XXX-url> — not dispatching, awaiting triage.
   ```
   Tag the manager **only** when the item needs sprint-level triage:
   - High-priority (customer-facing prod) issue
   - Critical-component classification (e.g. a customer dashboard)
   - Recurring failures >3 in 24h on the same `(job, step)`

   Otherwise post without mention — quiet visibility.
7. **Update** `memory/.last_seen_ts` to the latest source timestamp.
8. **Sleep** — `ScheduleWakeup(60)`, yield.

On transient errors (source rate-limit, GitHub API hiccup): log to `memory/.errors.log`, sleep 120s, retry. Hard-fail only on catastrophic errors (token revoked, tmux session killed).

## First run

When `memory/.last_seen_ts` does not exist:

- Default the watermark to `now - 1h`
- Sweep that 1h window once and file (deduped) issues
- Post one "online" message to the Telegram group: `"<bot> online, monitoring starts now, last-hour catch-up: N alerts filed"`
- Then enter the steady-state 60s loop

## What this bot does NOT do

- **No dispatch.** You never `tmux send-keys` to peer worker bots or call `dispatch.sh`.
- **No PRs.** You file issues, not pull requests. You do not branch, commit, or push.
- **No remediation.** You do not run `dbt run`, `dbt test`, re-materialize Dagster assets, re-deploy services, restart pipelines, or touch production state.
- **No source-side replies.** Do not respond in the Slack thread / source channel unless the manager explicitly dispatches you to. Source-side replies belong to the manager's routing.

Your value is durable visibility without sprint disruption. Issues pile in the backlog; the manager sequences them into future dispatches per priority.

## Configuration knobs

The bot's `mission:` in `fleet.yaml` (or its CLAUDE.md) should specify the **fleet-specific** parameters:

| Knob | Where to set | Example |
|---|---|---|
| Alert source(s) | bot stanza `mission:` | Slack channel ID, Dagster GraphQL URL, Sentry project ID |
| Classification model | bot stanza `mission:` or referenced library file | "4-layer staleness model" / "SEV-1/2/3" / custom |
| Layer → repo mapping | bot stanza `mission:` | Layer 1/2 → Artemis-xyz/gokustats-back-end, Layer 3 → Artemis-xyz/dbt |
| Layer → owner mapping | bot stanza `mission:` | Layer 3 → @saheeli (dbt) |
| Dedupe window | bot stanza or default | 24h |
| Poll cadence | bot stanza | 60s (default) |
| Telegram group ID | bot.conf env (`TELEGRAM_GROUP_CHAT_ID`) | -1003955438790 |
| Manager handle (for triage tags) | bot stanza | @quintorious_bot |

The protocol above is the *invariant*; the knobs above are *fleet-specific*.

## Why durable issues, not just Telegram

Telegram posts age out of attention in minutes. Alert work needs an artifact that:

- Survives a re-read days later when someone notices the recurring pattern
- Auto-promotes to your tracker (Linear, Jira) via GitHub issue → tracker bridge actions
- Pins the dedupe record to a place workers can find it without scrolling chat
- Lets the manager triage from a list view, not a chat scroll

A pure-Telegram alert bot floods the chat and loses every recurrence. The issue is the source of truth; the Telegram poke is the heads-up.

## Yielding to direct mentions

While the loop is sleeping (between ticks), any direct `@<your-handle>` mention in Telegram preempts the next scheduled wakeup. On preemption:

1. Acknowledge within 10s via `mcp__plugin_telegram_telegram__reply` (per `telegram-routing` protocol)
2. Answer the question (read-only)
3. Resume the loop — `ScheduleWakeup(60)` from where you left off

If you cannot respond to a direct mention within 30s, you are doing the loop wrong — break out of whatever skill is consuming you and acknowledge first.
