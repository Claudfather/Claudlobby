"""Shared plane test scaffolding — one definition of the throwaway plane root
(three test files had the same four lines) and a read-only connection that
owns its close."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from claudlobby.plane.db import connect_ro, db_file
from claudlobby.plane.emit_api import emit_batch


def plane_root(tmp_path: Path, *, capture: str = '{"*": "full"}') -> Path:
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text(capture)
    return root


def open_assignment_ids(root: Path) -> list[str]:
    """The plane's open assignments by production's own definition of open."""
    from claudlobby.plane.queries import NON_TERMINAL_CLAUSE
    with ro(root) as conn:
        return sorted(r[0] for r in conn.execute(
            "SELECT a.assignment_id FROM assignments a WHERE" + NON_TERMINAL_CLAUSE))


@contextmanager
def ro(root: Path):
    conn = connect_ro(db_file(root))
    try:
        yield conn
    finally:
        conn.close()


# --- the cutover-era scaffolding every plane suite shares (moved here from the
# deleted shadow suite and the flip suite, F18 closure R2a) --------------------

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parent.parent
F = "f"
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
NOW_EPOCH = int(datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc).timestamp())
MATCHER = REPO / "lib" / "dispatch-overdue.py"
FLEET_YAML = ("fleet:\n  name: f\n  service_prefix: com.test\n  bots:\n"
              "    w1:\n      expertise: [software-engineering]\n"
              "    w2:\n      expertise: [software-engineering]\n")


def _paths(root):
    """An overlay root whose lib/ IS the repo's lib/ — the matcher a door
    loads is the install's own script, never a copy. `bots:` nests under
    `fleet:` (a top-level `bots:` parses to zero bots, silently)."""
    from claudlobby.paths import Paths
    (root / "local" / F / "runtime").mkdir(parents=True, exist_ok=True)
    (root / "local" / F / "fleet.yaml").write_text(FLEET_YAML)
    if not (root / "lib").exists():
        (root / "lib").symlink_to(REPO / "lib")
    return Paths(root=root, fleet_dir=root / "local" / F)


def _epoch(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _dispatch(root, n, task_id, ts, *, bot="w1", ledger=None):
    """One dispatch as the live door lands it (the importer suite's helper):
    work item + assignment + communication. *ledger* is accepted for the
    callers that still collect the legacy-shaped rows (the report and resolver
    suites write them for the readers R2b moves)."""
    row = _drow(ts, task_id, bot=bot)
    row["dispatched_at"] = _epoch(ts)
    deadline = datetime.fromtimestamp(row["expected_by"], timezone.utc).isoformat()
    wi, asg, msg = _live_dispatch(root, n, task_id, ts=ts, bot=bot, expected_by=deadline)
    row["plane_msg_id"], row["plane_work_item_id"], row["plane_assignment_id"] = msg, wi, asg
    if ledger is not None:
        ledger.append(row)
    return wi, asg


_REPORT_SEQ = [0]


def _report(root, wi, asg, ts, *, bot="w1", event="completed", extra=None, status=None):
    """What the REAL report door lands for one report: the `report` communication
    and, when it resolved an assignment, the task event — both under one
    `report-back:<msg_id>` ref. (`event=None` = a report that resolved nothing.)"""
    from claudlobby.plane.emit_api import emit_batch
    _REPORT_SEQ[0] += 1
    msg = f"msg_{'e' * 24}{_REPORT_SEQ[0]:0>8x}"
    ref = f"report-back:{msg}"
    events = [{"event_type": "communication", "emitter": "report-back", "fleet": F,
               "source_ref": ref, "occurred_at": ts,
               "payload": {"msg_id": msg, "sender": f"bot:{F}/{bot}", "recipient": f"bot:{F}/mgr",
                           "recipient_raw": "mgr", "message_class": "report",
                           **({"work_item_id": wi, "assignment_id": asg} if event else {}), "body": "r"}}]
    if event:
        events.append({"event_type": "task", "emitter": "report-back", "fleet": F,
                       "source_ref": ref, "occurred_at": ts,
                       "payload": {"work_item_id": wi, "assignment_id": asg, "event": event,
                                   "actor": f"bot:{F}/{bot}", **(extra or {})}})
    elif status in ("completed", "failed", "blocked"):
        events.append({"event_type": "system", "emitter": "report-back", "fleet": F,
                       "source_ref": ref, "occurred_at": ts,
                       "payload": {"event": "report_status", "subject_kind": "actor",
                                   "subject": f"bot:{F}/{bot}", "data": {"status": status, "msg_id": msg}}})
    emit_batch(root, events)
    return msg


def _complete(root, wi, asg, ts, task_id, reports=None, *, bot="w1"):
    _report(root, wi, asg, ts, bot=bot)
    if reports is not None:
        reports.append(_rrow(ts, task_id, "completed", bot=bot))


def _scene(tmp_path):
    """Two bots on the plane; w1 has one open and one completed task, w2 one
    open. Returns (root, paths, dispatch_rows, report_rows) — the two row
    lists are the legacy-shaped twins the old scene also wrote to files;
    no file is written any more."""
    root = plane_root(tmp_path)
    paths = _paths(root)
    d, r = [], []
    wi1, asg1 = _dispatch(root, "1", "t-1-aaaa", "2026-09-01T10:00:00Z", ledger=d)
    _complete(root, wi1, asg1, "2026-09-01T11:00:00Z", "t-1-aaaa", r)
    _dispatch(root, "2", "t-2-bbbb", "2026-09-02T10:00:00Z", ledger=d)
    _dispatch(root, "3", "t-3-cccc", "2026-09-02T12:00:00Z", bot="w2", ledger=d)
    return root, paths, d, r


def _env(root, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDLOBBY_FLEET", "FLEET_NAME")}
    env.update({"CLAUDLOBBY_ROOT": str(root), "HOME": str(root / "home")})
    env.update(extra)
    return env


def _matcher(root, *args, **extra):
    return subprocess.run([sys.executable, str(MATCHER), *args], capture_output=True,
                          text=True, timeout=120, env=_env(root, **extra))


def _cli(root, *args, **extra):
    return subprocess.run([sys.executable, "-m", "claudlobby", "--root", str(root),
                           "--fleet", F, "plane", *args], capture_output=True, text=True,
                          timeout=180, env=_env(root, **extra))


def _stdlib_readers():
    spec = importlib.util.spec_from_file_location("pr", REPO / "lib" / "plane-readers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- the legacy-shaped rows and the live dispatch (from the deleted parity suite) ---
# tests/test_plane_cutover_parity.py (25 tests: the parity door and the importer)
# went with `plane parity` / `plane import` in the F18 closure, R3 — there is
# no ledger left to compare or import. These four helpers were its exports;
# the suites that land a dispatch the way the live door does still ride them.

def _live_dispatch(root, n, task_id, *, ts, bot="w1", expected_by=None, fleet=None, ref=None):
    """A dispatch the LIVE door landed: three events, emitter dispatch-task.
    *expected_by* (ISO) mirrors the ledger row's deadline when a test needs
    the watchdog's question answered on both sides; *ref* overrides the
    source_ref (an id-less construct's ``dispatch-log:sha:<key>``)."""
    fl = fleet or F
    wi, asg, msg = f"wi_{n:0>32}", f"asg_{n:0>32}", f"msg_{n:0>32}"
    ref = ref or f"dispatch-log:{task_id}"
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "dispatch-task", "fleet": fl,
         "source_ref": ref, "occurred_at": ts,
         "payload": {"work_item_id": wi, "title": "t", "created_by": f"bot:{fl}/mgr"}},
        {"event_type": "assignment", "emitter": "dispatch-task", "fleet": fl,
         "source_ref": ref, "occurred_at": ts,
         "payload": {"assignment_id": asg, "work_item_id": wi,
                     "assignee": f"bot:{fl}/{bot}", "assigned_by": f"bot:{fl}/mgr",
                     "dispatch_msg_id": msg,
                     **({"expected_by": expected_by} if expected_by else {})}},
        {"event_type": "communication", "emitter": "dispatch-task", "fleet": fl,
         "source_ref": ref, "occurred_at": ts,
         "payload": {"msg_id": msg, "sender": f"bot:{fl}/mgr", "recipient": f"bot:{fl}/{bot}",
                     "message_class": "task_request", "command_type": "task",
                     "work_item_id": wi, "assignment_id": asg, "body": "t"}}])
    return wi, asg, msg


