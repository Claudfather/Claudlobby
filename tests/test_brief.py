"""Tests for `claudlobby brief` and its canonical work projection.

Two properties carry most of the weight here and are worth naming, because a
test that only checked "the section rendered" would pass while either was
broken:

  1. **Lifecycle comes from the task reducer and timing from the watchdog.**
     Every item names canonical task and assignment IDs. Overdue, grace and
     restart status remain assignment-keyed observations, never lifecycle.
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

from tests.plane_setup import initialize_plane

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from claudlobby.brief import (
    BOOT_CHAR_BUDGET,
    SCHEMA_VERSION,
    boot_provenance,
    build_brief,
    format_boot_brief,
    format_brief,
    ack_request,
    load_dispatch_doors,
)
from claudlobby.config import BotConfig, FleetConfig, ProjectConfig, ScopeConfig
from tests.package_fixtures import source_package
from tests.test_activation import cold, tmp_path  # noqa: F401 — selected activation and short paths
from tests.test_releases import installed  # noqa: F401 — cold fixture dependency
from tests.test_task_write_cli import active  # noqa: F401 — real selected activation fixture
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
        manager="ari",
        service_prefix="com.test",
        bots={"alex": bot, "ari": BotConfig(bot_id="ari", name="Ari", expertise=[])},
        mission="Ship things that earn their keep.",
    )
    base.update(kw)
    return FleetConfig(**base)


@pytest.fixture
def root(tmp_path: Path) -> Path:  # noqa: F811 — imported short-path fixture
    """A claudlobby root with the REAL dispatch matcher in claudlobby/_runtime_scripts/ (and the
    stdlib plane readers it imports beside itself).

    Copied rather than stubbed: the point of the dispatch assertions is that
    the brief and the watchdog share one implementation, which a stub would
    quietly sever.
    """
    (tmp_path / "lib").mkdir()
    # The native report reader resolves its codec from the package that holds
    # its realpath, so link the real scripts rather than copying them: the
    # fixture directory stays mutable (a case unlinks the matcher) while every
    # reader binds to this exact source package.
    for name in ("dispatch-overdue.py", "plane-readers.py", "plane-lookup.py"):
        (tmp_path / "lib" / name).symlink_to(REPO_ROOT / "claudlobby/_runtime_scripts" / name)
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "capture.json").write_text('{"*": "full"}')   # bodies kept, as on the estate
    (tmp_path / "runtime" / "fleet").mkdir(parents=True)
    (tmp_path / "runtime" / "bots" / "alex" / "data").mkdir(parents=True)
    spawn = tmp_path / "runtime" / "bots" / "alex" / "data" / ".spawn"
    spawn.touch()
    os.utime(spawn, (0, 0))  # Older than every fixture dispatch; non-orphan baseline.
    return tmp_path


@pytest.fixture
def paths(root: Path, monkeypatch, scratch_plane_env) -> Paths:
    for key, value in scratch_plane_env(root).items():
        monkeypatch.setenv(key, value)
    # The missing-matcher case deletes a native file. Select the fixture's
    # copies explicitly so the shared source package remains immutable.
    package = replace(source_package(), native=root / "lib")
    return Paths(root=root, fleet_dir=None, package=package)


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


def _land(paths: Paths, row: dict, *, fleet: str = FLEET,
          title: str = "t") -> tuple[str, str]:
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
        fleet=fleet, ref=None if row.get("task_id") else f"dispatch-log:sha:{n:0>32}",
        title=title)
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
    initialize_plane(paths.root)
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
    initialize_plane(paths.root)
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
    initialize_plane(paths.root)
    out = emit_batch(paths.root, [{
        "event_type": "system", "emitter": "test", "fleet": FLEET,
        "payload": {"event": "keepalive_skip", "subject_kind": "actor", "subject": f"bot:{FLEET}/alex",
                    "data": {"source": "test", "legacy_ts": "2026-05-27T10:00:00Z", "data": {}}}}])
    assert out[0].status == "committed", out


def _dispatch_ctx(paths: Paths) -> dict:
    return {"fleet": FLEET, "root": str(paths.root)}


def _seed_plane_for(paths: Paths, fleet: str) -> None:
    from claudlobby.plane.emit_api import emit_batch
    initialize_plane(paths.root)
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


def test_brief_json_schema_v2(paths: Paths):
    _seed_plane(paths)                       # every section is served from the plane
    brief = build_brief(_fleet(), paths, "alex", NOW)

    assert brief["schema"] == SCHEMA_VERSION
    assert brief["bot"] == "alex"
    assert brief["fleet"] == "test-fleet"
    for key in (
        "generated_at",
        "mission",
        "work",
        "workstreams",
        "reports",
        "alerts",
        "degraded",
    ):
        assert key in brief, f"envelope is missing {key}"
    assert set(brief["work"]) == {"scope", "items", "issues"}
    assert "dispatches" not in brief
    # alex is a worker (the fleet's manager is ari): the count and the list
    # command, never the rows (#2159); the manager's view also carries the rows.
    assert set(brief["reports"]) == {"cursor", "count", "list_command", "source"}
    manager = build_brief(_fleet(), paths, "ari", NOW)
    assert set(manager["reports"]) == {"cursor", "count", "list_command", "source", "unacked"}
    # Round-trips as JSON — R4 consumes this envelope, not the text form.
    json.dumps(brief)


def test_mission_carries_pointers_not_inlined_charters(paths: Paths, tmp_path: Path):  # noqa: F811
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


# --- canonical work with assignment-keyed attention ---------------------------


def test_worker_work_uses_canonical_ids_and_watchdog_attention(paths: Paths):
    late, late_asg = _land(
        paths, _dispatch("alex", NOW - 9000, NOW - 3000, task_id="old-late"))
    early, early_asg = _land(
        paths, _dispatch("alex", NOW - 500, NOW + 5000, task_id="old-early"))
    other, _ = _land(
        paths, _dispatch("ari", NOW - 9000, NOW - 3000, task_id="old-other"))

    work = build_brief(_fleet(), paths, "alex", NOW)["work"]
    assert work["scope"] == "assigned"
    assert [item["task_id"] for item in work["items"]] == [late, early]
    assert other not in {item["task_id"] for item in work["items"]}
    assert [item["assignment"]["assignment_id"] for item in work["items"]] == [
        late_asg, early_asg]
    assert [item["historical_references"] for item in work["items"]] == [
        ["old-late"], ["old-early"]]
    assert [item["attention"]["status"] for item in work["items"]] == [
        "overdue", "not_due"]
    assert [item["attention"]["past_due"] for item in work["items"]] == [
        True, False]


def test_progress_grace_does_not_close_canonical_work(paths: Paths):
    task_id, asg = _land(
        paths, _dispatch("alex", NOW - 9000, NOW - 3000, task_id="old-progress"))
    _land_report(paths, _report("alex", _iso(NOW - 100), status="progress",
                                task_id="old-progress"))
    item = build_brief(_fleet(), paths, "alex", NOW)["work"]["items"][0]
    assert item["task_id"] == task_id
    assert item["assignment"]["assignment_id"] == asg
    assert item["attention"]["past_due"] is True
    assert item["attention"]["status"] == "past_due"
    assert item["attention"]["reason"] == "progress_grace"


def test_respawn_marks_the_canonical_assignment_orphaned(paths: Paths):
    task_id, assignment_id = _land(
        paths, _dispatch("alex", NOW - 9000, NOW - 3000, task_id="old-orphan"))
    spawn = paths.runtime_bots / "alex" / "data" / ".spawn"
    os.utime(spawn, (NOW - 100, NOW - 100))
    brief = build_brief(_fleet(), paths, "alex", NOW)
    item = brief["work"]["items"][0]
    assert item["task_id"] == task_id
    assert item["assignment"]["assignment_id"] == assignment_id
    assert item["attention"]["status"] == "orphaned"
    assert task_id in format_boot_brief(brief, boot_provenance(paths, NOW))


# --- #2044: each open row's TEXT --------------------------------------------
# After a respawn a worker's brief named its rows by id alone, so nothing said
# what they asked. The work item's title is the dispatch text: whole in --json,
# clipped in text exactly as the re-check digest clips it, and never a blank.

LONG_TITLE = (
    "Fix the pane send lock: hold one sender per recipient pane across the whole send,\n"
    "including the verify and the repair, then report with the task id and the head."
) * 2


def _row_line(text: str, task_id: str) -> str:
    return next(line for line in text.splitlines()
                if task_id in line and "assignment" not in line)


def test_json_work_rows_carry_the_whole_title(paths: Paths):
    task_id, _ = _land(paths, _dispatch("alex", NOW - 500, NOW + 5000, task_id="t-long"),
                       title=LONG_TITLE)
    item = build_brief(_fleet(), paths, "alex", NOW)["work"]["items"][0]
    assert item["task_id"] == task_id
    assert item["title"] == LONG_TITLE


def test_text_work_rows_clip_the_title_as_the_recheck_digest_does(paths: Paths):
    from claudlobby.task_recheck import _clip
    task_id, _ = _land(paths, _dispatch("alex", NOW - 500, NOW + 5000, task_id="t-long"),
                       title=LONG_TITLE)
    text = format_brief(build_brief(_fleet(), paths, "alex", NOW))
    assert _clip(LONG_TITLE) in _row_line(text, task_id)
    assert LONG_TITLE.splitlines()[1] not in text  # one line per row, never the whole text


def test_boot_brief_names_each_open_row_by_its_text(paths: Paths):
    from claudlobby.task_recheck import _clip
    task_id, _ = _land(paths, _dispatch("alex", NOW - 9000, NOW - 3000, task_id="t-orph"),
                       title=LONG_TITLE)
    os.utime(paths.runtime_bots / "alex" / "data" / ".spawn", (NOW - 100, NOW - 100))
    brief = build_brief(_fleet(), paths, "alex", NOW)
    boot = format_boot_brief(brief, boot_provenance(paths, NOW))
    assert _clip(LONG_TITLE) in next(line for line in boot.splitlines() if task_id in line)


def test_three_titled_rows_still_fit_the_boot_budget(paths: Paths):
    from claudlobby.brief import BOOT_CHAR_BUDGET
    ids = [_land(paths, _dispatch("alex", NOW - 9000 + i, NOW - 3000, task_id=f"t-b{i}"),
                 title=LONG_TITLE)[0] for i in range(3)]
    os.utime(paths.runtime_bots / "alex" / "data" / ".spawn", (NOW - 100, NOW - 100))
    boot = format_boot_brief(build_brief(_fleet(), paths, "alex", NOW), boot_provenance(paths, NOW))
    assert all(task_id in boot for task_id in ids) and "more capped" not in boot
    assert len(boot) <= BOOT_CHAR_BUDGET


def test_a_row_with_no_recorded_title_says_so_and_is_degraded(paths: Paths, monkeypatch):
    # The contract refuses an empty title at ingest, so the title is blanked
    # after the read: this pins the render and the disclosure for a row whose
    # text is missing, whatever left it so, rather than a reachable ingest path.
    import claudlobby.task_state as task_state
    real = task_state.read_tasks

    def blanked(conn, **kw):
        snap = real(conn, **kw)
        return replace(snap, tasks=tuple(replace(t, title="") for t in snap.tasks))

    monkeypatch.setattr(task_state, "read_tasks", blanked)
    task_id, _ = _land(paths, _dispatch("alex", NOW - 500, NOW + 5000, task_id="t-blank"))
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert _find(brief, "work.title", "#2044")
    text = format_brief(brief)
    assert "(title not recorded)" in _row_line(text, task_id)
    boot = format_boot_brief(brief, boot_provenance(paths, NOW))
    assert "(title not recorded)" in next(line for line in boot.splitlines() if task_id in line)


def test_the_managers_fleet_view_carries_each_rows_text_too(paths: Paths):
    # #2044 asks for the text on the rows a manager dispatched as well as on a
    # worker's own. A manager's brief lists the fleet's whole intake, so the
    # same row reads the same way from the manager's side, in all three forms.
    from claudlobby.task_recheck import _clip
    task_id, _ = _land(paths, _dispatch("alex", NOW - 500, NOW + 5000, task_id="t-mgr"),
                       title=LONG_TITLE)
    brief = build_brief(_fleet(), paths, "ari", NOW)
    assert brief["work"]["scope"] == "fleet"
    assert [item["title"] for item in brief["work"]["items"]] == [LONG_TITLE]
    assert _clip(LONG_TITLE) in _row_line(format_brief(brief), task_id)
    boot = format_boot_brief(brief, boot_provenance(paths, NOW))
    assert _clip(LONG_TITLE) in next(line for line in boot.splitlines() if task_id in line)

def test_terminal_report_closes_canonical_work(paths: Paths):
    _land(paths, _dispatch("alex", NOW - 9000, NOW - 3000, task_id="old-done"))
    _land_report(paths, _report("alex", _iso(NOW - 100), task_id="old-done"))
    assert build_brief(_fleet(), paths, "alex", NOW)["work"]["items"] == []


def test_missing_matcher_omits_every_plane_section_rather_than_reporting_zero(paths: Paths):
    (paths.lib / "dispatch-overdue.py").unlink()
    _land(paths, _dispatch("alex", NOW - 9000, NOW - 3000, task_id="old-work"))
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["work"] == {} and brief["reports"] == {} and brief["workstreams"] == {}
    assert brief["alerts"] == []
    omitted = {x["field"] for x in brief["degraded"]
               if x["mode"] == "omitted" and x["issue"] == "#1467"}
    assert {"work", "reports", "alerts", "workstreams"} <= omitted
    assert "matcher" in _find(brief, "work", "#1467")[0]["reason"]
    text = format_brief(brief)
    assert "unavailable" in text and "ALERTS — critical events, last 24h (0)" not in text


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
    """Seed a historical ACK fact for read-side tests, without a second writer."""
    from claudlobby.plane.emit_api import emit_batch

    newest = max(unacked, key=lambda r: r["seq"] or 0)
    raw = ack_request(FLEET, bot, acked_through_seq=newest["seq"],
                      acked_through_ts=newest["ts"], count=len(unacked))
    return emit_batch(paths.root, [raw], require_commit=True)[0]


def test_brief_ack_is_a_plane_fact_and_the_unacked_list_shrinks(paths: Paths):
    """A historical `reports_acked` fact advances the shared read position by
    ingest ordering; no legacy cursor file exists anywhere."""
    _seed_plane(paths)
    for row in (
        _report("vera", "2026-08-08T10:00:00Z", status="completed"),
        _report("mason", "2026-08-08T11:00:00Z", status="blocked"),
        _report("vera", "2026-08-08T11:30:00Z", status="progress"),
    ):
        _land_report(paths, row)
    fleet = _fleet()

    brief = build_brief(fleet, paths, "ari", NOW)
    unacked = brief["reports"]["unacked"]
    # progress is not terminal — it closes nothing and acks nothing.
    assert [r["status"] for r in unacked] == ["completed", "blocked"]
    assert all(isinstance(r["seq"], int) for r in unacked)
    assert brief["reports"]["cursor"] is None

    out = _ack(paths, "ari", unacked)
    assert out.status == "committed", out
    again = build_brief(fleet, paths, "ari", NOW)
    assert again["reports"]["unacked"] == []
    assert again["reports"]["cursor"] == "2026-08-08T11:00:00Z"   # the legacy-form ts, for the render

    (alias, severity, detail), = _acked_events(paths)
    assert alias == f"bot:{FLEET}/ari" and severity == "notice"
    data = json.loads(detail)
    assert set(data) == {"acked_through_seq", "acked_through_ts", "count"}
    assert data["acked_through_seq"] == max(r["seq"] for r in unacked) and data["count"] == 2

    # a report landing after the ack reads unacked again
    _land_report(paths, _report("vera", "2026-08-08T12:00:00Z", status="completed"))
    assert [r["ts"] for r in build_brief(fleet, paths, "ari", NOW)["reports"]["unacked"]] == [
        "2026-08-08T12:00:00Z"]
    assert not list(paths.root.rglob("brief-cursor-*"))        # no JSON state, anywhere


def test_ack_is_per_viewer_on_the_plane(paths: Paths):
    """Each viewer's read position is its own actor's newest ack: the manager's
    ack clears its own list and leaves another viewer's count where it was."""
    _seed_plane(paths)
    _land_report(paths, _report("vera", "2026-08-08T10:00:00Z"))
    ari = build_brief(_fleet(), paths, "ari", NOW)["reports"]["unacked"]
    assert _ack(paths, "ari", ari).status == "committed"

    assert build_brief(_fleet(), paths, "ari", NOW)["reports"]["unacked"] == []
    assert build_brief(_fleet(), paths, "alex", NOW)["reports"]["count"] == 1


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
                        "subject": f"bot:{FLEET}/ari", "data": {"note": "no cursor here"}}}])[0].status

    _seed_plane(paths)
    _land_report(paths, _report("vera", "2026-08-08T10:00:00Z"))
    assert malformed() == "committed"
    reports = build_brief(_fleet(), paths, "ari", NOW)["reports"]
    assert len(reports["unacked"]) == 1 and reports["cursor"] is None

    assert _ack(paths, "ari", reports["unacked"]).status == "committed"
    assert build_brief(_fleet(), paths, "ari", NOW)["reports"]["unacked"] == []
    assert malformed() == "committed"
    again = build_brief(_fleet(), paths, "ari", NOW)["reports"]
    assert again["unacked"] == [] and again["cursor"] == "2026-08-08T10:00:00Z"


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
                    "recipient": f"bot:{FLEET}/ari", "recipient_raw": "ari",
                    "message_class": "report",
                    "body": "[BOTREPORT] zed | completed | cross-fleet done"}}])
    reports = build_brief(_fleet(), paths, "ari", NOW)["reports"]
    assert [(r["bot"], r["status"]) for r in reports["unacked"]] == [
        ("vera", "completed"), ("other/zed", "completed")]
    assert _ack(paths, "ari", reports["unacked"]).status == "committed"
    assert build_brief(_fleet(), paths, "ari", NOW)["reports"]["unacked"] == []


