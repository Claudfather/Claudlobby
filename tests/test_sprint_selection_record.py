"""The selection record must refuse the shapes that read as evidence and are not (#974 Phase 0).

The sprint's output is work selection. Its failure modes do not throw, and every
signal it emits today is emitted identically by a working sprint and a random
picker. The only discriminating observable is the candidate set REJECTED with
scores -- so these tests are mostly about what the recorder REFUSES, not what it
stores.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "lib" / "sprint-selection-record.py"
_spec = importlib.util.spec_from_file_location("sprint_selection_record", _SRC)
ssr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ssr)


def _queries(filtered=3, unfiltered=357, rc=0):
    return [
        {"kind": "filtered", "cmd": "gh issue list --label x", "rc": rc, "count": filtered},
        {"kind": "unfiltered", "cmd": "gh issue list", "rc": 0, "count": unfiltered},
    ]


def _cands(n=3, selected=1):
    return [
        {"id": 100 + i, "scores": {"mission_alignment": 9 - i, "impact": 5, "effort": 2}}
        for i in range(n)
    ]


def _record(**over):
    kw = dict(
        ts="2026-08-21T04:00:00Z",
        run_id="r1",
        repo="Claudfather/Claudlobby",
        picker_version="0.1.0",
        mission_sha="abc123",
        queries=_queries(),
        candidates=_cands(3),
        selected_ids=[100],
        max_issues=3,
    )
    kw.update(over)
    return ssr.build_record(**kw)


# --- what it refuses to write ------------------------------------------------

def test_refuses_a_winners_only_record():
    """The code-audit-sweep shape applied to a stochastic scorer.

    That selector is DETERMINISTIC, so a winner-only row is re-derivable and
    checkable. An LLM scoring 357 issues is not, so the same shape yields a
    ledger that looks audited and cannot be.
    """
    with pytest.raises(ssr.RecordError, match="winners-only"):
        _record(candidates=_cands(1), selected_ids=[100])


def test_refuses_a_candidate_with_no_scores():
    bad = _cands(3)
    bad[2].pop("scores")
    with pytest.raises(ssr.RecordError, match="no scores"):
        _record(candidates=bad)


def test_refuses_a_query_with_no_rc():
    """An unchecked rc is how an empty result reads as an empty backlog."""
    q = _queries()
    q[0].pop("rc")
    with pytest.raises(ssr.RecordError, match="no rc"):
        _record(queries=q)


def test_refuses_a_record_with_no_unfiltered_count():
    """Without it, a broken filter and an empty backlog are the same row."""
    with pytest.raises(ssr.RecordError, match="unfiltered"):
        _record(queries=[_queries()[0]])


def test_refuses_selected_ids_absent_from_candidates():
    with pytest.raises(ssr.RecordError, match="not present"):
        _record(selected_ids=[999])


# --- the nothing-observed bucket, which needs the higher bar -----------------

def test_broken_filter_is_a_DEFECT_not_an_empty_backlog():
    """raw=0 / unfiltered=357 -- measured live: the shipped example label
    `claudna-eligible` matches zero labels against a 357-issue backlog and
    returns [] at rc 0."""
    rec = _record(queries=_queries(filtered=0, unfiltered=357), candidates=[], selected_ids=[])
    verdict, findings = ssr.verify(rec)
    assert verdict == ssr.DEFECT
    assert any("0 of 357" in f and "NOT an empty backlog" in f for f in findings)


def test_genuinely_empty_backlog_is_distinguishable_from_a_broken_filter():
    """The whole point: these two must not produce the same row."""
    rec = _record(queries=_queries(filtered=0, unfiltered=0), candidates=[], selected_ids=[])
    _, findings = ssr.verify(rec)
    assert any("genuinely empty" in f for f in findings)
    assert not any("broken filter" in f for f in findings)


def test_scoring_out_everything_is_a_legitimate_result_not_a_defect():
    """raw=357 / selected=0 is the sprint working and choosing nothing."""
    rec = _record(queries=_queries(filtered=3, unfiltered=357), selected_ids=[])
    verdict, findings = ssr.verify(rec)
    assert rec["outcome"] == ssr.OUTCOME_NO_WORK
    assert verdict == ssr.OK, findings


# --- verification rungs ------------------------------------------------------

def test_nonzero_rc_means_the_candidate_set_is_not_evidence():
    rec = _record(queries=_queries(rc=1))
    verdict, findings = ssr.verify(rec)
    assert verdict == ssr.DEFECT
    assert any("not evidence" in f for f in findings)


def test_truncated_record_is_caught():
    """Silent truncation reads as exhaustive coverage."""
    rec = _record(queries=_queries(filtered=50), candidates=_cands(3))
    verdict, findings = ssr.verify(rec)
    assert verdict == ssr.DEFECT
    assert any("incomplete record" in f for f in findings)


def test_a_good_record_verifies_OK():
    """Negative control -- a gate that never passes teaches people to bypass it."""
    verdict, findings = ssr.verify(_record())
    assert verdict == ssr.OK, findings


def test_invalid_is_distinct_from_defect():
    """'The instrument is broken' and 'the run was bad' need opposite responses."""
    verdict, _ = ssr.verify({"schema": 99})
    assert verdict == ssr.INVALID
    assert ssr.INVALID != ssr.DEFECT


# --- mission staleness (M7 / W6) --------------------------------------------

def test_mission_focus_pointing_at_closed_work_is_a_defect():
    """No selection metric can catch this -- the error is in the yardstick."""
    rec = _record(mission_focus_refs=["1271", "1272"])
    verdict, findings = ssr.verify(rec, issue_states={"1271": "CLOSED", "1272": "CLOSED"})
    assert verdict == ssr.DEFECT
    assert any("CLOSED work" in f for f in findings)


def test_mission_staleness_is_silent_without_the_offline_seam():
    """No issue states supplied -> not checked. Absence of the check must not
    manufacture a pass OR a failure."""
    verdict, _ = ssr.verify(_record(mission_focus_refs=["1271"]))
    assert verdict == ssr.OK


def test_focus_ref_parser_matches_hash_refs_only():
    """`#NNN` only -- bare digits collide with task ids and version numbers."""
    doc = (
        "# M\n\n## Current sprint focus\n\n"
        "1. Migrate to Apps, see #1271 and #1272\n"
        "2. Version 2 of the thing, 1999 items\n\n"
        "## Requires approval\n\n- unrelated #4242\n"
    )
    assert ssr.parse_focus_refs(doc) == ["1271", "1272"]


def test_focus_ref_parser_returns_empty_when_section_absent():
    assert ssr.parse_focus_refs("# M\n\n## Other\n\n#123\n") == []


# --- CLI ---------------------------------------------------------------------

def test_cli_exit_codes_separate_ok_defect_invalid(tmp_path, capsys):
    good = tmp_path / "g.json"
    good.write_text(json.dumps(_record()))
    assert ssr.main(["verify", "--file", str(good)]) == 0

    bad = tmp_path / "b.json"
    bad.write_text(json.dumps(_record(queries=_queries(rc=1))))
    assert ssr.main(["verify", "--file", str(bad)]) == 1

    broken = tmp_path / "x.json"
    broken.write_text(json.dumps({"schema": 99}))
    assert ssr.main(["verify", "--file", str(broken)]) == 2


# --- parser shapes found by running against the REAL document ---------------
# Every case below was a false result produced by an earlier version of the
# parser against PROJECT_MISSION.md itself, not an invented edge case.

def test_focus_ref_parser_matches_the_bold_header_the_real_doc_uses():
    """The real doc writes `**Current sprint focus:**` as BOLD TEXT, not a heading.

    A heading-only pattern returned [] against the live file, so the staleness
    rung silently checked nothing -- indistinguishable from a doc with no stale
    refs. The nothing-observed bucket, one layer down.
    """
    assert ssr.parse_focus_refs("**Current sprint focus:**\n\n1. ship #4242\n") == ["4242"]


def test_freshness_guidance_is_not_scraped_as_a_focus_item():
    """A note naming the issues it just RETIRED must not read as active focus.

    Reproduced for real: the freshness note added to this very section cites
    #1271-#1274 as recently shipped, and an earlier parser reported them as
    focus refs -- so the checker would have flagged them CLOSED and raised a
    DEFECT manufactured by the note written to prevent staleness.
    """
    doc = (
        "**Current sprint focus:**\n\n"
        "> **Recently shipped:** App auth (#1271, #1272, #1273, #1274 - all closed).\n"
        "> See #974 for the tracking epic.\n\n"
        "1. Integrate the plugin install into bootstrap\n\n"
        "*Items 1 carries no ref. Not verified as part of the #1271-#1274 correction.*\n"
    )
    assert ssr.parse_focus_refs(doc) == []


def test_a_numbered_item_citing_its_issue_is_still_found():
    """Negative control -- excluding prose must not exclude the real contract."""
    doc = (
        "**Current sprint focus:**\n\n"
        "> guidance mentioning #999\n\n"
        "1. Do the thing #1271\n"
        "2. Do the other thing\n\n"
        "*trailing caveat #888*\n"
    )
    assert ssr.parse_focus_refs(doc) == ["1271"]


def test_the_real_project_mission_doc_parses_without_a_false_defect():
    """Runs against the SHIPPED file, not a fixture.

    A fixture is written by the same person at the same sitting under the same
    understanding of the layout, so it cannot surprise you about the shape of
    real output -- which is exactly how both parser bugs above survived until
    the file itself was parsed.
    """
    mission = Path(__file__).resolve().parent.parent / "PROJECT_MISSION.md"
    refs = ssr.parse_focus_refs(mission.read_text())
    closed = {"1271": "CLOSED", "1272": "CLOSED", "1273": "CLOSED", "1274": "CLOSED"}
    stale = [r for r in refs if closed.get(r) == "CLOSED"]
    assert not stale, (
        f"PROJECT_MISSION.md sprint focus cites CLOSED work as current: {stale}. "
        "The picker would source its goal from completed items."
    )


# --- #1317: three states, not two -------------------------------------------
# The staleness rung resolved a ref by asking "is it CLOSED?", so ABSENT and
# UNRECOGNISED both fell through to a silent pass -- making a coverage gap
# indistinguishable from a confirmed-open ref. It was the one rung in this module
# that exempted itself from the module's own standard, and it failed toward
# "fine", the direction nobody re-checks.

def test_a_ref_absent_from_the_map_is_UNKNOWN_not_a_silent_pass():
    """Reviewer's repro 1: refs=['9999'], states={'1271':'OPEN'}."""
    verdict, findings = ssr.verify(
        _record(mission_focus_refs=["9999"]), issue_states={"1271": "OPEN"}
    )
    assert any(f.startswith("UNKNOWN") for f in findings), findings
    assert any("absent from the map" in f for f in findings)


