"""Tests for `claudlobby brief` — the one read door (#904 PR 1, epic #1102 R1).

Two properties carry most of the weight here and are worth naming, because a
test that only checked "the section rendered" would pass while either was
broken:

  1. **The dispatch sections are the shared doors' output, not a re-join.**
     Asserted by calling ``lib/dispatch-overdue.py`` directly and comparing, so
     a re-implementation that drifted from the watchdog would fail even if it
     looked right on its own. Since the F18 closure (R2a) the doors read the
     PLANE and nothing else: every dispatch fixture below lands on a plane
     under the root (the live door's own event shapes), and "the ledger is
     absent / unreadable" became "the plane is unreachable" — one state, one
     remedy, the section omitted rather than zeroed.
  2. **A field this door cannot serve truthfully is never served silently.**
     Every degradation test checks the disclosure AND that the section did not
     quietly become an innocent-looking empty list.

Since the F18 closure (R2b) EVERY section reads the plane: reports land as the
report door lands them (a communication plus the task event or the
`report_status` marker), workstreams as the workstream door's construct and
verb events. Deleted with the ledgers: test_corrupt_registry_is_omitted_not_reported_as_empty
(no file to corrupt), test_poisoned_report_row_is_counted_not_silently_dropped and
test_poisoned_dispatch_row_is_counted (the #911 label measured malformed JSONL
rows; the plane holds no row to drop), test_residence_mismatch_bound_disclosed_only_in_overlay_mode
(the #526 label warned of a cross-fleet join the per-fleet alias cannot
produce), test_missing_report_ledger_omits_reports_rather_than_zero (→
test_a_plane_that_never_saw_the_fleet_omits_reports_rather_than_zero),
TestBootProvenance's registry-file cases, TestUnlistableBotsDir.test_the_alerts_section_degrades_instead_of_raising
and TestAlertsAbsentBotsDir (the alerts section reads no bots dir — →
TestAlertsReadThePlane).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from claudlobby.brief import (
    BOOT_CHAR_BUDGET,
    SCHEMA_VERSION,
    boot_provenance,
    build_brief,
    format_boot_brief,
    format_brief,
    record_ack,
    load_dispatch_doors,
)
from claudlobby.config import BotConfig, FleetConfig, ProjectConfig, ScopeConfig
from claudlobby.paths import Paths

from tests.conftest import (
    dispatch_row as _dispatch,
    report_row as _report,
)

NOW = 2_000_000
REPO_ROOT = Path(__file__).resolve().parent.parent


# --- fixtures -----------------------------------------------------------------


def _fleet(**kw) -> FleetConfig:
    bot = BotConfig(
        bot_id="alex",
        name="Alex",
        expertise=["software-engineering"],
        scope=ScopeConfig(org="acme", repos=["acme/widget"]),
    )
    base = dict(
        name="test-fleet",
        service_prefix="com.test",
        bots={"alex": bot, "ari": BotConfig(bot_id="ari", name="Ari", expertise=[])},
        mission="Ship things that earn their keep.",
    )
    base.update(kw)
    return FleetConfig(**base)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A claudlobby root with the REAL dispatch matcher in lib/ (and the
    stdlib plane readers it imports beside itself).

    Copied rather than stubbed: the point of the dispatch assertions is that
    the brief and the watchdog share one implementation, which a stub would
    quietly sever.
    """
    (tmp_path / "lib").mkdir()
    for name in ("dispatch-overdue.py", "plane-readers.py", "plane-lookup.py"):
        shutil.copy(REPO_ROOT / "lib" / name, tmp_path / "lib" / name)
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "capture.json").write_text('{"*": "full"}')   # bodies kept, as on the estate
    (tmp_path / "runtime" / "fleet").mkdir(parents=True)
    (tmp_path / "runtime" / "bots" / "alex" / "data").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=None)


FLEET = "test-fleet"


@pytest.fixture(autouse=True)
def _plane_carrier(monkeypatch):
    """Root mode names no fleet (``Paths.fleet_name`` is None), so the matcher
    reads the fleet from the carrier every session and timer carries."""
    monkeypatch.setenv("CLAUDLOBBY_FLEET", FLEET)
    for k in list(__import__("os").environ):
        if k.startswith("PLANE_READ_") or k == "FLEET_NAME":
            monkeypatch.delenv(k, raising=False)


