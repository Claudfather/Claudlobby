---
title: Context Management
---

# Context Management

- **After each completed task** → `/compact`
- **Switching repos / projects** → `/clear`
- **Stuck > 3 min** → stop, run `report-back.sh blocked`, don't spin

## Do not report a context percentage

**No tool reports your context usage to you.** No status line is composed for
any bot, and the pane carries no percentage.

So do not state a figure, and do not let a request for one talk you into
producing it. A number you cannot read is a number you would be **inventing**,
which the `no-fabrication` guardrail forbids outright — and unlike most
fabrications this one gets acted on, because restarts are routed by it. If you
are asked for a percentage, say the instrument does not exist and give the
observable picture below instead.

## Judge headroom by what you can observe

**Count the work.** Since this session started, or since your last `/clear`,
how many *units* have you finished — a PR opened, a review posted, an
investigation closed, a task cycle run end to end? Two or three substantial
units is where degradation typically starts to show. Treat that as a prompt to
check for the symptoms below, not as a threshold in itself.

**Lean on subagents early.** Once you have finished a couple of units, route
further reading through Explore/Plan subagents rather than pulling files into
your own context. This rung used to be keyed to "above 50%"; the action never
needed the number, and doing it early is close to free.

**Name the symptoms.** Any **one** of these is enough — they do not accumulate
into a score:

- You re-read a file you already read this session.
- You look up the same fact, path, or issue number a second time.
- You re-derive a decision you already made, or contradict your own earlier
  conclusion in the same session.
- You lose the thread mid-task and have to re-read the dispatch to recover it.
- You summarize your own earlier work and get it wrong.

## Your duty when you notice one

**Say so at your next report-back, in plain words, including the literal token
`context-degraded`.** That token is what your manager routes on — it is
greppable in the ledger in a way a prose hedge is not.

```bash
report-back.sh <your-bot-name> progress "context-degraded — re-read auth.py twice this session; 3 units done. Safe to restart, no WIP." --task <id>
```

Then state whether a restart is safe **right now**: is your work committed, is
a report pending, is a PR half-open? That is the part only you know, and it is
what decides whether the manager acts immediately or waits.

Then take no further major task until the manager answers. Raising it early is
the cheap path — a restart costs minutes, a degraded review cycle costs more —
and pushing through silently is the failure mode this protocol exists to
prevent.
