"""Cutover chunk 6b → F18 closure: the legacy WRITES' retirement as a RECORDED
fact, and who-reviewed's plane join.

The doors themselves write nothing since R1 (there is no ledger append left
to skip, so the four-fact `plane_write_retired` predicate is gone with it).
What remains is the epoch: `plane cutover --retire-writes` records
`legacy_write_retired` once every reader is declared (or forced, with the
reason), the readers of the once-retired ledgers follow that fact
(`brief.plane_retired_conn`), and the doctor reads each PLANE_LEGACY_WRITE_*
flag against it. R3 retires the door and the flag surface.

Deleted with the shadow (F18 closure, R2a): test_the_shadow_ends_with_the_retirement,
test_the_shadow_unit_composes_dormant_once_the_writes_are_retired,
test_the_orphan_list_follows_the_overdue_flip (the plane's orphan split is
test_plane_cutover_flip.test_the_orphan_split_holds_on_the_plane).
Re-pointed: test_the_plane_orphan_list_is_the_planes_own_not_the_ledgers →
test_the_orphan_list_is_the_planes_own_split.
"""

from __future__ import annotations

import json
import subprocess
import sys

from claudlobby.plane import cutover as cut
from claudlobby.plane.emit_api import emit_batch
from tests.conftest import load_lib_module
from tests.plane_fixtures import F, NOW_EPOCH, REPO, _cli, _declare, _matcher, _scene, ro as _ro
from tests.test_plane_cutover_flip import _composed
from tests.test_plane_cutover_parity import _live_dispatch, _rrow


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
                             "events": "PLANE_LEGACY_WRITE_EVENTS=0", "workstreams": "PLANE_LEGACY_WRITE_WORKSTREAMS=0"}
    assert "PLANE_LEGACY_WRITE_WORKSTREAMS=0" in done.stdout                       # the fourth door (cutover A2)
    again = _cli(root, "cutover", "--retire-writes")
    assert again.returncode == 0 and "already retired" in again.stdout             # nothing new recorded
    with _ro(root) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event = 'legacy_write_retired'").fetchone()[0] == 1
    later = _cli(root, "cutover", "--reader", "open", "--force", "after")          # a declaration after the retirement still records
    assert later.returncode == 0, later.stdout + later.stderr


def test_the_orphan_list_is_the_planes_own_split(tmp_path):
    """Every dispatch the plane holds for a bot older than its .spawn is the
    orphan list's — the one landed by the live door and the scene's alike;
    a bot that never respawned contributes nothing."""
    import os
    root, paths, _, _ = _scene(tmp_path)
    _live_dispatch(root, "8", "t-8-only-plane", ts="2026-09-02T09:00:00Z", expected_by="2026-09-02T10:00:00+00:00")
    bots = root / "bots"
    (bots / "w1" / "data").mkdir(parents=True)
    spawn = bots / "w1" / "data" / ".spawn"
    spawn.write_text("")
    os.utime(spawn, (NOW_EPOCH - 60, NOW_EPOCH - 60))                  # w1 respawned after both dispatches
    orphans = _matcher(root, "--orphans", str(NOW_EPOCH), "--fleet", F, "--bots-dir", str(bots))
    assert orphans.returncode == 0, orphans.stderr
    assert "t-8-only-plane" in orphans.stdout and "t-2-bbbb" in orphans.stdout
    assert "w2" not in orphans.stdout and "t-3-cccc" not in orphans.stdout
    over = _matcher(root, "--all", str(NOW_EPOCH), "--fleet", F, "--bots-dir", str(bots))
    assert "t-8-only-plane" not in over.stdout and "t-3-cccc" in over.stdout      # split, never paged twice


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
    assert cut.EVENT_RETIRED == "legacy_write_retired" and set(cut.WRITE_FLAGS) == {'dispatch', 'report', 'events', 'workstreams'}


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