def _iso(epoch: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


_SEQ = [0]


def _land(paths: Paths, row: dict, *, fleet: str = FLEET) -> tuple[str, str]:
    """Land one legacy-shaped dispatch row on the plane under ``paths.root``
    as the live door lands it (work item + assignment + communication) —
    the importer suite's helper, with the fleet the brief's carrier names."""
    from tests.plane_fixtures import _live_dispatch
    _SEQ[0] += 1
    tid = row.get("task_id") or f"t-{_SEQ[0]}-0000"
    n = f"{_SEQ[0]:x}"
    wi, asg, _msg = _live_dispatch(
        paths.root, n, tid, ts=_iso(row["dispatched_at"]), bot=row["bot"],
        expected_by=_iso(row["expected_by"]) if isinstance(row.get("expected_by"), int) else None,
        fleet=fleet, ref=None if row.get("task_id") else f"dispatch-log:sha:{n:0>32}")
    return wi, asg


def _land_all(paths: Paths, rows: list[dict]) -> None:
    for r in rows:
        _land(paths, r)


def _land_report(paths: Paths, row: dict, *, fleet: str = FLEET) -> None:
    """A report row as the real report door lands it: the report communication
    (its body the wire line, so the summary parses), then EITHER the task
    event on the assignment carrying the row's id (looked up by its
    ``dispatch-log:<id>`` ref) OR — when nothing linked — the ``report_status``
    marker that carries the status (the door's one-fact rule)."""
    from claudlobby.plane.db import connect_ro, db_file
    from claudlobby.plane.emit_api import emit_batch
    _SEQ[0] += 1
    msg = f"msg_{'f' * 24}{_SEQ[0]:0>8x}"
    ref = f"report-back:{msg}"
    bot, status = row["bot"], row.get("status", "")
    body = f"[BOTREPORT] {bot} | {status} | {row.get('summary', 'r')}"
    events = [{"event_type": "communication", "emitter": "report-back", "fleet": fleet,
               "source_ref": ref, "occurred_at": row["ts"],
               "payload": {"msg_id": msg, "sender": f"bot:{fleet}/{bot}", "recipient": f"bot:{fleet}/lead",
                           "recipient_raw": "lead", "message_class": "report", "body": body}}]
    tid = row.get("task_id")
    if tid and status in ("completed", "failed", "blocked", "progress"):
        conn = connect_ro(db_file(paths.root))
        try:
            hit = conn.execute("SELECT work_item_id, assignment_id FROM assignments WHERE source_ref = ?"
                               " ORDER BY ingest_seq DESC LIMIT 1", (f"dispatch-log:{tid}",)).fetchone()
        finally:
            conn.close()
        if hit:
            events.append({"event_type": "task", "emitter": "report-back", "fleet": fleet,
                           "source_ref": ref, "occurred_at": row["ts"],
                           "payload": {"work_item_id": hit[0], "assignment_id": hit[1],
                                       "event": row["status"], "actor": f"bot:{fleet}/{bot}"}})
    if len(events) == 1 and status in ("completed", "failed", "blocked", "progress"):
        events.append({"event_type": "system", "emitter": "report-back", "fleet": fleet,
                       "source_ref": ref, "occurred_at": row["ts"],
                       "payload": {"event": "report_status", "subject_kind": "actor",
                                   "subject": f"bot:{fleet}/{bot}", "data": {"status": status, "msg_id": msg}}})
    out = emit_batch(paths.root, events)
    assert all(o.status == "committed" for o in out), out


def _land_ws(paths: Paths, wid: str, *, opened_ts: str, last_progress_ts: str | None = None,
             lease_expires_ts: str | None = None, status: str = "active", title: str = "Ship the widget",
             owner: str = "alex", nxt: str = "build the door", fleet: str = FLEET) -> None:
    """One workstream as the workstream door lands it: the construct, then the
    verb events that give it a progress instant, a renewed lease and a status."""
    from claudlobby.plane.emit_api import emit_batch
    actor, ref = f"bot:{fleet}/{owner}", f"workstreams:{wid}"
    events = [{"event_type": "workstream", "emitter": "workstream-update", "fleet": fleet,
               "source_ref": ref, "occurred_at": opened_ts,
               "payload": {"workstream_id": wid, "title": title, "opened_by": actor, "owner": actor, "goal": nxt}}]

    def verb(event: str, at: str, **extra) -> None:
        events.append({"event_type": "workstream_event", "emitter": "workstream-update", "fleet": fleet,
                       "source_ref": ref, "occurred_at": at,
                       "payload": {"workstream_id": wid, "event": event, "actor": actor, **extra}})

    at = last_progress_ts or opened_ts
    if last_progress_ts:
        verb("progressed", last_progress_ts, next_step=nxt)
    if lease_expires_ts:
        verb("renewed", at, renewed_until=lease_expires_ts, note="renewed")
    if status == "done":
        verb("closed", at, disposition="done")
    elif status == "blocked":
        verb("blocked", at, note="blocked")
    out = emit_batch(paths.root, events)
    assert all(o.status == "committed" for o in out), out


def _plane_rows(paths: Paths) -> int:
    from claudlobby.plane.db import connect_ro, db_file
    conn = connect_ro(db_file(paths.root))
    try:
        return conn.execute("SELECT (SELECT COUNT(*) FROM events) + (SELECT COUNT(*) FROM workstreams)"
                            " + (SELECT COUNT(*) FROM communications)").fetchone()[0]
    finally:
        conn.close()


def _seed_plane(paths: Paths) -> None:
    """A plane that knows this fleet's bots (the registry rows every emission
    mints) but holds no dispatch — the genuine "nothing open" state."""
    from claudlobby.plane.emit_api import emit_batch
    out = emit_batch(paths.root, [{
        "event_type": "system", "emitter": "test", "fleet": FLEET,
        "payload": {"event": "keepalive_skip", "subject_kind": "actor", "subject": f"bot:{FLEET}/alex",
                    "data": {"source": "test", "legacy_ts": "2026-05-27T10:00:00Z", "data": {}}}}])
    assert out[0].status == "committed", out


def _dispatch_ctx(paths: Paths) -> dict:
    return {"fleet": FLEET, "root": str(paths.root)}


def _seed_plane_for(paths: Paths, fleet: str) -> None:
    from claudlobby.plane.emit_api import emit_batch
    out = emit_batch(paths.root, [{
        "event_type": "system", "emitter": "test", "fleet": fleet,
        "payload": {"event": "keepalive_skip", "subject_kind": "actor", "subject": f"bot:{fleet}/alex",
                    "data": {"source": "test", "legacy_ts": "2026-05-27T10:00:00Z", "data": {}}}}])
    assert out[0].status == "committed", out


def _find(brief: dict, field: str, issue: str | None = None) -> list[dict]:
    return [
        d
        for d in brief["degraded"]
        if d["field"] == field and (issue is None or d["issue"] == issue)
    ]


# --- envelope -----------------------------------------------------------------


def test_brief_json_schema_v1(paths: Paths):
    _seed_plane(paths)                       # every section is served from the plane
    brief = build_brief(_fleet(), paths, "alex", NOW)

    assert brief["schema"] == SCHEMA_VERSION
    assert brief["bot"] == "alex"
    assert brief["fleet"] == "test-fleet"
    for key in (
        "generated_at",
        "mission",
        "dispatches",
        "workstreams",
        "reports",
        "alerts",
        "degraded",
    ):
        assert key in brief, f"envelope is missing {key}"
    assert set(brief["dispatches"]) == {"open", "overdue", "orphaned"}
    assert set(brief["reports"]) == {"cursor", "unacked", "source"}
    # Round-trips as JSON — R4 consumes this envelope, not the text form.
    json.dumps(brief)


def test_mission_carries_pointers_not_inlined_charters(paths: Paths, tmp_path: Path):
    fleet = _fleet(
        mission_file="missions/fleet.md",
        projects={
            "widget": ProjectConfig(
                key="widget",
                title="Widget",
                repos=["acme/widget"],
                mission_file="missions/widget.md",
            ),
            "other": ProjectConfig(
                key="other",
                title="Other",
                repos=["acme/unrelated"],
                mission_file="missions/other.md",
            ),
        },
    )
    _seed_plane(paths)
    m = build_brief(fleet, paths, "alex", NOW)["mission"]

    assert m["anchor"] == "Ship things that earn their keep."
    assert m["charter"].endswith("missions/fleet.md")
    # Joined on scope repos: the bot's project is pointed at, the other is not.
    assert [p["project"] for p in m["projects"]] == ["widget"]
    assert m["projects"][0]["mission_file"].endswith("missions/widget.md")


# --- dispatches ---------------------------------------------------------------


def test_brief_dispatch_sections_match_overdue_doors(paths: Paths):
    """The three sections must BE the shared doors' output, not a second join."""
    _land_all(paths, [
        _dispatch("alex", NOW - 9000, NOW - 3000, task_id="t-late"),
        _dispatch("alex", NOW - 500, NOW + 5000, task_id="t-early"),
        _dispatch("ari", NOW - 9000, NOW - 3000, task_id="t-other-bot"),
    ])

    doors = load_dispatch_doors(paths)
    d = build_brief(_fleet(), paths, "alex", NOW)["dispatches"]

    expected_overdue = doors.overdue_all(
        NOW, bots_dir=str(paths.runtime_bots), **_dispatch_ctx(paths)
    ).get("alex", [])
    assert [r["task_id"] for r in d["overdue"]] == [t[3] for t in expected_overdue]
    assert [r["task_id"] for r in d["overdue"]] == ["t-late"]

    expected_open = doors.open_dispatches("alex", **_dispatch_ctx(paths))
    assert [r["task_id"] for r in d["open"]] == [t[2] for t in expected_open]

    # Another bot's rows never leak into this bot's brief.
    assert "t-other-bot" not in json.dumps(d)


def test_open_is_deadline_blind_superset_of_overdue(paths: Paths):
    """The readable distinction the door was built for: open-but-not-yet-due."""
    _land_all(paths, [
        _dispatch("alex", NOW - 9000, NOW - 3000, task_id="t-late"),
        _dispatch("alex", NOW - 500, NOW + 5000, task_id="t-early"),
    ])
    d = build_brief(_fleet(), paths, "alex", NOW)["dispatches"]

    assert [r["task_id"] for r in d["open"]] == ["t-late", "t-early"]  # oldest first
    assert {r["task_id"] for r in d["overdue"]} == {"t-late"}
    assert {r["task_id"] for r in d["overdue"]} <= {r["task_id"] for r in d["open"]}

    by_id = {r["task_id"]: r for r in d["open"]}
    assert by_id["t-late"]["past_due"] is True
    assert by_id["t-early"]["past_due"] is False


def test_terminal_report_closes_an_open_dispatch(paths: Paths):
    # The plane answers AS OF `now`: a report must land before the instant the
    # brief asks about, or it has not happened yet by the plane's account.
    _land(paths, _dispatch("alex", NOW - 9000, NOW - 3000, task_id="t-1"))
    _land_report(paths, _report("alex", _iso(NOW - 100), task_id="t-1"))
    d = build_brief(_fleet(), paths, "alex", NOW)["dispatches"]
    assert d["open"] == []
    assert d["overdue"] == []


def test_missing_matcher_omits_every_plane_section_rather_than_reporting_zero(paths: Paths):
    """An unloadable matcher must not render as 'nothing open' — and since
    every plane read rides its session (R2b-1 fold), the reports, alerts and
    workstreams sections are withheld with it, each field named."""
    (paths.lib / "dispatch-overdue.py").unlink()
    _land(paths, _dispatch("alex", NOW - 9000, NOW - 3000, task_id="t-1"))

    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["dispatches"] == {} and brief["reports"] == {} and brief["workstreams"] == {}
    assert brief["alerts"] == []
    omitted = {x["field"] for x in brief["degraded"] if x["mode"] == "omitted" and x["issue"] == "#1467"}
    assert {"dispatches.open", "dispatches.overdue", "dispatches.orphaned", "reports", "alerts",
            "workstreams"} <= omitted
    entry = _find(brief, "dispatches.open", "#1467")
    assert entry and "matcher" in entry[0]["reason"]
    assert "unavailable" in format_brief(brief)


# --- unacked reports + the ack cursor -----------------------------------------


def _acked_events(paths: Paths) -> list[tuple]:
    import sqlite3
    conn = sqlite3.connect(paths.root / "state" / "plane" / "plane.db")
    try:
        return conn.execute(
            "SELECT subject_alias, severity, detail FROM events WHERE kind='system'"
            " AND event='reports_acked' ORDER BY ingest_seq").fetchall()
    finally:
        conn.close()


def _ack(paths: Paths, bot: str, unacked: list[dict]):
    newest = max(unacked, key=lambda r: r["seq"] or 0)
    return record_ack(paths, FLEET, bot, acked_through_seq=newest["seq"],
                      acked_through_ts=newest["ts"], count=len(unacked))


def test_brief_ack_is_a_plane_fact_and_the_unacked_list_shrinks(paths: Paths):
    """Chunk K (#1467): `--ack` records ONE `reports_acked` system event on the
    viewer's own actor; the unacked list is what lies past it on the plane's
    own ordering (`ingest_seq`), and no cursor file exists anywhere."""
    _seed_plane(paths)
    for row in (
        _report("vera", "2026-08-08T10:00:00Z", status="completed"),
        _report("mason", "2026-08-08T11:00:00Z", status="blocked"),
        _report("vera", "2026-08-08T11:30:00Z", status="progress"),
    ):
        _land_report(paths, row)
    fleet = _fleet()

    brief = build_brief(fleet, paths, "alex", NOW)
    unacked = brief["reports"]["unacked"]
    # progress is not terminal — it closes nothing and acks nothing.
    assert [r["status"] for r in unacked] == ["completed", "blocked"]
    assert all(isinstance(r["seq"], int) for r in unacked)
    assert brief["reports"]["cursor"] is None

    out = _ack(paths, "alex", unacked)
    assert out.recorded, out
    again = build_brief(fleet, paths, "alex", NOW)
    assert again["reports"]["unacked"] == []
    assert again["reports"]["cursor"] == "2026-08-08T11:00:00Z"   # the legacy-form ts, for the render

    (alias, severity, detail), = _acked_events(paths)
    assert alias == f"bot:{FLEET}/alex" and severity == "notice"
    data = json.loads(detail)
    assert set(data) == {"acked_through_seq", "acked_through_ts", "count"}
    assert data["acked_through_seq"] == max(r["seq"] for r in unacked) and data["count"] == 2

    # a report landing after the ack reads unacked again
    _land_report(paths, _report("vera", "2026-08-08T12:00:00Z", status="completed"))
    assert [r["ts"] for r in build_brief(fleet, paths, "alex", NOW)["reports"]["unacked"]] == [
        "2026-08-08T12:00:00Z"]
    assert not list(paths.root.rglob("brief-cursor-*"))        # no JSON state, anywhere


def test_ack_is_per_viewer_on_the_plane(paths: Paths):
    """Two managers acking the same reports must not clobber each other: the
    fact is anchored on the acking bot's own actor."""
    _seed_plane(paths)
    _land_report(paths, _report("vera", "2026-08-08T10:00:00Z"))
    alex = build_brief(_fleet(), paths, "alex", NOW)["reports"]["unacked"]
    assert _ack(paths, "alex", alex).recorded

    assert build_brief(_fleet(), paths, "alex", NOW)["reports"]["unacked"] == []
    assert len(build_brief(_fleet(), paths, "ari", NOW)["reports"]["unacked"]) == 1


def test_a_malformed_ack_is_no_read_position_and_erases_none(paths: Paths):
    """A `reports_acked` row whose detail carries no integer cursor is not an
    ack: alone, the report shows (never hides — #949/#1024); landing AFTER a
    valid ack it does not reset the viewer to "never acked" — the newest
    READABLE ack holds (adversarial lens: one bad row erased a valid cursor)."""
    from claudlobby.plane.emit_api import emit_batch

    def malformed():
        return emit_batch(paths.root, [{
            "event_type": "system", "emitter": "brief", "fleet": FLEET,
            "payload": {"event": "reports_acked", "subject_kind": "actor",
                        "subject": f"bot:{FLEET}/alex", "data": {"note": "no cursor here"}}}])[0].status

    _seed_plane(paths)
    _land_report(paths, _report("vera", "2026-08-08T10:00:00Z"))
    assert malformed() == "committed"
    reports = build_brief(_fleet(), paths, "alex", NOW)["reports"]
    assert len(reports["unacked"]) == 1 and reports["cursor"] is None

    assert _ack(paths, "alex", reports["unacked"]).recorded
    assert build_brief(_fleet(), paths, "alex", NOW)["reports"]["unacked"] == []
    assert malformed() == "committed"
    again = build_brief(_fleet(), paths, "alex", NOW)["reports"]
    assert again["unacked"] == [] and again["cursor"] == "2026-08-08T10:00:00Z"


def test_a_silenced_plane_is_a_failed_ack(paths: Paths, monkeypatch):
    """`PLANE_EMIT_DISABLED=1` is the one silencer (the harness exemption): a
    plane that will not hold the ack marks nothing — failed, said by name,
    no fact recorded — never a quiet rc 0 that reads as acked."""
    from claudlobby.brief import record_ack

    _seed_plane(paths)
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    out = record_ack(paths, FLEET, "alex", acked_through_seq=3, acked_through_ts="x", count=1)
    assert out.status == "failed" and "PLANE_EMIT_DISABLED" in out.detail
    assert _acked_events(paths) == []


def test_a_fleets_reports_are_the_room_axis_and_progress_is_never_unacked(paths: Paths):
    """ONE definition of a fleet's reports (queries.FLEET_REPORTS_SQL): a worker on
    ANOTHER fleet reporting to this fleet's manager is this fleet's report — it
    reaches the manager's brief (the card counts it, so the brief must list it,
    or no ack could ever clear it); a `progress` note is never unacked; a
    report the plane holds with no status at all still needs reading."""
    from claudlobby.plane.emit_api import emit_batch

    _seed_plane(paths)
    _land_report(paths, _report("vera", "2026-08-08T10:00:00Z"))
    _land_report(paths, {"bot": "vera", "ts": "2026-08-08T10:30:00Z", "status": "progress",
                         "summary": "halfway"})
    emit_batch(paths.root, [{
        "event_type": "communication", "emitter": "report-back", "fleet": "other",
        "source_ref": "report-back:msg_" + "9" * 32, "occurred_at": "2026-08-08T11:00:00Z",
        "payload": {"msg_id": "msg_" + "9" * 32, "sender": "bot:other/zed",
                    "recipient": f"bot:{FLEET}/alex", "recipient_raw": "alex",
                    "message_class": "report",
                    "body": "[BOTREPORT] zed | completed | cross-fleet done"}}])
    reports = build_brief(_fleet(), paths, "alex", NOW)["reports"]
    assert [(r["bot"], r["status"]) for r in reports["unacked"]] == [
        ("vera", "completed"), ("other/zed", "completed")]
    assert _ack(paths, "alex", reports["unacked"]).recorded
    assert build_brief(_fleet(), paths, "alex", NOW)["reports"]["unacked"] == []


def test_failed_emit_is_a_failed_ack(paths: Paths, monkeypatch, caplog):
    """A failed emit marks nothing seen: rc 1, said by name, no fact recorded,
    the reports read unacked again — there is no file to fall back on."""
    import argparse
    import logging

    import claudlobby.plane.emit_api as emit_api
    from claudlobby.commands.core import cmd_brief

    fleet_dir = paths.root / "local" / "f1"
    (fleet_dir / "runtime").mkdir(parents=True)
    _write_fleet_yaml(fleet_dir, "f1", ["alex"])
    _seed_plane_for(paths, "f1")
    _land_report(paths, _report("vera", "2026-08-08T10:00:00Z"), fleet="f1")

    def boom(root, reqs):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(emit_api, "emit_batch", boom)
    args = argparse.Namespace(fleet="f1", root=str(paths.root), seed=False, bot="alex",
                              json=False, ack=True, boot=False)
    with caplog.at_level(logging.ERROR, logger="claudlobby"):
        assert cmd_brief(args) == 1
    assert "did NOT record the ack" in caplog.text and "disk on fire" in caplog.text
    assert _acked_events(paths) == []


def test_no_cursor_file_is_written_or_read_anywhere():
    """The deletion, pinned: no door under claudlobby/ or lib/ names the file."""
    import subprocess
    out = subprocess.run(["grep", "-rn", "-E", "brief-cursor|read_cursor|write_cursor|cursor_path",
                          str(REPO_ROOT / "claudlobby"), str(REPO_ROOT / "lib")],
                         capture_output=True, text=True)
    assert out.returncode == 1 and out.stdout == "", out.stdout


def test_reports_are_fleet_wide_not_self_scoped(paths: Paths):
    """'What did my workers finish that I have not acted on' — not 'my own'."""
    _seed_plane(paths)
    _land_report(paths, _report("vera", "2026-08-08T10:00:00Z"))
    _land_report(paths, _report("mason", "2026-08-08T10:05:00Z"))
    bots = {
        r["bot"]
        for r in build_brief(_fleet(), paths, "alex", NOW)["reports"]["unacked"]
    }
    assert bots == {"vera", "mason"}


# --- workstreams --------------------------------------------------------------


def test_brief_stall_flags_readonly(paths: Paths):
    _seed_plane(paths)
    # now = 2_000_000 epoch ≈ 1970-01-24; use epochs so the arithmetic is explicit.
    fresh = "1970-01-23T00:00:00Z"  # ~1 day before NOW
    old = "1970-01-01T00:00:00Z"  # ~23 days before NOW → past the 14d lease
    far = "1999-01-01T00:00:00Z"
    _land_ws(paths, "ws-fresh", opened_ts=old, last_progress_ts=fresh, lease_expires_ts=far)
    _land_ws(paths, "ws-stale", opened_ts=old, lease_expires_ts=far)            # progress = opened, 23 days ago
    _land_ws(paths, "ws-expired", opened_ts=old, last_progress_ts=fresh, lease_expires_ts=old)
    _land_ws(paths, "ws-done", opened_ts=old, status="done")
    before = _plane_rows(paths)

    w = build_brief(_fleet(), paths, "alex", NOW)["workstreams"]

    assert {e["id"] for e in w["active"]} == {"ws-fresh", "ws-stale", "ws-expired"}
    flags = {e["id"]: (e["stalled"], e["lease_expired"]) for e in w["active"]}
    assert flags["ws-fresh"] == (False, False)
    assert flags["ws-stale"] == (True, False)
    assert flags["ws-expired"] == (False, True)
    assert {e["id"] for e in w["stalled"]} == {"ws-stale", "ws-expired"}

    # THE read-only assertion: the plane holds exactly the rows it held before a brief run.
    assert _plane_rows(paths) == before


def test_workstreams_are_omitted_when_the_plane_cannot_answer(paths: Paths):
    """'No workstreams' and 'the plane could not be read' are different answers."""
    assert not (paths.root / "state" / "plane" / "plane.db").exists()

    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["workstreams"] == {}
    entry = _find(brief, "workstreams", "#1467")
    assert entry and entry[0]["mode"] == "omitted" and "plane" in entry[0]["reason"]


def test_a_plane_that_holds_the_fleet_but_no_workstream_is_not_degraded(paths: Paths):
    _seed_plane(paths)

    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["workstreams"] == {"active": [], "stalled": []}
    assert _find(brief, "workstreams") == []


def test_lease_window_follows_the_writer(paths: Paths, monkeypatch):
    _seed_plane(paths)
    _land_ws(paths, "ws-x", opened_ts="1970-01-18T00:00:00Z")           # progress = opened, ~6 days before NOW
    assert (
        build_brief(_fleet(), paths, "alex", NOW)["workstreams"]["active"][0]["stalled"]
        is False
    )

    monkeypatch.setenv("WORKSTREAM_LEASE_DAYS", "3")
    assert (
        build_brief(_fleet(), paths, "alex", NOW)["workstreams"]["active"][0]["stalled"]
        is True
    )


# --- the R0 trust gate --------------------------------------------------------


def test_the_911_label_retired_with_the_ledgers(paths: Paths):
    """#911 measured malformed JSONL rows the readers dropped; the plane holds
    no row to drop, so the label is gone rather than perpetually clean."""
    _land(paths, _dispatch("alex", NOW - 100, NOW + 100, task_id="t-1"))
    _land_report(paths, _report("vera", "2026-08-08T10:00:00Z"))
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert [d for d in brief["degraded"] if d["issue"] == "#911"] == []
    assert len(brief["reports"]["unacked"]) == 1


def test_alerts_are_labeled_until_the_event_type_ssot_lands(paths: Paths):
    """#903: absence of an alert is not evidence of health, and says so."""
    _seed_plane(paths)
    brief = build_brief(_fleet(), paths, "alex", NOW)

    entry = _find(brief, "alerts", "#903")
    assert entry and entry[0]["mode"] == "labeled"
    assert "absence of an alert is not evidence of health" in entry[0]["reason"]


def test_alert_label_clears_when_the_ssot_symbol_appears(paths: Paths, monkeypatch):
    """Keyed on #903's actual deliverable, so it retires itself."""
    from claudlobby import known_values

    monkeypatch.setattr(known_values, "FLEET_EVENT_TYPES", {"disk_high"}, raising=False)
    _seed_plane(paths)
    assert _find(build_brief(_fleet(), paths, "alex", NOW), "alerts", "#903") == []


def test_utilization_is_recorded_as_omitted(paths: Paths):
    """The cut section is an answer, not a gap to be inferred from absence."""
    _seed_plane(paths)
    brief = build_brief(_fleet(), paths, "alex", NOW)

    entry = _find(brief, "utilization", "#891")
    assert entry and entry[0]["mode"] == "omitted"
    assert "utilization" not in set(brief) - {"degraded"}


def test_no_residence_mismatch_label_on_the_plane(root: Path):
    """The #526 label warned that a host-global dispatch log joined per-fleet
    report ledgers on bot name alone. The plane's join is the per-fleet alias,
    so the collision cannot occur and the standing label is gone — in overlay
    mode too, where it used to fire whenever the section was served."""
    fleet_dir = root / "local" / "f1"
    (fleet_dir / "runtime" / "bots").mkdir(parents=True)
    overlay = Paths(root=root, fleet_dir=fleet_dir)
    _land(overlay, _dispatch("alex", NOW - 100, NOW + 100, task_id="t-1"), fleet="f1")

    brief = build_brief(_fleet(), overlay, "alex", NOW)
    assert [r["task_id"] for r in brief["dispatches"]["open"]] == ["t-1"]
    assert _find(brief, "dispatches", "#526") == []


# --- rendering ----------------------------------------------------------------


def test_format_marks_degraded_sections_inline_and_lists_them(paths: Paths):
    _seed_plane(paths)
    text = format_brief(build_brief(_fleet(), paths, "alex", NOW))

    assert "ALERTS" in text and "[degraded: #903]" in text
    assert "DEGRADED — fields this door will not serve as plain truth" in text
    assert "degraded field(s)" in text  # the top-of-output banner
    for section in ("MISSION", "DISPATCHES", "WORKSTREAMS", "REPORTS"):
        assert section in text


def test_a_matcher_predating_the_plane_only_reader_withholds_the_section(
    paths: Paths, monkeypatch
):
    """The matcher is the INSTALL's; one that predates the plane-only reader
    (F18 R2a) has ledger-era signatures that would raise out of a read-only
    command. The WHOLE section is withheld, all three fields named, and the
    DISPATCHES header carries the issue."""
    _seed_plane(paths)
    import claudlobby.brief as brief_mod

    real = brief_mod.load_dispatch_doors

    class _Old:
        def __init__(self, mod):
            self._mod = mod

        def __getattr__(self, name):
            if name == "open_plane":
                raise AttributeError(name)
            return getattr(self._mod, name)

    monkeypatch.setattr(brief_mod, "load_dispatch_doors", lambda p: _Old(real(p)))
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["dispatches"] == {}
    omitted = {x["field"] for x in brief["degraded"] if x["mode"] == "omitted"}
    assert {"dispatches.open", "dispatches.overdue", "dispatches.orphaned"} <= omitted
    entry = _find(brief, "dispatches.open", "#1467")
    assert entry and "predates" in entry[0]["reason"]
    header = next(
        ln for ln in format_brief(brief).splitlines() if ln.startswith("DISPATCHES")
    )
    assert "#1467" in header


def test_a_failure_after_the_first_answer_withholds_all_three(paths: Paths, monkeypatch):
    """Both questions ride ONE plane session; a failure on the second (the
    open list) withholds the section whole and names all three fields — the
    structural lens found only `open` named, so overdue was neither present
    nor listed."""
    _seed_plane(paths)
    import claudlobby.brief as brief_mod

    real = brief_mod.load_dispatch_doors

    class _Flaky:
        def __init__(self, mod):
            self._mod = mod

        def __getattr__(self, name):
            return getattr(self._mod, name)

        def open_dispatches(self, *a, **k):
            raise self._mod.PlaneUnreachable("gone mid-brief")

    monkeypatch.setattr(brief_mod, "load_dispatch_doors", lambda p: _Flaky(real(p)))
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["dispatches"] == {}
    omitted = {x["field"] for x in brief["degraded"] if x["mode"] == "omitted"}
    assert {"dispatches.open", "dispatches.overdue", "dispatches.orphaned"} <= omitted


def test_the_dispatches_section_opens_the_plane_once(paths: Paths, monkeypatch):
    """One session for both questions (the simplify lens found two opens —
    each an importlib exec, a connect and a registry scan)."""
    _seed_plane(paths)
    import claudlobby.brief as brief_mod

    real = brief_mod.load_dispatch_doors
    opened: list[int] = []

    class _Counting:
        def __init__(self, mod):
            self._mod = mod

        def __getattr__(self, name):
            return getattr(self._mod, name)

        def open_plane(self, *a, **k):
            opened.append(1)
            return self._mod.open_plane(*a, **k)

    monkeypatch.setattr(brief_mod, "load_dispatch_doors", lambda p: _Counting(real(p)))
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert "open" in brief["dispatches"] and "overdue" in brief["dispatches"]
    assert len(opened) == 1


def test_text_output_caps_long_sections_and_discloses_the_cap(paths: Paths):
    """Silent truncation reads as exhaustive coverage."""
    _seed_plane(paths)
    for n in range(25):
        _land_report(paths, _report("vera", f"2026-08-08T10:{n:02d}:00Z"))
    brief = build_brief(_fleet(), paths, "alex", NOW)
    text = format_brief(brief)

    # JSON is never capped — R4 consumes that.
    assert len(brief["reports"]["unacked"]) == 25
    assert "REPORTS — unacked (25)" in text
    assert "showing the oldest 10 of 25" in text
    # The oldest is kept (it is the one rotting), the newest is dropped.
    assert "10:00:00" in text and "10:24:00" not in text


def test_cli_registers_brief_subcommand():
    """Guards the wiring itself: the door is useless if argparse cannot reach
    it, and no test that calls build_brief() directly would notice."""
    import argparse

    from claudlobby.commands._parsers import register_subparsers

    parser = argparse.ArgumentParser()
    register_subparsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["brief", "--bot", "alex", "--json"])

    assert callable(args.func)
    assert args.bot == "alex"
    assert args.json is True
    assert args.ack is False


