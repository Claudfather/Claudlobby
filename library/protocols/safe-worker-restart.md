---
title: Safe Worker Restart
description: Three-check guard before restarting a worker bot or giving it a fresh session — protects against losing active WIP.
---

# Safe Worker Restart

A plain restart (`claudlobby --json bot restart BOT`) **continues the worker's exact session**: the same conversation at its current size, so it neither loses context nor relieves it. `bot restart BOT --fresh` starts a **new session with an empty context** that resumes from the worker's saved handoff, and `bot compact BOT` relieves context in place. A fresh session keeps only what the handoff and the disk hold, and workers often have real mental state loaded: an active WIP branch, a partially-formed plan, subagent research, or a pending linked report about to land. Blowing that away mid-task is the most expensive mistake an orchestrator can make.

## Before restarting any worker, verify all three:

1. **Current work is settled.** Read `claudlobby --json bot status BOT`, the
   current canonical assignment, and `claudlobby --json fleet reports list
   --bot BOT`. Require positive evidence that work has finished or a human has
   authorized interruption. A quiet session or unavailable reader is not proof
   that the worker is idle.

2. **No WIP on disk.** For every repo the worker operates in, run `git -C <dir> status --porcelain`. Any uncommitted changes mean a task is in flight. Don't restart.

3. **No pending report expected.** If you dispatched a task in the last ~5 min and haven't received its linked report, the bot is still working. Give it time.

Before a fresh session, request `claudlobby --json bot handoff BOT`. A handoff is a
model turn that often takes minutes and the request waits 30 s, so `unknown`
with reason `timeout` can still land: read `claudlobby --json bot handoff BOT
--check`, which sends nothing, until it reads `saved`. While it reads
`pending`, never request another handoff. One still `pending` once
`claudlobby --json bot status BOT` shows the bot idle was not written: ask the
bot in plain words to write its session handoff, then check again. Continue
only when the request or the check reads `data.handoff=saved`; anything else
stops this sequence.
Then run `claudlobby --json bot restart BOT --fresh` and inspect its readiness result.
An explicit operator restart hold overrides the criteria below.

## Safe to restart when:

- All three checks above show idle/clean
- Bot is visibly stuck (>5 min of unchanged session evidence AND no linked progress report)
- The worker has reported `context-degraded` AND the current task is demonstrably complete (PR merged, final report received). Only a fresh session or a compaction relieves it.
- The human explicitly requests it

## Reviewers are an exception

For reviewers (typically Sonnet, lower context budget): **do** give them a fresh session (`bot restart REVIEWER --fresh`) on the first `context-degraded` report, or after ~3 completed reports in a verified 24h window, because review sessions don't carry PR-level WIP — reviews are stateless between PRs and Sonnet degrades faster than Opus. Read `claudlobby --json fleet reports list --bot REVIEWER --status completed --since RFC3339_CUTOFF`, deriving the offset-bearing cutoff from the current time and following `next_cursor`. Still send a one-line "fresh session for <reviewer>" note to Telegram for visibility.

**Count the rows, do not count an empty result.** Use the selected fleet context, inspect the result envelope for an error, and follow every `next_cursor` before claiming a count. An unreadable or incomplete page is unknown, not zero. **A zero you have not seen the command succeed on is not a zero.**

## When in doubt, ask the human

The cost of an unneeded wait is low; the cost of nuking a half-finished PR is real. Frame the question concretely:

> "<bot> reported context-degraded after merging #472 — 3 tasks closed this session. Fresh session now, or hold for any follow-on?"
