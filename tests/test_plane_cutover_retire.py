"""Cutover chunk 6b — the legacy WRITES retired, per door, as the end of shadowing;
who-reviewed's plane join.

`PLANE_LEGACY_WRITE_DISPATCH` / `PLANE_LEGACY_WRITE_REPORT` default 1; a 0 is
honoured only while the plane is armed (a dispatch or report must land
somewhere). `plane cutover --retire-writes` refuses unless every reader is
declared (or --force with a reason), records `legacy_write_retired`, and the
shadow's compare/record modes end with it; the doctor reads each write flag
against the record; bot.conf carries a retired flag; who-reviewed attributes
from the plane's task events like it does from the ledger.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys

from claudlobby.plane import cutover as cut
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.db import connect, db_path
from tests.plane_fixtures import ro as _ro
from tests.test_plane_cutover_flip import F, _cli, _composed, _declare, _scene
from tests.test_plane_cutover_parity import _rrow
from tests.test_plane_door_e2e import _bash, _ledger_row, _plane_lib
from tests.test_plane_shadow import REPO


def _lines(path):
    return len(path.read_text().splitlines()) if path.exists() else 0


def test_the_legacy_writes_default_on_and_retire_only_while_the_plane_is_armed(tmp_path):
    libdir, env = _plane_lib(tmp_path)
    dlog = tmp_path / "state" / "dispatch-log.jsonl"
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "one"', env)
    assert r.returncode == 0 and _lines(dlog) == 1                                # default: written
    retired = {**env, "PLANE_LEGACY_WRITE_DISPATCH": "0", "PLANE_LEGACY_WRITE_REPORT": "0"}
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "two"', retired)
    assert r.returncode == 0 and _lines(dlog) == 1 and "write retired" in r.stderr   # armed: retired
    conn = connect(db_path(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 2     # the plane has both
    conn.close()
    task_id = _ledger_row(tmp_path)["task_id"]
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "done" --task {task_id}', retired)
    assert r.returncode == 0 and "write retired" in r.stderr
    assert not list(tmp_path.rglob("report-back.jsonl"))                          # never written
    unarmed = {**retired, "PLANE_EMIT_ENABLED": "0"}
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "three"', unarmed)
    assert r.returncode == 0 and _lines(dlog) == 2 and "writing the ledger anyway" in r.stderr
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "done" --task {task_id}', unarmed)
    assert r.returncode == 0 and "writing the ledger anyway" in r.stderr
    assert len(list(tmp_path.rglob("report-back.jsonl"))) == 1


def test_retire_writes_refuses_until_every_reader_is_declared_then_records(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    assert _cli(root, "cutover", "--retire-writes", "--reader", "open").returncode == 2
    assert _cli(root, "cutover").returncode == 2
    short = _cli(root, "cutover", "--retire-writes")
    assert short.returncode == 1 and "REFUSED" in short.stdout and "MISSING" in short.stdout
    with _ro(root) as conn:
        assert cut.retired(conn, F) is None
    for reader in ("open", "overdue"):
        _declare(root, reader)
    still = _cli(root, "cutover", "--retire-writes")
    assert still.returncode == 1 and "open_task" in still.stdout
    _declare(root, "open_task")
    done = _cli(root, "cutover", "--retire-writes")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PLANE_LEGACY_WRITE_DISPATCH=0" in done.stdout and "PLANE_LEGACY_WRITE_REPORT=0" in done.stdout
    with _ro(root) as conn:
        at, forced = cut.retired(conn, F)
        assert at and forced is None
        data = json.loads(conn.execute("SELECT detail FROM events WHERE event = 'legacy_write_retired'").fetchone()[0])
    assert data["undeclared"] == [] and set(data["declared"]) == {"open", "overdue", "open_task"}
    assert data["flags"] == {"dispatch": "PLANE_LEGACY_WRITE_DISPATCH=0", "report": "PLANE_LEGACY_WRITE_REPORT=0"}


def test_the_shadow_ends_with_the_retirement(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    assert _cli(root, "shadow").returncode == 0
    forced = _cli(root, "cutover", "--retire-writes", "--force", "operator")
    assert forced.returncode == 0 and "FORCED" in forced.stdout
    ended = _cli(root, "shadow")
    assert ended.returncode == 2 and "no legacy side left" in ended.stderr
    assert _cli(root, "shadow", "--record").returncode == 2
    assert _cli(root, "shadow", "--gate").returncode == 1                        # what was recorded still reads
    assert _cli(root, "shadow", "--check").returncode == 0


def test_the_doctor_reads_the_write_flags_against_the_retirement(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    (root / "home").mkdir()
    (root / "local" / F / ".env").write_text("PLANE_LEGACY_WRITE_DISPATCH=0\n")
    d = _cli(root, "doctor")
    assert "legacy write dispatch" in d.stdout and "NO retirement recorded" in d.stdout, d.stdout
    assert "legacy write report" in d.stdout and "writing (not retired)" in d.stdout
    _cli(root, "cutover", "--retire-writes", "--force", "operator")
    d2 = _cli(root, "doctor")
    assert "retired (recorded" in d2.stdout and "the flag still writes" in d2.stdout   # report not yet flipped


def test_write_flag_vs_retirement_names_the_missing_half():
    assert cut.write_flag_vs_retirement(True, None)[0] is False
    assert cut.write_flag_vs_retirement(False, "2026-09-03T09:00:00+00:00")[0] is False
    assert cut.write_flag_vs_retirement(True, "2026-09-03T09:00:00+00:00")[0] is True
    assert cut.write_flag_vs_retirement(False, None) == (True, "writing (not retired)")
    assert cut.undeclared({}) == ["open", "overdue", "open_task"] and cut.undeclared({"open": ("t", None)}) == ["overdue", "open_task"]


def test_bot_conf_carries_a_retired_write_flag(tmp_path, monkeypatch):
    _, conf = _composed(tmp_path, monkeypatch, {"PLANE_LEGACY_WRITE_DISPATCH": "0", "PLANE_READ_OPEN": "1"})
    assert "export PLANE_LEGACY_WRITE_DISPATCH=0" in conf and "PLANE_LEGACY_WRITE_REPORT" not in conf
    assert "PLANE_READ_OPEN" in conf
    _, bare = _composed(tmp_path / "b", monkeypatch, {"PLANE_LEGACY_WRITE_DISPATCH": "1"})
    assert "PLANE_LEGACY_WRITE" not in bare


def _who_reviewed():
    spec = importlib.util.spec_from_file_location("wr", REPO / "lib" / "who-reviewed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_who_reviewed_attributes_from_the_plane_like_the_ledger(tmp_path):
    wr = _who_reviewed()
    root, paths, _, r = _scene(tmp_path)
    ts = "2026-09-02T14:00:00Z"
    emit_batch(root, [{"event_type": "task", "emitter": "report-back", "fleet": F,
                       "source_ref": f"report-back:msg_{'5':0>32}", "occurred_at": ts,
                       "payload": {"work_item_id": f"wi_{'2':0>32}", "assignment_id": f"asg_{'2':0>32}",
                                   "event": "completed", "actor": f"bot:{F}/w1",
                                   "pr_url": "https://github.com/org/repo/pull/1046", "summary": "Request Changes on #1046"}}])
    ledger_rows = [{**_rrow(ts, "t-2-bbbb", "completed", pr_url="https://github.com/org/repo/pull/1046",
                            summary="Request Changes on #1046"), "_fleet": F, "_ledger": "ledger"}]
    plane_rows, why = wr.load_plane_rows(str(root))
    assert why is None and len(plane_rows) == 1
    assert {k: plane_rows[0][k] for k in ("bot", "pr_url", "task_id", "status", "_fleet")} == \
        {"bot": "w1", "pr_url": "https://github.com/org/repo/pull/1046", "task_id": "t-2-bbbb",
         "status": "completed", "_fleet": F}
    events = [{"ts": "2026-09-02T13:59:52Z", "state": "CHANGES_REQUESTED", "kind": "review"}]
    from_ledger = wr.attribute(events, ledger_rows, "org/repo", 1046)
    from_plane = wr.attribute(events, plane_rows, "org/repo", 1046)
    assert from_ledger[0]["verdict"] == from_plane[0]["verdict"] == "MATCH"
    assert from_plane[0]["candidates"][0]["bot"] == "w1"
    (root / "state" / "plane" / "plane.db").unlink()
    rows, why = wr.load_plane_rows(str(root))
    assert rows == [] and "no plane db" in why                                     # unreachable ≠ empty
    cli = subprocess.run([sys.executable, str(REPO / "lib" / "who-reviewed.py"), "org/repo", "1046",
                          "--source", "plane", "--root", str(root), "--reviews-json", "/dev/null"],
                         capture_output=True, text=True, timeout=60)
    assert cli.returncode in (3, 4) and cli.stdout == ""