def test_overdue_honours_the_env_expiry_cap_like_the_cli(paths: Paths, monkeypatch):
    """The matcher's Python API defaults max_age; only its main() reads the env
    var. A brief that ignored it would disagree with the very watchdog it
    mirrors, and 'byte-consistent with --all' is the contract."""
    # ~2.8h old: past its deadline, but inside the 24h default expiry cap.
    _land(paths, _dispatch("alex", NOW - 10_000, NOW - 5_000, task_id="t-old"))

    overdue = build_brief(_fleet(), paths, "alex", NOW)["dispatches"]["overdue"]
    assert [r["task_id"] for r in overdue] == ["t-old"]

    # A fleet that tightens the cap ages the row out; the brief must follow.
    monkeypatch.setenv("DISPATCH_OVERDUE_MAX_AGE_S", "1000")
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["dispatches"]["overdue"] == []
    # Still OPEN, though — expiry silences the watchdog, it does not close work.
    assert [r["task_id"] for r in brief["dispatches"]["open"]] == ["t-old"]


# --- consuming the shared doors defensively (#526 / #1014) ---------------------


def test_an_unreachable_plane_omits_dispatches_rather_than_alarming(paths: Paths):
    """No plane under the root: the matcher REFUSES (rc 3 / PlaneUnreachable —
    never "nothing open"), and the brief OMITS the section with the remedy
    named rather than serving a zero as truth."""
    assert not (paths.root / "state" / "plane" / "plane.db").exists()

    doors = load_dispatch_doors(paths)
    with pytest.raises(doors.PlaneUnreachable):
        doors.overdue_all(NOW, **_dispatch_ctx(paths))       # precondition: the door refuses

    brief = build_brief(_fleet(), paths, "alex", NOW)
    entries = _find(brief, "dispatches.overdue", "#1467") + _find(brief, "dispatches.open", "#1467")
    assert entries and all(e["mode"] == "omitted" for e in entries)
    assert all("plane" in e["reason"] and "state/plane/plane.db" in e["reason"] for e in entries)
    # never zero (a false all-clear): the section is not served, and says so
    assert brief["dispatches"] == {}, "a false all-clear was served for an unreachable plane"
    text = format_brief(brief)
    assert "unavailable" in text


