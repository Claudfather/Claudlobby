"""tests/test_report_addresses.py — an address counts only if its reader can open it (#1708).

The compression rule puts the detail at an address and sends the address. A
bot's own data/ is stable and it exists, but the composed rules of every other
bot in its fleet deny reading anything in its directory, the manager's included
(composer.py's Layer 0 makes no exception for a role), so a pointer into it
arrives unreadable. The protocols name only addresses the reader can open, and
keep data/ for the author's own scratch.

The sweep pin follows tests/test_cadence_retirement.py: the library lines that
name a data/ directory are counted per file on KEEP, each with the reason it
survives. A new library file, or a new line in a kept one, fails by
construction, so whoever adds it says why it is not an address handed to a
reader who cannot open it. documentation/ is not composed into bots, so it is
not pinned here.
"""

from __future__ import annotations

import re
from pathlib import Path

from claudlobby.composer import compose_claude_md
from tests.conftest import install_real_template, load_test_fleet, make_paths
from tests.test_isolation import _denied, _deny

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "library"

#: The test, stated where token-efficiency and comms-topology list the addresses.
READER_TEST = "The detail must be at an address the addressee can open, not merely one that exists"
#: Each list's refusal of the author's own directory: token-efficiency and
#: report-back share the first wording, comms-topology states the second.
NEVER_OWN_DIR = "Never your own `data/` or anything else in your bot directory"
OWN_DIR_FAILS = "A path in your own bot directory, `data/` included, does not"

#: A data/ directory; not metadata/ or a path that only ends in data.
SWEEP = re.compile(r"(?<![\w.-])data/")

#: library-relative path -> (lines SWEEP matches, why they survive).
KEEP = {
    "protocols/token-efficiency.md": (1, "rule zero says your own data/ is not an address"),
    "protocols/comms-topology.md": (4, "Bot to bot says how a path in your bot directory fails its reader"),
    "protocols/report-back.md": (1, "Where the detail goes says never your own data/"),
    "expertise/code-review.md": (1, "what a bot may write in its own directory"),
    "skills/gws-reauth/SKILL.md": (2, "the bot's own scratch file for a callback URL"),
    "tools/README.md": (2, "where a tool's runtime output goes"),
    "protocols/fleet-observability.md": (5, "framework marker files scripts read (the idle marker, the stop record), and the retired event files"),
    "skills/adversarial-review/SKILL.md": (1, "users/data/systems, not a directory"),
    "lessons/review/empirical-verification.md": (1, "evidence the author quotes; no reader is sent to the path"),
}

#: The offers this fix removed from token-efficiency, comms-topology and report-back.
RETIRED = (
    "repo path, your `data/`",
    "or your `data/` |",
    "a path under your `data/`.",
    "a worklog, a vault note",
    "that deny does not currently block",
    "a doc in your `data/` or the fleet's `shared/`",
)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _hits() -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(p for p in LIB.rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lines = [(i, line) for i, line in enumerate(text.splitlines(), start=1) if SWEEP.search(line)]
        if lines:
            found[path.relative_to(LIB).as_posix()] = lines
    return found


def test_every_data_line_in_the_library_is_kept_with_a_reason():
    # Keyed on both sides, so a new file, a changed count and a dead KEEP entry all fail.
    found = _hits()
    counts = {rel: len(lines) for rel, lines in found.items()}
    kept = {rel: n for rel, (n, _why) in KEEP.items()}
    differ = {rel: found.get(rel, []) for rel in counts.keys() | kept.keys()
              if counts.get(rel, 0) != kept.get(rel, 0)}
    assert counts == kept, f"say why each data/ line is no address for another reader: {differ}"


def test_the_composed_protocols_carry_the_reader_test(fleet_dir):
    """A worker's CLAUDE.md rendered by the compositor from the real protocols and template."""
    # The fixture's stub would shadow the real report-back: the fleet overlay wins.
    (fleet_dir / "library" / "protocols" / "report-back.md").unlink()
    manifest = fleet_dir / "fleet.yaml"
    text = manifest.read_text()
    assert text.count("expertise: [software-engineering]") == 1
    manifest.write_text(text.replace(
        "expertise: [software-engineering]",
        "expertise: [software-engineering]\n"
        "      protocols: [report-back, token-efficiency, comms-topology]",
    ))
    install_real_template(fleet_dir)
    fleet = load_test_fleet(fleet_dir)
    composed = _flat(compose_claude_md(fleet.bots["worker-1"], fleet, make_paths(fleet_dir)))
    assert composed.count(READER_TEST) == 2, "token-efficiency and comms-topology each state it"
    assert composed.count(NEVER_OWN_DIR) == 2, "token-efficiency and report-back each refuse it"
    assert OWN_DIR_FAILS in composed
    assert "an address your manager can open" in composed
    for phrase in RETIRED:
        assert phrase not in composed, phrase


def test_a_managers_composed_rules_deny_a_workers_data_and_not_the_shared_tree(fleet_dir):
    """The policy the text relies on, in what the compositor composes for the manager.

    `_denied` models Claude Code's matcher; the live check (a peer's data/ refused,
    the fleet's shared/ opened) is in #1708's PR. A rule that someday covers the
    shared tree fails here before the protocols name an address nobody can open.
    """
    fleet, paths = load_test_fleet(fleet_dir), make_paths(fleet_dir)
    deny = _deny(fleet.bots["lead"], fleet, paths)
    assert _denied(deny, "Read", paths.bot_runtime("worker-1") / "data" / "report.md"), deny
    assert not _denied(deny, "Read", paths.shared_docs / "knowledge" / "note.md"), deny
    assert not _denied(deny, "Read", paths.bot_runtime("lead") / "data" / "report.md"), deny
