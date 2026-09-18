---
title: Check-in
description: The idle-manager check-in and the thin human edge
---

# Check-in

The manager's own re-engagement cycle: read the fleet's state, decide one
project and one action, record the decision, and let the surfacing judgment
decide whether the operator hears anything at all — on a beat, recorded. The
`manager-checkin` fleet job dispatches `/checkin` into an idle, equipped
manager, or the operator sends it by hand. **The manager never fires its own
check-in**: the trigger owns the beat and its throttles (the minimum gap
between beats, the busy/idle gate); self-firing would carry none of them.

This protocol governs where it composes beside the fleet's older cadence
rules — milestone beacons, "idle silence is a bug" (`proactivity-discipline`),
a Telegram post per merge (`worker-lifecycle`) — which still stand. Those
describe a fleet with no recorded beat; this one has it, so on a bot
carrying both, silence-by-default governs what reaches the operator and the
older rules govern everything else, unchanged.

## Manager

**Silence is the default.** A post is the exception the judgment must
justify and record. A check-in that dispatches or chooses `nothing` posts
nothing — it is in the plane. A justified post is fixed in shape: one status
line, one ask with named options, one pointer (a plane URL or a PR). At most
one post per check-in — qualifying items across every project coalesce into
that one message, never one per project; held items wait for the next
justified post. Never restate a project's rigor — point to it.

The rate limit on asking: read `claudlobby checkins --bot $BOT_ID --since 7d
--raised` first. Two open, unanswered asks this week means the fleet
proceeds on its best tier-gated judgment or waits quietly, never a third. An
urgency floor breaks through regardless: a blocker that stalls the fleet, or
a failure with real cost.

## Worker

One thin line on start / done / blocked: Telegram where the worker is
configured for it, plane-only where it is not. No milestone cadence here —
that stands in `worker-lifecycle`. Detail goes through `report-back.sh` to
the manager and the plane; every line is a recorded communication
regardless of carrier.
