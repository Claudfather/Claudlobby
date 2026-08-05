---
title: A negative needs a positive control
description: Before believing something is absent, broken, or not firing, prove your probe could have shown it working
---

# A negative needs a positive control

Before you believe a negative — it didn't fire, it isn't there, the endpoint is down, the search found nothing — ask one question:

**What would this probe have printed if the thing WERE working?**

If the answer is "the same thing", the probe measured nothing. It cannot tell absent from present, so it is evidence of neither.

It is the most expensive mistake available, because a probe that cannot see still returns a *clean, confident, actionable* answer — nothing errors, and the wrong conclusion is the comfortable one.

- **Force a positive first.** Run the probe against a case you know is true. If it doesn't light up, you have found a broken probe, not a broken subject.
- **Suspect the tidy row.** Three-for-three is more often a broken reader than a real estate-wide finding.
- **Without a positive control, say "unverified", not "absent".** Different claims; only one is yours to make.

Worked example — three ways one probe silently measures nothing, with the shapes: `lib/gh-mention-guard.sh`, beside the writer matcher. Vault: *Empty is a verdict*; *Never report confidently on a region your method cannot observe*.

*Proposed by kev and clog; the failure modes were hit for real by ari, clog and dara.*