def test_a_plane_that_knows_the_fleet_but_holds_no_dispatch_is_answered_not_omitted(paths: Paths):
    """The genuine "nothing open" state: the plane holds the fleet's bots (the
    registry rows every emission mints) and no dispatch — answered as empty
    lists, no omission."""
    _seed_plane(paths)
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["dispatches"]["open"] == [] and brief["dispatches"]["overdue"] == []
    assert [d for d in _find(brief, "dispatches") if d["mode"] == "omitted"] == []


def test_a_plane_that_never_saw_the_fleet_is_unreachable_not_empty(paths: Paths):
    """A schema-valid plane holding no bot of the named fleet is a wrong root or
    a fleet it never saw: refused, never read as "nothing open" (#1014's class)."""
    _land(paths, _dispatch("alex", NOW - 100, NOW + 100, task_id="t-1"), fleet="another-fleet")
    brief = build_brief(_fleet(), paths, "alex", NOW)
    entries = _find(brief, "dispatches.open", "#1467")
    assert entries and entries[0]["mode"] == "omitted" and "holds no bot of fleet" in entries[0]["reason"]
    assert brief["dispatches"] == {}


def test_a_plane_that_never_saw_the_fleet_omits_reports_rather_than_zero(paths: Paths):
    """'unacked (0)' from a plane that holds no bot of the fleet asserts nobody
    is waiting on a decision — #949 and #1024 exactly, re-created by the fix."""
    _land(paths, _dispatch("alex", NOW - 100, NOW + 100, task_id="t-1"), fleet="another-fleet")

    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["reports"] == {}
    entry = _find(brief, "reports", "#1467")
    assert entry and entry[0]["mode"] == "omitted" and "holds no bot of fleet" in entry[0]["reason"]

    text = format_brief(brief)
    assert "unacked (0)" not in text
    assert "REPORTS" in text and "unavailable" in text
    # The omission must not swallow later sections.
    assert "ALERTS" in text


