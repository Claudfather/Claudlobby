---
name: fleet-observe
description: "The monitor's reasoning pass. Reads the assembled digest summary and emits cited findings — each tied to a session_id and to a decision it should change. Reports; never acts outside its own fleet."
argument-hint: "[days] [fleet]"
tool_grants:
  - "Bash(jq *)"
  - "Bash(python3 *)"
---

# Fleet Observe

Reason over the assembled summary and emit findings worth someone's attention.

This is the pass the whole monitor exists for. Everything before it collects;
this step decides what any of it *means*. Contract: the `fleet-monitoring`
protocol. Rail: the `monitor-read-only` guardrail.

Invoked on a cadence tick by `autonomous-runner`, which resolves the goal chain
first — weigh findings against what the fleet is **for**, not against raw anomaly
size.

## Step 1 — Get the input

Run `/fleet-digest $1 $2`. Reason over its output and nothing else.

If it reports the log absent or the digester dormant, **that is your result**:
report the coverage gap as the finding and stop. Do not go looking for substitute
evidence, and never open a raw transcript.

## Step 2 — Reason across five lenses

Take each deliberately; they surface different things.

**Workflow dynamics.** What repeatedly gets in the way? Recurring `failed` /
`would_change` themes are the fleet telling you where its process is wrong. One
session hitting a wall is noise; the same wall in six sessions across three bots
is a finding.

**Library usefulness.** Which composed assets show up in `worked` and `reusable`?
Which never appear at all? An asset that has never been cited by any session is a
candidate for retirement — say so. Unused content is a cost, not a neutral.

**Logging gaps.** Where did a digest row have to say "unclear" or leave the rubric
empty on a substantial session? That is an instrument failing to capture
something real.

**Observability gaps.** What did you *want* to know and could not? Name the
missing instrument concretely. **A gap you can name is a finding, not a
non-result** — often the most valuable thing a pass produces.

**Token bloat.** Where is spend concentrated, and is it buying anything? Compare
`transcript_bytes` and `tool_calls` against what the sessions achieved. Look for
repetition: the same large file re-read across sessions, the same context
rebuilt, work redone because it was not handed off.

## Step 3 — Test every candidate before it ships

A candidate is a finding only if **all four** hold:

1. **Cited** — a `session_id` (or event line / command output) and the field it
   came from.
2. **Recurring or consequential** — repeated, or a single event with real cost.
   One-offs are context.
3. **Names a decision** — a specific decision it should change, with an owner.
4. **Actionable by that owner** — if nobody can act, it is a platform gap;
   file it as one rather than sending it nowhere.

Fail any test and it is not a finding. Drop it or demote it to context. Do not
promote weak candidates to fill out a report — a pass with zero findings and
honest coverage is a **success**, and saying so builds the trust that makes the
real findings land.

## Step 4 — Emit

```
FLEET OBSERVATION — <window>

COVERAGE
  <verbatim from /fleet-digest — never summarised away>

FINDINGS
  [<severity>] <one-line finding>
    evidence: session <session_id> (<field>) — "<redacted quote>"
              + <N> similar: <session_id>, <session_id>
    decision:  <the decision this should change>
    owner:     <fleet manager | Chris | this fleet>

CONTEXT (not findings)
  <observations that failed the tests above, kept brief>

INSTRUMENT GAPS
  <what you wanted to know and could not, and what would answer it>
```

Severity: `high` (acting is urgent) · `medium` (should change a decision this
cycle) · `low` (worth knowing). Reserve `high` for real cost — a monitor that
cries wolf gets muted, and then the instrument is worthless.

Redact before quoting: secrets to their last four characters, personal
identifiers to a placeholder.

## Step 5 — Route, do not act

Findings go to the owning fleet's **manager**, or to your Lead for synthesis and
escalation. You never act on a finding outside your own fleet — see
`monitor-read-only`. Urgency changes how fast and how loudly you report; it never
changes that.

When a finding implicates your own fleet, say so plainly and hand it to the Lead
rather than grading it yourself.

## Structured result

`autonomous-runner` parses this to apply its `on_outcome` policy:

```json
{"outcome": "completed",
 "findings": 3, "high": 0, "coverage_days": 7, "fleets": 5,
 "summary": "3 findings (0 high); 2 fleets had the digester off"}
```

| `outcome` | When |
|---|---|
| `completed` | The pass ran — **including with zero findings** |
| `partial` | Ran, but coverage was materially incomplete (say so in `summary`) |
| `blocked` | Could not run: log unreadable, rollups failing, fleet unresolvable |
| `needs_input` | A finding needs a human decision before the next pass is useful |

## Do not

- **Do not act on findings** outside your own fleet — report them.
- **Do not read raw transcripts.** If the digest cannot answer it, that is an
  instrument gap: file it.
- **Do not manufacture findings** to justify the tick. Zero is a real answer.
- **Do not restate metrics** `fleet-pulse` already reports. Reasoning is the
  product; a metrics restatement at Opus prices is the anti-pattern this whole
  protocol was written against.
- **Do not drop the coverage block.** A finding list without its coverage reads
  as complete when it may cover a fraction of the estate.