def _drow(ts, task_id, *, manager="mgr", bot="w1", task="do the thing\nmore",
          expected_by=1788000000, plane=("", "", "")):
    """A dispatch row in the retired ledger's shape (the readers that still
    collect legacy-shaped rows compare against it)."""
    msg, wi, asg = plane
    return {"ts": ts, "manager": manager, "bot": bot, "task_id": task_id,
            "workstream": "", "task": task, "dispatched_at": 1787900000,
            "expected_by": expected_by, "claudron_hits": 0, "supersedes": "",
            "open_at_dispatch": 0, "plane_msg_id": msg, "plane_work_item_id": wi,
            "plane_assignment_id": asg}


def _rrow(ts, task_id, status, *, bot="w1", summary="done", pr_url="",
          progress="", anomaly="", plane_msg_id=""):
    """A report row in the retired ledger's shape."""
    return {"ts": ts, "bot": bot, "task_id": task_id, "status": status,
            "summary": summary, "pr_url": pr_url, "issues": "", "skill": "",
            "progress": progress, "artifact": "", "task_anomaly": anomaly,
            "plane_msg_id": plane_msg_id}


def _write(path: Path, rows, *, extra_lines=()):
    """JSON lines to *path* — for the suites that still pin a legacy row shape."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "".join(extra_lines))
