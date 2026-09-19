# tests/test_checkins_summary.py
"""`claudlobby checkins --summary` (manager check-in PR 3, task 3): the window
rolled up -- actions, ask rate, considered lengths, unavailable frequencies and
dispatch outcomes, grouped by project_key. `summarize(rows)` is a PURE function
of the row dicts `collect_checkins` returns (no db, no clock), so the math is
unit-tested with hand-built rows below; the CLI-level tests drive the same
fixtures `test_checkins_outcome_join.py` imports from `test_checkins_cli.py`
rather than inventing a second seeding path.

This door produces FACTS ONLY -- a count, a distribution -- never a verdict:
no threshold, no "healthy"/"unhealthy" wording anywhere here or in cmd_checkins."""

from __future__ import annotations

import copy
import re

from claudlobby.commands import checkins as cmd
from claudlobby.plane.emit_api import emit_batch
from tests.test_checkins_cli import F, _Args, _decision, _out, root  # noqa: F401

CK1, CK2, CK3 = ("ck_" + c * 32 for c in "abc")


def _r(action=None, project_key=None, considered=None, unavailable=None,
       raised=False, record="x", dispatches=None) -> dict:
    """A hand-built row in the shape `collect_checkins`/`_row` return -- only
    the fields `summarize` reads. `record` defaults to a truthy sentinel (any
    non-None value counts as "has a record"); pass record=None for a
    record-less row, the shape PR 1 gives a truncated or data-less check-in."""
    return {
        "action": action, "project_key": project_key,
        "considered": considered or [], "unavailable": unavailable or [],
        "raise": {"decided": raised, "reason": None},
        "record": record, "dispatches": dispatches or [],
    }


# --- summarize(): pure function of hand-built rows --------------------------

def test_summarize_is_a_pure_function_of_the_rows():
    rows = [_r(action="nothing", project_key="shop"), _r(action="dispatch", project_key="shop")]
    result = cmd.summarize(rows)
    assert set(result) == {"totals", "projects"}
    assert result["totals"]["checkins"] == 2
    assert result["totals"]["actions"] == {"dispatch": 1, "ask": 0, "nothing": 1}
    assert [p["project_key"] for p in result["projects"]] == ["shop"]


def test_the_action_distribution_counts_every_action_and_the_record_less_rows():
    rows = [_r(action="dispatch"), _r(action="dispatch"), _r(action="ask"),
            _r(action="nothing"), _r(action="nothing"), _r(action="nothing"),
            _r(action=None, record=None)]
    totals = cmd.summarize(rows)["totals"]
    assert totals["checkins"] == 7
    assert totals["actions"] == {"dispatch": 2, "ask": 1, "nothing": 3}
    assert totals["no_record"] == 1


def test_the_ask_rate_is_raised_over_checkins():
    rows = [_r(raised=True), _r(raised=True), _r(), _r(), _r(), _r(), _r(), _r()]
    totals = cmd.summarize(rows)["totals"]
    assert totals["raised"] == 2
    assert totals["ask_rate"] == 0.25
    assert cmd.summarize([])["totals"]["ask_rate"] == 0.0     # never a ZeroDivisionError


def test_the_considered_lengths_exclude_the_record_less_rows():
    rows = [_r(considered=[]), _r(considered=["a", "b", "c"]),
            _r(considered=["a", "b", "c", "d", "e"]),
            _r(record=None, considered=["should", "not", "count"])]
    totals = cmd.summarize(rows)["totals"]
    assert totals["considered"] == {"rows": 3, "empty": 1, "min": 0, "max": 5, "mean": 2.667}
    assert totals["no_record"] == 1


def test_the_unavailable_frequencies_are_per_token():
    rows = [_r(unavailable=["gh"]), _r(unavailable=["gh", "claudron"])]
    assert cmd.summarize(rows)["totals"]["unavailable"] == {"gh": 2, "claudron": 1}


def test_dispatch_outcomes_count_join_rows_and_the_unjoined_decisions():
    rows = [
        _r(action="dispatch", dispatches=[{"outcome": "completed", "status": "completed"}]),
        _r(action="dispatch", dispatches=[{"outcome": "open", "status": "progress"}]),
        _r(action="dispatch", dispatches=[]),          # a dispatch decision with no join row
    ]
    totals = cmd.summarize(rows)["totals"]
    assert totals["dispatch_outcomes"] == {"completed": 1, "blocked": 0, "failed": 0,
                                            "retired": 0, "open": 1, "unjoined": 1}
    assert totals["dispatches"] == 2


def test_a_non_dispatch_decision_with_no_join_row_is_not_unjoined():
    rows = [_r(action="ask"), _r(action="nothing")]
    totals = cmd.summarize(rows)["totals"]
    assert totals["dispatch_outcomes"]["unjoined"] == 0
    assert totals["dispatches"] == 0


