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
- The worker has reported `context-degraded` AND the current task is demonstrably complete (PR merged, final report received)
- The human explicitly requests it

## Reviewers are an exception

For reviewers (typically Sonnet, lower context budget): **do** restart on the first `context-degraded` report, or after ~3 completed rows in a 24h `claudlobby --fleet {{FLEET_NAME}} report-back` window, because review sessions don't carry PR-level WIP — reviews are stateless between PRs and Sonnet degrades faster than Opus. Still send a one-line "restarting <reviewer>" note to Telegram for visibility.

**Count the rows, do not count an empty result.** The plane's rows are per fleet, so `--fleet` is what scopes a `report-back` query; a run that cannot be scoped or cannot reach the plane REFUSES (rc 3, `UNREACHABLE` on stderr) rather than printing an empty result. Before #1216 the flagless form was silent — empty stdout, exit 0 — so "0 completed" and "I could not read the record" were the same output, and the failure direction is *do not restart*, which is the one nobody investigates. **A zero you have not seen the command succeed on is not a zero.**

## When in doubt, ask the human

The cost of an unneeded wait is low; the cost of nuking a half-finished PR is real. Frame the question concretely:

> "<bot> reported context-degraded after merging #472 — 3 tasks closed this session. Restart now, or hold for any follow-on?"
