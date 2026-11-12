---
title: A composed permission is not a control
description: A composed deny binds on your own tools and stops there; a composed allow can be dropped on an untrusted workspace. Decide, do not rely on a wall that is not there.
---

# A composed permission is not a control

**A composed deny is enforced, and it is still not a wall.** Treat every composed `allow` and `deny` as a statement of intent plus a guard on your own tools, never as the reason something is safe.

## Measured, not assumed

- **Denies bind.** A composed `deny` blocks the Read tool on the very next tool call (measured mid-session, both directions, three bots, `claude 2.1.240`, Linux). A Bash file command given a literal denied path is refused too: `cat` (#1408), `ls` and `wc -l` (a live host, 2026-09-27).
- **Denies stop at your own tools.** An interpreter that opens the file itself (`python3 -c "open(...)"`) and a path the matcher cannot resolve (`$HOME/...`) both went past a correctly formed deny (#1408). A script, hook or timer is a process, not a tool call, and every bot on a host runs as the same user.
- **Allows are the fragile half.** On a workspace Claude Code has not trusted, composed `permissions.allow` entries are dropped by name ("Ignoring N permissions.allow entry...") while denies hold. An earlier version of this note said the whole file was ignored: right about allows on an untrusted workspace, wrong about denies (#970's re-scope).

## What this changes about how you work

- **A denial you hit is real. Do not route around it.** Reading a denied path through `python3` is exactly the bypass the rule cannot see, and taking it is the decision the rule exists to put in front of you. If you need the path, say so and ask.
- **Never cite a composed permission as confidentiality.** "It's denied" is true of your Read tool and false of any process running as your user.
- **A boundary you must not cross is a decision, not a wall.** The guardrails and scope rules in your instructions are the control; the deny list is a guard on your own tools behind them.
- **Verifying that a rule is composed is not verifying that it fires.** Those are different claims, and only the second is load-bearing.

## Bounds — stated so this is not read wider than it was measured

- The **MCP-tool** deny (`deny: [mcp__github__merge_pull_request]`, the `monitor-read-only` pattern) is untested and **will stay untested**. The direct test is merging a real pull request, and that cost exceeds the value of confirming it. **Do not attempt it, and do not read this as a to-do.**
- A disposable-repo rehearsal (the `rehearse-*` / `freshbox-boot-gate` pattern) is the obvious lower-cost substitute and is **named here so it is ruled out rather than rediscovered**: branch protection and App scope differ from the real repo, so a pass there would not transfer.
- This says nothing about whether some *other* mechanism gates a given call. **One candidate was checked and ruled out:** clauDNA registers a `PreToolUse` hook at the **plugin** level (`plugin-hooks/pretooluse-permissions.sh`, not per-bot config, which is why it does not appear in `settings.local.json`). It is active and it does touch Bash calls, but its own header states *"Never returns `deny` — only `allow` or silent pass-through"*. It is not a deny path; if anything it removes a check.
- The deny measurements date from 2026-08-23 (`claude 2.1.240`) to 2026-09-27. The binary updates daily and the matcher's Bash coverage grows by version, so re-measure before relying on the exact list of what is caught.