def test_an_unrecognised_state_is_UNKNOWN_not_a_silent_pass():
    """Reviewer's repro 2: states={'1271':'MERGED'} -- neither OPEN nor CLOSED."""
    verdict, findings = ssr.verify(
        _record(mission_focus_refs=["1271"]), issue_states={"1271": "MERGED"}
    )
    assert any(f.startswith("UNKNOWN") for f in findings), findings
    assert any("MERGED" in f for f in findings)


def test_UNKNOWN_is_disclosed_but_never_scored_as_a_failure():
    """A gap must be VISIBLE without being a DEFECT.

    Scoring it as a failure would train a caller to suppress the check and lose
    the CLOSED detection with it -- the `credentials.py` shape-3 posture.
    """
    verdict, findings = ssr.verify(
        _record(mission_focus_refs=["9999"]), issue_states={"1271": "OPEN"}
    )
    assert verdict == ssr.OK          # not scored as a failure
    assert findings                    # but never silent


def test_UNKNOWN_alongside_CLOSED_still_reports_the_DEFECT_and_both():
    """A gap must not mask a real defect, nor be dropped by one."""
    verdict, findings = ssr.verify(
        _record(mission_focus_refs=["1271", "9999"]),
        issue_states={"1271": "CLOSED"},
    )
    assert verdict == ssr.DEFECT
    assert any("CLOSED work" in f for f in findings)
    assert any(f.startswith("UNKNOWN") for f in findings)


def test_OPEN_and_CLOSED_still_behave_after_the_three_state_change():
    """Do not fix the absent case and break the present one."""
    v_open, f_open = ssr.verify(
        _record(mission_focus_refs=["1271"]), issue_states={"1271": "OPEN"}
    )
    assert (v_open, f_open) == (ssr.OK, [])

    v_closed, f_closed = ssr.verify(
        _record(mission_focus_refs=["1271"]), issue_states={"1271": "CLOSED"}
    )
    assert v_closed == ssr.DEFECT
    assert any("CLOSED work" in f for f in f_closed)


def test_an_EMPTY_map_resolves_every_ref_to_UNKNOWN_but_None_stays_silent():
    """`None` means the check was not requested; `{}` means it was and nothing is known.

    Collapsing those would either make the offline seam mandatory or re-open the
    silent pass, so they are deliberately different.
    """
    v_empty, f_empty = ssr.verify(_record(mission_focus_refs=["1271"]), issue_states={})
    assert v_empty == ssr.OK
    assert any(f.startswith("UNKNOWN") for f in f_empty), f_empty

    v_none, f_none = ssr.verify(_record(mission_focus_refs=["1271"]), issue_states=None)
    assert (v_none, f_none) == (ssr.OK, [])
