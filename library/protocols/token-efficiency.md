---
title: Token-Efficiency
description: Lossless brevity for outbound comms — reference-don't-paste, TLDR-first, structured deltas. Density rules only; posting cadence and channel routing are governed elsewhere.
---

# Token-Efficiency

Every outbound message costs tokens twice — once to write, once in every context that reads it. Compress losslessly: cut tokens, never signal.

**Rule zero — lossless, never lossy.** **The detail must be at an address the addressee can open, not merely one that exists:** a PR or issue (on a public repo, no business names or secrets), a doc in the fleet's `shared/` or a vault note, a repo path at a pushed commit. Never your own `data/` or anything else in your bot directory: no other bot in your fleet can open it, your manager included, so it is only for your scratch.

- **Reference, don't paste.** Work products travel as `path-or-URL + one-line what`. Never paste file bodies, diffs, or logs when an address exists — create the address first if needed.
- **Telegram = TLDR-first.** Outcome in 1–3 sentences + pointer(s). When someone asks for detail, give it in full — an explicit request is never re-summarized.
- **Reports = structured deltas.** status | what changed | blockers | next | pointers. No session narratives; the linked report summary stays one line.

### The bars

A rule needs a unit that bounds size. "Outcome in 1–3 sentences" *is* a count — but a sentence has no length ceiling, so clause-stacking satisfies it with 400 words in good faith. Count the characters instead:

| Surface | Bar | Basis | Overflow goes |
|---|---|---|---|
| Telegram / chat message | **≤ 600 chars** | roughly a phone screen without scrolling | a pointer — issue, PR, doc path |
| linked report summary | **≤ 200 chars**, one line | measured: real verdicts carrying finding + pointer land at 155–165 | the PR/issue body, or a doc in the fleet's `shared/` |
| Progress post | **≤ 200 chars** | same class as a report summary | the work itself |
| Dispatch payload | **≤ 2000 chars** | measured 1,400–1,600 of load-bearing content; loosest because its reader is a fresh agent with no shared context, so it must stand alone | paths and issue refs the worker reads |

**Observations, not physics.** Each number is calibrated against real traffic and should move when traffic says otherwise — but deliberately, in a PR, with the measurement. A bar quietly widened to fit today's message is not a bar.

- **Dispatch = task + pointers.** Hand workers paths and issue refs, not inlined context walls (the `[fleet memory: title (path)]` pattern, generalized).
- **Progress posts = one line + pointer.** Where a post is due, make it dense.

**Density, never frequency or routing.** Cadence is governed elsewhere (`checkin`, `inbound-acknowledgment`); this protocol still governs density only — substantive analysis still goes through the channel (messaging-channel-discipline); it just arrives dense. This protocol governs what a message contains, not whether it is sent.

**Never compress:** blockers, direct answers to direct questions, error output (verbatim), safety-relevant facts, required structured fields, and any substitution of the instrument or method from what was specified.
