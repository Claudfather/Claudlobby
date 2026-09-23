#!/usr/bin/env python3
"""The base rate behind #1725's ruling: how often does a state-changing git
command PRESENT ITSELF to `vault-git-decide.py` at all?

WHY THIS EXISTS AS A COMMITTED SCRIPT. The guard's input model is its ceiling:
it catches a DIRECT git invocation and cannot catch git reached through shell
composition. That is a known, stated bound, and the argument for shipping it
anyway rests on a base rate -- most real invocations are direct. A base rate
asserted in a PR body expires the moment anyone doubts it, with no way to
re-derive it. So the number ships with the thing that produces it.

    python3 lib/vault-git-base-rate.py                 # default corpus
    python3 lib/vault-git-base-rate.py --transcripts DIR --json

WHAT IT MEASURES, AND THE TRAP IT IS BUILT AROUND. An independent reproduction
of this figure first read 42.6%, and the cause was the harness measuring a
narrower population than the claim. The specific way to get that wrong here is
seductive: use the guard's own parser for BOTH halves. Then every command in the
denominator is one the guard could parse, the answer is 100% by construction,
and the harness certifies the claim by assuming it.

So the two halves are deliberately asymmetric:

  DENOMINATOR -- an independent, over-broad TEXT match. Any command whose raw
  text mentions git and a state-changing verb, however it is reached: through a
  variable, a substitution, xargs, an alias, a script, a heredoc. This is the
  population the claim is about, and it must be found without the guard's help
  or composed forms vanish from it.

  NUMERATOR -- the guard's OWN tokenizer, verb set and flag rules
  (`_shell_words`, `_parse_git_args`, `STATE_VERBS`, `CONDITIONAL`), asking
  whether `git` lands as a command word with either an unconditional state
  verb or a conditional one carrying its OWN dangerous flag -- the same
  two-step verdict `decide()` itself makes (#1755: the first version checked
  verb membership alone, flag-blind). The DEFINITION is shared with the
  denominator's rule (one dangerous-conditional-verb table, `CONDITIONAL`,
  consulted by both halves); the PARSING is not -- this still walks the
  guard's own tokenizer, never the denominator's regex, or the asymmetry the
  whole design rests on collapses. Not a re-implementation: a private copy of
  a predicate is how the measured thing and the shipped thing quietly
  diverge.

The gap between them is the answer: commands that ARE state-changing git and
that the guard cannot see.

WHAT IT DOES NOT COVER, stated because a harness measuring something narrower
than its claim is how the 42.6% happened:

  * It measures what bots TYPED, not what ran. A command that failed, was
    denied, or was never submitted counts the same as one that succeeded.
  * It reads Claude Code transcripts only. Anything reaching git outside a Bash
    tool call -- a hook, a timer, a systemd unit, a script's own internals -- is
    invisible to it. Those are exactly where composition is most likely, so the
    directness rate here is an UPPER bound on the estate, not an estimate of it.
  * The denominator is over-broad on purpose and will include prose: the words
    "git checkout" inside a commit message or a report body match the text rule
    and are not invocations. Over-counting the denominator makes the reported
    rate CONSERVATIVE, which is the safe direction for a claim used to justify
    shipping a guard with a known ceiling.
  * Deduplication is off: a command issued twenty times counts twenty times,
    because the question is about invocations reaching the guard, not about
    distinct command shapes.

WHAT IT READS, AND WHAT THE PR ORIGINALLY CLAIMED. On the estate corpus this
reports roughly **90% directness** and **88% bare-`cd`**. The #1725 discussion
carried 87% and 98.3%. Neither is reproduced -- the first is now EXCEEDED and
the second is not met -- and this script, not the sentence, is the thing of
record: re-run it rather than quoting any of these numbers.

The difference is not a subtlety and is worth keeping, because every step of it
was the harness measuring a different population than the claim:

  1. 68.1% -- verb text matched anywhere after the word `git`. The gap filled
     with heredoc bodies (report prose, memory notes, dispatch payloads) and
     `grep 'git push' lib/`. None of those run git.
  2. 82.8% -- heredoc bodies stripped, `git` required at a command position.
  3. 81.9% -- `commit`/`push`/`branch`/`pull` made conditional on the flag that
     makes them dangerous, matching the guard's own split. Counting
     `$(git branch --show-current)` as state-changing had filled the gap with
     harmless reads.
  4. 81.8% -- `sh -c "..."` bodies added to the denominator, after measuring
     that they are the guard's ACTUAL blind spot.
  5. 91.6% -- hyphenated plumbing names stopped matching the verb. `\b` sits
     between `merge` and the `-` of `merge-base`, so the bare boundary matched
     inside `merge-base`, `merge-tree` and `checkout-index`. Reported by review
     as a latent defect with "zero current impact"; MEASURED, it was the
     LARGEST of the five -- 408 occurrences, 352 of them `merge-base`, which is
     simply how everyone scopes a PR. It had been inflating the denominator by
     roughly a ninth and deflating the rate by about ten points. A latent
     denominator defect and a live one look identical until someone counts.
  6. 90.3% -- the NUMERATOR's conditional check became flag-aware (#1755),
     matching the denominator's own rule (reading 3) instead of counting bare
     `branch`/`commit`/`pull`/`push` as guard-visible the same as their
     dangerous forms. It had been inflating BOTH the 81.8% and the 91.6%
     readings by about the same ~1.5 points -- which is exactly why the DELTA
     between them looked sound while the ABSOLUTE stayed wrong, and why a
     figure was retracted rather than published from either.

  7. 92.6% -- the verb needed the same boundary on its LEFT (#1753). Reading 5
     fixed the right side; `\b` still matched the `merge` inside
     `.git/rebase-merge`, the `rebase` inside `pull.rebase` and the `am` inside
     `git commit -am`. 54 commands, 37 of them the single string
     `.git/rebase-merge `, which is how everyone probes for a stopped rebase.
     It removed NOTHING the guard could see (0 of 54) -- the shape of a pure
     denominator artifact, which can only ever have inflated the gap. Stated as
     a RULE (a subcommand is a whole shell word) rather than as a denylist,
     because reading 5 named two colliding names and a third fell out of the
     rule.

That last step, reading 5, corrected a wrong model held while writing this. A
substitution, an `xargs` stage and a pipeline all leave `git` as a bare token,
so the guard SEES them; what it cannot see is a quoted body. The blind spot is
narrower than assumed and sits in a different place.

AND THEN THE BREAKDOWN MOVED IT AGAIN (#1753). The residual is now classified by
mechanism, and the form the whole discussion was about is not in it:

    wrapper_quoted_body  (`sh -c "git rebase main"`)     0   of 163   0.0%
    continuation_glued_token                            81   of 165  ~49%

**Half the residual is a BACKSLASH LINE CONTINUATION.** `... && backslash` then `git`
at column 0: the escaped newline becomes part of the next word, the token is
"\ngit", and the guard's `_unseparate` -- which strips `;&` -- leaves it
unequal to "git". Nothing is composed. It is the most ordinary multi-line shape
there is, and the guard's own claim is that it catches a DIRECT invocation.
Confirmed live through the shipped decision door, with controls: the spaced form
DENIES, the continuation form ALLOWS, an INDENTED continuation denies (the
whitespace ends the token), and a plain newline denies. Filed as #1759; a
measurement should not quietly become a fix.

So the remedy the issue put on the table -- parse one level into a quoted body --
would have addressed a form that occurs **zero** times in 67,000 Bash calls,
while the form that occurs 81 times is a one-character-class widening of a
predicate that already exists. That gap between the intuition and the count is
the entire argument for measuring first.

A NOTE ON THE BREAKDOWN'S OWN ASYMMETRY, because it is the module's rule one
level down: the residual is BY DEFINITION what the guard's parser could not see,
so classifying it WITH that parser would return one bucket and explain nothing.
The classes are independent text rules. The continuation class alone
cross-checks against the guard's tokenizer -- it is a claim about that
tokenizer -- and the cross-check only COUNTS disagreements; the independent rule
decides.

    THAT LAST SENTENCE WAS FALSE WHEN IT WAS FIRST WRITTEN, and review found it
    (#1760). The code read `if glued or glued_by_tokenizer`, so the guard's
    parser COULD flip a classification -- the collapse trap one level down, in
    the one class that mentions the tokenizer. Two things make it worth keeping
    in the record rather than quietly fixing. The bias was ONE-DIRECTIONAL (an
    `or` can only add) and it pointed at the FLAGSHIP bucket, which is the worst
    place for a small one-sided error; and its measured effect was NIL, because
    every disagreement on the corpus ran the other way (independent yes,
    tokenizer no), so nothing was in the bucket because of it. A property the
    text claims and the code does not hold is not rescued by the number
    happening to agree.

    Investigating it moved the number, though, and by the SECOND correction
    rather than the first: both disagreements were a continuation inside a
    QUOTED string -- one of this estate's own probe strings -- and a
    backslash-newline inside quotes is text, not a shell continuation. Quoting
    now takes precedence, so 83/50.9% became 81/165 -- a shade under half, and
    the figure to quote is this one.

    QUOTE THE COUNT WITH ITS DENOMINATOR, NOT THE PERCENTAGE, and the reason is
    a bound nobody had noticed: this harness reads the transcripts of the
    sessions that USE it, so the work of measuring enters the corpus it
    measures. Across three runs in one hour the continuation count held at 81
    while the residual grew 163 -> 165 and the share moved 49.7% -> 49.1% --
    entirely from probe commands the measurement itself had just produced
    (`harness_command_string` 7 -> 9, which is exactly those). The mechanism
    counts are stable; the shares drift, and they drift toward whatever was
    recently being investigated. Precedence puts an artifact test first, which is conservative about
this file's own finding rather than flattering to it, and overlaps are reported
rather than hidden by the ordering.

Five of these six were the DENOMINATOR, hand-written and so scrutinised as
hand-written code is. The sixth was the NUMERATOR, and it took a second
reviewer, on a different PR, to catch: importing the guard's real tokenizer
made this half FEEL verified, when only the tokenizer was -- the flag rule
layered on top of it (which verb needs which flag to count) was still a
private, unverified copy of `decide()`'s own branch, the exact thing the
asymmetric design exists to prevent on the denominator side and had quietly
never been asked of the numerator.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

DEFAULT_TRANSCRIPTS = Path.home() / ".claude" / "projects"

#: Deliberately independent of the guard -- see the module docstring.
#: Unconditionally state-changing: the verb alone moves the tree or history.
_STATE_WORDS = ("checkout", "switch", "rebase", "reset", "merge", "stash",
                "clean", "worktree", "cherry-pick", "revert", "am")

#: Dangerous only WITH a flag, mirroring the guard's `CONDITIONAL` -- matched by
#: text here rather than by importing it, so the denominator stays independent.
#:
#: Counting these unconditionally was the second denominator defect. `$(git
#: branch --show-current)` and `$(git rev-parse HEAD)` are pure READS that the
#: guard rightly ignores; counting them as state-changing put a pile of harmless
#: substitutions in the gap and read as composition hiding danger. The claim is
#: about commands that CHANGE state, so the denominator has to mean that too.
_CONDITIONAL_WORDS = {
    "commit": (r"--amend",),
    "push": (r"--force\b", r"-f\b", r"--force-with-lease"),
    "branch": (r"-[dDmM]\b", r"--delete\b", r"--move\b"),
    "pull": (r"--rebase\b", r"-r\b"),
}

#: `git` AT A COMMAND POSITION, which deliberately includes every composition
#: form the guard cannot parse: a substitution, a pipeline stage, `xargs`, an
#: `sh -c` body, a loop body. What it excludes is `git` appearing as PROSE or as
#: a search string, which is not an invocation at all.
#:
#: The first version of this script omitted the position requirement and matched
#: the verb text anywhere after the word `git`. It reported 68.1%, and the gap
#: was almost entirely heredoc bodies -- report text, memory files, dispatch
#: payloads -- plus `grep 'git push' lib/`. None of those run git. That is the
#: same defect as the 42.6% reproduction this script's docstring warns about,
#: inverted: a denominator measuring something WIDER than the claim deflates the
#: rate exactly as a narrower one inflated it. Both are the harness measuring a
#: different population than the sentence it is checking.
#: MEASURED against the shipped tokenizer rather than assumed. `$(git ...)`,
#: `xargs git ...` and pipeline stages all leave `git` as a BARE TOKEN, so the
#: guard sees them -- it is stronger here than a first reading suggests. What it
#: genuinely cannot see is a git command inside a QUOTED BODY (`sh -c "git
#: rebase main"`), which tokenizes as a single string. Both kinds belong in the
#: denominator: it is the population of state-changing invocations, not the
#: population the guard happens to miss.
_CMD_POS = (r"(?:^|[;&|(){}\n`]|\$\(|&&|\|\||\bxargs\s+(?:-[^\s]+\s+)*"
            r"|\bthen\s+|\bdo\s+|\belse\s+|\bsudo\s+|\btime\s+"
            r"|\b(?:ba|z|da)?sh\s+-c\s*[\"']|\beval\s+[\"']?"
            #: #1753: the breakdown names `ssh <host> "..."` and `su -c "..."`
            #: as wrapper bodies, and a class the DENOMINATOR cannot deliver is
            #: a class whose zero means nothing. Added so the detector for the
            #: form this measurement reports as absent can actually be fed one.
            r"|\bssh\s+\S+\s+[\"']|\bsu\s+-c\s*[\"'])\s*")
#: A HYPHEN AFTER THE VERB MEANS A DIFFERENT COMMAND. `\b` sits between `merge`
#: and the `-` of `merge-base`, so a bare word boundary matches inside every
#: hyphenated plumbing name -- `merge-base`, `merge-tree`, `checkout-index` --
#: none of which change the tree the way the verb alone does. Found by review on
#: the first two; the third fell out of fixing it as a RULE rather than
#: denylisting the two that were named. Zero impact on the corpus today, which
#: is exactly the kind of denominator defect that starts mattering silently.
#:
#: `(?![-\w])` rather than `\b`: hyphenated verbs that ARE real state changes
#: (`cherry-pick`) match as whole alternatives and are unaffected.
_VERB_END = r"(?![-\w])"
#: AND THE SAME BOUNDARY ON THE LEFT -- the seventh defect, and the mirror image
#: of reading 5 (#1753). Fixing the right side left the left side open, so `\b`
#: still matched the `merge` inside `.git/rebase-merge`, the `rebase` inside
#: `pull.rebase`, and the `am` inside `git commit -am`. MEASURED on the estate
#: corpus: 54 commands, ~30% of the entire residual, 37 of them the single string
#: `.git/rebase-merge ` -- which is how everyone probes for a stopped rebase. It
#: removed NOTHING the guard could see (0 of 54), which is the shape of a true
#: denominator artifact: it can only ever have inflated the gap.
#:
#: A git subcommand is a WHOLE SHELL WORD, so the rule is stated that way rather
#: than as a denylist of the names that happened to collide -- reading 5 named
#: two and a third fell out of the rule, and this is the same lesson arriving
#: from the other side.
_VERB_START = r"(?<![-./\w])"
_GIT_STATE_RE = re.compile(
    _CMD_POS + r"git\b[^\n;|&]{0,200}?\b" + _VERB_START + r"("
    + "|".join(re.escape(w) for w in _STATE_WORDS) + r")" + _VERB_END)
_GIT_COND_RES = [
    re.compile(_CMD_POS + r"git\b[^\n;|&]{0,200}?\b" + _VERB_START
               + re.escape(verb) + _VERB_END + r"[^\n;|&]{0,200}?(?:" + "|".join(flags) + r")")
    for verb, flags in _CONDITIONAL_WORDS.items()
]


def _is_state_changing(shell: str) -> bool:
    """Independent of the guard: an unconditional state verb, or a conditional
    one carrying the flag that makes it dangerous."""
    if _GIT_STATE_RE.search(shell):
        return True
    return any(rx.search(shell) for rx in _GIT_COND_RES)

#: A heredoc body is authored TEXT, not shell. Report prose, memory notes and
#: dispatch payloads routinely quote git commands, and counting those as
#: invocations is what produced the deflated first reading.
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?\n.*?\n\s*\1\s*$",
                         re.DOTALL | re.MULTILINE)


def _strip_heredocs(command: str) -> str:
    """Remove heredoc BODIES, keeping the surrounding shell."""
    return _HEREDOC_RE.sub("<<STRIPPED>>", command)
#: The directory-change half of the same ruling.
_CD_RE = re.compile(r"(?<![\w-])cd\s+\S")
_BARE_CD_RE = re.compile(r"(?:^|[;&|]\s*|\bthen\s+|\bdo\s+)cd\s+(?!-)\S")


def _load_guard(lib_dir: Path):
    """Import the SHIPPED guard. Its hyphenated name is not importable, and a
    re-implementation of its tokenizer is the thing this script must not have."""
    path = lib_dir / "vault-git-decide.py"
    spec = importlib.util.spec_from_file_location("vault_git_decide", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the guard at {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bash_commands(transcripts: Path):
    """Every Bash command in the corpus, with the file it came from."""
    files = sorted(transcripts.rglob("*.jsonl"))
    for fp in files:
        try:
            handle = fp.open(errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                msg = rec.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for blk in content:
                    if (isinstance(blk, dict) and blk.get("type") == "tool_use"
                            and blk.get("name") == "Bash"):
                        cmd = (blk.get("input") or {}).get("command")
                        if isinstance(cmd, str) and cmd:
                            yield fp, cmd
    return


#: Commands the guard's own tokenizer could not parse. See `_guard_sees_git_state`.
_TOKENIZER_FAILURES: list[str] = []


def _guard_sees_git_state(guard, command: str) -> bool:
    """Would the guard's OWN parser find a state-changing git invocation here?

    Mirrors how `decide()` walks tokens AND VERDICTS -- `git` as a command
    word, the guard's own flag parser for the verb, then STATE_VERBS
    unconditional / CONDITIONAL only WITH its own dangerous flag present in
    the remaining args, exactly as `decide()` itself branches (#1755: the
    first version checked verb membership alone, so `git branch -v` counted
    the same as `git branch -D` -- flag-blind, inflating every reading by
    about 1.5 points, the sixth denominator/numerator defect and the first on
    this side).

    The DEFINITION is shared (`guard.STATE_VERBS`, `guard.CONDITIONAL`) --
    one dangerous-conditional-verb table, consulted by both halves. The
    PARSING is not: this still walks the guard's OWN `_shell_words` /
    `_parse_git_args`, never the denominator's regex. Sharing the parser too
    would make this reading 100% by construction (module docstring) --
    sharing only the definition is what lets the numerator become
    flag-aware without collapsing the asymmetry the design rests on."""
    try:
        tokens = guard._shell_words(command)
    except Exception:
        # Counted and reported, never silently folded into "not seen": a
        # tokenizer that threw would deflate the numerator and look exactly
        # like composition hiding git.
        _TOKENIZER_FAILURES.append(command[:80])
        return False
    for i, tok in enumerate(tokens):
        if guard._unseparate(tok) != "git":
            continue
        _scope, verb, verb_idx, _unknown = guard._parse_git_args(tokens[i + 1:])
        if not verb:
            continue
        v = guard._unseparate(verb)
        if v in guard.STATE_VERBS:
            return True
        rest = tokens[i + 2 + verb_idx:]
        if any(flag in rest for flag in guard.CONDITIONAL.get(v, ())):
            return True
    return False


# --------------------------------------------------------------------------
# THE RESIDUAL, BROKEN DOWN BY MECHANISM (#1753)
# --------------------------------------------------------------------------
# The gap is a number until you know what is IN it. #1725 shipped on a model
# that named `sh -c "git rebase main"` as the guard's blind spot, and the
# remedy on the table was to parse one level into a quoted body. Nobody had
# measured the SHARE of real traffic taking that form, so the remedy was
# sized by intuition.
#
# THE CLASSIFIER IS INDEPENDENT TEXT RULES, for the reason the denominator is:
# the residual is BY DEFINITION what the guard's parser could not see, so
# asking that parser to explain it would return "unparseable" for everything
# and the breakdown would be one bucket by construction. That is the module
# docstring's collapse, one level down.
#
# ONE EXCEPTION, and it is disclosed rather than quiet: the continuation class
# CROSS-CHECKS itself against the guard's tokenizer, because that class is a
# claim ABOUT the tokenizer. The independent rule decides; the cross-check only
# counts disagreements, so a classifier drifting from the thing it describes is
# visible instead of assumed.
#
# PRECEDENCE IS CONSERVATIVE ABOUT THIS FILE'S OWN FINDING. An artifact test
# runs FIRST: if the verb belongs to a different command than the git token,
# the match evidences nothing about the guard at all, and counting it as a
# bypass would inflate the very class this measurement discovered. Overlaps
# are reported (`mechanism_overlaps`) rather than hidden by the ordering.

#: A quoted string introduced by something that makes its body a COMMAND.
_WRAPPER_INTRO = re.compile(
    r"(?:(?:ba|z|da)?sh\s+-c|\beval|\bsend-keys|\bssh\s+\S+|\bsu\s+-c"
    r"|\btimeout\s+\S+\s+(?:ba)?sh\s+-c)\s*$")
#: A quoted command string that is TEST INPUT -- this estate probing this very
#: guard. Counted apart from the wrapper class rather than folded into it: our
#: own fixtures are not production traffic, and folding them in would inflate
#: the blind spot with the tests that measure it.
_HARNESS_INTRO = re.compile(
    r"(?:\bfor\s+\w+\s+in|\bprobe|\bpython3?\s+-c|\bcase\b"
    r"|\bcmds?\s*=\s*\[?)\s*$")
#: A quoted string introduced by something that makes its body TEXT.
_DATA_INTRO = re.compile(
    r"(?:-m|--message|--body|--body-file|--title|--reason|--question|--text"
    r"|--summary|-F|\becho|\bprintf|\bgrep|\bsed|\bawk|\bMSG=|\bjq"
    r"|tg-post\.sh|report-back\.sh[^\n]*|dispatch-task\.sh[^\n]*)\s*$")
#: A BACKSLASH LINE CONTINUATION with the next word starting at column 0. The
#: escaped newline becomes part of that word, so the token is "\ngit" and the
#: guard's `_unseparate` -- which strips `;&` -- leaves it unequal to "git".
#: An indented continuation is unaffected: the whitespace ends the token.
_CONTINUATION_GLUED = re.compile(r"\\\n(?=\S)")

#: Ordered, because the classification is a partition. See the precedence note.
RESIDUAL_MECHANISMS = (
    "verb_belongs_to_another_command",
    "continuation_glued_token",
    "wrapper_quoted_body",
    "harness_command_string",
    "string_data_or_prose",
    "quoted_introducer_unrecognised",
    "unclassified",
)


def _enclosing_quote(shell: str, idx: int):
    """(quote char, the text just before its opening) when *idx* sits inside a
    quoted string -- an ODD count of that quote character before it. A parity
    test, not a shell parser, deliberately: this must not depend on the
    tokenizer whose blind spot it is describing."""
    for q in ('"', "'"):
        before = shell[:idx]
        if before.count(q) % 2 == 1:
            start = before.rindex(q)
            return q, shell[max(0, start - 48):start]
    return None, None


def _classify_residual(shell: str, match_start: int, verb_at: int | None,
                       glued_by_tokenizer: bool) -> tuple[str, bool]:
    """(mechanism, overlapped) for one residual match.

    *overlapped* says an EARLIER-precedence class won over a later one that
    also matched, so the ordering is auditable instead of silent.

    ``glued_by_tokenizer`` IS NOT CONSULTED HERE and is accepted only so the
    caller can count disagreements against it. An earlier version read
    ``if glued or glued_by_tokenizer``, which let the GUARD'S parser flip the
    classification -- review found it, and it is the module docstring's collapse
    trap one level down: share definitions, never parsing. The bias was also
    ONE-DIRECTIONAL (an `or` can only ever add) and it pointed at the flagship
    bucket, which is the worst place for a small one-sided error. Measured on
    the corpus it moved NOTHING -- every disagreement ran the other way
    (independent yes, tokenizer no), so no command was in the bucket because of
    it -- but a property the text claimed and the code did not hold is not
    rescued by the number happening to agree.
    """
    git_at = shell.find("git", match_start)
    end = verb_at if verb_at is not None else match_start
    between = shell[git_at + 3:end] if 0 <= git_at < end else ""
    # IMMEDIATELY before this git token, not anywhere earlier in the command.
    # The loose form inflated this class -- any command carrying a continuation
    # anywhere scored as glued -- and it inflated it in the direction that
    # flatters the finding this file just made, which is the direction to be
    # most suspicious of.
    glued = git_at >= 2 and shell[git_at - 2:git_at] == "\\\n"
    q, before = _enclosing_quote(shell, end)
    intro = (before or "").strip()
    quoted_class = None
    if q is not None:
        if _WRAPPER_INTRO.search(intro):
            quoted_class = "wrapper_quoted_body"
        elif _HARNESS_INTRO.search(intro):
            quoted_class = "harness_command_string"
        elif _DATA_INTRO.search(intro):
            quoted_class = "string_data_or_prose"
        else:
            quoted_class = "quoted_introducer_unrecognised"

    if any(ch in between for ch in ')("\''):
        return "verb_belongs_to_another_command", bool(glued or quoted_class)
    # A QUOTED match cannot be a live continuation bypass, and this ordering is
    # the second correction from the same review. A backslash-newline INSIDE a
    # quoted string is text, not a shell continuation -- the two disagreements
    # the cross-check found were both this shape (a continuation inside one of
    # this estate's own probe strings), and the earlier ordering counted them in
    # the flagship bucket. Quoted-first is the conservative reading of a number
    # this file is publishing.
    if quoted_class:
        return quoted_class, bool(glued)
    if glued:
        return "continuation_glued_token", False
    return "unclassified", False


def _tokenizer_sees_a_glued_git(guard, command: str) -> bool:
    """The cross-check, never the decision: does a token exist that IS `git`
    once whitespace is stripped, but is not `git` to the guard? That is the
    continuation defect from the guard's own side."""
    try:
        toks = guard._shell_words(command)
    except Exception:
        return False
    return any(guard._unseparate(t) != "git" and guard._unseparate(t).strip() == "git"
               for t in toks)