def test_dispatch_statuses_count_the_raw_plane_statuses_and_skip_the_missing_ones():
    rows = [
        _r(action="dispatch", dispatches=[
            {"outcome": "completed", "status": "completed"},
            {"outcome": "blocked", "status": "returned_blocked"},
        ]),
        _r(action="dispatch", dispatches=[
            {"outcome": "completed", "status": "completed"},
            {"outcome": "unjoined", "status": None},       # no assignment row -- must not count as a status
        ]),
    ]
    totals = cmd.summarize(rows)["totals"]
    assert totals["dispatch_statuses"] == {"completed": 2, "returned_blocked": 1}


def test_the_groups_are_by_project_key_with_null_last():
    rows = [_r(project_key="shop"), _r(project_key="docs"),
            _r(project_key="shop"), _r(project_key=None)]
    result = cmd.summarize(rows)
    assert [p["project_key"] for p in result["projects"]] == ["docs", "shop", None]
    assert sum(p["checkins"] for p in result["projects"]) == result["totals"]["checkins"]


def test_the_group_blocks_have_the_same_shape_as_totals():
    rows = [_r(project_key="shop"), _r(project_key=None)]
    result = cmd.summarize(rows)
    totals_keys = set(result["totals"])
    for group in result["projects"]:
        assert set(group) - {"project_key"} == totals_keys


def test_summarize_does_not_mutate_its_rows():
    rows = [
        _r(action="nothing", record=None),
        _r(action="dispatch", project_key="shop",
           dispatches=[{"outcome": "completed", "status": "completed"}]),
    ]
    before = copy.deepcopy(rows)
    cmd.summarize(rows)
    assert rows == before


# --- cmd_checkins --summary: CLI-level, through the real emit spine ---------

def test_summary_refuses_last_and_limit(root, capsys):
    rc = cmd.cmd_checkins(_Args(root, summary=True, last=True))
    out, err = capsys.readouterr()
    assert rc == 2 and out == "" and "--last" in err

    rc = cmd.cmd_checkins(_Args(root, summary=True, limit=3))
    out, err = capsys.readouterr()
    assert rc == 2 and out == "" and "--limit" in err


def test_summary_json_and_text_agree_on_the_counts(root, capsys):
    _decision(root, "mgr", CK1, age_h=3, action="dispatch")
    _decision(root, "mgr", CK2, age_h=2, action="ask",
              raise_={"decided": True, "reason": "a fork", "held": []})
    _decision(root, "mgr", CK3, age_h=1, action="nothing")

    assert cmd.cmd_checkins(_Args(root, summary=True, json=True)) == 0
    totals = _out(capsys)["totals"]

    assert cmd.cmd_checkins(_Args(root, summary=True)) == 0
    text = capsys.readouterr().out

    assert f"checkins: {totals['checkins']}" in text
    assert f"raised: {totals['raised']}" in text
    # the k=v pairs stop at the trailing asymmetry clause (the "(" below) --
    # never swallowed into the parsed counts
    m = re.search(r"dispatch_outcomes:\s*([^(\n]+)", text)
    assert m is not None
    parsed = {k: int(v) for k, v in (pair.split("=") for pair in m.group(1).split())}
    assert parsed == totals["dispatch_outcomes"]
    # A2: the asymmetry (unjoined counts BOTH a join row naming an unknown
    # assignment AND a dispatch decision that joined no row) is visible on
    # the same line a reader meets the counts, not just in a docstring
    assert "unjoined also counts dispatch decisions that joined nothing" in text


def test_summary_over_an_empty_window_answers_at_rc_0(root, capsys):
    # a plane that has SEEN the fleet (one identity row) but holds no decision
    # is EMPTY, not unreachable -- the same shape as PR 1's own rc-0 test
    emit_batch(root, [{"event_type": "system", "emitter": "t", "fleet": F,
                       "payload": {"event": "report_status", "subject_kind": "actor",
                                   "subject": f"bot:{F}/w1", "data": {"status": "progress"}}}])
    assert cmd.cmd_checkins(_Args(root, summary=True, json=True)) == 0
    env = _out(capsys)
    assert env["totals"]["checkins"] == 0
    assert env["projects"] == []

    assert cmd.cmd_checkins(_Args(root, summary=True)) == 0
    text = capsys.readouterr().out
    assert "checkins: 0" in text


def test_summary_on_an_unreachable_plane_refuses_at_rc_3(tmp_path, capsys):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert cmd.cmd_checkins(_Args(bare, summary=True)) == 3
    assert "UNREACHABLE" in capsys.readouterr().err
