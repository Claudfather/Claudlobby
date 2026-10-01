"""The cutover's first door: the plane answered by legacy task id.

Pins: found (latest by ingest order, assignee filtered case-insensitively);
not-found is rc 0 + empty stdout + a stderr note (a stamped id is not
proof the row exists, so callers keep their legacy fallback); unreachable
is rc 3; and the plane semantics the wiring buys — a `superseded` task
event makes the retired assignment TERMINAL, so it leaves the open set
(the 14-of-189 class the JSONL door already dropped). The reader remains for stored historical rows.
"""

from __future__ import annotations

from tests.plane_setup import initialize_plane

import subprocess
import sys
from pathlib import Path

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import open_assignment_ids, plane_root

REPO = Path(__file__).resolve().parent.parent
LOOKUP = REPO / "claudlobby/_runtime_scripts" / "plane-lookup.py"
F = "f"


def _root(tmp_path):
    return plane_root(tmp_path)


def _dispatch(root, n, task_id, bot="w1"):
    wi, asg, msg = f"wi_{n:0>32}", f"asg_{n:0>32}", f"msg_{n:0>32}"
    initialize_plane(root)
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "dispatch-task", "fleet": F,
         "source_ref": f"dispatch-log:{task_id}",
         "payload": {"work_item_id": wi, "title": "t", "created_by": f"bot:{F}/mgr"}},
        {"event_type": "assignment", "emitter": "dispatch-task", "fleet": F,
         "source_ref": f"dispatch-log:{task_id}",
         "payload": {"assignment_id": asg, "work_item_id": wi,
                     "assignee": f"bot:{F}/{bot}", "assigned_by": f"bot:{F}/mgr",
                     "dispatch_msg_id": msg}}])
    return wi, asg, msg


def _run(root, *args):
    return subprocess.run([sys.executable, str(LOOKUP), "--root", str(root), *args],
                          capture_output=True, text=True, timeout=60)


def test_found_prints_ids_latest_first_and_filters_assignee(tmp_path):
    root = _root(tmp_path)
    _dispatch(root, "a", "t-1-aaaa", bot="w1")
    wi, asg, msg = _dispatch(root, "b", "t-1-aaaa", bot="w1")   # a redispatch: latest wins
    r = _run(root, "--task-id", "t-1-aaaa", "--assignee", f"bot:{F}/W1")  # case-insensitive
    assert r.returncode == 0 and r.stdout.split() == [wi, asg, msg]
    miss = _run(root, "--task-id", "t-1-aaaa", "--assignee", f"bot:{F}/other")
    assert miss.returncode == 0 and miss.stdout == "" and "not found" in miss.stderr
    _, peer_asg, _ = _dispatch(root, "c", "t-1-aaaa", bot="w2")
    scoped = _run(root, "--task-id", "t-1-aaaa", "--all-open",
                  "--assignee", f"bot:{F}/W2")
    assert scoped.returncode == 0
    assert [line.split()[1] for line in scoped.stdout.splitlines()] == [peer_asg]


def test_two_field_output_for_assignment_without_dispatch_message(tmp_path):
    root = _root(tmp_path)
    wi, asg = f"wi_{'c':0>32}", f"asg_{'c':0>32}"
    initialize_plane(root)
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "historical-fixture", "fleet": F,
         "source_ref": "dispatch-log:t-2-cccc",
         "payload": {"work_item_id": wi, "title": "t", "created_by": f"bot:{F}/mgr"}},
        {"event_type": "assignment", "emitter": "historical-fixture", "fleet": F,
         "source_ref": "dispatch-log:t-2-cccc",
         "payload": {"assignment_id": asg, "work_item_id": wi,
                     "assignee": f"bot:{F}/w1", "assigned_by": f"bot:{F}/mgr"}}])
    out = _run(root, "--task-id", "t-2-cccc")
    assert out.returncode == 0 and out.stdout.split() == [wi, asg]