def measure(transcripts: Path, lib_dir: Path) -> dict:
    guard = _load_guard(lib_dir)
    files = 0
    total_cmds = 0
    git_state_text = 0
    guard_visible = 0
    cd_any = 0
    cd_bare = 0
    hidden_samples: list[str] = []
    seen_files: set[Path] = set()
    mech: dict[str, int] = {k: 0 for k in RESIDUAL_MECHANISMS}
    mech_samples: dict[str, list[str]] = {k: [] for k in RESIDUAL_MECHANISMS}
    overlaps = 0
    glued_disagreements = 0

    for fp, cmd in _bash_commands(transcripts):
        if fp not in seen_files:
            seen_files.add(fp)
        total_cmds += 1
        if _CD_RE.search(cmd):
            cd_any += 1
            if _BARE_CD_RE.search(cmd):
                cd_bare += 1
        shell = _strip_heredocs(cmd)
        if _is_state_changing(shell):
            git_state_text += 1
            if _guard_sees_git_state(guard, cmd):
                guard_visible += 1
            else:
                if len(hidden_samples) < 12:
                    hidden_samples.append(cmd[:160])
                hit = _GIT_STATE_RE.search(shell)
                verb_at = None
                if hit is not None:
                    verb_at = shell.index(hit.group(1), hit.start())
                else:
                    for rx in _GIT_COND_RES:
                        hit = rx.search(shell)
                        if hit is not None:
                            break
                glued_tok = _tokenizer_sees_a_glued_git(guard, cmd)
                start = hit.start() if hit is not None else 0
                name, over = _classify_residual(shell, start, verb_at, glued_tok)
                mech[name] += 1
                overlaps += bool(over)
                # The cross-check, counted rather than trusted: the independent
                # rule and the guard's own tokenizer must agree about this class.
                _g = shell.find("git", start)
                own = _g >= 2 and shell[_g - 2:_g] == "\\\n"
                if own != glued_tok:
                    glued_disagreements += 1
                if len(mech_samples[name]) < 4:
                    mech_samples[name].append(cmd[:150])

    files = len(seen_files)
    return {
        "files": files,
        "commands": total_cmds,
        "git_state_by_text": git_state_text,
        "git_state_guard_visible": guard_visible,
        "directness_pct": (100.0 * guard_visible / git_state_text) if git_state_text else None,
        "cd_any": cd_any,
        "cd_bare": cd_bare,
        "bare_cd_pct": (100.0 * cd_bare / cd_any) if cd_any else None,
        "tokenizer_failures": len(_TOKENIZER_FAILURES),
        "residual": git_state_text - guard_visible,
        "residual_mechanisms": mech,
        "residual_samples": mech_samples,
        "mechanism_overlaps": overlaps,
        "glued_crosscheck_disagreements": glued_disagreements,
        "hidden_samples": hidden_samples,
        "transcripts_root": str(transcripts),
    }


