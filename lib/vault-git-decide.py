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

**WHICH CHANNELS THIS GUARD MODELS, and which it does not.** Git's scope can
be set through several independent channels, and this predicate reads exactly
two of them. Nine holes have now been found in it; the first eight were all one
channel's flags, and the ninth was a second channel that argv parsing cannot
see at all. That is what a scope predicate over another tool's CLI costs, and
the useful response is to state the bound rather than to keep implying
coverage:

  MODELLED
    * the pre-verb flags in ``GLOBAL_FLAGS`` -- ``-C``, ``--git-dir``,
      ``--work-tree`` aim the invocation; anything outside that table stops the
      read and refuses.
    * ``GIT_DIR`` / ``GIT_WORK_TREE`` assigned WITHIN the same command, in any
      of its spellings (``VAR=x git ...``, ``export VAR=x; git ...``,
      ``env VAR=x git ...``).
    * a ``cd`` earlier in the same command, and the payload's ``cwd``.

  NOT MODELLED -- each one is a real way to reach the vault that this returns
  "allow" for, and none is a hypothetical:
    * ``GIT_DIR`` exported by an EARLIER tool call. The hook is handed one
      command and no environment, so this is not a gap to be closed here; it
      needs a different instrument.
    * git config that relocates the tree -- ``core.worktree``, and
      ``safe.directory`` widening what git will touch at all.
    * an alias, a shell function, a wrapper script, or ``sh -c``: none presents
      a ``git`` token for the walk in :func:`decide` to find (Claudlobby #1730).
    * whether a path belongs to the vault's repository is :func:`_inside`'s
      question, and it has two known wrong answers of its own (#1729).

So the honest claim is that this refuses what it cannot read **of a git command
line**, not that it cannot be got around. A reader who needs the second
property needs a different mechanism, and should meet that here rather than
infer it from the care taken below.

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

#: git's pre-verb global options and how many argv slots each consumes.
#:
#: **This table is the guard's model of git's own CLI, and anything outside it
#: is a refusal rather than a bypass.** The predicate reached six holes by
#: enumerating what is DANGEROUS and letting everything else through; each hole
#: was a different way for a command to point somewhere the guard did not look,
#: and each was found by a different method. Enumerating what is UNDERSTOOD
#: inverts that: a flag nobody modelled stops the read instead of slipping past
#: it.
#:
#: Arity matters as much as membership. The verb is the first non-flag token,
#: so a flag whose separated value gets read as a verb makes the guard judge
#: the WRONG WORD: under the old parser `git --super-prefix x/ checkout main`
#: was judged on `x/`, found no dangerous verb, and was allowed — while git
#: itself reads `checkout` there (measured, 2.39.5).
#:
#: Every arity below was MEASURED on git 2.39.5 rather than read off the
#: synopsis, because the synopsis does not settle it: `--git-dir=<path>` is
#: documented attached and git accepts `--git-dir <path>` too.
#:
#: THE TABLE IS DELIBERATELY MINIMAL, and that is the safe direction. Omitting
#: a real flag costs a refusal inside the vault and nothing anywhere else;
#: including one with the wrong arity re-opens exactly the hole above. So a
#: flag earns a row by appearing in commands bots actually write, not by
#: existing — `--super-prefix` and `--config-env` are both real and both absent.
GLOBAL_FLAGS = {
    # value-taking, separated form measured as accepted
    "-C": 1, "-c": 1, "--git-dir": 1, "--work-tree": 1, "--namespace": 1,
    # no value
    "--bare": 0, "--no-replace-objects": 0, "--no-optional-locks": 0,
    "--literal-pathspecs": 0, "--glob-pathspecs": 0, "--noglob-pathspecs": 0,
    "--icase-pathspecs": 0, "--exec-path": 0, "--paginate": 0, "-p": 0,
    "--no-pager": 0, "-P": 0, "--help": 0, "-h": 0, "--version": 0, "-v": 0,
    "--html-path": 0, "--man-path": 0, "--info-path": 0,
}

#: The two flags that relocate something, and WHAT each one relocates. They do
#: not compose the way the obvious reading suggests, which is the whole reason
#: this is a table rather than a set — see :func:`_judge_git`.
#: Environment variables git honours exactly as it honours the flags above.
#: MEASURED: `GIT_DIR=<vault>/.git git symbolic-ref --short HEAD` run from
#: outside the vault answers the VAULT's branch, and
#: `GIT_DIR=<a> git --git-dir=<b> rev-parse --absolute-git-dir` answers `<b>`,
#: so a flag beats the variable. This is a SECOND CHANNEL, not another flag:
#: everything the allowlist does is correct and simply does not apply to a
#: caller that exports scope instead of passing it.
ENV_SCOPE = ("GIT_DIR", "GIT_WORK_TREE")

#: Each axis, in git's own precedence: the flag, then the variable, then
#: wherever `-C` (or the shell) left us.
REPO_ORDER = ("--git-dir", "GIT_DIR", "-C")      # which repository's refs move
TREE_ORDER = ("--work-tree", "GIT_WORK_TREE", "-C")  # which files are written


def _parse_git_args(args: list[str]) -> tuple[dict[str, str], str | None,
                                              int, str | None]:
    """``(scope, verb, verb_index, unrecognised)`` for one invocation's argv.

    Walks the pre-verb options using :data:`GLOBAL_FLAGS` arities, so the verb
    is the first token that is genuinely a subcommand rather than some flag's
    value. The first token that looks like an option and is not in the table
    stops the walk and is returned as ``unrecognised`` — from there neither the
    scope nor the verb can be trusted, and the caller fails closed.
    """
    scope: dict[str, str] = {}
    j = 0
    while j < len(args):
        a = args[j]
        if not a.startswith("-"):
            return scope, a, j, None
        name, eq, value = a.partition("=")
        if eq:
            if name not in GLOBAL_FLAGS:
                return scope, None, j, a
            scope.setdefault(name, value)
            j += 1
            continue
        arity = GLOBAL_FLAGS.get(a)
        if arity is None:
            return scope, None, j, a
        if arity and j + 1 < len(args):
            scope.setdefault(a, args[j + 1])
        j += 1 + arity
    return scope, None, len(args), None




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
    env: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "cd" and i + 1 < len(tokens):
            last_cd = tokens[i + 1]
            i += 2
            continue
        # A scope variable, however it was spelled: a bare `GIT_DIR=x git ...`
        # prefix, `export GIT_DIR=x; git ...`, or `env GIT_DIR=x git ...` all
        # arrive as this one token shape, so one test covers all three.
        name, eq, value = tok.partition("=")
        if eq and name in ENV_SCOPE:
            env[name] = value
            i += 1
            continue
        if tok == "git" or tok.endswith("/git"):
            verdict, detail = _judge_git(tokens, i, vault, cwd, last_cd, env)
            if verdict != "allow":
                return verdict, detail
        i += 1
    return "allow", ""


def _judge_git(tokens: list[str], start: int, vault: str, cwd: str | None,
               last_cd: str | None,
               env: dict[str, str] | None = None) -> tuple[str, str]:
    """One git invocation: where does it point, and what does it do."""
    args = tokens[start + 1:]
    scope, verb, verb_idx, unrecognised = _parse_git_args(args)

    # AN OPTION THIS GUARD CANNOT READ REFUSES, FULL STOP -- and the word
    # "unconditional" is the entire fix. This check used to sit AFTER the scope
    # test, so it only fired once the part that HAD been parsed already said
    # vault-bound. That is a statement about a partial read, and it made the
    # verdict depend on flag ORDER: a real `--git-dir` sitting behind an
    # unmodelled flag was never reached, so the same command allowed or denied
    # depending on which flag came first. The safety property the allowlist was
    # adopted for -- an unmodelled flag becomes a refusal rather than a bypass
    # -- is only true when nothing is consulted before it.
    #
    # THIS DENY HAS NO ALLOW TWIN, and that is deliberate rather than an
    # oversight in a file where every other deny has one. A twin would assert
    # that some unreadable command is safe, which is the claim this branch
    # exists to stop making. The cost is bounded and was measured rather than
    # assumed: every pre-verb flag in this fleet's own scripted git usage is
    # already in GLOBAL_FLAGS, so nothing real is refused today.
    if unrecognised is not None:
        return "deny", f"an option this guard does not recognise ({unrecognised})"

    # A flag beats the variable, which is git's own precedence -- measured:
    # `GIT_DIR=<a> git --git-dir=<b> rev-parse --absolute-git-dir` answers <b>.
    for key, value in (env or {}).items():
        scope.setdefault(key, value)

    # --- SCOPE, first and always ------------------------------------------
    # A git invocation aims TWO things independently, and MEASURING them is the
    # only way to get this right — three separate holes came from assuming they
    # move together:
    #
    #   * which repository's refs move  — `--git-dir`, else `-C`, else cwd
    #   * which files on disk are written — `--work-tree`, else `-C`, else cwd
    #
    # `--git-dir` relocates the first and NOT the second: measured on git
    # 2.39.5, `git --git-dir=<other>/.git reset --hard` run inside the vault
    # wrote the other repository's tracked files into the vault's tree.
    # `--work-tree` is the exact mirror and is worse: `git --work-tree=<away>
    # checkout <branch>` run inside the vault moved THE VAULT'S OWN HEAD onto a
    # side branch — the state that caused the outage this guard exists for.
    # Only `-C` moves both, because git chdirs there before anything else.
    #
    # So cwd leaves the picture only when BOTH axes have been aimed elsewhere,
    # and the invocation is vault-bound if EITHER axis lands in the vault.
    def _axis(flags: tuple[str, ...]) -> str | None:
        for f in flags:
            if f in scope:
                return scope[f]
        return last_cd  # None here means "wherever the shell already is"

    targets: list[str | None] = [_axis(REPO_ORDER), _axis(TREE_ORDER)]

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
    if verb is None:
        return "allow", ""
    rest = args[verb_idx + 1:]

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