def test_empty_root_is_unreachable_not_a_relative_path(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _dispatch(root, "a", "t-1-aaaa")
    monkeypatch.chdir(root)           # a db sits exactly where "" would resolve
    r = _run("", "--task-id", "t-1-aaaa")
    assert r.returncode == 3 and r.stdout == "" and "unreachable" in r.stderr


def test_assignee_filter_fails_closed_without_a_registry_alias(tmp_path):
    root = _root(tmp_path)
    _dispatch(root, "a", "t-1-aaaa")
    conn = connect(db_path(root))
    try:
        conn.execute("UPDATE assignments SET assignee_uid = 'actor_' || substr(hex(randomblob(16)),1,32)")
        conn.commit()
    finally:
        conn.close()
    r = _run(root, "--task-id", "t-1-aaaa", "--assignee", f"bot:{F}/w1")
    assert r.returncode == 0 and r.stdout == "" and "not found" in r.stderr


def test_not_found_is_empty_rc0_and_unreachable_is_rc3(tmp_path):
    root = _root(tmp_path)
    _dispatch(root, "a", "t-1-aaaa")
    nf = _run(root, "--task-id", "t-9-zzzz")
    assert nf.returncode == 0 and nf.stdout == "" and "legacy fallback" in nf.stderr
    un = _run(tmp_path / "nope", "--task-id", "t-1-aaaa")
    assert un.returncode == 3 and un.stdout == "" and "unreachable" in un.stderr


def test_by_assignment_returns_open_only_unless_any_state(tmp_path):
    """#1492: `--by-assignment <asg>` names the row only while it is OPEN — task-act
    resolves an OPEN row through it — and `--any-state` also names a CLOSED row so the
    refusal can still print its `sha:` key. Pins the `open_only` flag: inverting it
    makes task-act's refusal blind to a closed row and its act reach for one."""
    root = _root(tmp_path)
    wi_o, asg_o, _ = _dispatch(root, "a", "sha:" + "a" * 32)   # stays OPEN
    wi_c, asg_c, _ = _dispatch(root, "c", "sha:" + "b" * 32)   # closed below
    emit_batch(root, [{"event_type": "task", "emitter": "report-back", "fleet": F,
                       "source_ref": "dispatch-log:sha:" + "b" * 32,
                       "payload": {"work_item_id": wi_c, "assignment_id": asg_c,
                                   "event": "completed", "actor": f"bot:{F}/w1"}}])
    # OPEN: named with or without --any-state
    assert _run(root, "--by-assignment", asg_o).stdout.split()[:2] == [wi_o, asg_o]
    assert _run(root, "--by-assignment", asg_o, "--any-state").stdout.split()[:2] == [wi_o, asg_o]
    # CLOSED: EMPTY without --any-state (open_only), named WITH it
    assert _run(root, "--by-assignment", asg_c).stdout.strip() == ""
    assert _run(root, "--by-assignment", asg_c, "--any-state").stdout.split()[:2] == [wi_c, asg_c]


def test_superseded_event_makes_the_old_assignment_terminal(tmp_path):
    """What --supersedes now buys: the retired assignment leaves the open set."""
    root = _root(tmp_path)
    wi1, asg1, _ = _dispatch(root, "a", "t-1-aaaa")
    wi2, asg2, _ = _dispatch(root, "b", "t-2-bbbb")
    emit_batch(root, [{"event_type": "task", "emitter": "dispatch-task", "fleet": F,
                       "source_ref": "dispatch-log:t-2-bbbb",
                       "payload": {"work_item_id": wi1, "assignment_id": asg1,
                                   "event": "superseded", "successor_id": asg2}}])
    assert open_assignment_ids(root) == [asg2]


def test_the_cutover_modes_are_gone(tmp_path):
    """F18 closure R3: `--declared` / `--retired` / `--door` are not grammar any
    more — a stale caller hears a usage error, never an empty instant."""
    r = _run(tmp_path, "--declared", "events", "--fleet", "f")
    assert r.returncode == 2 and "unrecognized arguments" in r.stderr, (r.returncode, r.stderr)
    r = _run(tmp_path, "--retired", "--fleet", "f")
    assert r.returncode == 2, (r.returncode, r.stderr)
