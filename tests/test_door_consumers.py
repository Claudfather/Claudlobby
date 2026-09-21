"""The door consumer map, kept honest by re-derivation (#1573 PR B, task 2).

A `lib/` door whose signature or meaning changes has a set of call sites, and
that set is the thing a plan gets wrong. PR B's first draft listed two of the
four consumers of the boot-progress predicate; the two it missed were both on
the alerting path, and because the new argument is OPTIONAL neither would have
failed loudly -- they would simply never have gained the new rung, and a queued
bot on a launchd host would have paged twice per tick for its whole admission
wait. The set is greppable, so a test can own it: `tests/door_consumers.json`
records the consumers a human argued about, and this file re-derives them and
refuses any disagreement.

Same shape and same reason as `tests/test_supervisor_ratchet.py`, with one
deliberate difference. The ratchet is a FENCE on a population nobody intends to
enumerate (109 calls in 20 files), so it tolerates a shrink and only refuses
growth. This map is a four-entry ENUMERATION that a human is expected to keep
current, so it refuses in BOTH directions: a consumer that disappears leaves a
stale row behind, and a stale row silently pre-authorises a brand-new call
landing in that same file up to the old count -- the exact hazard the ratchet's
own zero-entry rule exists to close, just reached from the other side.

Keyed by FILE and COUNT, never by line number. Lines move on every edit, and a
line-keyed map would be stale before it was reviewed.

TWO RULES, and the second is the one with teeth:

  1. The derived (file -> count) map equals the checked-in one, exactly.
  2. Every consumer the map marks `bot_dir: required` actually passes a SECOND
     argument at every one of its call sites, and none of them sits behind a
     same-line `&&` short-circuit. Rule 1 alone would pass a branch where all
     four call sites still exist and none of them was updated -- which is
     precisely the state this map was written to catch.

A `driver` is exempt from rule 2 by name, because varying the arity is what a
driver does. Exempting it by silence would have been the same mistake one layer
down.

Self-exclusion: this file and the map are skipped by the scan. Both necessarily
carry the door's name, and a test that counted its own prose as a call site
would be unable to describe what it checks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
MAP_PATH = Path(__file__).resolve().parent / "door_consumers.json"
SCAN_ROOTS = ("lib", "tests", "claudlobby")
SELF_EXCLUDED = frozenset({f"tests/{Path(__file__).name}", f"tests/{MAP_PATH.name}"})


def _call_pattern(door: str) -> re.Pattern[str]:
    """A CALL, not a mention: the door's name followed by whitespace and the
    first character of a shell argument (a quote or a `$`).

    Deliberately narrow in one direction. It excludes the definition line
    (`name() {` -- no whitespace), every usage string (`name <bot_service>` --
    `<`), every possessive (`name's`) and every sentence that merely names the
    door (`name reads ...`, `name is the ...`). A prose mention that happened
    to be followed by a quoted word would be counted, and that is the safe
    direction: it lands as an unexplained row a human must reconcile, rather
    than as a call site nobody was told about.
    """
    return re.compile(r"\b" + re.escape(door) + r"[ \t]+[\"$]")


def _arity_two_pattern(door: str) -> re.Pattern[str]:
    """The door called with TWO arguments on one line.

    Each argument is either a double-quoted word or a run of characters that
    stops at shell punctuation. Stopping at `;` `&` `|` `)` `}` is what makes
    the single-argument case fail rather than silently swallowing the
    terminator and reading the next shell keyword as an argument -- measured:
    without the exclusion, `name "$SVC"; then` matched `then` as argument two.
    """
    arg = r"(?:\"[^\"]*\"|[^\s;&|)}]+)"
    return re.compile(r"\b" + re.escape(door) + r"[ \t]+" + arg + r"[ \t]+" + arg)


# A same-line guard in front of the call. The marker rung needs no unit name,
# so a `[ -n "$BOT_SERVICE" ] &&` in front of the call makes it unreachable for
# exactly the bots that have no service name -- which on launchd is every bot
# mid-boot, since the platform's other rung does not exist.
_SHORT_CIRCUIT = re.compile(r"&&[^&|]*$")


def _scan_files() -> list[tuple[str, str]]:
    """(repo-relative path, text) for every scanned regular file."""
    out: list[tuple[str, str]] = []
    for root in SCAN_ROOTS:
        base = REPO_DIR / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            rel = str(path.relative_to(REPO_DIR))
            if rel in SELF_EXCLUDED:
                continue
            out.append((rel, path.read_text(errors="ignore")))
    return out


def _derive(door: str) -> dict[str, int]:
    pat = _call_pattern(door)
    counts: dict[str, int] = {}
    for rel, text in _scan_files():
        n = len(pat.findall(text))
        if n:
            counts[rel] = n
    return counts


def _doors() -> dict:
    return json.loads(MAP_PATH.read_text())["doors"]


def test_every_call_site_is_in_the_map():
    for door, spec in _doors().items():
        derived = _derive(door)
        declared = {f: e["calls"] for f, e in spec["consumers"].items()}

        unlisted = {f: n for f, n in derived.items() if f not in declared}
        missing = {f: n for f, n in declared.items() if f not in derived}
        moved = {
            f: (declared[f], derived[f])
            for f in sorted(set(declared) & set(derived))
            if declared[f] != derived[f]
        }

        assert not unlisted, (
            f"{door}: call site(s) in file(s) absent from {MAP_PATH.name}: "
            f"{unlisted}. A consumer nobody listed is a consumer nobody "
            f"updated -- add it to the map with its count and why it calls "
            f"this door, and check it was migrated."
        )
        assert not missing, (
            f"{door}: {MAP_PATH.name} lists file(s) that no longer call it: "
            f"{missing}. Delete the row -- a stale row pre-authorises a new "
            f"call landing in that file up to the old count."
        )
        assert not moved, (
            f"{door}: call count changed for "
            + ", ".join(f"{f}: {a} -> {b}" for f, (a, b) in sorted(moved.items()))
            + f". Update {MAP_PATH.name} to the measured count once the new or "
            f"removed call site has been reviewed."
        )


def test_the_derivation_is_not_vacuous():
    """A positive control for the matcher itself.

    Exact equality would be satisfied by a matcher that found nothing against a
    map that listed nothing, so the shape of the map is asserted too: a door
    under enforcement has consumers, and each is a file that exists.
    """
    doors = _doors()
    assert doors, f"{MAP_PATH.name} enforces no door at all"
    for door, spec in doors.items():
        derived = _derive(door)
        assert derived, (
            f"{door}: the call matcher found ZERO call sites anywhere under "
            f"{SCAN_ROOTS}. Either the door was renamed (update the map) or "
            f"the matcher stopped matching, in which case this file has been "
            f"silently certifying nothing."
        )
        for rel in spec["consumers"]:
            assert (REPO_DIR / rel).is_file(), f"{door}: {rel} does not exist"


def test_every_runtime_consumer_passes_a_bot_dir():
    """Rule 2 -- the one with teeth.

    The map's whole purpose is that a door which GAINED an argument actually
    receives it everywhere it matters. A count-only check passes a branch where
    every call site still exists and not one of them was changed.
    """
    for door, spec in _doors().items():
        call = _call_pattern(door)
        arity2 = _arity_two_pattern(door)
        for rel, entry in spec["consumers"].items():
            if entry.get("bot_dir") != "required":
                continue
            text = (REPO_DIR / rel).read_text(errors="ignore")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if not call.search(line):
                    continue
                assert arity2.search(line), (
                    f"{rel}:{lineno} calls {door} without a second argument: "
                    f"{line.strip()!r}. The map marks this consumer "
                    f"bot_dir: required -- the marker rung is unreachable "
                    f"without it, and on launchd it is the only rung there is."
                )
                before = line[: call.search(line).start()]
                assert not _SHORT_CIRCUIT.search(before), (
                    f"{rel}:{lineno} guards {door} behind a same-line `&&`: "
                    f"{line.strip()!r}. The marker rung needs no unit name, so "
                    f"a short-circuit on one makes it unreachable for exactly "
                    f"the bots that have none."
                )
