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

File scope is imported from tests/test_bash_parse.py's LIB_SCRIPTS rather
than re-derived here -- that is the parse gate's own answer to "what counts
as a lib/ script" (`*.sh` plus extensionless files with a bash shebang), and
a second implementation of that same question is exactly the kind of drift
this repo's own doors (env_tiers.py, source_state.py) exist to refuse
elsewhere. lib/supervisor.sh itself is excluded from the scan: it is the
adapter whose whole job is to hold these calls, so its own count is expected
to grow and is meaningless to fence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.test_bash_parse import LIB_SCRIPTS

ALLOWLIST_PATH = Path(__file__).resolve().parent / "supervisor_ratchet_allowlist.json"
ADAPTER_NAME = "supervisor.sh"

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
    """Measured NOW, over the same file set the parse gate covers, minus the
    adapter itself. Never read from the allowlist -- the allowlist is the
    claim being checked, not the source of truth for what exists on disk."""
    counts: dict[str, int] = {}
    for path in LIB_SCRIPTS:
        if path.name == ADAPTER_NAME:
            continue
        n = 0
        for line in path.read_text(errors="ignore").splitlines():
            if CALL_PATTERN.search(line):
                n += 1
        if n:
            counts[f"lib/{path.name}"] = n
    return counts


def _allowlist() -> dict[str, int]:
    return json.loads(ALLOWLIST_PATH.read_text())


def test_no_new_or_grown_direct_supervisor_calls():
    current = _current_counts()
    allowed = _allowlist()

    new = {}
    grown = {}
    shrunk = {}
    for name, n in current.items():
        if name not in allowed:
            new[name] = n
        elif n > allowed[name]:
            grown[name] = (allowed[name], n)
        elif n < allowed[name]:
            shrunk[name] = (allowed[name], n)

    if shrunk:
        # Not a failure -- a shrink is progress. Printed so it is visible in
        # -rs / -v output; the allowlist itself still needs a human edit to
        # record the new, lower number (this test does not auto-update it,
        # or a real regression could ratchet itself down unnoticed one
        # accidental shrink-then-regrow cycle at a time).
        lines = ", ".join(f"{k}: {a} -> {b}" for k, (a, b) in sorted(shrunk.items()))
        print(f"supervisor ratchet: shrink recorded (update the allowlist) -- {lines}")

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
