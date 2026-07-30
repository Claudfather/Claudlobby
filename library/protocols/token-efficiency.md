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
- **Dispatch = task + pointers.** Hand workers paths and issue refs, not inlined context walls (the `[fleet memory: title (path)]` pattern, generalized).
- **Progress posts = one line + pointer.** The mandated cadence stands; make each beat dense.

**Density, never frequency or routing.** Acks, heartbeats, milestone cadence, wait-point beacons, and channel-routing rules stand unchanged — substantive analysis still goes through the channel (messaging-channel-discipline); it just arrives dense. This protocol governs what a message contains, not whether it is sent.

**Never compress:** blockers, direct answers to direct questions, error output (verbatim), safety-relevant facts, required structured fields.
