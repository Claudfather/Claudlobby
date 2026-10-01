# tests/test_checkins_summary.py
"""Canonical check-in summary: the window
rolled up -- actions, ask rate, considered lengths, unavailable frequencies and
dispatch outcomes, grouped by project_key. `summarize(rows)` is a PURE function
of the row dicts `collect_checkins` returns (no db, no clock), so the math is
unit-tested with hand-built rows below; the real-row test drives the same
fixtures `test_checkins_outcome_join.py` imports from `test_checkins_cli.py`
rather than inventing a second seeding path.

This door produces FACTS ONLY -- a count, a distribution -- never a verdict:
no threshold, no "healthy"/"unhealthy" wording anywhere in the rollup."""

from __future__ import annotations

import copy

from claudlobby.commands import checkins as cmd
from tests.test_checkins_cli import _decision, _query, root  # noqa: F401

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
        _r(action="dispatch", dispatches=[{"outcome": "completed", "assignment_state": "closed",
                                           "terminal_event": "completed"}]),
        _r(action="dispatch", dispatches=[{"outcome": "open", "assignment_state": "active",
                                           "terminal_event": None}]),
        _r(action="dispatch", dispatches=[]),          # a dispatch decision with no join row
    ]
    totals = cmd.summarize(rows)["totals"]
    assert totals["dispatch_outcomes"] == {"completed": 1, "blocked": 0, "failed": 0,
                                            "retired": 0, "open": 1, "unknown": 0,
                                            "unjoined": 1}
    assert totals["dispatches"] == 2


def test_a_non_dispatch_decision_with_no_join_row_is_not_unjoined():
    rows = [_r(action="ask"), _r(action="nothing")]
    totals = cmd.summarize(rows)["totals"]
    assert totals["dispatch_outcomes"]["unjoined"] == 0
    assert totals["dispatches"] == 0


def test_assignment_states_and_terminal_events_skip_unjoined_links():
    rows = [
        _r(action="dispatch", dispatches=[
            {"outcome": "completed", "assignment_state": "closed", "terminal_event": "completed"},
            {"outcome": "blocked", "assignment_state": "closed", "terminal_event": "returned_blocked"},
        ]),
        _r(action="dispatch", dispatches=[
            {"outcome": "completed", "assignment_state": "closed", "terminal_event": "completed"},
            {"outcome": "unjoined", "assignment_state": None, "terminal_event": None},
        ]),
    ]
    totals = cmd.summarize(rows)["totals"]
    assert totals["assignment_states"] == {"closed": 3}
    assert totals["terminal_events"] == {"completed": 2, "returned_blocked": 1}


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
           dispatches=[{"outcome": "completed", "assignment_state": "closed",
                        "terminal_event": "completed"}]),
    ]
    before = copy.deepcopy(rows)
    cmd.summarize(rows)
    assert rows == before


# --- shared query and pure rollup stay one owner -------------------------------

def test_window_rollup_counts_real_decision_rows(root):  # noqa: F811 — imported shared pytest fixture
    _decision(root, "mgr", CK1, age_h=3, action="dispatch")
    _decision(root, "mgr", CK2, age_h=2, action="ask",
              raise_={"decided": True, "reason": "a fork", "held": []})
    _decision(root, "mgr", CK3, age_h=1, action="nothing")
    summary = cmd.summarize(_query(root, since="7d"))
    assert summary["totals"]["checkins"] == 3
    assert summary["totals"]["raised"] == 1
    assert summary["totals"]["dispatch_outcomes"]["unjoined"] == 1
    assert cmd.summarize([])["totals"]["checkins"] == 0
