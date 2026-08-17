---
title: A pipe reports the last stage's exit status, so a failed command can read as rc 0
description: Never take $? after a pipe as the status of the command you care about — use ${PIPESTATUS[0]} or run it unpiped, and read the body rather than any single status
---

# A pipe reports the last stage's exit status, so a failed command can read as rc 0

A shell pipeline's `$?` is the **last** stage's exit status. So `<anything> | head`, `| grep`, `| jq`,
`| tail`, `| awk` reports *that* filter's status — almost always `0` — no matter how the real command
failed. The failure prints as success.

**This is a property of the shell, not of any one tool.** It does not depend on which CLI you ran, which
API it called, or how it failed — auth, network, a bad flag and a timeout all produce it identically.

## The rule

- **Never read `$?` after a pipe** when you need the status of an earlier stage.
- Use **`${PIPESTATUS[0]}`**, or run the command **unpiped** first and capture its status.
- **A status alone is not enough.** Many tools write their error body to **stdout**, so anything already
  reading the stream is parsing an error that looks like a payload: a `jq` default substitutes silently,
  and even a non-empty `grep -c` can be counting the error text. **Read the body.**
- Never pipe into a filter as the *only* consumer when you intend to gate on success.

## It has bitten at least three unrelated tools here

The generalisation is stated because it was independently rediscovered, not because it sounds likely:

| tool | shape | where it is recorded |
|---|---|---|
| `gh` | `gh api … \| head` returned rc `0` against a body reading `Bad credentials`, under a fleet-wide 401 | `library/integrations/github.md` |
| `pytest` | piping into `grep` captures grep's status, which is only ever 0 or 1, laundering every broken run into "this is evidence" | repo `CLAUDE.md` |
| `claudron` | `claudron status` on a directory with no vault exits **rc 3**; taken through `\| head` it reads `0` | measured 2026-08-17 |

The `claudron` row is the one that shows the reach: it is not a GitHub tool, not an API client, and has
nothing in common with `gh` except being a command in a pipeline.

## Why vigilance is the wrong instrument

**The failure wears the exact shape of the answer you were looking for.** rc `0` is what success looks
like, so nothing prompts a second look — which is why this is written as a rule rather than a caution,
and why a checked helper is tracked separately (claudlobby #1066). Treat the rule as an interim measure,
not a solved problem.

**Related, and deliberately not restated here:** the `gh`-specific rungs — which `gh` surfaces stay alive
under a 403, and the 401-versus-403 discriminator — live in `library/integrations/github.md`. The
send-side shell hazards (history expansion on `!word`, backticks as command substitution inside
double quotes) are in the `dispatch` protocol and `library/lessons/tmux-dispatch-shell-expansion.md`.
