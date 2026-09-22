#!/usr/bin/env python3
"""Decide whether a Bash command would change git STATE inside the vault.

The decision half of `lib/vault-git-guard.sh`, split out for the same reason
`mention-rewrite.py` is split out of the mention guard: the hard part is
parsing, parsing belongs somewhere unit-testable, and `sed`/`case` cannot carry
the rule legibly.

**Scope is decided before the verb, never the other way round.** The dangerous
failure here is not missing a rebase in the vault — it is refusing legitimate
git work in a bot's own `projects/` checkout, on every bot at once, because a
hook is live the instant `generate` writes it. So every path answers "is this
git invocation pointed INSIDE the vault" first, and a command that is not
vault-bound is allowed without ever looking at its verb.

**Where a git invocation points**, in order:
  1. every path named by a scope-setting flag — ``-C``, ``--git-dir``,
     ``--work-tree`` — and the invocation is vault-bound if ANY of them is,
  2. otherwise the last ``cd <path>`` appearing before that git token,
  3. otherwise the payload's ``cwd``.
Each candidate resolves through the equivalent of ``realpath -m`` (symlinks
followed, relative paths resolved against `cwd`, non-existent paths still
normalised).

Reading only ``-C`` — which is what this guard shipped with — leaves the two
flags a script reaches for when it walks several repositories without ``cd``-ing
into each. ``git --git-dir=<vault>/.git checkout <branch>`` then resolved its
scope from ``cwd``, and was allowed from anywhere else on disk.

**An unresolvable candidate falls back to `cwd` rather than to "allow".** A
shell variable, a glob or a quoted expression is exactly what an operator
reaches for when doing something wide, and treating "I cannot read this" as "it
must be outside the vault" would put the guard's blind spot precisely where the
risk is. If `cwd` itself is absent the command is ALLOWED and the caller emits
`vault_guard_unresolved`, so the fail-open is counted rather than silent.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys

#: git subcommands that move HEAD, the index or refs. Matched on the SUBCOMMAND
#: token alone — never a substring of the whole command line, or `git log
#: --grep=reset` would be refused.
STATE_VERBS = frozenset({
    "checkout", "switch", "rebase", "reset", "merge",
    "stash", "clean", "worktree", "cherry-pick", "revert", "am",
})

#: Verbs that are safe in general and dangerous only with a flag. Kept separate
#: because `commit` and `push` are how work normally leaves a bot, and refusing
#: them wholesale inside the vault would break capture rather than protect it.
CONDITIONAL = {
    "commit": ("--amend",),
    "push": ("--force", "-f", "--force-with-lease"),
    "branch": ("-d", "-D", "-m", "-M", "--delete", "--move"),
    "pull": ("--rebase", "-r"),
}

#: Verbs refused unless they carry an explicit SAFE flag — the inverse of
#: :data:`CONDITIONAL`, and the inversion is the whole reason it exists.
#:
#: `pull`'s dangerous form is the one with no flag at all. `pull.rebase` turns a
#: bare `git pull` into a rebase in the vault — the operation denied by name in
#: :data:`STATE_VERBS` above, reached by a command far more ordinary than the
#: one that spells it. A deny-list of flags cannot express that; only requiring
#: the safe spelling can. The vault's stated invariant is that it stays on its
#: default branch and is ONLY EVER FAST-FORWARDED, so the fast-forward spelling
#: is the one that passes and every other is refused.
#:
#: `pull` is in both tables deliberately. Measured on git 2.39.5:
#: `git pull --ff-only --rebase` is NOT rejected as a contradiction — git takes
#: the rebase path and asks which branch to rebase against — so carrying the
#: safe flag is not on its own evidence that the safe thing will happen.
NEEDS_SAFE_FLAG = {
    "pull": ("--ff-only",),
}

#: Flags that point a git invocation somewhere other than the shell's cwd.
#: `-C` was the only one this guard read when it first shipped, which left the
#: two flags a script reaches for when it walks several repositories WITHOUT
#: `cd`-ing into each — `--git-dir` and `--work-tree` — resolving their scope
#: from `cwd` and passing the vault check untested. That is accident-shaped,
#: and accident is the threat model here: the outage began with a bot running
#: `rebase` in the vault because its instructions were ambiguous, not with
#: anything trying to evade a check.
SCOPE_FLAGS = ("-C", "--git-dir", "--work-tree")

#: The subset of :data:`SCOPE_FLAGS` that replaces the WORKING TREE, and so
#: takes `cwd` out of the picture. `--git-dir` is absent on purpose.
WORKTREE_FLAGS = ("-C", "--work-tree")


def _resolve(path: str, cwd: str | None) -> str | None:
    """`realpath -m` semantics: normalise whether or not the path exists."""
    if not path:
        return None
    try:
        base = path if os.path.isabs(path) else os.path.join(cwd or "", path)
        return os.path.realpath(base)
    except (OSError, ValueError):
        return None


def _looks_unresolvable(tok: str) -> bool:
    """A token whose value this hook cannot know: a variable, a glob, a
    substitution. Not an error — a reason to fall back to `cwd`."""
    return any(ch in tok for ch in "$*?`") or tok.startswith("~")


def _enclosing_repo(path: str) -> str | None:
    """The git repository a path belongs to: nearest ancestor holding `.git`.

    Walks up rather than forking `git rev-parse`, which would cost a process on
    every guarded tool call.
    """
    cur = path
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _inside(path: str, vault: str) -> bool:
    """Is this path governed by the VAULT's git repository?

    **Not a prefix test, and that distinction is the whole guard.** Measured on
    a live host during this change's canary: every bot's `projects/<repo>`
    checkout nests INSIDE the vault directory — mine sat seven segments below
    it. A prefix test therefore refuses `checkout`, `rebase` and `reset` in
    every bot's own repository, fleet-wide, the instant `generate` runs. That is
    the exact failure this design is most afraid of, and it reached a live
    canary because the unit fixtures had made vault and projects SIBLINGS, a
    shape the real host does not have.

    A nested checkout has its own `.git`, so the question that actually matters
    is which repository the path belongs to: vault-bound iff the nearest
    enclosing repo IS the vault. A path under the vault directory but inside its
    own clone is that clone's business.
    """
    if not (path == vault or path.startswith(vault.rstrip("/") + "/")):
        return False
    repo = _enclosing_repo(path)
    if repo is None:
        # Under the vault directory but in no repository at all: the vault's
        # own tree, before any clone. Guarded.
        return True
    return repo == vault


def _scope_targets(args: list[str]) -> tuple[list[str], bool]:
    """Every path one git invocation points at, and whether it MOVED.

    Returns ``(targets, redirected)``. ALL targets rather than the first,
    because the verdict is any-of: each flag can independently aim the
    invocation at a different repository, and a command is vault-bound if any
    one of them is.

    ``redirected`` is true only for the flags that replace the WORKING TREE —
    ``-C`` (git chdirs there before anything else) and ``--work-tree``. It is
    deliberately false for ``--git-dir``, and that asymmetry is measured rather
    than reasoned: see :func:`_judge_git`.

    Both spellings are read — `--git-dir <p>` and `--git-dir=<p>` — since the
    attached form is the one a script writes.
    """
    out: list[str] = []
    redirected = False
    j = 0
    while j < len(args):
        a = args[j]
        if a in SCOPE_FLAGS and j + 1 < len(args):
            out.append(args[j + 1])
            redirected = redirected or a in WORKTREE_FLAGS
            j += 2
            continue
        matched = False
        for f in SCOPE_FLAGS:
            if a.startswith(f + "="):
                out.append(a[len(f) + 1:])
                redirected = redirected or f in WORKTREE_FLAGS
                matched = True
                break
        j += 1
    return out, redirected


def decide(command: str, vault: str, cwd: str | None) -> tuple[str, str]:
    """Return (verdict, detail). verdict is allow | deny | unresolved."""
    try:
        tokens = shlex.split(command, comments=False)
    except ValueError:
        # Unbalanced quotes: the command is not parseable, so no claim can be
        # made about where it points. Treated as unresolvable rather than
        # allowed outright, so it still falls back to cwd below.
        tokens = command.split()

    last_cd: str | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "cd" and i + 1 < len(tokens):
            last_cd = tokens[i + 1]
            i += 2
            continue
        if tok == "git" or tok.endswith("/git"):
            verdict, detail = _judge_git(tokens, i, vault, cwd, last_cd)
            if verdict != "allow":
                return verdict, detail
        i += 1
    return "allow", ""


def _judge_git(tokens: list[str], start: int, vault: str,
               cwd: str | None, last_cd: str | None) -> tuple[str, str]:
    """One git invocation: where does it point, and what does it do."""
    args = tokens[start + 1:]

    # --- SCOPE, first and always ------------------------------------------
    # An invocation is vault-bound if ANY of its targets is. Taking only the
    # first would let a harmless-looking `-C` launder the flag beside it:
    # `git -C /elsewhere --git-dir=<vault>/.git reset --hard` still moves the
    # vault's refs.
    flag_targets, redirected = _scope_targets(args)
    targets: list[str | None] = list(flag_targets)

    # WHERE THE SHELL IS STANDING STAYS A TARGET UNLESS SOMETHING REPLACED IT,
    # and `--git-dir` does not replace it. MEASURED, git 2.39.5: with a
    # `--git-dir` naming another repository and no `--work-tree`, git treats the
    # CURRENT DIRECTORY as that repository's working tree —
    # `git --git-dir=<other>/.git reset --hard` run inside the vault wrote the
    # other repo's tracked files into the vault's tree. So a scope flag pointing
    # somewhere else never subtracts the place the command is standing; only
    # `-C` (git chdirs first) and an explicit `--work-tree` do.
    if not redirected:
        targets.append(last_cd)  # may be None: resolved from `cwd` below

    resolved: list[str] = []
    unreadable = False
    for t in targets:
        r = None
        if t is not None and not _looks_unresolvable(t):
            r = _resolve(t, cwd)
        if r is None:
            unreadable = True
        else:
            resolved.append(r)
    if unreadable:
        if cwd is None:
            return "unresolved", "no cwd in payload and an unreadable target"
        here = _resolve(cwd, None)
        if here is not None:
            resolved.append(here)
    if not any(_inside(r, vault) for r in resolved):
        return "allow", ""

    # --- only now, the verb -----------------------------------------------
    verb = None
    k = 0
    while k < len(args):
        a = args[k]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            k += 2
            continue
        if a.startswith("-"):
            k += 1
            continue
        verb = a
        break
    if verb is None:
        return "allow", ""
    rest = args[k + 1:]

    if verb in STATE_VERBS:
        return "deny", verb
    for flag in CONDITIONAL.get(verb, ()):
        if flag in rest:
            return "deny", f"{verb} {flag}"
    safe = NEEDS_SAFE_FLAG.get(verb)
    if safe is not None and not any(f in rest for f in safe):
        return "deny", f"{verb} without {safe[0]}"
    return "allow", ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    ap.add_argument("--cwd", default="")
    ap.add_argument("--command", required=True)
    a = ap.parse_args()
    verdict, detail = decide(a.command, a.vault, a.cwd or None)
    print(f"{verdict}\t{detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
