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

**The effective directory** of a git invocation, in order:
  1. the argument of ``git -C <path>`` when present,
  2. otherwise the last ``cd <path>`` appearing before that git token,
  3. otherwise the payload's ``cwd``.
Each candidate resolves through the equivalent of ``realpath -m`` (symlinks
followed, relative paths resolved against `cwd`, non-existent paths still
normalised).

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
}


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
    target: str | None = None
    for j, a in enumerate(args):
        if a == "-C" and j + 1 < len(args):
            target = args[j + 1]
            break
        if a.startswith("-C="):
            target = a[3:]
            break
    if target is None:
        target = last_cd

    resolved: str | None = None
    if target is not None and not _looks_unresolvable(target):
        resolved = _resolve(target, cwd)
    if resolved is None:
        # Unreadable target, or none given: the invocation runs wherever the
        # shell already is.
        if cwd is None:
            return "unresolved", "no cwd in payload and an unreadable target"
        resolved = _resolve(cwd, None)
    if resolved is None or not _inside(resolved, vault):
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
