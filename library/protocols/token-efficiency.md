---
title: Token-Efficiency
description: Lossless brevity for outbound comms — reference-don't-paste, TLDR-first, structured deltas. Density rules only; posting cadence and channel routing are governed elsewhere.
---

# Token-Efficiency

Every outbound message costs tokens twice — once to write, once in every context that reads it. Compress losslessly: cut tokens, never signal.

**Rule zero — lossless, never lossy.** A cut is a pointer or an expand-on-demand, never an omission. Before compressing, the full detail must exist at a stable address (PR, issue, shared doc, repo path, your `data/`). No address → don't compress.

- **Reference, don't paste.** Work products travel as `path-or-URL + one-line what`. Never paste file bodies, diffs, or logs when an address exists — create the address first if needed.
- **Telegram = TLDR-first.** Outcome in 1–3 sentences + pointer(s). When someone asks for detail, give it in full — an explicit request is never re-summarized.
- **Reports = structured deltas.** status | what changed | blockers | next | pointers. No session narratives; the `[BOTREPORT]` summary stays one line.

### The bars

A disposition nobody can fail is not a rule. These are countable, so check before you send:

| Surface | Bar | Overflow goes |
|---|---|---|
| Telegram / chat message | **≤ 600 chars** | a pointer — issue, PR, doc path |
| `[BOTREPORT]` summary | **≤ 200 chars**, one line | the PR/issue body, or your `data/` |
| Progress post | **≤ 200 chars** | the work itself |
| Dispatch payload | **≤ 800 chars** | paths and issue refs the worker reads |

**One line is not a length.** A 2,000-character single line satisfies "stays one line" and defeats the entire point — the reader has to parse a wall to find the verdict. Line *count* was never the constraint; reading cost is.

**Lead with the decision.** First sentence = the outcome or the ask. Evidence, caveats, and method come after, or at a pointer. A reader who stops after one sentence should still have the actionable part.

**Over the bar is a signal, not a sin.** It means the detail has no address yet. Create the address — file the issue, push the branch, write the doc — then send the pointer. That is the fix; trimming words is not.

**Exempt, as always:** blockers, direct answers to direct questions, error output, safety-relevant facts, and anything explicitly asked for in full.
- **Dispatch = task + pointers.** Hand workers paths and issue refs, not inlined context walls (the `[fleet memory: title (path)]` pattern, generalized).
- **Progress posts = one line + pointer.** The mandated cadence stands; make each beat dense.

**Density, never frequency or routing.** Acks, heartbeats, milestone cadence, wait-point beacons, and channel-routing rules stand unchanged — substantive analysis still goes through the channel (messaging-channel-discipline); it just arrives dense. This protocol governs what a message contains, not whether it is sent.

**Never compress:** blockers, direct answers to direct questions, error output (verbatim), safety-relevant facts, required structured fields.
