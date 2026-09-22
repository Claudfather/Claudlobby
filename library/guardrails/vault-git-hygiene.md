---
title: The vault's git state is not yours to move
description: Branch, rebase and reset happen in a projects/ checkout — never in the vault clone. A composed hook enforces it; this says why, so the refusal is not the first you hear of it.
---

# The vault's git state is not yours to move

The vault is a git checkout that every bot on the host reads its fleet manifests
from and writes its knowledge into. It stays on its default branch and is only
ever fast-forwarded.

**What is refused, inside the vault only:** `checkout`, `switch`, `rebase`,
`reset`, `merge`, `stash`, `clean`, `worktree`, `cherry-pick`, `revert`, `am`,
plus `commit --amend`, `push --force`/`--force-with-lease`,
`branch -d`/`-D`/`-m`, and **any `pull` that is not `--ff-only`**. A composed
`PreToolUse` hook refuses these before the command runs.

`pull` is on that list for the same reason the clone is only ever
fast-forwarded: under `pull.rebase` a bare `git pull` *is* a rebase in the
vault, so the fast-forward spelling is the one that passes and nothing else is.

**What is not touched:** everything else in the vault — `status`, `log`, `diff`,
`add`, `commit`, `fetch`, `pull --ff-only`, `push` — and *every* git command in
your own `projects/` checkouts, including the refused verbs. Scope is decided
before the verb: a command that is not pointed inside the vault is allowed
without its verb being read.

**Where a command points is read from the flags, not just from where you are.**
`-C`, `--git-dir` and `--work-tree` all aim a git invocation, and a preceding
`cd` does too; if any of them names the vault, the command is the vault's
business wherever it was typed. So reaching for `--git-dir` to work on several
repositories at once does not step around this page — it is the ordinary way
someone arrives here without meaning to.

**Why it is a hook and not just this page.** A vault clone on a side branch is
the one state where captures look durable in `git log` and exist on no other
machine. On a live host that state lasted a month: 59 commits of knowledge
accrued off the default branch, 53 of them never pushed anywhere, and the
rebase that finally tried to reconcile them was killed mid-pick. The tree sat
detached for twelve days with the wrong files checked out — a fleet's mission,
charter and project manifest gone from disk — and a `generate` composed from
the reverted manifest. Guidance saying "do not" already existed.

**If the vault looks wedged, do not repair it by hand.** Do not abort a rebase,
reset, or check anything out. Report it. An aborted rebase in a live tree is
how the twelve days started.

**To move the vault, use `claudron sync`.** It is the only door that is allowed
to change that clone's state, and it is the one place the safety checks live.
