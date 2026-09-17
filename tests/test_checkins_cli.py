# tests/test_checkins_cli.py
"""`claudlobby checkins` — the read door (spec §11), minimal form. Seeded through
the real emit spine. The plane session's connection yields tuples (status.py:218);
the door reads named rows through open_ro."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudlobby.commands import checkins as cmd
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import plane_root

REPO_ROOT = Path(__file__).resolve().parent.parent
F = "ck-fleet"
CK1, CK2, CK3 = ("ck_" + c * 32 for c in "abc")


@pytest.fixture(autouse=True)
def root(tmp_path: Path) -> Path:
    r = plane_root(tmp_path)
    (r / "lib").mkdir()
    for name in ("dispatch-overdue.py", "plane-readers.py", "plane-lookup.py"):
        shutil.copy(REPO_ROOT / "lib" / name, r / "lib" / name)
    return r


class _Args:
    def __init__(self, root, **kw):
        self.root, self.fleet, self.seed = str(root), None, False
        self.checkins_fleet = kw.get("fleet", F)
        self.bot = kw.get("bot")
        self.since = kw.get("since", "7d")
        self.last = kw.get("last", False)
        self.raised = kw.get("raised", False)
        self.json = kw.get("json", False)


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _record(ck: str, **over) -> dict:
    d = {"schema": 1, "checkin_id": ck, "prev_checkin_id": None,
         "inputs_seen": {"open_tasks": 0, "considered": [], "unavailable": []}, "delta": {},
         "action": "nothing", "project_key": None, "rationale": f"r-{ck[-4:]}",
         "raise": {"decided": False, "reason": "quiet", "held": []}}
    if "raise_" in over:                      # `raise` is a keyword: the tests pass it as raise_
        d["raise"] = over.pop("raise_")
    d.update(over)
    return d


def _decision(root, bot: str, ck: str, *, age_h: float, **over):
    emit_batch(root, [{
        "event_type": "system", "emitter": "checkin-record", "fleet": F,
        "source_ref": f"checkin:{ck}", "occurred_at": _ago(hours=age_h),
        "payload": {"event": "checkin_decision", "subject_kind": "actor",
                    "subject": f"bot:{F}/{bot}", "data": _record(ck, **over)}}])


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_rows_are_newest_first_by_occurred_at_scoped_to_fleet_and_bot(root, capsys):
    _decision(root, "mgr", CK1, age_h=5)          # arrival order CK1, CK2, CK3;
    _decision(root, "mgr", CK2, age_h=1, prev_checkin_id=CK1, action="dispatch", project_key="shop")
    _decision(root, "other", CK3, age_h=2)        # occurred order CK2 (1h), CK3 (2h), CK1 (5h)
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = _out(capsys)["checkins"]
    assert [r["checkin_id"] for r in rows] == [CK2, CK3, CK1]
    assert rows[0]["prev_checkin_id"] == CK1 and rows[0]["action"] == "dispatch" and rows[0]["bot"] == "mgr"
    assert rows[0]["raise"] == {"decided": False, "reason": "quiet"}
    assert cmd.cmd_checkins(_Args(root, bot="mgr", json=True)) == 0
    assert [r["checkin_id"] for r in _out(capsys)["checkins"]] == [CK2, CK1]


def test_last_ignores_the_window(root, capsys):
    _decision(root, "mgr", CK1, age_h=24 * 30)    # a manager idle for a month
    assert cmd.cmd_checkins(_Args(root, bot="mgr", last=True, json=True)) == 0
    assert [r["checkin_id"] for r in _out(capsys)["checkins"]] == [CK1]   # the chain is not broken by --since


def test_since_window(root, capsys):
    _decision(root, "mgr", CK1, age_h=30)
    _decision(root, "mgr", CK2, age_h=1)
    _decision(root, "mgr", CK3, age_h=2)
    assert cmd.cmd_checkins(_Args(root, since="24h", json=True)) == 0
    assert [r["checkin_id"] for r in _out(capsys)["checkins"]] == [CK2, CK3]
    assert cmd.cmd_checkins(_Args(root, since="yesterday")) == 2          # usage: the door ladder's code


def test_raised_filters_to_the_asks(root, capsys):
    _decision(root, "mgr", CK1, age_h=1)
    _decision(root, "mgr", CK2, age_h=2, action="ask", raise_={"decided": True, "reason": "a fork", "held": []})
    _decision(root, "mgr", CK3, age_h=3, action="ask", raise_={"decided": True, "reason": "another", "held": []})
    assert cmd.cmd_checkins(_Args(root, raised=True, json=True)) == 0
    assert [r["checkin_id"] for r in _out(capsys)["checkins"]] == [CK2, CK3]      # the ask count READ 0 takes


def test_a_truncated_record_is_listed_and_marked_never_dropped(root, capsys):
    _decision(root, "mgr", CK1, age_h=1, rationale="x" * 20000)       # over the 16 KiB DIAGNOSTIC cap
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = _out(capsys)["checkins"]
    assert len(rows) == 1 and rows[0]["truncated"] is True and rows[0]["checkin_id"] is None


def test_a_row_without_a_record_is_listed_not_a_traceback(root, capsys):
    # contracts.py:418 accepts a system event with no data; the reader must not raise
    emit_batch(root, [{"event_type": "system", "emitter": "t", "fleet": F, "source_ref": f"checkin:{CK1}",
                       "payload": {"event": "checkin_decision", "subject_kind": "actor", "subject": f"bot:{F}/mgr"}}])
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    rows = _out(capsys)["checkins"]
    assert len(rows) == 1 and rows[0]["record"] is None and rows[0]["checkin_id"] is None and rows[0]["truncated"] is False
    assert cmd.cmd_checkins(_Args(root)) == 0
    assert "carries no record" in capsys.readouterr().out


def test_the_text_listing_shows_the_losers_and_the_unavailable_inputs(root, capsys):
    _decision(root, "mgr", CK1, age_h=1,
              inputs_seen={"open_tasks": 1, "considered": ["#7 docs — not mission work"], "unavailable": ["gh"]})
    assert cmd.cmd_checkins(_Args(root)) == 0
    out = capsys.readouterr().out
    assert "passed over: #7 docs — not mission work" in out and "unavailable: gh" in out


def test_the_text_listing_caps_at_ten_rows_and_says_so(root, capsys):
    for i in range(12):
        _decision(root, "mgr", "ck_" + f"{i:032x}", age_h=i + 1)
    assert cmd.cmd_checkins(_Args(root)) == 0
    out = capsys.readouterr().out
    assert "showing the newest 10 of 12" in out and out.count("surfacing:") == 10
    assert cmd.cmd_checkins(_Args(root, json=True)) == 0
    assert len(_out(capsys)["checkins"]) == 12                            # --json is never capped


def test_an_empty_fleet_plane_answers_no_checkins_at_rc_0(root, capsys):
    # a plane that has SEEN the fleet (one identity row — the roster the session
    # opens on) but holds no decision is EMPTY, not unreachable
    emit_batch(root, [{"event_type": "system", "emitter": "t", "fleet": F,
                       "payload": {"event": "report_status", "subject_kind": "actor",
                                   "subject": f"bot:{F}/w1", "data": {"status": "progress"}}}])
    assert cmd.cmd_checkins(_Args(root)) == 0
    out = capsys.readouterr().out
    assert F in out and "no check-ins" in out


def test_a_fleet_the_plane_has_never_seen_refuses_at_rc_3(root, capsys):
    # plane_session's roster rule (#1014's class), inherited on purpose: a typo'd
    # --fleet must not read as "no rows" (cycle-3 question 2)
    _decision(root, "mgr", CK1, age_h=1)
    assert cmd.cmd_checkins(_Args(root, fleet="never-seen")) == 3
    assert "UNREACHABLE" in capsys.readouterr().err


def test_unreachable_plane_refuses_at_rc_3(tmp_path, capsys):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert cmd.cmd_checkins(_Args(bare)) == 3
    assert "UNREACHABLE" in capsys.readouterr().err       # refuse_unreachable's own token, upper-case
