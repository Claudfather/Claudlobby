# tests/test_checkin_contract.py
"""The schema-1 decision record (manager check-in spec §7): lib/checkin-contract.py
(stdlib, the dispatch-overdue.py precedent) and the two severity registrations."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT = REPO_ROOT / "lib" / "checkin-contract.py"
CK = "ck_" + "a" * 32
PREV = "ck_" + "b" * 32

_spec = importlib.util.spec_from_file_location("checkin_contract", CONTRACT)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

INPUTS = {"open_tasks": 2, "stalls": 0, "unacked": 1, "issues_seen": 9, "issues_considered": 4,
          "knowledge_hits": 1, "considered": ["#12 flaky test — not mission work"], "unavailable": []}


def _decision(**over) -> dict:
    d = {
        "prev_checkin_id": None,
        "inputs_seen": dict(INPUTS),
        "delta": {"tasks_opened": 0, "tasks_completed": 1, "stalls_appeared": 0,
                  "stalls_cleared": 0, "issues_new": 1, "messages_new": None, "held_pending": 0},
        "action": "nothing",
        "project_key": None,
        "rationale": "All work in flight; nothing new worth starting.",
        "raise": {"decided": False, "reason": "no delta the operator would want", "held": []},
    }
    d.update(over)
    return d


def test_a_decision_normalizes_with_the_door_supplied_id():
    out = cc.normalize(_decision(), checkin_id=CK)
    assert out["schema"] == 1 and out["checkin_id"] == CK
    assert out["prev_checkin_id"] is None
    assert out["delta"]["messages_new"] is None          # null survives: could not measure
    assert out["delta"]["tasks_completed"] == 1
    assert out["inputs_seen"]["issues_seen"] == 9 and out["inputs_seen"]["issues_considered"] == 4
    assert out["inputs_seen"]["considered"] == ["#12 flaky test — not mission work"]
    assert "targets" not in out and "focus_declared" not in out["inputs_seen"]


def test_the_contract_never_mints():
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(_decision())                         # no id from the door, none in the body
    assert any("checkin_id" in r for r in exc.value.reasons)


def test_prev_is_kept():
    out = cc.normalize(_decision(prev_checkin_id=PREV), checkin_id=CK)
    assert out["prev_checkin_id"] == PREV


def test_prev_checkin_id_is_required_so_a_skipped_read_0_cannot_pose_as_a_first_checkin():
    d = _decision()
    del d["prev_checkin_id"]
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(d, checkin_id=CK)
    assert any("prev_checkin_id required" in r for r in exc.value.reasons)


def test_an_input_count_the_manager_could_not_measure_is_null_never_zero():
    seen = {**INPUTS, "issues_seen": None, "issues_considered": None}
    out = cc.normalize(_decision(inputs_seen=seen), checkin_id=CK)
    assert out["inputs_seen"]["issues_seen"] is None and out["inputs_seen"]["issues_considered"] is None


def test_a_missing_input_count_is_a_defect_not_a_zero():
    seen = {k: v for k, v in INPUTS.items() if k != "stalls"}
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(_decision(inputs_seen=seen), checkin_id=CK)
    assert any("inputs_seen.stalls required" in r for r in exc.value.reasons)


def test_a_missing_delta_count_is_a_defect_not_a_null():
    d = _decision()
    del d["delta"]["held_pending"]
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(d, checkin_id=CK)
    assert any("delta.held_pending required" in r for r in exc.value.reasons)


def test_nothing_with_a_visible_backlog_must_record_its_losers():
    # the nothing rows are the population an inert verdict is diagnosed from (cycle-7 gap)
    d = _decision()                                   # issues_seen 9 and a non-empty considered: fine
    d["inputs_seen"]["considered"] = []
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(d, checkin_id=CK)
    assert any("action nothing with issues_seen > 0" in r for r in exc.value.reasons)
    d["inputs_seen"]["issues_seen"] = 0               # nothing was there to pass over: no losers required
    assert cc.normalize(d, checkin_id=CK)["action"] == "nothing"
    d["inputs_seen"]["issues_seen"] = None            # could not measure: not a positive count
    assert cc.normalize(d, checkin_id=CK)["action"] == "nothing"


def test_a_missing_list_key_is_a_defect_not_an_empty_list():
    # the provenance half of the cited rule (cycle-8): an omitted `unavailable` would
    # normalize into the claim that nothing was unavailable
    for key in ("considered", "unavailable"):
        d = _decision()
        del d["inputs_seen"][key]
        with pytest.raises(cc.ContractError) as exc:
            cc.normalize(d, checkin_id=CK)
        assert any(f"inputs_seen.{key} required" in r for r in exc.value.reasons)
    d = _decision(); d["inputs_seen"]["unavailable"] = []       # present and empty is fine
    assert cc.normalize(d, checkin_id=CK)["inputs_seen"]["unavailable"] == []


def test_raise_decided_true_requires_an_ask():
    # --raised filters on raise.decided; a dispatch row that set it would count as an ask
    d = _decision(action="dispatch", project_key="shop")
    d["raise"] = {"decided": True, "reason": "surfaced", "held": []}
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(d, checkin_id=CK)
    assert any("raise.decided true requires action ask" in r for r in exc.value.reasons)


def test_a_previous_id_with_no_read_door_is_fabricated():
    d = _decision(prev_checkin_id=PREV)
    d["inputs_seen"]["unavailable"] = ["checkins"]
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(d, checkin_id=CK)
    assert any("prev_checkin_id must be null when checkins is unavailable" in r for r in exc.value.reasons)
    d["prev_checkin_id"] = None
    assert cc.normalize(d, checkin_id=CK)["prev_checkin_id"] is None


def test_a_filtered_count_with_no_mission_is_fabricated():
    d = _decision()
    d["inputs_seen"]["unavailable"] = ["mission"]
    with pytest.raises(cc.ContractError) as exc:                # issues_considered is 4 in the fixture
        cc.normalize(d, checkin_id=CK)
    assert any("issues_considered must be null when mission is unavailable" in r for r in exc.value.reasons)
    d["inputs_seen"]["issues_considered"] = None
    assert cc.normalize(d, checkin_id=CK)["inputs_seen"]["issues_considered"] is None


def test_dispatch_must_record_its_losers():
    d = _decision(action="dispatch", project_key="shop")
    d["inputs_seen"]["considered"] = []
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(d, checkin_id=CK)
    assert any("inputs_seen.considered non-empty" in r for r in exc.value.reasons)
    d["inputs_seen"]["considered"] = ["#12 flaky test — not mission work"]
    assert cc.normalize(d, checkin_id=CK)["action"] == "dispatch"


@pytest.mark.parametrize("over, needle", [
    ({"action": "dispatch"}, "must name project_key"),
    ({"action": "ask"}, "raise.decided"),
    ({"action": "propose"}, "action must be one of"),        # schema 2, not this chunk
    ({"action": "coffee"}, "action must be one of"),
    ({"rationale": "x" * 601}, "rationale must be <= 600"),
    ({"rationale": ""}, "rationale"),
    ({"project_key": "Not-A-Slug"}, "project_key"),
    ({"prev_checkin_id": "nope"}, "prev_checkin_id"),
    ({"inputs_seen": {**INPUTS, "open_tasks": -1}}, "inputs_seen.open_tasks"),
    ({"inputs_seen": {**INPUTS, "considered": ["x"] * 11}}, "inputs_seen.considered"),
    ({"inputs_seen": {**INPUTS, "considered": ["x" * 201]}}, "inputs_seen.considered"),
    ({"delta": {"tasks_opened": -1}}, "delta.tasks_opened"),
    ({"raise": {"decided": False, "reason": ""}}, "raise.reason"),
    ({"raise": {"decided": False, "reason": "r" * 601}}, "raise.reason must be <= 600"),
    ({"raise": {"decided": "yes", "reason": "r"}}, "raise.decided"),
    ({"raise": {"decided": False, "reason": "r", "held": ["h"] * 11}}, "raise.held"),
    ({"inputs_seen": {**INPUTS, "unavailable": None}}, "inputs_seen.unavailable must be a list"),
    ({"inputs_seen": {**INPUTS, "unavailable": 5}}, "inputs_seen.unavailable must be a list"),
])
def test_defects_are_listed_by_name(over, needle):
    # a non-list unavailable (None, 5, ...) must raise ContractError -- pytest.raises(cc.ContractError)
    # itself fails the test if a TypeError escapes instead, which is the pre-fix behaviour
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(_decision(**over), checkin_id=CK)
    assert any(needle in r for r in exc.value.reasons), exc.value.reasons


def test_a_non_list_considered_reports_only_the_type_defect_not_a_spurious_non_empty_one():
    # considered: None must not ALSO trip "needs considered non-empty" -- that message
    # implies the fix is filling the list, when the real defect is the type. The default
    # fixture (action "nothing", issues_seen=9 > 0) is exactly the branch that pre-fix
    # paired the type defect with the spurious one.
    d = _decision()
    d["inputs_seen"]["considered"] = None
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(d, checkin_id=CK)
    assert any("inputs_seen.considered must be a list" in r for r in exc.value.reasons), exc.value.reasons
    assert not any("non-empty" in r for r in exc.value.reasons), exc.value.reasons


def test_every_defect_is_reported_not_just_the_first():
    with pytest.raises(cc.ContractError) as exc:
        cc.normalize(_decision(action="coffee", rationale=""), checkin_id=CK)
    assert len(exc.value.reasons) >= 2


def test_the_cli_is_a_filter():
    ok = subprocess.run([sys.executable, str(CONTRACT), "--checkin-id", CK],
                        input=json.dumps(_decision()), capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["checkin_id"] == CK
    bad = subprocess.run([sys.executable, str(CONTRACT), "--checkin-id", CK], input="not json",
                         capture_output=True, text=True)
    assert bad.returncode == 2 and "not JSON" in bad.stderr
    bad = subprocess.run([sys.executable, str(CONTRACT), "--checkin-id", CK],
                         input=json.dumps(_decision(action="coffee")), capture_output=True, text=True)
    assert bad.returncode == 2 and "checkin-contract: action must be one of" in bad.stderr and bad.stdout == ""
    bad = subprocess.run([sys.executable, str(CONTRACT), "--checkin-id", "nope"],
                         input=json.dumps(_decision()), capture_output=True, text=True)
    assert bad.returncode == 2 and "checkin_id" in bad.stderr


@pytest.mark.parametrize("kind", ["checkin_decision", "checkin_dispatch"])
def test_the_two_kinds_carry_notice_severity(tmp_path, kind):
    emit_batch(tmp_path, [{
        "event_type": "system", "emitter": "t", "fleet": "f",
        "payload": {"event": kind, "subject_kind": "actor", "subject": "bot:f/mgr", "data": {"schema": 1}}}])
    conn = connect(db_path(tmp_path))
    row = conn.execute("SELECT severity FROM events WHERE kind='system' AND event=?", (kind,)).fetchone()
    conn.close()
    assert row["severity"] == "notice"
