"""Phase-6 surfaces over data already flowing: the org chart (a pure read
of the fleet keyframe's org_edges) and utilization (the legacy busy-%
math over the plane's heartbeat series — one definition)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.orgchart import org_tree
from claudlobby.plane.utilization import bot_utilization

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
F = "f"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    return root


def _fleet_kf(edges, manager="erlich", roster=None):
    bots = sorted({e["bot"] for e in edges if isinstance(e.get("bot"), str)})
    return {"event_type": "registry_snapshot", "emitter": "t", "fleet": F,
            "payload": {"entity_type": "fleet", "entity_alias": F,
                        "cause": "generate", "scan_id": "s1",
                        "payload": {"alias": F, "service_prefix": "com.t",
                                    "manager": manager, "mission": None,
                                    "mission_file": None,
                                    "groups": [{"name": "eng", "manager": manager,
                                                "members": bots, "mission": None}],
                                    "org_edges": edges, "roster": roster or bots,
                                    # the live keyframe's 4-key shape (contract-required)
                                    "defaults_summary": {"model": "opus", "effort": None,
                                                         "account": "default",
                                                         "list_tier_hashes": {}},
                                    "env_keys": [],
                                    "jobs": [], "plugins_additional": [],
                                    "vault_binding": {}, "telegram": {},
                                    "declared_hash": "d", "schema_version": "1"}}}


def _done():
    return {"event_type": "declaration", "emitter": "t", "fleet": F,
            "payload": {"event": "scan_completed", "subject_kind": "host",
                        "subject": "h1", "scan_id": "s1", "scope": F,
                        "counts": {}, "complete": True}}


def test_org_tree_folds_edges_into_a_reporting_tree(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_fleet_kf([
        {"bot": "palpatine", "reports_to": None},
        {"bot": "erlich", "reports_to": "palpatine"},
        {"bot": "dinesh", "reports_to": "erlich"},
        {"bot": "gilfoyle", "reports_to": "erlich"}]), _done()])
    conn = connect(db_path(root))
    try:
        t = org_tree(conn, F)
    finally:
        conn.close()
    assert t["manager"] == "erlich" and t["bots"] == 4 and t["cycles"] == []
    assert [r["bot"] for r in t["roots"]] == ["palpatine"]
    erlich = t["roots"][0]["reports"][0]
    assert erlich["bot"] == "erlich"
    assert [c["bot"] for c in erlich["reports"]] == ["dinesh", "gilfoyle"]


def test_a_reporting_cycle_is_cut_and_disclosed_never_recursed(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_fleet_kf([
        {"bot": "a", "reports_to": "b"}, {"bot": "b", "reports_to": "a"},
        {"bot": "c", "reports_to": None}]), _done()])
    conn = connect(db_path(root))
    try:
        t = org_tree(conn, F)          # must terminate
    finally:
        conn.close()
    assert "c" in [r["bot"] for r in t["roots"]]
    assert t["cycles"]                 # a and b surfaced, not hidden


def test_no_fleet_keyframe_is_none(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [{"event_type": "metric_sample", "emitter": "t", "fleet": F,
                       "payload": {"subject_kind": "host", "subject": "h",
                                   "metric": "host.job_ran", "value": 1}}])
    conn = connect(db_path(root))
    try:
        assert org_tree(conn, F) is None
    finally:
        conn.close()


def _hb(root, bot, states, start, step_s=60):
    evs = []
    for i, st in enumerate(states):
        evs.append({"event_type": "metric_sample", "emitter": "keepalive", "fleet": F,
                    "occurred_at": (start + timedelta(seconds=i * step_s)).isoformat(),
                    "payload": {"subject_kind": "bot_instance",
                                "subject": f"bot:{F}/{bot}", "metric": "bot.heartbeat",
                                "value": {"state": st}}})
    emit_batch(root, evs)


def test_utilization_is_the_legacy_math_over_the_plane_series(tmp_path):
    root = _root(tmp_path)
    # the legacy math extends the LAST sample's state to `now` (one
    # definition — we inherit that semantic), so the series ends AT now:
    # 3 BUSY intervals + 1 IDLE interval, and a zero-length tail
    start = NOW - timedelta(minutes=4)
    _hb(root, "erlich", ["BUSY", "BUSY", "BUSY", "IDLE", "IDLE"], start)
    _hb(root, "dinesh", ["IDLE", "IDLE", "IDLE"], NOW - timedelta(minutes=2))
    conn = connect(db_path(root))
    try:
        u = {r["short"]: r for r in bot_utilization(conn, now=NOW, fleet=F)}
    finally:
        conn.close()
    assert u["erlich"]["busy_pct_24h"] == 75.0        # 3 busy min / 4 measured
    assert u["erlich"]["last_state"] == "IDLE" and u["erlich"]["idle_since"]
    # an always-idle bot has been idle since its FIRST sample (the legacy
    # definition: the start of the current idle run), never None
    assert u["dinesh"]["busy_pct_24h"] == 0.0
    assert u["dinesh"]["idle_since"] == (NOW - timedelta(minutes=2)).isoformat()
    assert u["erlich"]["samples"] == 5


def test_utilization_skips_unreadable_samples_and_scopes_by_fleet(tmp_path):
    root = _root(tmp_path)
    start = NOW - timedelta(minutes=5)
    _hb(root, "erlich", ["BUSY", "IDLE"], start)
    emit_batch(root, [{"event_type": "metric_sample", "emitter": "x", "fleet": F,
                       "payload": {"subject_kind": "bot_instance",
                                   "subject": f"bot:{F}/erlich",
                                   "metric": "bot.heartbeat", "value": 42}}])   # poison
    # an explicit in-window stamp: without one, ingest stamps the REAL wall
    # clock, which is hours past the frozen NOW and is (correctly) dropped
    # as a future sample
    other = {"event_type": "metric_sample", "emitter": "keepalive", "fleet": "g",
             "occurred_at": (NOW - timedelta(minutes=1)).isoformat(),
             "payload": {"subject_kind": "bot_instance", "subject": "bot:g/twin",
                         "metric": "bot.heartbeat", "value": {"state": "BUSY"}}}
    emit_batch(root, [other])
    conn = connect(db_path(root))
    try:
        rows = bot_utilization(conn, now=NOW, fleet=F)
        allf = bot_utilization(conn, now=NOW)
    finally:
        conn.close()
    assert [r["short"] for r in rows] == ["erlich"]
    assert rows[0]["samples"] == 2                     # poison skipped, not counted
    assert sorted(r["alias"] for r in allf) == [f"bot:{F}/erlich", "bot:g/twin"]


def test_endpoints_and_typed_absence(tmp_path):
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app

    root = _root(tmp_path)
    emit_batch(root, [_fleet_kf([{"bot": "erlich", "reports_to": None}]), _done()])
    _hb(root, "erlich", ["BUSY", "IDLE"], NOW - timedelta(minutes=3))
    c = TestClient(create_app(root))
    org = c.get("/api/org", params={"fleet": F}).json()
    assert org["state"] == "ok" and org["data"]["roots"][0]["bot"] == "erlich"
    util = c.get("/api/utilization", params={"fleet": F}).json()
    assert util["state"] == "ok" and util["data"][0]["short"] == "erlich"
    miss = c.get("/api/org", params={"fleet": "nope"}).json()
    assert miss["state"] == "idle"          # never another fleet's tree under this name
    empty = TestClient(create_app(tmp_path / "none")).get("/api/org").json()
    assert empty["state"] == "absent" and "data" not in empty


def test_future_and_out_of_order_samples_never_produce_impossible_percentages(tmp_path):
    """Self-probe (the lens was rate-limited): a future-stamped sample gave
    -50% and ingest-ordered samples gave 300%. Time order + a negative
    clamp at the one definition + dropping future stamps."""
    root = _root(tmp_path)
    def hb(bot, at, st):
        return {"event_type": "metric_sample", "emitter": "k", "fleet": F,
                "occurred_at": at.isoformat(),
                "payload": {"subject_kind": "bot_instance", "subject": f"bot:{F}/{bot}",
                            "metric": "bot.heartbeat", "value": {"state": st}}}
    # out of order: IDLE@-1m ingested before BUSY@-3m
    emit_batch(root, [hb("a", NOW - timedelta(minutes=1), "IDLE")])
    emit_batch(root, [hb("a", NOW - timedelta(minutes=3), "BUSY")])
    # future: BUSY@-2m then IDLE@+30m (RTC skew)
    emit_batch(root, [hb("b", NOW - timedelta(minutes=2), "BUSY"),
                      hb("b", NOW + timedelta(minutes=30), "IDLE")])
    conn = connect(db_path(root))
    try:
        u = {r["short"]: r for r in bot_utilization(conn, now=NOW, fleet=F)}
    finally:
        conn.close()
    assert u["a"]["busy_pct_24h"] == round(100 * 120 / (120 + 60), 1)   # 2m busy, 1m idle
    assert 0.0 <= u["b"]["busy_pct_24h"] <= 100.0
    assert u["b"]["samples"] == 1                                       # future dropped


def test_org_tree_keeps_every_roster_bot_and_lists_a_duplicate_edge_once(tmp_path):
    """Self-probe: a roster bot with no edge VANISHED from the chart, a
    duplicate edge rendered its bot twice, and `bots` counted edges."""
    root = _root(tmp_path)
    emit_batch(root, [_fleet_kf([{"bot": "a", "reports_to": None},
                                 {"bot": "b", "reports_to": "a"},
                                 {"bot": "b", "reports_to": "a"}],
                                roster=["a", "b", "c"]), _done()])
    conn = connect(db_path(root))
    try:
        t = org_tree(conn, F)
    finally:
        conn.close()
    assert [r["bot"] for r in t["roots"]] == ["a", "c"]          # c present as a root
    assert [x["bot"] for x in t["roots"][0]["reports"]] == ["b"]  # once
    assert t["bots"] == 3
    assert t["cycles"] == []       # c is a legitimate root, NOT a disclosed cycle
    assert not any(r.get("cycle") for r in t["roots"])


def test_org_fleet_none_is_deterministic_and_disclosed(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_fleet_kf([{"bot": "x", "reports_to": None}]), _done()])
    g = _fleet_kf([{"bot": "y", "reports_to": None}])
    g["fleet"] = "g"; g["payload"]["entity_alias"] = "g"; g["payload"]["payload"]["alias"] = "g"
    d = _done(); d["fleet"] = "g"; d["payload"]["scope"] = "g"
    emit_batch(root, [g, d])
    conn = connect(db_path(root))
    try:
        t = org_tree(conn, None)
        assert t["fleet"] == "f" and t["available"] == ["f", "g"]   # sorted-first, disclosed
        assert org_tree(conn, "nope") is None                        # typed absent, not g's tree
    finally:
        conn.close()


def test_malformed_org_edges_are_skipped_and_disclosed_never_a_500(tmp_path):
    """Independent lens (sev-3): org_edges is list[dict] at the contract but
    the INNER shape is untyped, so a dict reports_to or an int bot reached
    the reader and threw — HTTP 500 instead of the typed envelope every
    other bad input gets. Skip + disclose; the good edges still build."""
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app
    root = _root(tmp_path)
    emit_batch(root, [_fleet_kf([
        {"bot": "erlich", "reports_to": None},
        {"bot": "dinesh", "reports_to": {"oops": 1}},     # dict reports_to
        {"bot": 42, "reports_to": "erlich"},              # int bot
        {"bot": "gilfoyle", "reports_to": "erlich"}],
        roster=["erlich", "dinesh", "gilfoyle"]), _done()])
    body = TestClient(create_app(root), raise_server_exceptions=False).get(
        "/api/org", params={"fleet": F}).json()
    assert body["state"] == "ok"
    d = body["data"]
    assert d["malformed_edges"] == 2
    assert [r["bot"] for r in d["roots"]] == ["dinesh", "erlich"]   # dinesh: no valid edge -> root
    assert [x["bot"] for x in d["roots"][1]["reports"]] == ["gilfoyle"]


def test_idle_since_is_the_first_idle_of_the_run(tmp_path):
    """Independent lens: idle_since diverged from the legacy definition
    (first IDLE of the current run vs last BUSY). One definition now."""
    root = _root(tmp_path)
    start = NOW - timedelta(minutes=3)
    _hb(root, "erlich", ["BUSY", "IDLE", "IDLE", "IDLE"], start)
    conn = connect(db_path(root))
    try:
        u = bot_utilization(conn, now=NOW, fleet=F)[0]
    finally:
        conn.close()
    assert u["idle_since"] == (start + timedelta(minutes=1)).isoformat()


def test_sql_fleet_scope_escapes_like_wildcards(tmp_path):
    """The fleet filter is a LIKE in SQL (perf): a fleet named with `_`
    must not match a sibling differing at that character."""
    root = _root(tmp_path)
    for fl in ("f_x", "fxx"):
        emit_batch(root, [{"event_type": "metric_sample", "emitter": "k", "fleet": fl,
                           "occurred_at": (NOW - timedelta(minutes=1)).isoformat(),
                           "payload": {"subject_kind": "bot_instance",
                                       "subject": f"bot:{fl}/b", "metric": "bot.heartbeat",
                                       "value": {"state": "BUSY"}}}])
    conn = connect(db_path(root))
    try:
        assert [r["alias"] for r in bot_utilization(conn, now=NOW, fleet="f_x")] == ["bot:f_x/b"]
    finally:
        conn.close()
