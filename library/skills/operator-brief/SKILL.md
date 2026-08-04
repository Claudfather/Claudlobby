---
name: operator-brief
description: "Report to the human in two blocks — what's done as one-liners, and what's open for their input with forks and leans. The standing shape for 'where are we?'"
argument-hint: "[--since <window>]"
---

# Operator Brief

The answer to *"what's done and what needs me?"* — in a fixed shape, so the human never has to ask for the format.

Two blocks, always in this order, nothing else.

## The contract

**DONE** — one line each. Outcome, not activity. A reader skimming only this block should know what changed.

**OPEN FOR YOU** — numbered. Only items that genuinely need *this human*. Each carries options and the team's lean.

Anything that needs somebody else, or has an obvious answer, is not in block two. It is either done, or it is yours to handle.

## Block 1 — DONE

- **One line per item.** If it needs two, the second belongs at a pointer.
- **State the outcome, not the journey.** *"the documented test command works again on any machine that has run a fleet"* — not *"investigated and fixed a pytest collection issue"*.
- **Say what it means, not what it is called.** Identifiers only where the reader would type them.
- Group trivially-related items on one line rather than listing six near-identical fixes.
- **No item without a number or a name** someone could look up.

## Block 2 — OPEN FOR YOU

Each item is exactly this, and short:

```
N. <THE DECISION IN FOUR WORDS>. <One sentence of why it is live now.>
   <Option a> / <Option b>. Lean: <which, and the one reason>.
```

Rules that make it usable rather than a second inbox:

- **Only what needs *this* human.** Cost envelopes, cross-fleet policy, anything with an external-facing cost, anything that changes what a fleet is *for*, and genuine forks where the team's lean could reasonably be overruled.
- **Every item has a lean.** Serving a naked choice is offloading the work. If there is no lean, the item is not ready — say what would produce one.
- **Every item names what it blocks**, or say it blocks nothing. *"nothing is waiting on this"* is a legitimate and useful line.
- **Bulk-ratifiable items get one line, not a section.** If seven decisions have strong leans, list them as leans and offer to take them all unless overruled.
- **Renumber into one queue** when items come from several plans. Two documents each numbering from F1 makes "F1" ambiguous — a real failure, not a hypothetical one.

## Gathering it

Mechanical inputs, cheapest first. Never recite from memory — poll.

```bash
# Landed since the window
gh pr list --repo <org>/<repo> --state merged --limit 30 \
  --json number,title,mergedAt --jq '.[] | select(.mergedAt > "<since>") | "#\(.number) \(.title)"'

# Waiting, and on whom
gh pr list --repo <org>/<repo> --state open --limit 20 \
  --json number,title,reviewDecision,mergeStateStatus

# Fleet state — is anything stuck or idle with work outstanding
claudlobby --fleet <fleet> status
```

**The mechanical half is the easy half.** Block 2 is a judgement the tooling cannot make: which open things genuinely need *this person*. Do that thinking; do not paste a query result into it.

## What this is not

- **Not a digest of activity.** Nobody asked what the fleet did all day. If an item changed no decision and blocks nothing, it is not in either block.
- **Not an escalation queue.** An item nobody can act on is context, and context does not go here.
- **Not a place to hedge.** *"we could do a or b"* with no lean is the shape that gets ignored, and it should be.

## Serving the forks afterwards

Block 2 is the *menu*. When the human starts answering, switch to one decision per message with a progress marker — *"2 of 4"* — per `telegram-mobile-presentation`. Apply each answer before serving the next; a ratified decision often reshapes the ones after it.

## Failure mode this exists to prevent

Being asked *"what's open?"* repeatedly and answering in a different shape each time — so the human re-derives the format on every read, and items that need them stay buried under items that do not.

If you find yourself composing this from scratch, that is the signal to run the skill instead.