def test_orphan_list_is_labeled_when_respawn_cannot_be_detected(paths: Paths):
    """#1014's family: no bots dir means the empty orphan list is a construction,
    not a measurement."""
    shutil.rmtree(paths.runtime_bots)
    _land(paths, _dispatch("alex", NOW - 100, NOW + 100, task_id="t-1"))

    brief = build_brief(_fleet(), paths, "alex", NOW)
    entry = _find(brief, "dispatches.orphaned", "#1014")
    assert entry and entry[0]["mode"] == "labeled"
    # Open/overdue are unaffected and still served.
    assert [r["task_id"] for r in brief["dispatches"]["open"]] == ["t-1"]


def test_orphan_label_absent_when_the_bots_dir_exists(paths: Paths):
    _land(paths, _dispatch("alex", NOW - 100, NOW + 100, task_id="t-1"))
    assert _find(build_brief(_fleet(), paths, "alex", NOW), "dispatches.orphaned") == []


def _write_fleet_yaml(fleet_dir: Path, name: str, bots: list[str]) -> None:
    """A REAL fleet.yaml — ``bots:`` nests under ``fleet:``.

    Spelled out because getting it wrong is silent: a top-level ``bots:`` key
    parses fine and yields ZERO declared bots, so `cmd_brief` returns 1 for
    "bot not found" and any test asserting only on the exit code passes for
    entirely the wrong reason.
    """
    fleet_dir.mkdir(parents=True, exist_ok=True)
    (fleet_dir / "fleet.yaml").write_text(
        f"fleet:\n  name: {name}\n  service_prefix: com.test\n  bots:\n"
        + "".join(f"    {b}:\n      expertise: [software-engineering]\n" for b in bots)
    )


