---
title: Safe Worker Restart
description: Three-check guard before restarting a worker bot — protects against blowing away active WIP.
---

# Safe Worker Restart

Restarting a worker tmux session **clears its context**. Workers often have real mental state loaded: an active WIP branch, a partially-formed plan, subagent research, or a pending `[BOTREPORT]` about to land. Blowing that away mid-task is the most expensive mistake an orchestrator can make.

## Before restarting any worker, verify all three:

1. **tmux pane is idle.** `tmux capture-pane -t <bot> -p | tail -10` must NOT show any of:
   - `Thinking`, `Running`, `Reading`, `Writing`, `Editing`
   - `Spelunking`, `Prestidigitating`, or any other "active processing" verb
   - `esc to interrupt`
   
   If it does, the bot is mid-task — wait.

2. **No WIP on disk.** For every repo the worker operates in, run `git -C <dir> status --porcelain`. Any uncommitted changes mean a task is in flight. Don't restart.

3. **No pending report expected.** If you dispatched a task in the last ~5 min and haven't seen a `[BOTREPORT]`, the bot is still working. Give it time.

## Safe to restart when:

- All three checks above show idle/clean
- Bot is visibly stuck (>5 min of unchanged pane output AND no `[BOTREPORT]`)
- Context is >70% AND the current task is demonstrably complete (PR merged, final report received)
- The human explicitly requests it

## Reviewers are an exception

For reviewers (typically Sonnet, lower context budget): **do** restart above 60% context, because review sessions don't carry PR-level WIP — reviews are stateless between PRs and Sonnet degrades faster than Opus. Still send a one-line "restarting <reviewer>" note to Telegram for visibility.

## When in doubt, ask the human

The cost of an unneeded wait is low; the cost of nuking a half-finished PR is real. Frame the question concretely:

> "<bot> at 68% context after merging #472. Restart now to free headroom, or hold for any follow-on?"
