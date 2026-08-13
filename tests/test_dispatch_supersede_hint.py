"""Tests for the #1032 undeclared-supersession hint.

The property under test is not "does it detect a re-dispatch" — it cannot, and
must not claim to. It is **does it stay quiet enough to be worth reading**, and
**does it refuse to decide**. Both are load-bearing:

  * Quiet: 51% of real dispatches go to a bot already holding an open row. A note
    on half of all traffic is the dead-signal defect #1032 is itself about, so the
    loud tier is gated on a narrower test (11% measured) and everything else is
    recorded instead of said.
  * Refuses to decide: it never rewrites `--supersedes`, never blocks, and never
    picks a row. Oldest-open-first would have produced the SAME wrong pairing in
    the filed instance, so "helpfully resolve it" is measurably no better here,
    and blanket-closing older rows is #447.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, REPO_ROOT / "lib" / name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


hint_mod = _load("dispatch-supersede-hint.py", "dispatch_supersede_hint")


def _write(path: Path, rows: list[dict]) -> str:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


def _dispatch(bot, tid, task, da=1000, eb=99999):
    return {
        "ts": "2026-08-12T10:00:00Z",
        "manager": "m",
        "bot": bot,
        "task_id": tid,
        "task": task,
        "dispatched_at": da,
        "expected_by": eb,
    }


def _fixture(tmp_path, dispatches, reports=()):
    return (
        _write(tmp_path / "d.jsonl", dispatches),
        _write(tmp_path / "r.jsonl", list(reports)),
    )


# ------------------------------------------------------------------ references


def test_refs_reads_hash_numbers():
    assert hint_mod.refs("look at #1032 and #447") == {"1032", "447"}


def test_refs_ignores_a_url_path_segment():
    """A `/1032` in a path is not a reference; the lookbehind excludes `/`."""
    assert hint_mod.refs("https://github.com/o/r/issues/1032") == set()


def test_refs_ignores_short_and_long_runs():
    """`#1` is prose ("the #1 priority"); a long run is not an issue number."""
    assert hint_mod.refs("the #1 priority is #12") == set()
    assert hint_mod.refs("#1234567") == set()


def test_refs_does_not_match_inside_a_task_id():
    """Task ids carry 10-digit epochs; they must never read as references."""
    assert hint_mod.refs("t-1786559778-3b08") == set()


def test_ref_from_url_takes_the_trailing_number():
    assert hint_mod.ref_from_url("https://github.com/o/r/issues/1032") == {"1032"}
    assert hint_mod.ref_from_url("https://github.com/o/r/pull/1032/") == {"1032"}
    assert hint_mod.ref_from_url("") == set()
    assert hint_mod.ref_from_url("https://example.com/no-number") == set()


def test_the_url_form_is_only_applied_to_the_incoming_dispatch():
    """The stored `task` field never contains the envelope's `ref:` URL.

    `dispatch-task.sh` assembles the envelope separately from the payload it
    stores, so a `ref:https?://…` pattern matched **0 of 463** real rows. It was
    dead code in the first cut of the helper and only running it against the real
    ledger showed that. `refs()` is therefore hash-only, and the URL form is a
    separate function applied to the NEW dispatch, where the value does exist.
    """
    stored = "ref:https://github.com/o/r/issues/1032 in the body"
    assert hint_mod.refs(stored) == set()


# ------------------------------------------------------------------- the tiers


def test_quiet_tier_counts_open_rows_and_says_nothing(tmp_path):
    """The 51% case: open rows exist, nothing references anything. Recorded only."""
    d, r = _fixture(
        tmp_path,
        [
            _dispatch("w1", "t-1", "do the first thing"),
            _dispatch("w1", "t-2", "do the second thing", da=1100),
        ],
    )
    count, matching, note = hint_mod.hint("w1", d, r, "a third unrelated thing")
    assert count == 2
    assert matching == []
    assert note == "", (
        "the quiet tier must never speak — 51% of traffic looks like this"
    )


def test_loud_tier_fires_on_a_shared_reference(tmp_path):
    d, r = _fixture(tmp_path, [_dispatch("w1", "t-1", "investigate #4242 please")])
    count, matching, note = hint_mod.hint("w1", d, r, "another look at #4242")
    assert count == 1
    assert matching == ["t-1"]
    assert "t-1" in note and "--supersedes t-1" in note


def test_loud_tier_fires_via_the_envelope_ref_url(tmp_path):
    """A new `ref:…/issues/4242` matches an open row whose prose says `#4242`."""
    d, r = _fixture(tmp_path, [_dispatch("w1", "t-1", "investigate #4242 please")])
    _c, matching, note = hint_mod.hint(
        "w1", d, r, "another look", new_ref="https://github.com/o/r/issues/4242"
    )
    assert matching == ["t-1"] and note


def test_the_note_names_the_choice_and_never_asserts_supersession(tmp_path):
    """Phrasing is a property, not a detail.

    Two dispatches naming one issue may be parallel work. The note must put the
    id within copy-paste reach without claiming the caller forgot something.
    """
    d, r = _fixture(tmp_path, [_dispatch("w1", "t-1", "#4242 work")])
    _c, _m, note = hint_mod.hint("w1", d, r, "more on #4242")
    assert "if this REPLACES it" in note
    assert "if it is additional work, nothing to do" in note
    for accusation in ("forgot", "should have", "error", "missing --supersedes"):
        assert accusation not in note.lower()


def test_no_open_rows_means_silence_and_zero(tmp_path):
    d, r = _fixture(tmp_path, [])
    assert hint_mod.hint("w1", d, r, "anything about #4242") == (0, [], "")


def test_another_bots_open_row_never_matches(tmp_path):
    """Openness is per-bot; one bot's row must not surface on another's dispatch."""
    d, r = _fixture(tmp_path, [_dispatch("w2", "t-1", "investigate #4242")])
    assert hint_mod.hint("w1", d, r, "more on #4242") == (0, [], "")


def test_a_closed_row_does_not_match(tmp_path):
    """Openness comes from the shipped door, so a terminal report retires the row."""
    d, r = _fixture(
        tmp_path,
        [_dispatch("w1", "t-1", "investigate #4242")],
        [
            {
                "ts": "2026-08-12T10:30:00Z",
                "bot": "w1",
                "task_id": "t-1",
                "status": "completed",
                "summary": "done",
            }
        ],
    )
    assert hint_mod.hint("w1", d, r, "more on #4242") == (0, [], "")


def test_many_matches_are_capped_and_the_cap_is_disclosed(tmp_path):
    """A note that must be scrolled past has already failed to route attention."""
    d, r = _fixture(
        tmp_path,
        [_dispatch("w1", f"t-{i}", "on #4242", da=1000 + i) for i in range(5)],
    )
    _c, matching, note = hint_mod.hint("w1", d, r, "more #4242")
    assert len(matching) == 5
    assert "+2 more" in note, "silently showing 3 of 5 is the coverage-honesty defect"


# ------------------------------------------------------------------- fail-open


def test_an_unreadable_dispatch_log_never_breaks_a_dispatch(tmp_path):
    """A dispatch must never fail because a hint helper could not read a file."""
    r = _write(tmp_path / "r.jsonl", [])
    assert hint_mod.hint("w1", str(tmp_path / "missing.jsonl"), r, "#4242") == (
        0,
        [],
        "",
    )


def test_a_raising_open_door_degrades_to_silence(tmp_path):
    class Boom:
        def open_dispatches(self, *a, **k):
            raise RuntimeError("door exploded")

    d, r = _fixture(tmp_path, [_dispatch("w1", "t-1", "#4242")])
    assert hint_mod.hint("w1", d, r, "#4242", overdue_mod=Boom()) == (0, [], "")


def test_task_texts_survives_a_corrupt_line(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text('{"bot":"w1","task_id":"t-1","task":"ok"}\nNOT JSON\n')
    assert hint_mod.task_texts(str(p), "w1") == {"t-1": "ok"}


# ------------------------------------------------------ what it must never do


def test_it_returns_no_decision_only_facts(tmp_path):
    """The contract is (count, candidates, note) — never a chosen supersedes id.

    #1027's thesis is that the ledger records what was SENT, never what was
    MEANT. This helper cannot see intent either, so its output must remain
    something a human acts on rather than something a script applies.
    """
    d, r = _fixture(tmp_path, [_dispatch("w1", "t-1", "#4242")])
    result = hint_mod.hint("w1", d, r, "#4242")
    assert isinstance(result, tuple) and len(result) == 3
    count, matching, note = result
    assert (
        isinstance(count, int) and isinstance(matching, list) and isinstance(note, str)
    )
    # Candidates, plural-capable — never collapsed to a single "the" answer.
    assert matching == ["t-1"]


def test_a_new_reference_does_NOT_match_an_unrelated_open_row(tmp_path):
    """The false-positive case, and the one the first test pass missed entirely.

    A mutation replacing the reference comparison with `True` survived, because
    every existing case either had no refs on the new task or no open rows at
    all. Nothing asserted that a referenced dispatch stays SILENT against an
    open row about something else — which is exactly the 51%-noise failure the
    loud tier exists to avoid.
    """
    d, r = _fixture(tmp_path, [_dispatch("w1", "t-1", "an unrelated task, no refs")])
    count, matching, note = hint_mod.hint("w1", d, r, "please look at #4242")
    assert count == 1, "the row is still open and still counted"
    assert matching == [] and note == "", "spoke about a row sharing no reference"


def test_a_different_reference_does_not_match(tmp_path):
    d, r = _fixture(tmp_path, [_dispatch("w1", "t-1", "work on #1111")])
    assert hint_mod.hint("w1", d, r, "work on #2222")[1:] == ([], "")


def test_the_overflow_count_matches_the_ids_actually_listed(tmp_path):
    """Pins the disclosure to what is shown, not to a second copy of the cap."""
    d, r = _fixture(
        tmp_path,
        [_dispatch("w1", f"t-{i}", "on #4242", da=1000 + i) for i in range(7)],
    )
    _c, matching, note = hint_mod.hint("w1", d, r, "more #4242")
    listed = [t for t in matching if t in note]
    assert len(matching) == 7
    assert f"+{len(matching) - len(listed)} more" in note, (
        f"note lists {len(listed)} ids but does not disclose the rest: {note!r}"
    )
