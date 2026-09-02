"""Presence derivation (chunk 2) — the whole verdict table, unit-level.

derive_presence is a PURE function of (heartbeats, live panes, now), so
every cell of the working/idle/down/stale/unknown/sampling table is pinned
without a db or tmux. The load-bearing laws (spec §9b, #1361): the live
poll decides LIVENESS (down wins over any record), the recorded half
decides ACTIVITY but only while FRESH, and no-evidence is never
evidence-of-down. Plus one endpoint pin over the real query + a stubbed
sampler, so the wiring is proven, not just the kernel.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.plane.presence import (
    STALE_AFTER_S, derive_presence, presence_counts,
)

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def _hb(alias, state, *, age_s=10, marker=5):
    ingested = (NOW - timedelta(seconds=age_s)).isoformat()
    val = {"state": state}
    if marker is not None:
        val["marker_age_s"] = marker
    return {"alias": alias, "value": json.dumps(val), "ingested_at": ingested}


def _pane(fleet, bot, status):
    return {"fleet": fleet, "bot": bot, "status": status}


def _one(heartbeats, panes):
    rows = derive_presence(heartbeats, panes, now=NOW)
    assert len(rows) == 1
    return rows[0]


BOT = "bot:f/erlich"


def test_live_up_plus_busy_heartbeat_is_working():
    r = _one([_hb(BOT, "BUSY")], [_pane("f", "erlich", "up")])
    assert r.presence == "working"
    assert r.marker_age_s == 5
    assert r.live == "up"


def test_live_up_plus_idle_heartbeat_is_idle():
    assert _one([_hb(BOT, "IDLE")], [_pane("f", "erlich", "up")]).presence \
        == "idle"


def test_dead_session_is_down_even_with_a_fresh_busy_heartbeat():
    """The live poll wins on liveness: a session that just died is down
    though the last heartbeat, seconds old, said BUSY. This is #1361's
    exact failure — trusting a recorded signal past its truth."""
    r = _one([_hb(BOT, "BUSY", age_s=2)], [_pane("f", "erlich", "down")])
    assert r.presence == "down"
    assert r.marker_age_s is None    # a down bot has no live activity age


def test_a_quiet_recording_is_stale_not_its_last_guess():
    """Up per the live poll, but the recorded half went dark past the
    horizon — render stale, never the stale BUSY/IDLE verdict."""
    r = _one([_hb(BOT, "BUSY", age_s=STALE_AFTER_S + 60)],
             [_pane("f", "erlich", "up")])
    assert r.presence == "stale"
    assert r.heartbeat_age_s > STALE_AFTER_S
    assert r.marker_age_s is None    # a stale marker is as quiet as its hb


def test_up_with_no_heartbeat_is_unknown_never_down():
    """No-evidence is not evidence-of-down (source_state #1216): a live-up
    bot that has never recorded is unknown, not down."""
    assert _one([], [_pane("f", "erlich", "up")]).presence == "unknown"


def test_sampling_pane_with_no_record_is_sampling():
    assert _one([], [_pane("f", "erlich", "sampling")]).presence == "sampling"


def test_up_with_unknown_heartbeat_is_unknown():
    assert _one([_hb(BOT, "UNKNOWN")], [_pane("f", "erlich", "up")]).presence \
        == "unknown"


def test_recorded_but_not_yet_sampled_is_judged_on_the_record():
    """A bot that recorded a heartbeat but the sampler has not discovered:
    its live status defaults to sampling (no-evidence), so liveness cannot
    say down — the fresh record classifies it."""
    r = _one([_hb(BOT, "IDLE")], [])         # no pane at all
    assert r.presence == "idle"
    assert r.live == "sampling"


def test_the_union_of_both_key_sets_is_covered():
    rows = derive_presence(
        [_hb("bot:f/a", "BUSY"), _hb("bot:f/b", "IDLE")],
        [_pane("f", "b", "up"), _pane("f", "c", "down")],
        now=NOW)
    by = {r.alias: r.presence for r in rows}
    # a: recorded BUSY, never sampled -> working on the record alone
    assert by["bot:f/a"] == "working"
    # b: recorded IDLE + live up -> idle
    assert by["bot:f/b"] == "idle"
    # c: live down, never recorded -> down (liveness needs no record)
    assert by["bot:f/c"] == "down"


def test_counts_carry_every_state_key_even_at_zero():
    rows = derive_presence([_hb(BOT, "BUSY")], [_pane("f", "erlich", "up")],
                           now=NOW)
    counts = presence_counts(rows)
    assert counts["working"] == 1
    assert counts["down"] == 0        # present as a fact, not omitted
    assert set(counts) >= {"working", "idle", "down", "stale",
                           "unknown", "sampling"}


def test_a_future_ingest_clock_is_treated_as_fresh_not_negative():
    """RTC-skew guard: a heartbeat 'ingested' in the future (age < 0) is
    fresh, never stale — the reader must not mis-type a skewed clock."""
    fut = {"alias": BOT, "value": json.dumps({"state": "IDLE"}),
           "ingested_at": (NOW + timedelta(seconds=30)).isoformat()}
    assert _one([fut], [_pane("f", "erlich", "up")]).presence == "idle"


# --- endpoint wiring (real query + stubbed sampler) ------------------------

def test_presence_endpoint_joins_both_halves(tmp_path):
    from claudlobby.plane.emit_api import emit_batch
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app

    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    # a registry keyframe (so the heartbeat's subject resolves to an alias)
    # + a heartbeat sample for the same instance
    emit_batch(root, [
        {"event_type": "registry_snapshot", "emitter": "t", "fleet": "f",
         "payload": {"entity_type": "bot", "entity_alias": BOT,
                     "cause": "generate", "scan_id": "s1",
                     "payload": {"alias": BOT, "account": "a", "service": "s",
                                 "model": "opus",
                                 "posture": {"permissions_mode": "plan"},
                                 "composed_hashes": {}, "declared_hash": "d",
                                 "schema_version": "1"}}},
        {"event_type": "metric_sample", "emitter": "keepalive", "fleet": "f",
         "payload": {"subject_kind": "bot_instance", "subject": BOT,
                     "metric": "bot.heartbeat",
                     "value": {"state": "BUSY", "marker_age_s": 3}}}])

    class _Sampler:
        available = True

        def snapshot(self):
            return {"panes": [_pane("f", "erlich", "up")],
                    "sampler_running": True}

        def start(self):
            pass

        async def stop(self):
            pass

    client = TestClient(create_app(root, sampler=_Sampler()))
    body = client.get("/api/presence").json()
    assert body["state"] == "ok"
    bots = {b["alias"]: b for b in body["data"]["bots"]}
    assert bots[BOT]["presence"] == "working"
    assert body["data"]["counts"]["working"] == 1
    assert body["data"]["sampler_available"] is True


def test_stale_horizon_follows_the_keepalive_active_window(tmp_path,
                                                           monkeypatch):
    """Finding 1 fold: the endpoint's staleness horizon reads
    KEEPALIVE_ACTIVE_WINDOW_S (keepalive's own cadence, a separate
    process), so tuning the tick can't silently mis-type staleness. With
    a 60s window, a 90s-old heartbeat is stale where the 180s default
    would call it fresh."""
    from claudlobby.plane.emit_api import emit_batch
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app

    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    emit_batch(root, [
        {"event_type": "registry_snapshot", "emitter": "t", "fleet": "f",
         "payload": {"entity_type": "bot", "entity_alias": BOT,
                     "cause": "generate", "scan_id": "s1",
                     "payload": {"alias": BOT, "account": "a", "service": "s",
                                 "model": "opus",
                                 "posture": {"permissions_mode": "plan"},
                                 "composed_hashes": {}, "declared_hash": "d",
                                 "schema_version": "1"}}},
        {"event_type": "metric_sample", "emitter": "keepalive", "fleet": "f",
         "payload": {"subject_kind": "bot_instance", "subject": BOT,
                     "metric": "bot.heartbeat", "value": {"state": "BUSY"}}}])
    # backdate the sample's ingest clock 90s
    import sqlite3
    db = sqlite3.connect(root / "state" / "plane" / "plane.db")
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    db.execute("UPDATE ingest_ledger SET ingested_at=? WHERE ingest_seq="
               "(SELECT ingest_seq FROM metric_samples LIMIT 1)", (old,))
    db.commit()
    db.close()

    class _S:
        available = True

        def snapshot(self):
            return {"panes": [_pane("f", "erlich", "up")],
                    "sampler_running": True}

        def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setenv("KEEPALIVE_ACTIVE_WINDOW_S", "60")
    client = TestClient(create_app(root, sampler=_S()))
    bot = client.get("/api/presence").json()["data"]["bots"][0]
    assert bot["presence"] == "stale"       # 90s > 60s window


def test_presence_endpoint_discloses_a_dead_recorded_half(tmp_path):
    """Finding 4 fold: when the db cannot answer, the endpoint still
    serves the live half AND flags recorded_unavailable + the panel
    state — the UI must have something to surface, never a silent
    badge-less grid."""
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app

    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    # a real db file that is not a plane db → UNREADABLE, not absent
    (root / "state" / "plane" / "plane.db").write_bytes(b"not a database")

    class _S:
        available = True

        def snapshot(self):
            return {"panes": [_pane("f", "erlich", "down")],
                    "sampler_running": True}

        def start(self):
            pass

        async def stop(self):
            pass

    body = TestClient(create_app(root, sampler=_S())).get(
        "/api/presence").json()
    assert body["state"] != "ok"
    assert body["data"]["recorded_unavailable"] is True
    # the live half still rendered: the down bot is present from the poll
    assert body["data"]["counts"]["down"] == 1
    assert body.get("remediation")


def test_poison_heartbeat_value_never_crashes_the_panel(tmp_path):
    """r-probe SEV-1 (proven end-to-end): MetricSample.value is `object`,
    so a bot.heartbeat with a SCALAR/list value commits — and a reader
    assuming a dict 500'd the WHOLE panel, all healthy bots invisible.
    A value that can't be read as a state object IS unknown-state; the
    healthy bots survive."""
    from claudlobby.plane.emit_api import emit_batch
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app

    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')

    def _kf(alias):
        return {"event_type": "registry_snapshot", "emitter": "t",
                "fleet": "f", "payload": {
                    "entity_type": "bot", "entity_alias": alias,
                    "cause": "generate", "scan_id": "s1",
                    "payload": {"alias": alias, "account": "a", "service": "s",
                                "model": "opus",
                                "posture": {"permissions_mode": "plan"},
                                "composed_hashes": {}, "declared_hash": "d",
                                "schema_version": "1"}}}

    def _hbs(alias, value):
        return {"event_type": "metric_sample", "emitter": "x", "fleet": "f",
                "payload": {"subject_kind": "bot_instance", "subject": alias,
                            "metric": "bot.heartbeat", "value": value}}

    good = "bot:f/good"
    poison = "bot:f/poison"
    emit_batch(root, [_kf(good), _kf(poison),
                      _hbs(good, {"state": "BUSY", "marker_age_s": 1}),
                      _hbs(poison, 42)])          # scalar value — committed

    class _S:
        available = True

        def snapshot(self):
            return {"panes": [_pane("f", "good", "up"),
                              _pane("f", "poison", "up")],
                    "sampler_running": True}

        def start(self):
            pass

        async def stop(self):
            pass

    body = TestClient(create_app(root, sampler=_S())).get(
        "/api/presence").json()
    assert body["state"] == "ok"           # no 500
    by = {b["alias"]: b["presence"] for b in body["data"]["bots"]}
    assert by[good] == "working"           # healthy bot survives the poison
    assert by[poison] == "unknown"         # unreadable value -> unknown-state


def test_a_raising_sampler_never_takes_the_recorded_half_down(tmp_path):
    """r-probe SEV-2: the two halves must fail independently. A sampler
    whose snapshot() raises degrades to no-live-poll (disclosed), and the
    recorded half still renders."""
    from claudlobby.plane.emit_api import emit_batch
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app

    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    emit_batch(root, [
        {"event_type": "registry_snapshot", "emitter": "t", "fleet": "f",
         "payload": {"entity_type": "bot", "entity_alias": BOT,
                     "cause": "generate", "scan_id": "s1",
                     "payload": {"alias": BOT, "account": "a", "service": "s",
                                 "model": "opus",
                                 "posture": {"permissions_mode": "plan"},
                                 "composed_hashes": {}, "declared_hash": "d",
                                 "schema_version": "1"}}},
        {"event_type": "metric_sample", "emitter": "keepalive", "fleet": "f",
         "payload": {"subject_kind": "bot_instance", "subject": BOT,
                     "metric": "bot.heartbeat", "value": {"state": "IDLE"}}}])

    class _Raises:
        available = True

        def snapshot(self):
            raise RuntimeError("sampler exploded")

        def start(self):
            pass

        async def stop(self):
            pass

    body = TestClient(create_app(root, sampler=_Raises())).get(
        "/api/presence").json()
    assert body["state"] == "ok"                       # no 500
    assert body["data"]["sampler_available"] is False  # disclosed degraded
    # the recorded half judged the bot on its own (no live poll -> sampling)
    assert body["data"]["bots"][0]["presence"] == "idle"