def test_ack_refuses_when_the_report_section_was_not_served(paths: Paths, caplog):
    """Advancing a cursor past reports nobody could read marks unread work as
    handled, permanently — the one irreversible thing this command can do.

    Asserts the REASON, not just the exit code: `cmd_brief` returns 1 for
    "bot not found" as well, so a bare `== 1` would pass on a fixture whose
    fleet.yaml declares no bots at all.
    """
    import argparse
    import logging

    from claudlobby.commands.core import cmd_brief

    assert not (paths.root / "state" / "plane" / "plane.db").exists()  # no plane -> section omitted

    fleet_dir = paths.root / "local" / "f1"
    (fleet_dir / "runtime").mkdir(parents=True)
    _write_fleet_yaml(fleet_dir, "f1", ["alex"])

    # The fixture is load-bearing: prove the bot really resolves, so the exit
    # code below can only come from the refusal path.
    from claudlobby.config import load_fleet

    assert "alex" in load_fleet(fleet_dir / "fleet.yaml")[0].bots

    args = argparse.Namespace(
        fleet="f1",
        root=str(paths.root),
        seed=False,
        bot="alex",
        json=False,
        ack=True,
        boot=False,
    )
    with caplog.at_level(logging.ERROR, logger="claudlobby"):
        assert cmd_brief(args) == 1
    assert "refusing to ack" in caplog.text
    assert "not found" not in caplog.text
    assert not (paths.root / "state" / "plane" / "plane.db").exists()   # a refusal records nothing


