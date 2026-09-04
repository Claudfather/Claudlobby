"""Cutover chunk 6b — the legacy WRITES retired, per door, as the end of shadowing;
who-reviewed's plane join.

A door skips its JSONL append only on FOUR facts (`plane_write_retired` in
lib-common): the flag says 0, the plane is armed, the retirement is RECORDED
(`plane cutover --retire-writes`, read through `plane-lookup.py --retired`),
and THIS emission succeeded (`PLANE_EMIT_LAST_RC`). Every other case writes
the ledger and says why — a dispatch or report must land somewhere.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone

from claudlobby.plane import cutover as cut
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from tests.conftest import load_lib_module
from tests.plane_fixtures import ro as _ro
from tests.test_plane_cutover_flip import F, _cli, _composed, _declare, _ledgers, _matcher, _scene
from tests.test_plane_cutover_parity import _rrow
from tests.test_plane_door_e2e import _bash, _ledger_row, _plane_lib
from tests.test_plane_shadow import REPO

E2E_FLEET = "e2e-fleet"
RETIRED = {"PLANE_LEGACY_WRITE_DISPATCH": "0", "PLANE_LEGACY_WRITE_REPORT": "0"}


def _lines(path):
    return len(path.read_text().splitlines()) if path.exists() else 0


def _record_retirement(root, fleet=E2E_FLEET):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    decl = {r: (now, None) for r in cut.GATED}
    emit_batch(root, [cut.retirement_event(fleet, decl, now)])


def test_the_legacy_writes_default_on(tmp_path):
    libdir, env = _plane_lib(tmp_path)
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "one"', env)
    assert r.returncode == 0 and _lines(tmp_path / "state" / "dispatch-log.jsonl") == 1
    task_id = _ledger_row(tmp_path)["task_id"]
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "done" --task {task_id}', env)
    assert r.returncode == 0 and len(list(tmp_path.rglob("report-back.jsonl"))) == 1


def test_a_retired_write_is_skipped_only_on_all_four_facts(tmp_path):
    libdir, env = _plane_lib(tmp_path)
    dlog = tmp_path / "state" / "dispatch-log.jsonl"
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "seed"', env)     # a real db + one row
    assert r.returncode == 0 and _lines(dlog) == 1
    retired = {**env, **RETIRED}
    # flag 0, armed, emit ok — but NO retirement recorded: writes, says so
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "undeclared"', retired)
    assert r.returncode == 0 and _lines(dlog) == 2 and "no legacy_write_retired is recorded" in r.stderr
    _record_retirement(tmp_path)
    # all four facts: skipped, the plane has the row
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "retired"', retired)
    assert r.returncode == 0 and _lines(dlog) == 2 and "the plane recorded it" in r.stderr
    conn = connect(db_path(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 3
    conn.close()
    task_id = _ledger_row(tmp_path)["task_id"]
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "done" --task {task_id}', retired)
    assert r.returncode == 0 and "the plane recorded it" in r.stderr
    assert not list(tmp_path.rglob("report-back.jsonl"))                          # never written
    # unarmed: writes anyway
    unarmed = {**retired, "PLANE_EMIT_ENABLED": "0"}
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "unarmed"', unarmed)
    assert r.returncode == 0 and _lines(dlog) == 3 and "plane is unarmed" in r.stderr
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "done" --task {task_id}', unarmed)
    assert r.returncode == 0 and "plane is unarmed" in r.stderr
    assert len(list(tmp_path.rglob("report-back.jsonl"))) == 1


def test_a_failed_emission_writes_the_ledger_even_when_retired(tmp_path):
    """The structural lens's blocker, driven: every emit rung dead (no daemon,
    a failing cold CLI) and the write retired — the dispatch and the report
    must still land in the ledger, disclosed."""
    libdir, env = _plane_lib(tmp_path)
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "seed"', env)
    assert r.returncode == 0
    _record_retirement(tmp_path)
    dead = tmp_path / "dead-cli"
    dead.write_text("#!/bin/bash\nexit 1\n")
    dead.chmod(0o755)
    broken = {**env, **RETIRED, "PLANE_EMIT_CLI": str(dead), "PLANE_SOCKET": str(tmp_path / "no.sock")}
    dlog = tmp_path / "state" / "dispatch-log.jsonl"
    before = _lines(dlog)
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "must land"', broken)
    assert r.returncode == 0 and _lines(dlog) == before + 1, r.stderr
    assert "did not record this one" in r.stderr and "writing the ledger" in r.stderr
    assert "legacy record stands" not in r.stderr                                  # the old lie is gone
    task_id = _ledger_row(tmp_path)["task_id"]
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "done" --task {task_id}', broken)
    assert r.returncode == 0 and "did not record this one" in r.stderr
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
    assert still.returncode == 1 and "open_task" in still.stdout and "unassigned" in still.stdout
    _declare(root, "open_task"); _declare(root, "unassigned"); _declare(root, "events")
    done = _cli(root, "cutover", "--retire-writes")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PLANE_LEGACY_WRITE_DISPATCH=0" in done.stdout and "PLANE_LEGACY_WRITE_REPORT=0" in done.stdout
    assert "--unassigned" in done.stdout and "frozen" not in done.stdout.lower()   # every reader follows its flip
    with _ro(root) as conn:
        at, forced = cut.retired(conn, F)
        assert at and forced is None
        data = json.loads(conn.execute("SELECT detail FROM events WHERE event = 'legacy_write_retired'").fetchone()[0])
    assert data["undeclared"] == [] and set(data["declared"]) == {"open", "overdue", "open_task", "unassigned", "events"}
    assert data["flags"] == {"dispatch": "PLANE_LEGACY_WRITE_DISPATCH=0", "report": "PLANE_LEGACY_WRITE_REPORT=0",
                             "events": "PLANE_LEGACY_WRITE_EVENTS=0"}
    again = _cli(root, "cutover", "--retire-writes")
    assert again.returncode == 0 and "already retired" in again.stdout             # nothing new recorded
    with _ro(root) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event = 'legacy_write_retired'").fetchone()[0] == 1
    later = _cli(root, "cutover", "--reader", "open", "--force", "after")
    assert later.returncode == 0 and "stands on what was recorded" in later.stdout


def test_the_shadow_ends_with_the_retirement(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    assert _cli(root, "shadow").returncode == 0
    forced = _cli(root, "cutover", "--retire-writes", "--force", "operator")
    assert forced.returncode == 0 and "FORCED" in forced.stdout
    for args in ((), ("--record",), ("--replay-hours", "3")):
        ended = _cli(root, "shadow", *args)
        assert ended.returncode == 2 and "no legacy side left" in ended.stderr, args
    assert _cli(root, "shadow", "--gate").returncode == 1                        # what was recorded still reads
    assert _cli(root, "shadow", "--check").returncode == 0


def test_the_orphan_list_follows_the_overdue_flip(tmp_path):
    """--orphans is the overdue reader's own split, so it flips with it and never
    reads a ledger the retirement froze."""
    import os
    from tests.test_plane_cutover_flip import NOW_EPOCH
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    bots = root / "bots"
    (bots / "w1" / "data").mkdir(parents=True)
    spawn = bots / "w1" / "data" / ".spawn"
    spawn.write_text("")
    os.utime(spawn, (NOW_EPOCH - 60, NOW_EPOCH - 60))
    jsonl = _matcher(root, "--orphans", dl, rl, str(NOW_EPOCH), "--source", "jsonl", "--bots-dir", str(bots))
    plane = _matcher(root, "--orphans", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F, "--bots-dir", str(bots))
    assert jsonl.returncode == 0 == plane.returncode and jsonl.stdout == plane.stdout and "t-2-bbbb" in plane.stdout
    _declare(root, "overdue")
    auto = _matcher(root, "--orphans", dl, rl, str(NOW_EPOCH), "--bots-dir", str(bots),
                    PLANE_READ_OVERDUE="1", CLAUDLOBBY_FLEET=F)
    assert auto.stdout == plane.stdout


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


def test_the_retirement_token_is_registered():
    from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY
    assert SYSTEM_EVENT_SEVERITY["legacy_write_retired"] == "notice"
    assert cut.EVENT_RETIRED == "legacy_write_retired" and set(cut.WRITE_FLAGS) == {"dispatch", "report", "events"}


def test_write_flag_vs_retirement_names_the_missing_half():
    assert cut.write_flag_vs_retirement(True, None)[0] is False
    assert cut.write_flag_vs_retirement(False, "2026-09-03T09:00:00+00:00")[0] is False
    assert cut.write_flag_vs_retirement(True, "2026-09-03T09:00:00+00:00") == (True, "retired (recorded 2026-09-03T09:00:00+00:00)")
    assert cut.write_flag_vs_retirement(False, None) == (True, "writing (not retired)")
    assert cut.undeclared({}) == ["open", "overdue", "open_task", "unassigned", "events"]
    assert cut.undeclared({"open": ("t", None)}) == ["overdue", "open_task", "unassigned", "events"]


def test_bot_conf_carries_a_retired_write_flag(tmp_path, monkeypatch):
    _, conf = _composed(tmp_path, monkeypatch, {"PLANE_LEGACY_WRITE_DISPATCH": "0", "PLANE_READ_OPEN": "1"})
    assert "export PLANE_LEGACY_WRITE_DISPATCH=0" in conf and "PLANE_LEGACY_WRITE_REPORT" not in conf
    assert "PLANE_READ_OPEN" in conf
    _, bare = _composed(tmp_path / "b", monkeypatch, {"PLANE_LEGACY_WRITE_DISPATCH": "1"})
    assert "PLANE_LEGACY_WRITE" not in bare


def test_who_reviewed_attributes_from_the_plane_like_the_ledger(tmp_path):
    wr = load_lib_module("who-reviewed")
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
    reviews = tmp_path / "reviews.json"
    reviews.write_text(json.dumps({"reviews": [], "comments": []}))
    ok = subprocess.run([sys.executable, str(REPO / "lib" / "who-reviewed.py"), "org/repo", "1046",
                         "--source", "plane", "--root", str(root), "--reviews-json", str(reviews), "--json"],
                        capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["scope"]["source"] == "plane"
    (root / "state" / "plane" / "plane.db").unlink()
    rows, why = wr.load_plane_rows(str(root))
    assert rows == [] and "no plane db" in why                                     # unreachable ≠ empty
    gone = subprocess.run([sys.executable, str(REPO / "lib" / "who-reviewed.py"), "org/repo", "1046",
                           "--source", "plane", "--root", str(root), "--reviews-json", str(reviews)],
                          capture_output=True, text=True, timeout=60)
    assert gone.returncode == 4 and gone.stdout == "" and "unreachable" in gone.stderr


def test_the_plane_orphan_list_is_the_planes_own_not_the_ledgers(tmp_path):
    """A dispatch the plane holds and the ledger does not (the ledger frozen by
    a retirement) is orphaned by the plane's own split — the mutant that kept
    reading the ledger for orphans survived the parity pin, so this one is
    asymmetric on purpose."""
    import os
    from tests.test_plane_cutover_flip import NOW_EPOCH
    from tests.test_plane_cutover_parity import _live_dispatch
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    _live_dispatch(root, "8", "t-8-only-plane", ts="2026-09-02T09:00:00Z", expected_by="2026-09-02T10:00:00+00:00")
    bots = root / "bots"
    (bots / "w1" / "data").mkdir(parents=True)
    spawn = bots / "w1" / "data" / ".spawn"
    spawn.write_text("")
    os.utime(spawn, (NOW_EPOCH - 60, NOW_EPOCH - 60))                  # w1 respawned after both dispatches
    plane = _matcher(root, "--orphans", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F, "--bots-dir", str(bots))
    jsonl = _matcher(root, "--orphans", dl, rl, str(NOW_EPOCH), "--source", "jsonl", "--bots-dir", str(bots))
    assert plane.returncode == 0 and "t-8-only-plane" in plane.stdout and "t-2-bbbb" in plane.stdout
    assert "t-8-only-plane" not in jsonl.stdout

