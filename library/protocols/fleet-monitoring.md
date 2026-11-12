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

- **A session ending is an event the session reports.** clauDNA's SessionEnd
  seals its segment inside the session. The `session-export` timer reads the
  store's export door every 15 minutes: polling a contract door, never transcripts
  or liveness. The monitor itself neither polls nor produces summaries.
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
| Session summaries | `claudlobby event list --type session_summary` | one row per sealed segment, including skipped |
| Bot events | `claudlobby event list` (the one door for bot events — never open `state/plane/plane.db` by hand; see `fleet-observability`, whose composed recipe this used to duplicate and now defers to) | see `fleet-observability` |
| Rollups | `claudlobby fleet uptime` · `utilization` · `--json fleet reports list` | fleet-level measures and paginated reports; inspect `ok` and follow `next_cursor` |

### Digest row contract

The `session-export` fleet job records clauDNA's export as `session_summary`
system events. `.data` is the record the event reader lifts from the nested
envelope; `/fleet-digest` carries the query's fleet, bot and timestamp with it.
The predecessor's rows never reached this reader (#1456/#1503).

| Field | Meaning |
|---|---|
| `ts` · `session_id` · `session_uid` · `agent_cli` · `bot` · `fleet` | identity; findings cite `session_id` |
| `status` · `skipped_reason` | `ok` or `skipped`; all unknown reasons remain skips |
| `turns` · `transcript_bytes` | summary input volume, null on skipped rows |
| `prompts` · `skills` · `failures` · `interrupts` | segment counts, null when unavailable after retirement |
| `journey.title/intent/outcome/arc/done/in_progress/next` | outcome is `shipped`, `partial`, `blocked`, `abandoned` or `exploratory`; arc is `[{step,result}]`; progress lists contain objects with `text` |
| `blocks.count/kinds` · `procedures` | block count by home, procedure count |
| `producer.model/duration_ms/cost_usd` | producer provenance, null on skipped rows |
| `seg` · `sealed_at` · `sealed_by` | sealed-segment identity and boundary |

The job runs by default. A bot with summaries off still yields `skipped: disabled`.
An empty window means no sealed segment, a disabled job, or an old/stale entrypoint;
name which from `<bot>.json` in the `session-export` directory under
`<fleet_state>` using granted `jq`.
The operator's `host doctor` session-export rung is the other view of those files;
the monitor runs neither doctor nor the private export tick. Missing/unreadable
state means unknown coverage. All rows skipped means print the coverage line
(summaries available on 0 of N represented bots, with reason tally) and stop.
Availability counts distinct bots with at least one ok row, over distinct bots
represented in the window. It does not establish switch configuration: armed bots
may produce only trivial/no-transcript/gave-up skips. A skip is not failure.
Journeys are model-written evidence; redact before quoting.

## Token discipline

The monitor is an Opus reasoning pass over an accumulating log — the one design
where cost grows silently with time.

- **Budget before you read.** Estimate at **≈4 characters per token** to size a
  window before committing to it. That heuristic is for *planning* a pass; for
  reporting observed Claude token counts use `claudlobby --json fleet usage --since 7d`.
  Its coverage names missing or partial transcripts; it does not measure money
  or remaining subscription quota.
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
