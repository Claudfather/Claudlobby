---
title: Fleet Monitoring Protocol
description: Scheduled, low-frequency reasoning over pre-aggregated session digests — the monitor's cadence, evidence contract, and the detection machinery it deliberately does not build
---

# Fleet Monitoring Protocol

How a monitor bot reasons about fleet health on a schedule, over digests that
already exist.

**This protocol extends `fleet-observability`; it does not restate it.** That
protocol owns the event stream — sources, the plane's row shape, decision table, `plane prune` retention,
and the read-at-decision-points cadence a *manager* uses. This one covers what is
different about a monitor: it runs on a **schedule** rather than at decision
points, it reasons over **pre-aggregated digests** rather than raw events, and its
output is a **cited finding** rather than a routing decision. Attach both.

## The shape

```
   sessions end  ──►  SessionEnd hook distils each one  ──►  digest log
                      (inside the ending session)              │
                                                               ▼
   schedule fires  ──►  /fleet-digest (assemble)  ──►  /fleet-observe (reason)
                                                               │
                                                               ▼
                                            cited findings ──► managers · Chris
```

Everything upstream of the schedule already happened. The monitor never causes a
digest to be produced; it reads what accrued.

## What this protocol deliberately does NOT build

**Read this block before proposing any change to the monitor.** These are not
omissions to be filled in later — each is a rejected design, and the negative
space is the point.

- **Nothing watches for sessions ending.** No poller, no watcher, no filesystem
  notify, no "has this session gone quiet" heuristic. The digester is a
  `SessionEnd` hook that fires *inside* the session that is ending. A session
  ending is not an event the monitor detects; it is an event the session reports.
- **Nothing tails transcripts.** The monitor never opens a raw transcript. Ever.
  They are large, they carry secrets, and a distilled row already exists.
- **No real-time or near-real-time path.** The cadence is hours-to-daily. There is
  no fast lane, no interrupt, no "urgent finding" trigger that shortens it.
  Urgency changes who gets told, never how often the monitor wakes.
- **No new detection machinery of any kind.** If something needs detecting,
  either an existing instrument covers it or the *gap* is the finding. Building a
  detector is a platform change that goes through a PR like any other — never
  something the monitor grows on its own.

Every one of these is cheap to violate and expensive to unwind: each would turn a
scheduled reasoning pass into a always-on watcher with its own failure modes,
its own supervision needs, and its own token floor. If a future change needs one
of them, that change needs to argue with this block first.

## Cadence

Scheduled and low-frequency. Daily is the default; anything under hourly is
almost certainly wrong.

**Wiring: extend `autonomous-runner`, do not write a second runner.** That skill
is already the generic continuous-job wrapper — it resolves the goal chain first
(Fleet Mission → project mission → work item), reads its cadence from `bot.conf`,
runs the idle and quota checks, invokes a configured skill, parses the structured
result, and applies an `on_outcome` policy. That *is* the cadence-tick machinery.
The monitor configures it to invoke `/fleet-observe`:

```yaml
bots:
  monitor-bot:
    skills: [autonomous-runner, fleet-digest, fleet-observe]
    protocols: [fleet-monitoring, fleet-observability]
    autonomous_runner:
      skill: /fleet-observe
      cadence: 1d
```

Two things follow from that wiring:

- The **goal chain resolves before the pass**, so a finding is weighed against
  what the fleet is *for*, not against raw anomaly size.
- `autonomous_runner.skill` is validated against the `--auto`-eligible clauDNA
  list, so a non-clauDNA skill raises a **warning, not an error** — the wrapper
  still invokes it. Expect that warning until `fleet-observe` is added to the
  eligible set; it is not a misconfiguration.

A missed tick is a non-event. The digest log is durable and the next pass reads
the wider window; there is no catch-up storm to design around.

## The evidence contract

The monitor reads **only** pre-aggregated sources:

| Source | Path | Shape |
|---|---|---|
| Transcript digests | `$CLAUDLOBBY_ROOT/state/transcript-digests/transcript-digest-YYYY-MM-DD.jsonl` | one row per finished session |
| Bot events | `claudlobby events` (the one door for bot events — never open `state/plane/plane.db` by hand; see `fleet-observability`, whose composed recipe this used to duplicate and now defers to) | see `fleet-observability` |
| Rollups | `claudlobby uptime` · `utilization` · `report-back` | fleet-level aggregates |

### Digest row contract

Written by `lib/transcript-digest.sh` (`SessionEnd`). Fields the monitor depends
on:

| Field | Meaning |
|---|---|
| `ts` · `session_id` · `bot` · `fleet` | identity — **`session_id` is what a finding cites** |
| `status` | `ok` · `skipped` (below `SESSION_DIGEST_MIN_TURNS`) · `error` |
| `turns` · `tool_calls` · `transcript_bytes` · `digest_chars` | volume signals |
| `context` · `worked` · `failed` · `would_change` · `reusable` | the distilled rubric — the reasoning substrate |
| `error` | present only on a failed digest |

Three properties to hold onto:

- **The log is dormant by default.** A fleet appears only once it sets
  `SESSION_DIGEST_ENABLED=1`. **Absence of rows means the instrument is off, not
  that the fleet is quiet** — never infer health from an empty window. Say which
  fleets were in scope and which had no coverage.
- **`skipped` is not failure.** It means the session was below the turn floor.
  Counting `skipped` rows as problems manufactures findings out of short sessions.
- **The rubric fields are model-written free text** over a real session. Treat
  them as evidence, not as structured data, and redact before quoting.

## Token discipline

The monitor is an Opus reasoning pass over an accumulating log — the one design
where cost grows silently with time.

- **Budget before you read.** Estimate at **≈4 characters per token** to size a
  window before committing to it. That heuristic is for *planning* a pass; for
  reporting actual spend use `lib/transcript-usage.py`, which reads real usage.
- **Bound the window, then say what you bounded.** A pass that covers seven days
  says seven days. A pass that dropped rows to fit says how many and why.
- **Aggregate before reasoning.** `/fleet-digest` reduces the raw log to a
  summary; `/fleet-observe` reasons over that summary. Feeding thousands of raw
  rows into the reasoning pass is the failure mode this split exists to prevent.
- **A quiet pass is a cheap pass.** "Nothing notable, 412 rows across 5 fleets"
  is a complete and successful result. Do not spend tokens manufacturing a
  finding to justify the tick.

## What counts as a finding

Two mandatory parts. Missing either, it is not a finding:

1. **A citation** — `session_id` plus the field it came from, an event line, or a
   command output. Never a summary of an impression.
2. **A decision it should change** — named, with an owner. "Token use is up 30%"
   is an observation. "…concentrated in three sessions that each re-read the same
   40 KB plan doc; that fleet should attach it by reference — owner: their
   manager" is a finding.

Findings are **reported, never acted on** outside the monitor's own fleet. The
`monitor-read-only` guardrail is the enforced rail; this is the workflow half of
the same rule.

### Coverage honesty

Every pass states its scope: which fleets, which window, how many rows, and what
it could not see. A pass that quietly covered 40% of the estate while reading as
complete is worse than no pass — it converts a gap into false assurance. If the
window was capped, the log was missing, or a fleet had the digester off, that
belongs in the output, not in a footnote nobody reads.

## Anti-patterns

| Anti-pattern | Why it fails |
|---|---|
| Restating `fleet-pulse` metrics at Opus prices | The metrics already exist and are free; the reasoning is what is new |
| A finding with no named decision | Produces a dashboard nobody reads |
| Inferring health from an empty digest window | The instrument is dormant by default; absence is not evidence |
| Shortening the cadence after an interesting finding | Turns a scheduled pass into a watcher — see the negative block |
| Reading a raw transcript "just this once" | The digest exists; if it is inadequate, that is the finding |
| Counting `skipped` rows as problems | Manufactures findings from short sessions |
| Acting on a finding in another fleet's tree | Violates `monitor-read-only`; report to its manager instead |
