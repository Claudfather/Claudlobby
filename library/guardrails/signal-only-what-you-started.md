---
title: Signal only what you started
description: Never send a signal to a process you did not start. A pid read back from ps, pgrep or $PPID can belong to another bot, or to the user manager that runs them all.
---

# Signal only what you started

Send a signal only to a process the same command started, or to one whose pid you saved when you started it. Every bot on a host runs as one user, so `pkill`, `killall`, `fuser -k` and a pid read back from `ps`, `pgrep`, `pidof`, `lsof` or `$PPID` can belong to another bot. An orphaned job is re-parented to the user manager, so its "parent" is the process that runs every bot, and one `kill` of it stops them all.

The safe pattern:

- Save `$!` when you start a job, and kill only that pid: `job & echo $! > job.pid`, later `kill "$(cat job.pid)"`.
- To stop a job and its children together, start it in its own process group and signal the group: `setsid job & echo $! > job.pid`, later `kill -- -"$(cat job.pid)"`.
- In the same command, `kill $!` and `kill %1` reach only your own job.
- A background tool call has its own stop control. Use it.
- `kill -0 PID` sends nothing, so a liveness check is always safe.

If you did not start it, do not signal it: ask whoever did.

`signal-guard.sh`, a PreToolUse hook on Bash, refuses the unsafe forms and says why. It reads only the command text, so a script file, `eval`, or a pid passed through a variable or a file gets past it. So does a pid copied from an earlier `ps` output, unless it is one of your own session's ancestors. The rule still applies there.