def _bound_lines(r: dict) -> list[str]:
    """#1742's rule: a base rate with no denominator description is a number
    people quote. State the corpus, the window and what was skipped -- in the
    output, not only in a doc."""
    return [
        f"CORPUS   : {r['files']} transcript file(s) under {r['transcripts_root']}",
        f"           {r['commands']} Bash command(s) total",
        "WINDOW   : whatever those transcripts retain -- Claude Code prunes them,",
        "           so this is a recent-activity sample, not an all-time census.",
        "SELF     : this reads the transcripts of the sessions that RUN it, so",
        "           measuring adds to the corpus measured -- probe commands from a",
        "           session investigating git land in the next reading. Mechanism",
        "           COUNTS are stable across runs; the SHARES drift toward",
        "           whatever was recently being investigated. Quote the count and",
        "           its denominator, not the percentage.",
        "SKIPPED  : git reached outside a Bash tool call (hooks, timers, units,",
        "           script internals) is INVISIBLE here. Composition is likeliest",
        "           there, so this rate is an UPPER bound on the estate.",
        "COUNTING : denominator is an INDEPENDENT text rule -- `git` at a command",
        "           position (including $( ), pipelines, xargs, sh -c) with an",
        "           unconditional state verb, or a conditional one carrying the",
        "           flag that makes it dangerous. Heredoc BODIES are stripped, so",
        "           quoted git in report prose is not counted as an invocation.",
        "           It does NOT use the guard's parser: doing so on both halves",
        "           would make the answer 100% by construction.",
        "           Residual over-count: a git command inside a single-quoted",
        "           string that is not a heredoc still reads as an invocation.",
        "           No deduplication -- invocations, not distinct shapes.",
        f"HARNESS  : the guard's tokenizer failed on {r['tokenizer_failures']} "
        "command(s).",
        "           A failure there deflates the numerator and is indistinguishable",
        "           from composition, so it is counted rather than assumed zero.",
    ]



