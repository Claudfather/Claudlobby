"""Ratchet: every direct systemctl/launchctl call site outside
lib/supervisor.sh is grandfathered at its count on this branch's tip and may
only shrink from here (#1573 boot admission, task 6).

lib/supervisor.sh is the one adapter meant to own systemctl/launchctl calls
from here on. Everywhere else in lib/, a direct call is a call site a later
PR is supposed to migrate onto the adapter, never a new one to add. Absent a
fence, "migrate one of the existing 108" and "add a 109th while nobody is
looking, in an unrelated diff" are indistinguishable. This test makes the
second one fail.

Grandfather shape, not enforcement shape -- the sibling pattern is
tests/test_defaults_registry.py's grandfathered defaults: every existing call
site is pre-approved by NAME and COUNT in
tests/supervisor_ratchet_allowlist.json, argued once at this branch's tip
rather than re-argued on every future PR that happens to touch one of these
20 files. A new file with any match, or an existing file whose count rose,
fails, naming the file and the delta. A shrink -- some direct call migrated
onto the adapter, or simply removed -- passes and is printed: recording a
NUMBER (not just a name) is what lets a shrink be noticed at all, where a
plain boolean grandfather list could only ever say "still present somewhere".

Scope is every REGULAR FILE under lib/, recursively, keyed repo-relative
(`lib/<subdir>/<name>`) -- deliberately not tests/test_bash_parse.py's
LIB_SCRIPTS (which answers a narrower, different question: "what must parse
as a bash script", `*.sh` plus extensionless-with-a-bash-shebang, and only at
the top level of lib/). A stray `systemctl`/`launchctl` call does not care
whether the file it sits in is a bash script, or whether that file lives in a
subdirectory (lib/personal/*.sh existed unwatched the whole time) -- the
ratchet's question is wider than the parse gate's, so it needs its own scope
rather than inheriting one built to answer something else. Measured
identical to the old, narrower scope at this tip regardless (108 calls in 20
files either way, because nothing outside those 20 files -- including every
lib/__pycache__/*.pyc, lib/logs/*.log and lib/personal/*.sh -- happens to
contain the literal token), so the allowlist itself needs no change; only the
scope that finds it does. lib/supervisor.sh itself is excluded from the scan,
by full repo-relative path: it is the adapter whose whole job is to hold
these calls, so its own count is expected to grow and is meaningless to
fence.

`_current_counts()` records EVERY scanned file's count, zero included --
never filtered to nonzero. A filtered `_current_counts()` was the actual
defect this file shipped with: an allowlisted file whose only call is
migrated away or deleted drops out of the returned dict entirely (count 0
looks like "never scanned" instead of "scanned, now clean"), so the
comparison below never even SEES that name to notice the shrink, the stale
entry sits in the allowlist forever, and -- the sharp edge -- a brand-new
call landing in that same file reads as "unchanged" up to the old allowlisted
count, passing green, because nothing ever recorded that the count had
touched zero in between. The comparison walks `set(current) | set(allowed)`
rather than either side alone, so an allowlisted file that vanishes from disk
entirely (not just zeroed) is caught the same way a zeroed one is -- both
read as "current count 0" via `.get(name, 0)`. `new` stays gated on `n > 0`:
with every file now recorded, most of the ~135 scanned files are legitimately
at 0 and never were allowlisted, and only an actual call (n > 0) in a file
absent from the allowlist is news.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_DIR / "lib"
ALLOWLIST_PATH = Path(__file__).resolve().parent / "supervisor_ratchet_allowlist.json"
ADAPTER_NAME = "supervisor.sh"
ADAPTER_REL = f"lib/{ADAPTER_NAME}"

# A bare `systemctl` or `launchctl` token: not glued to a longer identifier
# immediately before (a letter, underscore or hyphen would make it part of
# one) and not immediately followed by anything but a space or end of line --
# so `some-systemctl-wrapper` and `systemctl-ish` are excluded, while a real
# invocation (`systemctl --user ...`) or a bare mention at end of line is
# counted. Deliberately dumb -- a textual ratchet, not an AST: it fences
# growth, it does not judge intent, and a comment mentioning either binary as
# a bare word counts exactly like a real call (lib-common.sh's own new
# source-line comment had to be reworded with backticks for this reason,
# rather than the ratchet being taught to skip comments).
CALL_PATTERN = re.compile(r"(^|[^A-Za-z_-])(systemctl|launchctl)( |$)")


def _current_counts() -> dict[str, int]:
    """Measured NOW, over every regular file under lib/ recursively, minus
    the adapter itself -- EVERY scanned file, zero-count ones included. Never
    read from the allowlist -- the allowlist is the claim being checked, not
    the source of truth for what exists on disk.

    Zero-count files are deliberately IN this dict, not filtered out: a file
    a caller cares about (because it is in the allowlist) needs to be seen at
    0 for the comparison below to notice a call was removed, exactly as much
    as it needs to be seen at a grown number. Filtering here would just move
    the old bug to a different line.
    """
    counts: dict[str, int] = {}
    for path in sorted(LIB_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = f"lib/{path.relative_to(LIB_DIR)}"
        if rel == ADAPTER_REL:
            continue
        n = 0
        for line in path.read_text(errors="ignore").splitlines():
            if CALL_PATTERN.search(line):
                n += 1
        counts[rel] = n
    return counts


def _allowlist() -> dict[str, int]:
    return json.loads(ALLOWLIST_PATH.read_text())


def test_no_new_or_grown_direct_supervisor_calls():
    current = _current_counts()
    allowed = _allowlist()

    new = {}
    grown = {}
    shrunk = {}
    for name in sorted(set(current) | set(allowed)):
        # A name absent from `current` no longer exists on disk at all --
        # treated identically to a file that exists but is now clean (0):
        # both are "nothing to grandfather here any more".
        n = current.get(name, 0)
        a = allowed.get(name)
        if a is None:
            if n > 0:
                new[name] = n
            # else: never allowlisted and still carries no call -- routine,
            # not news (this is most of the ~135 files under lib/).
        elif n > a:
            grown[name] = (a, n)
        elif n < a:
            shrunk[name] = (a, n)

    if shrunk:
        # Not a failure -- a shrink is progress. Printed so it is visible in
        # -rs / -v output; the allowlist itself still needs a human edit --
        # this test does not auto-update it, or a real regression could
        # ratchet itself down unnoticed one accidental shrink-then-regrow
        # cycle at a time. A shrink to exactly 0 is RETIRED, not merely
        # smaller: leaving a 0 entry behind is what
        # test_allowlist_has_no_zero_count_entries below exists to refuse, so
        # that case gets its own, more direct instruction.
        lines = []
        for k, (a, b) in sorted(shrunk.items()):
            if b == 0:
                lines.append(f"{k}: {a} -> 0 (retired -- delete this entry from {ALLOWLIST_PATH.name})")
            else:
                lines.append(f"{k}: {a} -> {b}")
        print(f"supervisor ratchet: shrink recorded (update the allowlist) -- {', '.join(lines)}")

    assert not new, (
        f"new file(s) carrying direct systemctl/launchctl calls, absent from "
        f"{ALLOWLIST_PATH.name}: {new}. Route a new supervision call through "
        f"lib/supervisor.sh's verbs instead of calling systemctl/launchctl "
        f"directly; if this file's presence here is deliberate and "
        f"unrelated to supervision, add it to the allowlist with its "
        f"measured count and a reason."
    )
    assert not grown, (
        "direct systemctl/launchctl call count grew for: "
        + ", ".join(f"{k}: {a} -> {b}" for k, (a, b) in sorted(grown.items()))
        + f". lib/supervisor.sh (#1573 task 6) is where a new supervision "
        f"call belongs; if this growth is deliberate and unrelated to the "
        f"adapter, update {ALLOWLIST_PATH.name} to the new measured count."
    )


def test_allowlist_has_no_zero_count_entries():
    # A retired file (every direct call migrated onto the adapter, or simply
    # removed) leaves the list entirely rather than lingering at 0 -- a 0
    # entry would silently pre-authorise a brand-new call landing in a file
    # everyone believes is clean, which is the exact hazard this ratchet
    # exists to catch.
    allowed = _allowlist()
    zeroed = [name for name, n in allowed.items() if n <= 0]
    assert not zeroed, (
        f"{ALLOWLIST_PATH.name} carries a non-positive count for: {zeroed} "
        f"-- remove the entry instead of leaving it at 0"
    )