def test_no_cursor_file_is_written_or_read_anywhere():
    """The deletion, pinned: no door under claudlobby/ (runtime scripts included) names the file."""
    import subprocess
    out = subprocess.run(["grep", "-rn", "-E", "brief-cursor|read_cursor|write_cursor|cursor_path",
                          str(REPO_ROOT / "claudlobby")],
                         capture_output=True, text=True)
    assert out.returncode == 1 and out.stdout == "", out.stdout


def test_reports_are_fleet_wide_not_self_scoped(paths: Paths):
    """'What did my workers finish that I have not acted on' — not 'my own'."""
    _seed_plane(paths)
    _land_report(paths, _report("vera", "2026-08-08T10:00:00Z"))
    _land_report(paths, _report("mason", "2026-08-08T10:05:00Z"))
    bots = {
        r["bot"]
        for r in build_brief(_fleet(), paths, "ari", NOW)["reports"]["unacked"]
    }
    assert bots == {"vera", "mason"}


WORKER_REPORT = "an invented summary " * 15   # 300 bytes, near a real row's size


@pytest.mark.parametrize("manager", ["ari", "alex"])
def test_a_viewer_that_is_not_the_manager_gets_the_count_not_the_rows(paths: Paths, manager):
    """#2159: a worker never acknowledges reports, so the fleet's reports past its read
    position only grow, and every row came before its own work. Its view carries how
    many there are and the command that lists them, in a section whose size does not
    grow with them; the fleet's manager, read from the activated config, keeps the rows."""
    _seed_plane(paths)
    for n in range(40):   # under the manager's bound, so its view below is whole
        _land_report(paths, _report("vera", f"2026-08-08T10:{n:02d}:00Z", summary=WORKER_REPORT))
    fleet = _fleet(manager=manager)
    worker = "alex" if manager == "ari" else "ari"

    brief = build_brief(fleet, paths, worker, NOW)
    reports = brief["reports"]
    assert "unacked" not in reports
    assert reports["count"] == 40
    assert reports["list_command"] == "claudlobby --json fleet reports list --unacknowledged"
    assert len(json.dumps(reports)) < 300, "a worker's reports section grew with the reports"
    entry, = _find(brief, "reports.unacked", "#2159")
    assert entry["mode"] == "omitted" and entry["count"] == 40
    text = format_brief(brief)
    assert "REPORTS — unacked (40)" in text and "fleet reports list --unacknowledged" in text
    assert "an invented summary" not in text

    managed = build_brief(fleet, paths, manager, NOW)
    assert len(managed["reports"]["unacked"]) == managed["reports"]["count"] == 40
    assert _find(managed, "reports.unacked") == []