def _breakdown_bound_lines(r: dict) -> list[str]:
    """The breakdown's own bound, emitted only when the breakdown ran. A caller
    that did not ask for it gets no line rather than a fabricated zero."""
    return [
        "BREAKDOWN: the residual is classified by INDEPENDENT text rules, never by",
        "           the guard's parser -- it could not parse these by definition, so",
        "           asking it would return one bucket. Classes are a partition with",
        "           an artifact test FIRST, which is conservative about the",
        f"           continuation finding; {r['mechanism_overlaps']} match(es) also fit a",
        "           later class and are counted under the earlier one.",
        f"           The continuation class CROSS-CHECKS against the guard's own",
        f"           tokenizer and never CONSULTS it: "
        f"{r['glued_crosscheck_disagreements']} disagreement(s), counted only.",
        "           `unclassified` is printed, never folded into a neighbour.",
    ]

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transcripts", default=str(DEFAULT_TRANSCRIPTS),
                    help="root holding Claude Code *.jsonl transcripts")
    ap.add_argument("--lib-dir", default=str(Path(__file__).resolve().parent),
                    help="directory holding the shipped vault-git-decide.py")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--samples", action="store_true",
                    help="print commands the guard could not see")
    ap.add_argument("--breakdown", action="store_true",
                    help="break the residual down by mechanism (#1753)")
    a = ap.parse_args(argv)

    root = Path(a.transcripts).expanduser()
    if not root.is_dir():
        print(f"vault-git-base-rate: no transcript corpus at {root} — refusing "
              "rather than reporting a rate over nothing", file=sys.stderr)
        return 3

    r = measure(root, Path(a.lib_dir))
    if r["git_state_by_text"] == 0:
        print("vault-git-base-rate: the corpus holds no state-changing git "
              "commands — no rate to report (this is not 0%)", file=sys.stderr)
        return 3

    if a.json:
        bounds = _bound_lines(r) + (_breakdown_bound_lines(r) if a.breakdown else [])
        print(json.dumps({**r, "bounds": bounds}, indent=2))
        return 0

    print("#1725 base rate — does a state-changing git command present itself "
          "to the guard?")
    print()
    print(f"  state-changing git commands (text match) : {r['git_state_by_text']}")
    print(f"  of those, the guard's parser SEES        : {r['git_state_guard_visible']}")
    print(f"  DIRECTNESS RATE                          : {r['directness_pct']:.1f}%")
    print()
    print(f"  directory changes                        : {r['cd_any']}")
    print(f"  of those, a BARE `cd`                    : {r['cd_bare']}")
    print(f"  BARE-cd RATE                             : {r['bare_cd_pct']:.1f}%")
    print()
    if a.breakdown:
        print("  RESIDUAL BY MECHANISM "
              f"({r['residual']} command(s) the guard cannot see)")
        for name in RESIDUAL_MECHANISMS:
            n = r["residual_mechanisms"][name]
            pct = (100.0 * n / r["residual"]) if r["residual"] else 0.0
            print(f"    {n:>5}  {pct:5.1f}%  {name}")
        print()
    for line in _bound_lines(r):
        print("  " + line)
    if a.breakdown:
        for line in _breakdown_bound_lines(r):
            print("  " + line)
    if a.breakdown and a.samples:
        print()
        for name in RESIDUAL_MECHANISMS:
            rows = r["residual_samples"][name]
            if not rows:
                continue
            print(f"  -- {name}")
            for row in rows[:3]:
                print("     " + row.replace("\n", " ⏎ ")[:150])
    if a.samples and r["hidden_samples"]:
        print()
        print("  commands the guard could NOT see (sample):")
        for s in r["hidden_samples"]:
            print("    " + s.replace("\n", " ⏎ "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
