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

That last step, reading 5, corrected a wrong model held while writing this. A
substitution, an `xargs` stage and a pipeline all leave `git` as a bare token,
so the guard SEES them; what it cannot see is a quoted body. The blind spot is
narrower than assumed and sits in a different place.

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
            r"|\b(?:ba|z|da)?sh\s+-c\s*[\"']|\beval\s+[\"']?)\s*")
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
_GIT_STATE_RE = re.compile(
    _CMD_POS + r"git\b[^\n;|&]{0,200}?\b("
    + "|".join(re.escape(w) for w in _STATE_WORDS) + r")" + _VERB_END)
_GIT_COND_RES = [
    re.compile(_CMD_POS + r"git\b[^\n;|&]{0,200}?\b" + re.escape(verb)
               + _VERB_END + r"[^\n;|&]{0,200}?(?:" + "|".join(flags) + r")")
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
            elif len(hidden_samples) < 12:
                hidden_samples.append(cmd[:160])

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
        print(json.dumps({**r, "bounds": _bound_lines(r)}, indent=2))
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
    for line in _bound_lines(r):
        print("  " + line)
    if a.samples and r["hidden_samples"]:
        print()
        print("  commands the guard could NOT see (sample):")
        for s in r["hidden_samples"]:
            print("    " + s.replace("\n", " ⏎ "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