def test_the_managers_rows_stop_at_the_bound_and_say_so(paths: Paths, monkeypatch):
    """The manager's view keeps the oldest rows up to the bound (#2159): the count
    stays whole and the cut is labeled, so the rows never read as all of them."""
    import claudlobby.brief as brief_mod

    monkeypatch.setattr(brief_mod, "REPORT_ROW_LIMIT", 3, raising=False)
    _seed_plane(paths)
    for n in range(5):
        _land_report(paths, _report("vera", f"2026-08-08T10:0{n}:00Z"))
    brief = build_brief(_fleet(), paths, "ari", NOW)
    reports = brief["reports"]
    assert [r["ts"] for r in reports["unacked"]] == [
        "2026-08-08T10:00:00Z", "2026-08-08T10:01:00Z", "2026-08-08T10:02:00Z"]
    assert reports["count"] == 5
    entry, = _find(brief, "reports.unacked", "#2159")
    assert entry["mode"] == "labeled" and entry["count"] == 5
    assert "showing the oldest 3 of 5" in format_brief(brief)


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
    assert brief["workstreams"] == {"active": [], "stalled": [], "blocked": []}
    assert _find(brief, "workstreams") == []


def test_lease_window_follows_selected_fleet_config(paths: Paths):
    _seed_plane(paths)
    _land_ws(paths, "ws-x", opened_ts="1970-01-18T00:00:00Z")           # progress = opened, ~6 days before NOW
    assert (
        build_brief(_fleet(), paths, "alex", NOW)["workstreams"]["active"][0]["stalled"]
        is False
    )

    fleet = _fleet()
    fleet.workstreams.lease_days = 3
    assert (
        build_brief(fleet, paths, "alex", NOW)["workstreams"]["active"][0]["stalled"]
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
    assert brief["reports"]["count"] == 1


def test_alerts_are_labeled_as_the_bots_own(paths: Paths):
    """#2109: fleet- and host-level alerts are not read here, so absence of an
    alert is not evidence of health, and says so."""
    _seed_plane(paths)
    brief = build_brief(_fleet(), paths, "alex", NOW)

    entry = _find(brief, "alerts", "#2109")
    assert entry and entry[0]["mode"] == "labeled"
    assert "this bot's own critical events" in entry[0]["reason"]
    assert "fleet- and host-level alerts" in entry[0]["reason"]
    assert "absence of an alert is not evidence of health" in entry[0]["reason"]
    assert _find(brief, "alerts", "#903") == []    # the hand-list label is gone


def _losses(paths: Paths, *rows: str) -> None:
    plane = paths.root / "state" / "plane"
    plane.mkdir(parents=True, exist_ok=True)
    (plane / ".emit-losses").write_text("".join(f"{row}\n" for row in rows))


def test_a_known_emit_loss_labels_the_alerts_it_may_hide(paths: Paths):
    """#2165: an emit the plane did not record (here #2169's stage_empty) is a
    gap in what alerts can show, and only plane doctor read the counter. The
    brief, which a manager reads at every check-in, labels alerts with the
    count while a known loss sits in the 24 h window."""
    _seed_plane(paths)
    _losses(paths, f"{NOW - 60}\tstage_empty\t-\t.1-ev_x.batch.9.tmp",
            f"{NOW - 120}\treap\temit_fleet_event\tbound=10s")
    brief = build_brief(_fleet(), paths, "alex", NOW)

    entry = _find(brief, "alerts", "#2165")
    assert entry and entry[0]["mode"] == "labeled" and entry[0]["count"] == 1, entry
    assert "stage_empty" in entry[0]["reason"], entry
    assert "plane spool list --quarantined" in entry[0]["reason"], entry


def test_reaps_alone_do_not_label_the_alerts(paths: Paths):
    """A reaped emit's fate is unknown, and a loaded host reaps hundreds a day:
    a label that never clears gets read past, so reaps alone add none, and a
    known loss past the 24 h window adds none either."""
    _seed_plane(paths)
    _losses(paths, f"{NOW - 60}\treap\temit_fleet_event\tbound=10s",
            f"{NOW - 90000}\tstage_empty\t-\t.0-ev_old.batch.8.tmp")
    brief = build_brief(_fleet(), paths, "alex", NOW)

    assert _find(brief, "alerts", "#2165") == []


def test_an_unreadable_loss_counter_labels_the_alerts_unknown(paths: Paths):
    """A counter that cannot be read is a gap, not a zero."""
    _seed_plane(paths)
    (paths.root / "state" / "plane" / ".emit-losses").mkdir(parents=True)
    brief = build_brief(_fleet(), paths, "alex", NOW)

    entry = _find(brief, "alerts", "#2165")
    assert entry and entry[0]["mode"] == "labeled" and "unknown" in entry[0]["reason"], entry


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
    overlay = Paths(root=root, fleet_dir=fleet_dir,
                    package=replace(source_package(), native=root / "lib"))
    task_id, _ = _land(overlay, _dispatch("alex", NOW - 100, NOW + 100,
                                        task_id="t-1"), fleet="f1")

    brief = build_brief(_fleet(name="f1"), overlay, "alex", NOW)
    assert [r["task_id"] for r in brief["work"]["items"]] == [task_id]
    assert _find(brief, "work", "#526") == []


# --- rendering ----------------------------------------------------------------


def test_format_marks_degraded_sections_inline_and_lists_them(paths: Paths):
    _seed_plane(paths)
    text = format_brief(build_brief(_fleet(), paths, "alex", NOW))

    assert "ALERTS" in text and "[degraded: #2109]" in text
    assert "DEGRADED — fields this door will not serve as plain truth" in text
    assert "degraded field(s)" in text  # the top-of-output banner
    for section in ("MISSION", "WORK", "WORKSTREAMS", "REPORTS"):
        assert section in text


def test_a_matcher_predating_the_plane_only_reader_withholds_the_section(
    paths: Paths, monkeypatch
):
    """The matcher is the INSTALL's; one that predates the plane-only reader
    (F18 R2a) has ledger-era signatures that would raise out of a read-only
    command. The work section is withheld and its header carries the issue."""
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
    assert brief["work"] == {}
    omitted = {x["field"] for x in brief["degraded"] if x["mode"] == "omitted"}
    assert "work" in omitted
    entry = _find(brief, "work", "#1467")
    assert entry and "predates" in entry[0]["reason"]
    header = next(
        ln for ln in format_brief(brief).splitlines() if ln.startswith("WORK")
    )
    assert "#1467" in header


def test_a_failed_task_snapshot_withholds_work(paths: Paths, monkeypatch):
    """A failed canonical reducer must not turn queued work into an empty list."""
    _seed_plane(paths)
    from claudlobby.task_state import TaskStateError
    monkeypatch.setattr("claudlobby.task_state.read_tasks",
                        lambda *a, **k: (_ for _ in ()).throw(TaskStateError("unavailable")))
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["work"] == {}
    omitted = {x["field"] for x in brief["degraded"] if x["mode"] == "omitted"}
    assert "work" in omitted


def test_the_work_section_opens_the_plane_once(paths: Paths, monkeypatch):
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
    assert "items" in brief["work"] and "issues" in brief["work"]
    assert len(opened) == 1


def test_text_output_caps_long_sections_and_discloses_the_cap(paths: Paths):
    """Silent truncation reads as exhaustive coverage."""
    _seed_plane(paths)
    for n in range(25):
        _land_report(paths, _report("vera", f"2026-08-08T10:{n:02d}:00Z"))
    brief = build_brief(_fleet(), paths, "ari", NOW)
    text = format_brief(brief)

    # The manager's JSON carries every row up to its bound (#2159); the text shows 10.
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
    assert not hasattr(args, "ack")
    with pytest.raises(SystemExit) as rejected:
        parser.parse_args(["brief", "--ack"])
    assert rejected.value.code == 2


def test_overdue_honours_the_env_expiry_cap_like_the_cli(paths: Paths, monkeypatch):
    """The matcher's Python API defaults max_age; only its main() reads the env
    var. A brief that ignored it would disagree with the very watchdog it
    mirrors, and 'byte-consistent with --all' is the contract."""
    # ~2.8h old: past its deadline, but inside the 24h default expiry cap.
    task_id, _ = _land(paths, _dispatch("alex", NOW - 10_000, NOW - 5_000,
                                     task_id="t-old"))

    item = build_brief(_fleet(), paths, "alex", NOW)["work"]["items"][0]
    assert item["task_id"] == task_id
    assert item["attention"]["status"] == "overdue"

    # A fleet that tightens the cap ages the row out; the brief must follow.
    monkeypatch.setenv("DISPATCH_OVERDUE_MAX_AGE_S", "1000")
    brief = build_brief(_fleet(), paths, "alex", NOW)
    item = brief["work"]["items"][0]
    assert item["task_id"] == task_id  # expiry does not close canonical work
    assert item["attention"]["status"] == "past_due"
    assert item["attention"]["reason"] == "max_age_cap"


# --- consuming the shared doors defensively (#526 / #1014) ---------------------


def test_an_unreachable_plane_omits_work_rather_than_alarming(paths: Paths):
    """No plane under the root: the matcher REFUSES (rc 3 / PlaneUnreachable —
    never "nothing open"), and the brief OMITS the section with the remedy
    named rather than serving a zero as truth."""
    assert not (paths.root / "state" / "plane" / "plane.db").exists()

    doors = load_dispatch_doors(paths)
    with pytest.raises(doors.PlaneUnreachable):
        doors.overdue_all(NOW, **_dispatch_ctx(paths))       # precondition: the door refuses

    brief = build_brief(_fleet(), paths, "alex", NOW)
    entries = _find(brief, "work", "#1467")
    assert entries and all(e["mode"] == "omitted" for e in entries)
    assert all("plane" in e["reason"] and "state/plane/plane.db" in e["reason"] for e in entries)
    # never zero (a false all-clear): the section is not served, and says so
    assert brief["work"] == {}, "a false all-clear was served for an unreachable plane"
    text = format_brief(brief)
    assert "unavailable" in text


def test_a_plane_that_knows_the_fleet_but_holds_no_work_is_answered_not_omitted(paths: Paths):
    """The genuine "nothing open" state: the plane holds the fleet's bots (the
    registry rows every emission mints) and no dispatch — answered as empty
    lists, no omission."""
    _seed_plane(paths)
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["work"]["items"] == [] and brief["work"]["issues"] == []
    assert [d for d in _find(brief, "work") if d["mode"] == "omitted"] == []


def test_a_plane_that_never_saw_the_fleet_is_unreachable_not_empty(paths: Paths):
    """A schema-valid plane holding no bot of the named fleet is a wrong root or
    a fleet it never saw: refused, never read as "nothing open" (#1014's class)."""
    _land(paths, _dispatch("alex", NOW - 100, NOW + 100, task_id="t-1"), fleet="another-fleet")
    brief = build_brief(_fleet(), paths, "alex", NOW)
    entries = _find(brief, "work", "#1467")
    assert entries and entries[0]["mode"] == "omitted" and "holds no bot of fleet" in entries[0]["reason"]
    assert brief["work"] == {}


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
    entry = _find(brief, "work.attention", "#1014")
    assert entry and entry[0]["mode"] == "labeled"
    # Open/overdue are unaffected and still served.
    assert len(brief["work"]["items"]) == 1


def test_orphan_label_absent_when_the_bots_dir_exists(paths: Paths):
    _land(paths, _dispatch("alex", NOW - 100, NOW + 100, task_id="t-1"))
    assert _find(build_brief(_fleet(), paths, "alex", NOW), "work.attention", "#1014") == []


def _write_fleet_yaml(fleet_dir: Path, name: str, bots: list[str], *, manager: str) -> None:
    """A REAL fleet.yaml — ``bots:`` nests under ``fleet:``.

    Spelled out because getting it wrong is silent: a top-level ``bots:`` key
    parses fine and yields ZERO declared bots, so a brief returns not-found and
    any test asserting only on the exit code passes for
    entirely the wrong reason.
    """
    fleet_dir.mkdir(parents=True, exist_ok=True)
    (fleet_dir / "fleet.yaml").write_text(
        f"fleet:\n  name: {name}\n  manager: {manager}\n  service_prefix: com.test\n  bots:\n"
        + "".join(f"    {b}:\n      expertise: [software-engineering]\n" for b in bots)
    )


def test_selected_report_ack_consumes_only_served_prefix_and_replays(active, monkeypatch, capsys):  # noqa: F811
    """One selected-activation flow: a later report stays unread, and replay
    cannot advance the position or write a second ACK event."""
    from uuid import uuid4

    from claudlobby import brief
    from claudlobby.__main__ import main
    from claudlobby.paths import load_lib_module

    root, release = active
    # The activation fixture seals a minimal native artifact; give the brief
    # its real shared reader through its existing import seam, not a fake query.
    monkeypatch.setattr(brief, "load_dispatch_doors",
                        lambda paths: load_lib_module(source_package().native, "dispatch-overdue.py"))
    for key, value in {"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root),
                       "FLEET_NAME": "example", "BOT_ID": "manager",
                       "BOT_DIR": str(root / "runtime/bots/manager"),
                       "CLAUDLOBBY_RELEASE_ID": release.release_id}.items():
        monkeypatch.setenv(key, value)

    def call(*argv, expected=0):
        assert main(["--root", str(root), "--json", *argv]) == expected
        result = json.loads(capsys.readouterr().out)
        assert result["ok"] is (expected == 0)
        return result

    _land_report(Paths(root=root, fleet_dir=None, package=source_package()),
                 _report("worker", "2026-08-08T10:00:00Z"), fleet="example")
    shown = call("fleet", "reports", "list", "--unacknowledged", "--limit", "1")
    cursor = shown["data"]["ack_cursor"]
    first = shown["data"]["items"]
    assert len(first) == 1 and cursor
    _land_report(Paths(root=root, fleet_dir=None, package=source_package()),
                 _report("worker", "2026-08-08T11:00:00Z"), fleet="example")

    request_id = str(uuid4())
    argv = ("fleet", "reports", "ack", "--through", cursor, "--request-id", request_id)
    acknowledged = call(*argv)
    assert acknowledged["data"]["recording"] == "committed"
    assert acknowledged["data"]["count"] == 1
    assert acknowledged["data"]["request_persisted"] is True
    assert acknowledged["data"]["replayed"] is False
    assert len(_acked_events(Paths(root=root, fleet_dir=None, package=source_package()))) == 1
    remaining = call("fleet", "reports", "list", "--unacknowledged")["data"]["items"]
    assert len(remaining) == 1 and remaining[0]["message_id"] != first[0]["message_id"]

    replay = call(*argv)
    assert replay["data"]["replayed"] is True
    assert len(_acked_events(Paths(root=root, fleet_dir=None, package=source_package()))) == 1
    stale = call("fleet", "reports", "ack", "--through", cursor,
                 "--request-id", str(uuid4()), expected=4)
    assert stale["error"]["code"] == "conflict"
    monkeypatch.setenv("BOT_ID", "worker")
    monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots/worker"))
    foreign = call("fleet", "reports", "ack", "--through", cursor,
                   "--request-id", str(uuid4()), expected=4)
    assert foreign["error"]["code"] == "conflict"
    monkeypatch.setenv("BOT_ID", "manager")
    monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots/manager"))
    current = call("fleet", "reports", "list", "--unacknowledged")["data"]["ack_cursor"]
    assert current
    with monkeypatch.context() as outage:
        def refused(*args, **kwargs):
            raise OSError("private Plane unavailable")
        outage.setattr("claudlobby.plane.emit_api.emit_batch", refused)
        failed = call("fleet", "reports", "ack", "--through", current,
                      "--request-id", str(uuid4()), expected=6)
    assert failed["error"]["code"] == "unavailable"
    assert len(_acked_events(Paths(root=root, fleet_dir=None, package=source_package()))) == 1


# --- the omit suppresses true positives too, and must say how many ------------


def test_an_omitted_work_section_carries_no_count_and_says_unavailable(paths: Paths):
    """The old ledger omission counted the past-deadline rows it could not
    adjudicate (the dispatch log was still readable while the report ledger
    was not). With the plane there is no half-readable state: unreachable
    means every list is unknown, so the entries carry no count and the render
    says unavailable — never a reassuring 0."""
    assert not (paths.root / "state" / "plane" / "plane.db").exists()
    brief = build_brief(_fleet(), paths, "alex", NOW)
    for e in _find(brief, "work", "#1467"):
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

    def test_counts_tasks_ever_and_24h(self, paths: Paths):
        _land_all(paths, [
            _dispatch("alex", NOW - 90_000, NOW - 89_000, task_id="t-old"),
            _dispatch("alex", NOW - 100, NOW + 500, task_id="t-new"),
        ])
        prov = boot_provenance(paths, NOW)
        assert prov["work"]["state"] == "ok"
        assert prov["work"]["tasks_ever"] == 2
        assert prov["work"]["tasks_24h"] == 1

    def test_an_unreachable_plane_is_state_not_zero(self, paths: Paths):
        assert not (paths.root / "state" / "plane" / "plane.db").exists()
        prov = boot_provenance(paths, NOW)
        assert prov["work"]["state"] == "unreachable"
        assert "tasks_ever" not in prov["work"]
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

    def test_no_open_tasks_renders_provenance_never_bare_zero(self, paths: Paths):
        brief = self._brief(paths, dispatches=[], reports=[])
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert "no open tasks for this bot" in out
        # the provenance clause, from the plane
        assert "plane: 0 tasks ever, 0 in 24h" in out
        assert "registry: 0 entries" in out
        assert "claudlobby brief --bot alex" in out  # the door line
        assert "current escalations and unseen reports: claudlobby fleet inbox" in out
        # The no-work line must carry its provenance clause.
        for line in out.splitlines():
            if "no open tasks" in line:
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
        late = next(item for item in brief["work"]["items"]
                    if item["historical_references"] == ["t-late"])
        assert late["task_id"] in out
        # Canonical open work remains visible even when one row is overdue.
        assert "3 open" in out and "1 overdue" in out
        assert out.count(late["task_id"]) == 1
        assert "full state: claudlobby brief --bot alex" in out

    def test_detail_cap_three_with_disclosed_overflow(self, paths: Paths):
        rows = [
            _dispatch("alex", NOW - (i * 100), NOW + 9_000, task_id=f"t-{i:02d}")
            for i in range(7)
        ]
        brief = self._brief(paths, dispatches=rows, reports=[])
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        detail = [ln for ln in out.splitlines() if " — " in ln and " old" in ln]
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

    def test_omitted_work_renders_unavailable_not_zero(self, paths: Paths):
        # No plane under the root -> the door omits the dispatch section (#1467).
        assert not (paths.root / "state" / "plane" / "plane.db").exists()
        brief = build_brief(_fleet(), paths, "alex", NOW)
        assert not brief["work"]
        out = format_boot_brief(brief, boot_provenance(paths, NOW))
        assert "UNAVAILABLE" in out
        assert "0 open" not in out
        assert "no open tasks for this bot" not in out

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
    def test_selected_viewer_envelope_and_boot_are_read_only(self, active, capsys, monkeypatch):
        import sqlite3
        from datetime import datetime, timezone

        from claudlobby import brief
        from claudlobby import env_tiers
        from claudlobby.__main__ import main
        from claudlobby.isolation import transcript_slug
        from claudlobby.paths import load_lib_module
        from claudlobby.plane.db import db_file

        root, release = active
        # The selected fixture seals a minimal native artifact. Use the real
        # shared source reader through the established brief import seam.
        monkeypatch.setattr(brief, "load_dispatch_doors",
                            lambda paths: load_lib_module(source_package().native,
                                                           "dispatch-overdue.py"))
        monkeypatch.setattr(env_tiers, "resolve", lambda paths, bot_name=None, fleet_name=None: {})
        with sqlite3.connect(db_file(root)) as conn:
            before = (conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0],
                      conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0])

        assert main(["--root", str(root), "--json", "brief"]) == 0
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["schema_version"] == 1 and envelope["command"] == "brief"
        assert envelope["request_id"] is None and envelope["release_id"] == release.release_id
        assert envelope["data"]["viewer_selection"] == "manager_default"
        assert envelope["data"]["brief"]["schema"] == 2
        assert envelope["data"]["brief"]["bot"] == "manager"
        assert envelope["data"]["brief"]["work"]["items"] == []
        assert "usage" not in envelope["data"]["brief"]
        assert "context" not in envelope["data"]["brief"]  # an ordinary brief reads no transcript

        transcript_dir = (Path.home() / ".claude/projects" /
                          transcript_slug(root / "runtime/bots/manager"))
        transcript_dir.mkdir(parents=True)
        (transcript_dir / "session.jsonl").write_text(json.dumps({
            "type": "assistant", "sessionId": "brief-session",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {"id": "brief-message", "model": "claude-test", "usage": {
                "input_tokens": 11, "output_tokens": 7,
                "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5}},
        }) + "\n")
        assert main(["--root", str(root), "--json", "brief", "--usage-since", "24h"]) == 0
        read = json.loads(capsys.readouterr().out)["data"]["brief"]
        with_usage = read["usage"]
        assert [with_usage["usage"][key] for key in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens")] == [11, 7, 3, 5]
        assert with_usage["coverage"]["status"] == "observed"
        assert with_usage["quota"]["status"] == "unavailable"
        # #2206: the same reader gives the viewer's live context beside it.
        assert (read["context"]["tokens"], read["context"]["reason"]) == (19, None)  # 11 + 3 + 5
        assert main(["--root", str(root), "brief", "--usage-since", "24h"]) == 0
        text = capsys.readouterr().out
        assert "USAGE — Claude transcript token counts" in text
        assert "context now: 19 tokens" in text
        assert main(["--root", str(root), "--json", "brief", "--usage-since", "8d"]) == 2
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_argument"
        assert main(["--root", str(root), "brief", "--boot", "--usage-since", "24h"]) == 2
        assert "--usage-since is for the full brief" in capsys.readouterr().err

        for key, value in {"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root),
                           "FLEET_NAME": "example", "BOT_ID": "worker",
                           "BOT_DIR": str(root / "runtime/bots/worker")}.items():
            monkeypatch.setenv(key, value)
        assert main(["--root", str(root), "--json", "brief"]) == 0
        generated = json.loads(capsys.readouterr().out)
        assert generated["data"]["brief"]["bot"] == "worker"
        assert generated["data"]["viewer_selection"] == "generated"
        assert main(["--root", str(root), "--json", "brief", "--bot", "manager"]) == 0
        projected = json.loads(capsys.readouterr().out)
        assert projected["data"]["brief"]["bot"] == "manager"
        assert projected["data"]["viewer_selection"] == "explicit"
        for key in ("CLAUDLOBBY_ROOT", "FLEET_ROOT", "FLEET_NAME", "BOT_ID", "BOT_DIR"):
            monkeypatch.delenv(key, raising=False)

        assert main(["--root", str(root), "brief", "--bot", "worker", "--boot"]) == 0
        output = capsys.readouterr().out
        assert "full state: claudlobby brief --bot worker" in output
        assert "current escalations and unseen reports: claudlobby fleet inbox" in output
        assert main(["--root", str(root), "--json", "brief", "--bot", "outside"]) == 3
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"
        assert main(["--root", str(root), "--json", "brief", "--boot"]) == 2
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_argument"
        with sqlite3.connect(db_file(root)) as conn:
            after = (conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0],
                     conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0])
        assert after == before

        # #1973: refusals name a remedy, and only the selection race is retryable.
        from claudlobby import activation_state
        real_read = activation_state.read_selection
        reads = []

        def racing(path):
            reads.append(path)
            value = real_read(path)
            return value if len(reads) == 1 else {**value, "release_id": "r-" + "e" * 64}

        with monkeypatch.context() as patch:
            patch.setattr(activation_state, "read_selection", racing)
            assert main(["--root", str(root), "--json", "brief"]) == 4
        raced = json.loads(capsys.readouterr().out)["error"]
        assert raced["retryable"] is True and raced["hint"] == "retry the same read"
        with monkeypatch.context() as patch:
            patch.setenv("FLEET_NAME", "")
            assert main(["--root", str(root), "--fleet", "example", "--json", "brief"]) == 2
        empty = json.loads(capsys.readouterr().out)["error"]
        assert empty["retryable"] is False and "FLEET_NAME" in empty["hint"]

        # The alias still looks like this manager, but its Plane actor no
        # longer matches the frozen activation binding. Never render it.
        with sqlite3.connect(db_file(root)) as conn:
            conn.execute("UPDATE identity_registry SET uid=? "
                         "WHERE kind='actor' AND alias='bot:example/manager'",
                         ("actor_" + "f" * 32,))
        assert main(["--root", str(root), "--json", "brief"]) == 4
        mismatched = json.loads(capsys.readouterr().out)
        assert mismatched["error"]["code"] == "conflict"
        assert mismatched["data"] == {} and mismatched["release_id"] == release.release_id


class TestUnlistableBotsDir:
    """brief's own contract is that it never serves a number it knows is wrong.

    An unlistable runtime/bots is the dir-source twin of an unreachable plane:
    ``is_dir()`` passes, then iteration fails (#1227 review follow-on).
    """

    def test_orphans_are_omitted_and_disclosed_not_reported_as_none(self, paths):
        """'no orphans' and 'could not look' have opposite remedies."""
        import os as _os

        from claudlobby.brief import _work_section, load_dispatch_doors, plane_session

        if _os.geteuid() == 0:
            pytest.skip("root ignores the mode bits")
        _seed_plane(paths)
        doors = load_dispatch_doors(paths)
        bots = paths.runtime_bots
        bots.chmod(0o000)
        try:
            degraded: list = []
            plane, note = plane_session(paths)
            assert plane is not None, note
            with plane:
                _work_section(doors, paths, _fleet(), "alex", 1787000000,
                              degraded, plane=plane)
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
    """One session for the work, workstreams, reports and alerts sections
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
    assert "items" in brief["work"] and "count" in brief["reports"] and "active" in brief["workstreams"]
    assert len(opened) == 1, opened


# --- fleet-owned intake, canonical identity, and unresolved history ----------


def _fleet_with_manager() -> FleetConfig:
    return _fleet(
        manager="mgr",
        bots={
            "alex": BotConfig(bot_id="alex", name="Alex", expertise=["software-engineering"]),
            "mgr": BotConfig(bot_id="mgr", name="Mgr", expertise=[]),
        },
    )


def test_manager_sees_queued_and_assigned_fleet_work_worker_sees_own(paths: Paths):
    from claudlobby.plane.db import db_file
    from tests.test_task_state import _assignment, _task
    import sqlite3

    assigned_task, assigned_asg = _land(
        paths, _dispatch("alex", NOW - 9000, NOW - 3000, task_id="old-assigned"))
    with sqlite3.connect(db_file(paths.root)) as conn:
        uid = load_dispatch_doors(paths)._plane_readers().fleet_uid(conn, FLEET)
        _task(conn, "wi_queued", fleet_uid=uid)
        conn.execute("UPDATE work_items SET project_key=NULL, workstream_id=NULL "
                     "WHERE work_item_id='wi_queued'")
        _assignment(conn, "asg_orphan", "wi_missing", fleet_uid=uid)

    fleet = _fleet_with_manager()
    manager = build_brief(fleet, paths, "mgr", NOW)["work"]
    assert manager["scope"] == "fleet"
    assert {item["task_id"] for item in manager["items"]} == {
        assigned_task, "wi_queued"}
    assigned = next(item for item in manager["items"] if item["task_id"] == assigned_task)
    queued = next(item for item in manager["items"] if item["task_id"] == "wi_queued")
    assert assigned["assignment"]["assignment_id"] == assigned_asg
    assert assigned["historical_references"] == ["old-assigned"]
    assert queued["state"] == "queued" and queued["assignment"] is None
    assert ("dangling_assignment", "wi_missing", "asg_orphan") in {
        (issue["code"], issue["task_id"], issue["assignment_id"])
        for issue in manager["issues"]}

    worker = build_brief(fleet, paths, "alex", NOW)["work"]
    assert worker["scope"] == "assigned"
    assert [item["task_id"] for item in worker["items"]] == [assigned_task]
    assert "wi_queued" not in {item["task_id"] for item in worker["items"]}
    assert "historical references: old-assigned" in format_brief(
        build_brief(fleet, paths, "mgr", NOW))
    assert "unresolved history: 1 issue(s)" in format_brief(
        build_brief(fleet, paths, "mgr", NOW))
    boot = format_boot_brief(
        build_brief(fleet, paths, "mgr", NOW), boot_provenance(paths, NOW))
    assert "wi_queued" in boot and "history issue(s)" in boot
    assert "no open tasks for this bot" not in boot


def test_unresolved_history_alone_prevents_no_work_claim(paths: Paths):
    from claudlobby.plane.db import db_file
    from tests.test_task_state import _assignment
    import sqlite3

    _seed_plane(paths)
    with sqlite3.connect(db_file(paths.root)) as conn:
        uid = load_dispatch_doors(paths)._plane_readers().fleet_uid(conn, FLEET)
        _assignment(conn, "asg_orphan", "wi_missing", fleet_uid=uid)
    brief = build_brief(_fleet(), paths, "alex", NOW)
    assert brief["work"]["items"] == []
    assert brief["work"]["issues"]
    boot = format_boot_brief(brief, boot_provenance(paths, NOW))
    assert "unresolved history" in boot and "no open tasks for this bot" not in boot
