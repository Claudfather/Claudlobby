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

**And pointing a command elsewhere is not the same as standing elsewhere.**
A git command aims two things separately, and one flag rarely moves both:

- **`--git-dir`** names the repository. The working tree is still the directory
  you are in — so from inside the vault, `git --git-dir=<other>/.git reset
  --hard` writes that other repo's files *into the vault*.
- **`--work-tree`** names the working tree. The repository is still the one
  found from the directory you are in — so from inside the vault,
  `git --work-tree=<elsewhere> checkout <branch>` moves *the vault's own HEAD*.
- **`-C`** moves both, because git changes directory before anything else.

Only `-C`, or `--git-dir` and `--work-tree` together, take the vault out of a
command's reach. Everything else run from inside the vault is the vault's
business whatever it points at.

**Setting `GIT_DIR` or `GIT_WORK_TREE` is the same as passing the flag.** Git
honours them identically. `GIT_DIR=<vault>/.git git reset --hard` is a command
aimed at the vault however far away you are standing, and `GIT_WORK_TREE=<away>
git checkout <branch>` run *inside* the vault moves the vault's own HEAD — the
same pair of traps as the two flags above, through a different door. Both are
refused like any other. A flag beats the variable, which is git's own rule.

**A flag this guard has not been taught is refused, not waved through.** It
knows git's pre-verb options and how many words each consumes; anything else
means it cannot reliably tell which repository is being aimed at, or even which
word is the subcommand, so it stops rather than guess. **This one refuses
wherever you are**, unlike every other rule here — once the guard has stopped
reading, "this is not pointed at the vault" would be a claim about the half it
managed to parse. If you hit it, the flag is probably fine and the guard simply
has not been taught it: say so rather than working around it.

**What this guard is, and what it is not.** It is a bar against the ordinary
accident: it catches a git command that names the vault directly. It is **not**
a fence. It reads one command line, and shell composition is unbounded — a
subshell, a variable holding the path, `eval`, `sh -c`, a wrapper script, an
alias. Nor can it see a `GIT_DIR` or `GIT_WORK_TREE` exported by an earlier
command, or git config that moves the tree.

**So the rule is the thing you are following, not the hook.** Where the hook
cannot see, the rule still stands: the vault's git state is not yours to move.
If you find yourself composing a command that would reach it, that is the
moment the page applies, and there is nothing there to stop you.