def test_ack_succeeds_when_the_plane_answers(paths: Paths):
    """The positive control for the test above — same fixture, a plane that
    answers, so a refusal here would mean the guard fires on the wrong condition."""
    import argparse

    from claudlobby.commands.core import cmd_brief

    fleet_dir = paths.root / "local" / "f1"
    (fleet_dir / "runtime").mkdir(parents=True)
    _write_fleet_yaml(fleet_dir, "f1", ["alex"])
    _seed_plane_for(paths, "f1")
    _land_report(paths, _report("vera", "2026-08-08T10:00:00Z"), fleet="f1")

    args = argparse.Namespace(
        fleet="f1", root=str(paths.root), seed=False, bot="alex", json=False, ack=True
    )
    assert cmd_brief(args) == 0
    (alias, severity, detail), = _acked_events(paths)
    assert alias == "bot:f1/alex" and json.loads(detail)["count"] == 1


# --- the omit suppresses true positives too, and must say how many ------------


def test_an_omitted_dispatch_section_carries_no_count_and_says_unavailable(paths: Paths):
    """The old ledger omission counted the past-deadline rows it could not
    adjudicate (the dispatch log was still readable while the report ledger
    was not). With the plane there is no half-readable state: unreachable
    means every list is unknown, so the entries carry no count and the render
    says unavailable — never a reassuring 0."""
    assert not (paths.root / "state" / "plane" / "plane.db").exists()
    brief = build_brief(_fleet(), paths, "alex", NOW)
    for e in _find(brief, "dispatches.open", "#1467") + _find(brief, "dispatches.overdue", "#1467"):
        assert e["count"] is None
    assert "(unavailable — see DEGRADED)" in format_brief(brief)


def test_every_degradation_carries_the_count_key(paths: Paths):
    """R4 reads this envelope; an absent key and a null one are different bugs."""
    _seed_plane(paths)
    for d in build_brief(_fleet(), paths, "alex", NOW)["degraded"]:
        assert "count" in d, d
# --- #1102 R3 / M1: the boot payload (locked fork R3-F1, O-B+r) ---------------


class TestBootProvenance:
    """boot_provenance() — the door-side facts rule 2 renders, from the PLANE
    (F18 R2b). Interim for #1122; the helper is deleted when the envelope
    carries these facts."""

    def test_counts_dispatches_ever_and_24h(self, paths: Paths):
        _land_all(paths, [
            _dispatch("alex", NOW - 90_000, NOW - 89_000, task_id="t-old"),
            _dispatch("alex", NOW - 100, NOW + 500, task_id="t-new"),
        ])
        prov = boot_provenance(paths, NOW)
        assert prov["dispatches"]["state"] == "ok"
        assert prov["dispatches"]["rows_ever"] == 2
        assert prov["dispatches"]["rows_24h"] == 1

    def test_an_unreachable_plane_is_state_not_zero(self, paths: Paths):
        assert not (paths.root / "state" / "plane" / "plane.db").exists()
        prov = boot_provenance(paths, NOW)
        assert prov["dispatches"]["state"] == "unreachable"
        assert "rows_ever" not in prov["dispatches"]
        assert prov["registry"]["present"] is False and "entries" not in prov["registry"]

    def test_registry_entries_come_from_the_plane(self, paths: Paths):
        _seed_plane(paths)
        prov = boot_provenance(paths, NOW)
        assert prov["registry"] == {"present": True, "entries": 0}
        _land_ws(paths, "ws-a", opened_ts="1970-01-20T00:00:00Z")
        assert boot_provenance(paths, NOW)["registry"]["entries"] == 1


