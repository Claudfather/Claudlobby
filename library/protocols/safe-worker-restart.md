---
title: Safe Worker Restart
description: Three-check guard before restarting a worker bot — protects against blowing away active WIP.
---

# Safe Worker Restart

Restarting a worker tmux session **clears its context**. Workers often have real mental state loaded: an active WIP branch, a partially-formed plan, subagent research, or a pending linked report about to land. Blowing that away mid-task is the most expensive mistake an orchestrator can make.

## Before restarting any worker, verify all three:

1. **Current work is settled.** Read `claudlobby --json bot status BOT`, the
   current canonical assignment, and `claudlobby --json fleet reports list
   --bot BOT`. Require positive evidence that work has finished or a human has
   authorized interruption. A quiet session or unavailable reader is not proof
   that the worker is idle.

2. **No WIP on disk.** For every repo the worker operates in, run `git -C <dir> status --porcelain`. Any uncommitted changes mean a task is in flight. Don't restart.

3. **No pending report expected.** If you dispatched a task in the last ~5 min and haven't received its linked report, the bot is still working. Give it time.

Before stopping, request `claudlobby --json bot handoff BOT` and require
`data.handoff=saved`; refused, skipped or unknown handoffs stop this sequence.
Then run `claudlobby --json bot restart BOT` and inspect its readiness result.
An explicit operator restart hold overrides the criteria below.

## Safe to restart when:

- All three checks above show idle/clean
- Bot is visibly stuck (>5 min of unchanged session evidence AND no linked progress report)
- The worker has reported `context-degraded` AND the current task is demonstrably complete (PR merged, final report received)
- The human explicitly requests it

## Reviewers are an exception

For reviewers (typically Sonnet, lower context budget): **do** restart on the first `context-degraded` report, or after ~3 completed reports in a verified 24h window, because review sessions don't carry PR-level WIP — reviews are stateless between PRs and Sonnet degrades faster than Opus. Read `claudlobby --json fleet reports list --bot REVIEWER --status completed --since RFC3339_CUTOFF`, deriving the offset-bearing cutoff from the current time and following `next_cursor`. Still send a one-line "restarting <reviewer>" note to Telegram for visibility.

**Count the rows, do not count an empty result.** Use the selected fleet context, inspect the result envelope for an error, and follow every `next_cursor` before claiming a count. An unreadable or incomplete page is unknown, not zero. **A zero you have not seen the command succeed on is not a zero.**

## When in doubt, ask the human

The cost of an unneeded wait is low; the cost of nuking a half-finished PR is real. Frame the question concretely:

> "<bot> reported context-degraded after merging #472 — 3 tasks closed this session. Restart now, or hold for any follow-on?"
