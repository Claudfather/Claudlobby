---
name: status
description: "Manager readout for the human: what moved, and what is waiting on their decision. One line each, bounds stated. Consumes `claudlobby brief` and adds the PR surface brief does not carry."
argument-hint: "[24h|7d|since <ISO>]"
tool_grants:
  - "Bash(claudlobby *)"
  - "Bash(gh *)"
  - "mcp__plugin_telegram_telegram__reply"
---

# Status

The human asks *"what's the latest?"* and *"what's open for my input?"* constantly. This skill is
that answer, produced the same way every time so they stop having to ask.

**It answers exactly two questions. Nothing else belongs in the output.**

| | The question | The test for including a line |
|---|---|---|
| **1** | What moved? | It **changed state** since the window opened — merged, reviewed, opened, deployed, verified. |
| **2** | What is open for *your* input? | **The human is the named owner of a named decision.** Not activity, not FYI. |

Anything failing both tests is context, and context goes in a pointer.

## Step 1 — read the shipped door, never raw state files

```bash
claudlobby --fleet "$FLEET_NAME" brief --bot "$BOT_NAME" --json
```

`brief` is the fleet's one read door. **Do not hand-roll greps over `state/` or `data/events/`** — a
hand-rolled reader silently disagrees with the framework's own, and yours is the one that is wrong.

It gives you: `mission`, `dispatches` (`open`/`overdue`/`orphaned`), `workstreams`, `reports.unacked`,
`alerts`, and — load-bearing — `degraded[]`.

If the command fails or the fleet has no brief, **say so and stop.** A readout assembled from a door
that would not open is not a readout.

## Step 2 — add the PR surface, which `brief` does not carry

`brief` has no PR surface. That gap is why a PR sat 11h unreviewed and another stalled 8 days on
Request Changes without appearing in any readout. So query it directly, per repo in scope:

```bash
gh pr list --repo "<org>/<repo>" --state merged  --search "merged:>=$SINCE" --json number,title,mergedAt
gh pr list --repo "<org>/<repo>" --state open --json number,title,isDraft,reviewDecision,updatedAt
```

**Read the body, never a bare exit status.** `gh` writes error bodies to stdout, and a pipeline's `$?`
is the last stage's — see the `exit-status-through-pipes` guardrail. A failed repo query is a **named
gap in the readout**, not a repo with nothing in it.

## Step 3 — the two lists

Default window is 24h. `7d` or `since <ISO>` overrides it. **State the window in the output** — "what
moved" is meaningless without it.

**MOVED** — one line each, state-change first:

```
#1357 phantom dispatch rows: filed to merged (d42eba7) to deployed to verified
```

**OPEN FOR YOU** — one line each, and each line must name **the decision**, not the topic:

```
Composer silent-arming: a regenerate arms enforcement with no announcement. Ship the notice, or accept it?
```

A line that names a topic without a decision ("the permissions work") is the failure this skill exists
to prevent. If you cannot name the decision, it is not open for them — route it to whoever owns it.

**Empty is a real answer.** "Nothing moved and nothing is waiting on you" is useful and takes one line.
Never pad either list to look busy.

## Non-negotiables

**Carry `degraded[]` through — never launder it.** If `brief` labels a field, the readout says so in
the same breath. `alerts (0)` from a door that just told you *absence of an alert is not evidence of
health* is a false all-clear, and it is the kind that gets acted on. One clause is enough:

```
No critical alerts — though the alert filter omits host-job types (#903), so that is not an all-clear.
```

A `mode: omitted` field must not appear as a number at all. **A field neither present nor disclosed
does not exist.**

**State the bounds.** Cap the lists, and say what was capped: `showing 5 of 143 unacked`. Silent
truncation reads as exhaustive coverage — the same offense as inventing output.

**Never report a context percentage.** No bot can measure one. A blank pane footer has two causes that
cannot be separated, so it certifies nothing. Report units of work finished and named degradation
symptoms instead (`context-management`).

**Never guess an identifier.** A PR number, SHA, or URL you did not observe is fabrication, and this
output is precisely where one gets believed.

## Step 4 — send it through the channel

The readout is *for the human*, so it goes through the messaging tool. **Terminal output never reaches
their device** (`messaging-channel-discipline`).

Plain text, no `parseMode` — technical identifiers render correctly and there are no escape failures.
See [_telegram-formatting.md](../_telegram-formatting.md).

Bars: **≤600 chars** for the message. Over the bar means the detail has no address yet — file the
issue, push the branch, then send the pointer. **Trimming words is not the fix.**

## What this is not

- Not a health check — that is `/selfcheck` (this bot) and `/fleet-status` (the workers).
- Not the monitor's input — that is `/fleet-digest`.
- Not a work log. If nothing changed state, the honest readout is one line saying so.
