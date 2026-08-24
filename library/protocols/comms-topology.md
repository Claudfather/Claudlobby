---
title: Comms-Topology
description: Where a message goes and who it is for — channel-dependent routing, rich detail on disk. Routing only; per-message density is governed by token-efficiency.
---

# Comms-Topology

**Compression is channel-dependent, not global.** Compress the channel the human reads. Preserve decision breadth everywhere it is load-bearing. A protocol that shortens every surface equally saves tokens by deleting the reasoning the fleet runs on.

So the rule is not *be brief*. It is **put each thing where it belongs**:

| Channel | Carries | Why |
|---|---|---|
| **To the human** | the decision or the ask, and an address | it is the scarcest attention the fleet touches |
| **To disk** | the rich detail, in full | durable, greppable, and still there next week |
| **Bot to bot** | a pointer whose target is rich | the reasoning survives without every reader paying for it |

## To the human

**One channel, two-way: the manager.** Workers do not report to the human directly; they write the detail to disk and report to their manager, who synthesises and carries it.

- **Lead with the decision or the ask.** A reader who stops after the first sentence still has the actionable part.
- **The detail goes to disk and the message carries its address.** Never paste the long version.
- **One decision per message.** Two forks in one message means the second gets answered carelessly or not at all.
- **An explicit request for detail is answered in full, and never re-summarised.**

## To disk

**Before compressing anything, the full version must already exist at a stable address** — a PR, an issue, a worklog, a vault note, a path under your `data/`. No address, no compression: that is an omission wearing a summary's clothes.

Disk is the medium that survives a restart, a compaction and a fleet reorganisation. Prefer it to any message for anything worth having tomorrow.

## Bot to bot

**Messages carry pointers; the pointed-at thing is rich.** Decision breadth is preserved by the *target*, not by the message body.

**The target must be reachable by the receiver, not just well-formed for you.** A PR, an issue, a
shared doc or a vault note resolve the same way for anyone. **A path under your own `data/` does
not**, and it fails in two different ways — neither of which looks like a failure when you send it:

- **Absolute** — a sibling bot's directory is **denied by composed permission**, not merely
  inconvenient. Every bot carries `Read(<other-bot>/**)` deny rules.
- **Relative** — `data/notes.md` resolves against the **receiver's own** working directory. It does
  not error; it opens a **different file**, and the receiver has no way to know.

So point bot-to-bot only at genuinely shared addresses.

This is the ratified shape, and it has one dependency the fleet has not yet earned — stated here rather than discovered later.

> ### ⚠ This half depends on a mechanism measured as not working
>
> A pointer is worth exactly as much as the act that opens it, and on this estate that act does not happen. Measured (#1280): **7 deliberate fetches against 179 blind injections across six bots**, replicated on two fleets, with every precondition satisfied and the instruction composed.
>
> **Do not read this section as solved.** Until the fetch is act-bound rather than recall-bound, a pointer sent between bots is likely to be a message that carried nothing.

**Interim rules, which do not fix it and are not pretending to:**

- **Send what makes the pointer decidable.** Not a summary of the target — the one fact the
  receiver needs to know whether they must open it. *"Scope doc, three options, F2 is yours"* beats
  *"see the scope doc"*.
  **The recognition form, because the rule above is a judgement call and judgement calls do not
  fire:** if your message is `see <link>` and nothing else, that shape **is** the failure mode. The
  words are on your screen as you type them; that is the whole trigger.
- **Hand over the command, not the verdict.** A conclusion cannot be re-run; a path, a query or a command can. This is what makes a pointer useful to someone who does not already agree with you.
- **If the receiver must act on the content, do not point — carry it.** A pointer is a citation, not a delivery mechanism. Where reading it is a precondition of doing the work correctly, the work is what fails when nobody reads it.

## What this does not govern

- **Density** — how compressed any single message is: `token-efficiency`.
- **Cadence and frequency** — when and how often to post: `proactivity-discipline`, `messaging-channel-discipline`. This protocol never licenses silence.
- **Escalation thresholds** — what reaches the human at all: role expertise.

**Never compressed, on any channel:** blockers, direct answers to direct questions, error output, safety-relevant facts, and anything explicitly asked for in full.
