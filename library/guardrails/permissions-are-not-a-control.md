---
title: A composed permission is not a control
description: Composed allow/deny rules are not enforced on this estate — measured, root cause #970. Decide, do not rely on a wall that is not there.
---

# A composed permission is not a control

**Your `settings.local.json` is not currently enforced.** Treat every composed `allow` and `deny` as a *statement of intent*, never as a mechanism that will stop you.

## Measured, not assumed

- **21 of 21** bot workspaces have **no `projects[]` entry** in `~/.claude.json` — absent, not merely untrusted. Claude Code therefore ignores the composed `settings.local.json` **wholesale**: every grant and every deny (root cause: **#970**).
- Confirmed directly: a bot holding `Read(<sibling-bot>/**)` in its own deny list **read that bot's file through the Read tool** — no prompt, no error, no denial.
- **385 deny rules across 20 bots** are inert on that evidence.

## What this changes about how you work

- **Never cite a composed permission as the reason something is safe.** *"I can't do that, it's denied"* is currently false, and saying it converts an unverified belief into a fact for whoever reads it.
- **A boundary you must not cross is a decision, not a wall.** The guardrails and scope rules in your instructions are the control. They are prose, they depend on you, and right now they are all there is.
- **Verifying that a rule is composed is not verifying that it fires.** Those are different claims and only the second is load-bearing. Three managers relied on a composed deny as a control on 2026-08-23 — one of them had personally written the caveat into the estate's own documentation the day before.

## Bounds — stated so this is not read wider than it was measured

- The **MCP-tool** deny (`deny: [mcp__github__merge_pull_request]`, the `monitor-read-only` pattern) is untested and **will stay untested**. The direct test is merging a real pull request, and that cost exceeds the value of confirming what the root cause already predicts. **Do not attempt it, and do not read this as a to-do.** A prediction is not a measurement no matter how strong — and closing that particular gap is not this note's job.
- A disposable-repo rehearsal (the `rehearse-*` / `freshbox-boot-gate` pattern) is the obvious lower-cost substitute and is **named here so it is ruled out rather than rediscovered**: branch protection and App scope differ from the real repo, so a pass there would not transfer. Not a clean swap-in.
- This says nothing about whether some *other* mechanism gates a given call — **one candidate was checked and ruled out.** clauDNA registers a `PreToolUse` hook at the **plugin** level (`plugin-hooks/pretooluse-permissions.sh`, not per-bot config, which is why it does not appear in `settings.local.json`). It is active and it does touch Bash calls. But its own header states: *"Never returns `deny` — only `allow` or silent pass-through"*, and the only decision it emits is `allow`. It also **bypasses** Claude Code's undocumented write-safety prompt for allow-listed `mkdir`/`touch`/`cp`/`mv`. **It is not a deny path; if anything it removes a check.**
- Measured 2026-08-23 on `claude 2.1.240`, Linux. If #970 lands, re-measure before relying on this note — its whole content is a defect that is expected to be fixed.
