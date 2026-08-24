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

- The **MCP-tool** deny (`deny: [mcp__github__merge_pull_request]`, the `monitor-read-only` pattern) has **not** been tested, because the only direct test is merging a real pull request. The root cause predicts identical failure. **Predicted is not measured** — do not report it either way.
- This says nothing about whether some *other* mechanism gates a given call. It says only that the composed permission is not the thing doing it.
- Measured 2026-08-23 on `claude 2.1.240`, Linux. If #970 lands, re-measure before relying on this note — its whole content is a defect that is expected to be fixed.
