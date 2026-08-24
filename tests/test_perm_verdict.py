"""Tests for lib/perm-verdict.py -- the transcript scorer for the permissions harness.

Records here are TRANSCRIBED FROM REAL CAPTURES on claude 2.1.240 (2026-08-24,
throwaway `claude -p` arms), not invented. The denial string in particular is
verbatim: a scorer whose fixtures were written by the same person, at the same
sitting, under the same assumption about the shape cannot be surprised by real
output -- which is how three earlier detectors in this work passed review and then
found nothing.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "lib" / "perm-verdict.py"
_spec = importlib.util.spec_from_file_location("perm_verdict", _SRC)
pv = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# Register before exec: @dataclass resolves cls.__module__ through sys.modules, and
# a module loaded by path alone is absent from it.
sys.modules["perm_verdict"] = pv
_spec.loader.exec_module(pv)


def rows(*, use_id="t1", tool="Bash", payload=None, result=None, is_error=False):
    """Build a transcript fragment. `result=None` omits the tool_result entirely."""
    out = [{"message": {"content": [
        {"type": "tool_use", "id": use_id, "name": tool, "input": payload or {}}]}}]
    if result is not None:
        out.append({"message": {"content": [
            {"type": "tool_result", "tool_use_id": use_id,
             "is_error": is_error, "content": result}]}})
    return out


def score(r, value, *, tool="Bash", key="command", mode="headless"):
    return pv.classify_probe(r, tool_name=tool, payload_key=key,
                             payload_value=value, mode=mode)


# --- the three informative outcomes ------------------------------------------

def test_executed():
    r = rows(payload={"command": "touch /tmp/MARK"}, result="(Bash completed with no output)")
    assert score(r, "touch /tmp/MARK").outcome == pv.EXECUTED


def test_denied_verbatim_real_string():
    r = rows(payload={"command": "factor 12"}, is_error=True,
             result="Permission to use Bash with command factor 12 has been denied.")
    assert score(r, "factor 12").outcome == pv.DENIED


def test_not_attempted_when_no_call():
    assert score([], "factor 12").outcome == pv.NOT_ATTEMPTED


# --- the fail-closed rung: an error is NOT a denial ---------------------------

def test_command_not_found_is_not_scored_as_denied():
    """The load-bearing guard. A broken probe must never read as enforcement.

    This is the `factor 12` lesson: that probe was ungranted as well as denied, so
    it came back not-executed in every arm and discriminated nothing.
    """
    r = rows(payload={"command": "nope"}, is_error=True, result="bash: nope: command not found")
    got = score(r, "nope")
    assert got.outcome == pv.ERROR_OTHER
    assert got.outcome != pv.DENIED
    assert not got.informative


def test_unrecognised_denial_wording_degrades_to_uninformative_not_executed():
    """If the vendor rewords a denial, we must refuse -- never fall through to a
    clean verdict. Fail toward refusing, never toward reassurance."""
    r = rows(payload={"command": "x"}, is_error=True, result="Some future wording nobody predicted")
    assert score(r, "x").outcome == pv.ERROR_OTHER


# --- vera's rc5 tightenings ---------------------------------------------------

def test_invocation_with_no_result_is_uninformative():
    r = rows(payload={"command": "hangs"}, result=None)
    got = score(r, "hangs")
    assert got.outcome == pv.NO_RESULT
    assert not got.informative


def test_exact_match_rejects_an_adjacent_command():
    r = rows(payload={"command": "touch /tmp/MARK-OTHER"}, result="")
    assert score(r, "touch /tmp/MARK").outcome == pv.NOT_ATTEMPTED


def test_the_exact_match_guard_has_teeth():
    """Mutation check: a SUBSTRING matcher would have passed the adjacent case.

    Without this, `test_exact_match_rejects_an_adjacent_command` could be green
    because nothing matched for an unrelated reason.
    """
    adjacent = "touch /tmp/MARK-OTHER"
    probe = "touch /tmp/MARK"
    assert probe in adjacent            # a substring matcher WOULD have fired
    r = rows(payload={"command": adjacent}, result="")
    assert score(r, probe).outcome == pv.NOT_ATTEMPTED   # exact matching does not


def test_pairing_is_by_id_not_document_order():
    """An interleaved result must not be credited to the wrong invocation."""
    r = [
        {"message": {"content": [
            {"type": "tool_use", "id": "A", "name": "Bash", "input": {"command": "probe"}}]}},
        {"message": {"content": [
            {"type": "tool_use", "id": "B", "name": "Bash", "input": {"command": "other"}}]}},
        {"message": {"content": [
            {"type": "tool_result", "tool_use_id": "B", "is_error": True,
             "content": "Permission to use Bash with command other has been denied."}]}},
    ]
    # 'probe' has no result of its own; B's denial must NOT be borrowed for it.
    assert score(r, "probe").outcome == pv.NO_RESULT


def test_conflicting_outcomes_report_ambiguous_with_no_tiebreak():
    r = [
        {"message": {"content": [
            {"type": "tool_use", "id": "A", "name": "Bash", "input": {"command": "p"}}]}},
        {"message": {"content": [
            {"type": "tool_result", "tool_use_id": "A", "is_error": True,
             "content": "Permission to use Bash with command p has been denied."}]}},
        {"message": {"content": [
            {"type": "tool_use", "id": "B", "name": "Bash", "input": {"command": "p"}}]}},
        {"message": {"content": [
            {"type": "tool_result", "tool_use_id": "B", "is_error": False, "content": "ok"}]}},
    ]
    got = score(r, "p")
    assert got.outcome == pv.AMBIGUOUS
    assert not got.informative


# --- the mode gate ------------------------------------------------------------

def test_interactive_scores_identically_to_headless():
    """Captured 2026-08-24 on claude 2.1.240: the interactive TUI and `claude -p`
    emit byte-identical denial records. Verified, not assumed -- which is the only
    reason `interactive` is in SIGNATURES_VALIDATED_FOR at all."""
    r = rows(payload={"command": "factor 12"}, is_error=True,
             result="Permission to use Bash with command factor 12 has been denied.")
    assert score(r, "factor 12", mode="headless").outcome == pv.DENIED
    assert score(r, "factor 12", mode="interactive").outcome == pv.DENIED


def test_validated_modes_are_exactly_those_with_captured_specimens():
    """Tripwire. Each entry must correspond to a real captured denial+success pair.

    If a mode is added here without that capture, this test is the thing that
    should have stopped it -- update it only alongside the provenance comment in
    the module naming the binary version and the capture date.
    """
    assert pv.SIGNATURES_VALIDATED_FOR == frozenset({"headless", "interactive"})


def test_an_unknown_mode_still_refuses():
    r = rows(payload={"command": "factor 12"}, is_error=True,
             result="Permission to use Bash with command factor 12 has been denied.")
    assert score(r, "factor 12", mode="ssh-tty").outcome == pv.UNVALIDATED_MODE


# --- verdict identity ---------------------------------------------------------

def test_mode_is_part_of_the_verdict_not_a_footnote():
    a = pv.verdict_identity("ENFORCED", "headless", "auto")
    b = pv.verdict_identity("ENFORCED", "interactive", "auto")
    assert a != b
    assert "headless" in a and "interactive" in b
    assert "permission-mode=auto" in a


# --- controls -----------------------------------------------------------------

def test_control_verdict_refuses_when_positive_control_did_not_fire():
    pos = pv.ProbeResult(pv.EXECUTED)
    neg = pv.ProbeResult(pv.EXECUTED)
    ok, why = pv.control_verdict(pos, neg)
    assert not ok and "positive control did not fire" in why


def test_control_verdict_refuses_on_blanket_failure():
    pos = pv.ProbeResult(pv.DENIED)
    neg = pv.ProbeResult(pv.ERROR_OTHER)
    ok, why = pv.control_verdict(pos, neg)
    assert not ok and "blanket failure" in why


def test_control_verdict_accepts_only_the_sound_pair():
    ok, _ = pv.control_verdict(pv.ProbeResult(pv.DENIED), pv.ProbeResult(pv.EXECUTED))
    assert ok


# --- the dry-run drives the real path ----------------------------------------

def test_dry_run_exercises_the_real_scorer_and_passes():
    assert pv.main(["--dry-run"]) == 0