class TestBootRender:
    """format_boot_brief() — the locked O-B+r payload. The empty-state line is
    the point (fork R3-F1); mission never renders; caps are token-enforced
    with disclosed overflow."""

    def _brief(self, paths_: Paths, **ledgers):
        rows = ledgers.get("dispatches", [])
        _land_all(paths_, rows)
        if not rows:
            _seed_plane(paths_)                       # a plane that knows the fleet, nothing open
        return build_brief(_fleet(), paths_, "alex", NOW)

    def test_all_quiet_renders_provenance_never_bare_zero(self, paths: Paths):
        brief = self._brief(paths, dispatches=[], reports=[])
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert "all quiet" in out
        assert "0 open" in out
        # the provenance clause, from the plane
        assert "plane: 0 dispatches ever, 0 in 24h" in out
        assert "registry: 0 entries" in out
        assert "claudlobby brief --bot alex" in out  # the door line
        # never a bare zero: the quiet line must carry its provenance clause
        for line in out.splitlines():
            if "0 open" in line:
                assert "plane" in line

    def test_busy_case_prioritizes_orphaned_then_overdue_then_open(
        self, paths: Paths
    ):
        rows = [
            _dispatch("alex", NOW - 5_000, NOW + 5_000, task_id="t-open-a"),
            _dispatch("alex", NOW - 4_000, NOW + 5_000, task_id="t-open-b"),
            _dispatch("alex", NOW - 3_000, NOW - 1_000, task_id="t-late"),
        ]
        brief = self._brief(paths, dispatches=rows, reports=[])
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert "t-late" in out
        # the door's open list is deadline-blind (a SUPERSET, #904), so the
        # overdue row counts there too; the boot payload keeps door semantics
        assert "3 open" in out and "1 overdue" in out
        assert out.count("t-late") == 1  # but each task renders once
        assert "full state: claudlobby brief --bot alex" in out

    def test_detail_cap_three_with_disclosed_overflow(self, paths: Paths):
        rows = [
            _dispatch("alex", NOW - (i * 100), NOW + 9_000, task_id=f"t-{i:02d}")
            for i in range(7)
        ]
        brief = self._brief(paths, dispatches=rows, reports=[])
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        detail = [ln for ln in out.splitlines() if " — sent " in ln]
        assert len(detail) == 3
        assert "+4 more" in out and "door" in out

    def test_token_cap_enforced_with_disclosure_kept(self, paths: Paths):
        rows = [
            _dispatch(
                "alex",
                NOW - (i * 10),
                NOW + 9_000,
                task_id=f"t-{'x' * 60}-{i:03d}",
            )
            for i in range(40)
        ]
        brief = self._brief(paths, dispatches=rows, reports=[])
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert len(out) <= BOOT_CHAR_BUDGET
        assert "more" in out and "door" in out  # overflow disclosure survived
        assert "full state: claudlobby brief" in out  # door line survived

    def test_omitted_dispatches_render_unavailable_not_zero(self, paths: Paths):
        # No plane under the root -> the door omits the dispatch section (#1467).
        assert not (paths.root / "state" / "plane" / "plane.db").exists()
        brief = build_brief(_fleet(), paths, "alex", NOW)
        assert not brief["dispatches"]
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert "UNAVAILABLE" in out
        assert "0 open" not in out
        assert "all quiet" not in out

    def test_mission_never_renders_in_boot_payload(self, paths: Paths):
        brief = self._brief(paths, dispatches=[], reports=[])
        assert brief["mission"]  # the envelope HAS it
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert "MISSION" not in out and "mission" not in out

    def test_labeled_degradation_marks_the_dispatch_line(self, paths: Paths):
        _seed_plane(paths)
        shutil.rmtree(paths.runtime_bots)                 # the orphan list labeled (#1014)
        brief = build_brief(_fleet(), paths, "alex", NOW)
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert "#1014" in out


class TestBootCLI:
    def _args(self, root, **kw):
        import argparse

        base = dict(
            fleet="f1",
            root=str(root),
            seed=False,
            bot="alex",
            json=False,
            ack=False,
            boot=True,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def _fleet_dir(self, paths_: Paths):
        fleet_dir = paths_.root / "local" / "f1"
        (fleet_dir / "runtime").mkdir(parents=True)
        (fleet_dir / "fleet.yaml").write_text(
            "fleet:\n  name: f1\n  service_prefix: com.test\n"
            "  bots:\n    alex:\n      expertise: [software-engineering]\n"
        )
        return fleet_dir

    def test_boot_flag_renders_quiet_payload_end_to_end(self, paths: Paths, capsys, monkeypatch):
        from claudlobby.commands.core import cmd_brief

        monkeypatch.setenv("CLAUDLOBBY_FLEET", "f1")
        fleet_dir = self._fleet_dir(paths)
        _seed_plane_for(paths, "f1")
        assert cmd_brief(self._args(paths.root)) == 0
        out = capsys.readouterr().out
        assert "all quiet" in out
        assert "full state: claudlobby brief --bot alex" in out

    def test_boot_is_mutually_exclusive_with_json_and_ack(self, paths: Paths):
        from claudlobby.commands.core import cmd_brief

        self._fleet_dir(paths)
        assert cmd_brief(self._args(paths.root, json=True)) == 1
        assert cmd_brief(self._args(paths.root, ack=True)) == 1


class TestUnlistableBotsDir:
    """brief's own contract is that it never serves a number it knows is wrong.

    An unlistable runtime/bots is the dir-source twin of an unreachable plane:
    ``is_dir()`` passes, then iteration fails (#1227 review follow-on).
    """

    def test_orphans_are_omitted_and_disclosed_not_reported_as_none(self, paths):
        """'no orphans' and 'could not look' have opposite remedies."""
        import os as _os

        from claudlobby.brief import _dispatch_section, load_dispatch_doors

        if _os.geteuid() == 0:
            pytest.skip("root ignores the mode bits")
        _seed_plane(paths)
        doors = load_dispatch_doors(paths)
        bots = paths.runtime_bots
        bots.chmod(0o000)
        try:
            degraded: list = []
            _dispatch_section(doors, paths, "alex", 1787000000, degraded)
            assert degraded, "an unlistable bots dir must be disclosed, not silent"
        finally:
            bots.chmod(0o755)


class TestAlertsReadThePlane:
    """The alerts section reads the plane and nothing else (F18 R2b): no flag,
    no bots dir, no event files. A plane that cannot answer is OMITTED — an
    empty list would mean "could not look", not "nothing is wrong" — and a
    plane that holds the fleet with no critical event is a real zero.
    """

    def test_an_unreachable_plane_is_omitted_and_says_so(self, paths):
        from claudlobby.brief import _alerts_section

        assert not (paths.root / "state" / "plane" / "plane.db").exists()
        degraded: list = []
        out = _alerts_section(paths, "alex", 1787000000, degraded)
        assert out == []
        omitted = [d for d in degraded if d.field == "alerts" and d.mode == "omitted"]
        assert omitted and omitted[0].issue == "#1467" and "cannot answer" in omitted[0].reason

    def test_a_plane_that_holds_the_fleet_is_a_real_zero(self, paths):
        """The positive control, and the line brief draws everywhere else:
        presence, not emptiness."""
        from claudlobby.brief import _alerts_section

        _seed_plane(paths)
        degraded: list = []
        out = _alerts_section(paths, "alex", 1787000000, degraded)
        assert out == []
        assert not [d for d in degraded if d.field == "alerts" and d.mode == "omitted"], (
            f"a plane that holds the fleet was wrongly omitted: {[d.reason for d in degraded]}"
        )


def test_build_brief_opens_the_plane_once_for_every_section(paths: Paths, monkeypatch):
    """One session for the dispatches, workstreams, reports and alerts sections
    (a brief once opened the plane five times and exec'd the readers six — the
    R2b-1 simplify lens)."""
    _seed_plane(paths)
    import claudlobby.brief as brief_mod

    real = brief_mod.load_dispatch_doors
    opened: list[int] = []

    class _Counting:
        def __init__(self, mod):
            self._mod = mod

        def __getattr__(self, name):
            return getattr(self._mod, name)

        def open_plane(self, *a, **k):
            opened.append(1)
            return self._mod.open_plane(*a, **k)

    monkeypatch.setattr(brief_mod, "load_dispatch_doors", lambda p: _Counting(real(p)))
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert "open" in brief["dispatches"] and "unacked" in brief["reports"] and "active" in brief["workstreams"]
    assert len(opened) == 1, opened
