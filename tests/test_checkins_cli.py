"""Historical check-in query semantics retained under the canonical reader."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudlobby.commands import checkins as cmd
from claudlobby.plane.db import open_ro
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.queries import checkin_rows_sql
from tests.plane_fixtures import plane_root
from tests.plane_setup import initialize_plane

F = "ck-fleet"
CK1, CK2, CK3 = ("ck_" + c * 32 for c in "abc")


@pytest.fixture(autouse=True)
def root(tmp_path: Path) -> Path:
    return plane_root(tmp_path)


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _record(ck: str, **over) -> dict:
    value = {"schema": 1, "checkin_id": ck, "prev_checkin_id": None,
             "inputs_seen": {"open_tasks": 0, "considered": [], "unavailable": []}, "delta": {},
             "action": "nothing", "project_key": None, "rationale": f"r-{ck[-4:]}",
             "raise": {"decided": False, "reason": "quiet", "held": []}}
    if "raise_" in over:
        value["raise"] = over.pop("raise_")
    value.update(over)
    return value


def _decision(root, bot: str, ck: str, *, age_h: float, **over):
    initialize_plane(root)
    emit_batch(root, [{
        "event_type": "system", "emitter": "checkin-record", "fleet": F,
        "source_ref": f"checkin:{ck}", "occurred_at": _ago(hours=age_h),
        "payload": {"event": "checkin_decision", "subject_kind": "actor",
                    "subject": f"bot:{F}/{bot}", "data": _record(ck, **over)}}])


def _query(root, *, since: str | None = None, bot: str | None = None,
           last: bool = False, raised: bool = False, limit: int | None = None,
           checkin_id: str | None = None, fleet: str = F) -> list[dict]:
    conn, reason = open_ro(root)
    assert conn is not None, reason
    with closing(conn):
        return cmd.collect_checkins(conn, fleet,
                                    since=cmd._since(since) if since is not None and not last else None,
                                    bot=bot, last=last, raised=raised, limit=limit,
                                    checkin_id=checkin_id)


def test_occurred_order_bot_filter_last_and_raised_remain_distinct(root):
    _decision(root, "mgr", CK1, age_h=24 * 30)
    _decision(root, "mgr", CK2, age_h=1, action="ask",
              raise_={"decided": True, "reason": "a fork", "held": []})
    _decision(root, "other", CK3, age_h=2)
    assert [r["checkin_id"] for r in _query(root)] == [CK2, CK3, CK1]
    assert [r["checkin_id"] for r in _query(root, bot="mgr", since="7d")] == [CK2]
    assert [r["checkin_id"] for r in _query(root, bot="mgr", last=True, raised=True)] == [CK2]
    assert [r["checkin_id"] for r in _query(root, bot="mgr", last=True)] == [CK2]
    assert [r["checkin_id"] for r in _query(root, bot="mgr2")] == []
    assert [r["checkin_id"] for r in _query(root, checkin_id=CK3)] == [CK3]
    assert _query(root, checkin_id=CK2, fleet="other-fleet") == []


def test_truncated_and_missing_decision_data_stay_visible_but_unknown(root):
    _decision(root, "mgr", CK1, age_h=2, rationale="x" * 20_000)
    emit_batch(root, [{"event_type": "system", "emitter": "t", "fleet": F,
                       "source_ref": f"checkin:{CK2}", "occurred_at": _ago(hours=1),
                       "payload": {"event": "checkin_decision", "subject_kind": "actor",
                                   "subject": f"bot:{F}/mgr"}}])
    rows = _query(root)
    assert len(rows) == 2
    assert rows[0]["record"] is None and rows[0]["truncated"] is False
    assert rows[1]["record"] is None and rows[1]["truncated"] is True
    assert cmd.summarize(rows)["totals"]["no_record"] == 2


def test_sql_bounds_limit_after_raised_filter_and_timezone_instant(root):
    tz = timezone(timedelta(hours=-4))
    recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).astimezone(tz).isoformat()
    initialize_plane(root)
    emit_batch(root, [{"event_type": "system", "emitter": "checkin-record", "fleet": F,
                       "source_ref": f"checkin:{CK1}", "occurred_at": recent,
                       "payload": {"event": "checkin_decision", "subject_kind": "actor",
                                   "subject": f"bot:{F}/mgr", "data": _record(CK1)}}])
    _decision(root, "mgr", CK2, age_h=2, action="ask",
              raise_={"decided": True, "reason": "first", "held": []})
    _decision(root, "mgr", CK3, age_h=3, action="ask",
              raise_={"decided": True, "reason": "second", "held": []})
    assert [r["checkin_id"] for r in _query(root, since="1h")] == [CK1]
    assert [r["checkin_id"] for r in _query(root, raised=True, limit=1)] == [CK2]
    assert [r["checkin_id"] for r in _query(root, limit=1)] == [CK1]
    assert "strftime('%s'" in checkin_rows_sql(since=True)
    assert "e.subject_alias = ?" in checkin_rows_sql(bot=True)
    assert checkin_rows_sql().count("?") == 5
