"""Ratchet: a pipe into a quiet grep (`writer | grep -q`) may only become rarer (#2428).

grep -q exits at its first match. A writer with more to send then meets a closed
pipe and dies of SIGPIPE, and under `set -o pipefail` (which lib-common.sh sets for
every script that sources it) that death is the pipeline's status: the match reads as
a miss. It only happens when the writer still has data to write once grep has
exited, so it hides on short input and appears on long input. Four guards read a hit
as a miss this way (#2428): a mutation admitted by the Shopify read-only guard, a held
paste read as submitted, a running turn read as not busy, and a declared bot read as
undeclared. The fixes feed grep a here-string (`grep -q PATTERN <<<"$text"`) or test
with a bash pattern, so grep's early exit closes nothing a writer still needs.

Every other existing site is grandfathered by NAME and COUNT in
tests/quiet_grep_ratchet_allowlist.json, measured after those fixes. A new file with a
site, or a file whose count rose, fails, naming the file and the delta. A shrink passes
and is printed, and an entry that reaches 0 must be deleted, as in
tests/test_supervisor_ratchet.py, whose rules this copies (`_current_counts` records
every scanned file, zero included, so a shrink to 0 is seen).

Scope: shell files (`*.sh`, `*.bash`, or a `sh`/`bash` shebang) under the four trees
where shell code lives: claudlobby/_runtime_scripts, library, harness and tests. Counts
are OCCURRENCES on logical lines: a physical line ending in `|` or `|&` or a backslash
is joined to the next, so a pipe split over two lines is one site. Full-line comments
are skipped, since the fixes explain themselves in comments that name the pattern; a
trailing comment that names it still counts. It is a textual fence, not a parser: it
fences growth, it does not judge whether a given writer can outlast the pipe.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = Path(__file__).resolve().parent / "quiet_grep_ratchet_allowlist.json"
SCOPE = ("claudlobby/_runtime_scripts", "library", "harness", "tests")
SKIP_PARTS = {"node_modules", "__pycache__"}

# A grep (or egrep, fgrep, by any path, after NAME=value words or `command`/`env`)
# with a quiet flag: an option cluster holding q, or --quiet/--silent, before the
# first word that is not an option.
GREP_QUIET = (
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:command\s+|env\s+(?:-\S+\s+)*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*)?"
    r"(?:\S*/)?[ef]?grep"
    r"(?:\s+-[A-Za-z0-9]+|\s+--[a-z-]+(?:=\S+)?)*?"
    r"\s+(?:-[A-Za-z0-9]*q[A-Za-z0-9]*|--quiet|--silent)(?=\s|$|;|\))"
)
# ...right after a pipe, `|` or `|&`, never `||`.
PIPE_INTO_GREP_QUIET = re.compile(r"(?<!\|)\|&?(?!\|)\s*" + GREP_QUIET)


def _is_shell(path: Path) -> bool:
    if path.suffix in (".sh", ".bash"):
        return True
    if path.suffix:
        return False
    try:
        with path.open("rb") as fh:
            first = fh.readline(200)
    except OSError:
        return False
    return first.startswith(b"#!") and re.search(rb"\b(ba)?sh\b", first) is not None


def _logical_lines(text: str) -> Iterator[str]:
    buf = ""
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
        elif line.endswith(("|", "|&")):
            buf += line + " "
        else:
            yield buf + line
            buf = ""
    if buf:
        yield buf


def count_sites(text: str) -> int:
    return sum(len(PIPE_INTO_GREP_QUIET.findall(line)) for line in _logical_lines(text))


def _current_counts(root: Path = REPO_DIR) -> dict[str, int]:
    """Every scanned shell file's count, zero included, measured now from disk."""
    counts: dict[str, int] = {}
    for top in SCOPE:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            rel_parts = path.relative_to(root).parts
            if any(part.startswith(".") or part in SKIP_PARTS for part in rel_parts):
                continue
            if not path.is_file() or not _is_shell(path):
                continue
            counts[path.relative_to(root).as_posix()] = count_sites(path.read_text(errors="ignore"))
    return counts


def _allowlist() -> dict[str, int]:
    return json.loads(ALLOWLIST_PATH.read_text())


def test_no_new_or_grown_pipes_into_a_quiet_grep():
    current = _current_counts()
    allowed = _allowlist()

    new, grown, shrunk = {}, {}, {}
    for name in sorted(set(current) | set(allowed)):
        n = current.get(name, 0)
        a = allowed.get(name)
        if a is None:
            if n > 0:
                new[name] = n
        elif n > a:
            grown[name] = (a, n)
        elif n < a:
            shrunk[name] = (a, n)

    if shrunk:
        lines = []
        for k, (a, b) in sorted(shrunk.items()):
            if b == 0:
                lines.append(f"{k}: {a} -> 0 (retired -- delete this entry from {ALLOWLIST_PATH.name})")
            else:
                lines.append(f"{k}: {a} -> {b}")
        print(f"quiet-grep ratchet: shrink recorded (update the allowlist) -- {', '.join(lines)}")

    assert not new, (
        f"new file(s) piping into a quiet grep, absent from {ALLOWLIST_PATH.name}: {new}. "
        f"Feed grep a here-string (grep -q PATTERN <<<\"$text\") or test with a bash "
        f"pattern: under pipefail a writer that outlasts grep's early exit turns a match "
        f"into a miss (#2428)."
    )
    assert not grown, (
        "pipes into a quiet grep grew for: "
        + ", ".join(f"{k}: {a} -> {b}" for k, (a, b) in sorted(grown.items()))
        + ". Use a here-string or a bash pattern instead (#2428); if the growth is "
        f"deliberate, update {ALLOWLIST_PATH.name} to the new measured count."
    )


def test_allowlist_has_no_zero_count_entries():
    zeroed = [name for name, n in _allowlist().items() if n <= 0]
    assert not zeroed, (
        f"{ALLOWLIST_PATH.name} carries a non-positive count for: {zeroed} "
        f"-- remove the entry instead of leaving it at 0"
    )


def test_the_counter_counts_what_it_claims():
    cases = {
        "printf '%s' \"$x\" | grep -q foo": 1,
        "grep -q foo <<<\"$x\"": 0,
        "a | grep -qE 'x|y'": 1,
        "a |& grep -q x": 1,
        "a || grep -q x": 0,
        "a | LC_ALL=C grep -qF x": 1,
        "a | /usr/bin/grep --quiet x": 1,
        "a | command grep -Fxq x": 1,
        "a | egrep -q x": 1,
        "a | grep -c x": 0,
        "a | grep -e q x": 0,
        "a | grep -q x && b | grep -q y": 2,
        "printf x |\n  grep -q y": 1,
        "printf x \\\n  | grep -q y": 1,
        "# printf x | grep -q y": 0,
        "  # a | grep -q y": 0,
    }
    got = {text: count_sites(text) for text in cases}
    assert got == cases


def test_the_scope_is_shell_files_in_the_four_trees(tmp_path):
    site = "printf x | grep -q y\n"
    files = {
        "claudlobby/_runtime_scripts/a.sh": site,
        "library/b": "#!/usr/bin/env bash\n" + site,
        "harness/c.bash": site + site,
        "tests/d.py": site,
        "tests/e.md": site,
        "tests/.venv/f.sh": site,
        "documentation/g.sh": site,
        "tests/h": "#!/usr/bin/env python3\n" + site,
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert _current_counts(tmp_path) == {
        "claudlobby/_runtime_scripts/a.sh": 1,
        "harness/c.bash": 2,
        "library/b": 1,
    }
