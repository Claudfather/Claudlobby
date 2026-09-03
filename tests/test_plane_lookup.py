"""The cutover's first door: the plane answered by legacy task id.

Pins: found (latest by ingest order, assignee filtered case-insensitively);
not-found is rc 0 + empty stdout + a stderr note (a stamped id is not
proof the row exists, so callers keep their legacy fallback); unreachable
is rc 3; and the plane semantics the wiring buys — a `superseded` task
event makes the retired assignment TERMINAL, so it leaves the open set
(the 14-of-189 class the JSONL door already dropped). The two doors'
call sites are pinned by shape; their live behaviour is the Mini canary.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.queries import NON_TERMINAL_CLAUSE

REPO = Path(__file__).resolve().parent.parent
LOOKUP = REPO / "lib" / "plane-lookup.py"
F = "f"


def _root(tmp_path):
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    return root


def _dispatch(root, n, task_id, bot="w1"):
    wi, asg, msg = f"wi_{n:0>32}", f"asg_{n:0>32}", f"msg_{n:0>32}"
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


def test_two_field_output_for_an_assignment_without_a_dispatch_msg_id(tmp_path):
    """dispatch_msg_id is optional on the contract; the door then prints
    two fields and dispatch-task.sh must NOT read the assignment id as the
    msg id. The parse+guard lines are extracted from the shipped script."""
    root = _root(tmp_path)
    wi, asg = f"wi_{'c':0>32}", f"asg_{'c':0>32}"
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "dispatch-task", "fleet": F,
         "source_ref": "dispatch-log:t-2-cccc",
         "payload": {"work_item_id": wi, "title": "t", "created_by": f"bot:{F}/mgr"}},
        {"event_type": "assignment", "emitter": "dispatch-task", "fleet": F,
         "source_ref": "dispatch-log:t-2-cccc",
         "payload": {"assignment_id": asg, "work_item_id": wi,
                     "assignee": f"bot:{F}/w1", "assigned_by": f"bot:{F}/mgr"}}])
    r = _run(root, "--task-id", "t-2-cccc")
    assert r.returncode == 0 and r.stdout.split() == [wi, asg]
    dt = (REPO / "lib" / "dispatch-task.sh").read_text()
    parse = next(l for l in dt.splitlines() if "SUP_WI=${_sup%% *}" in l)
    guard = next(l for l in dt.splitlines() if 'sup_frag="\\"supersedes_msg_id' in l)
    def frag(stdout):
        return subprocess.run(
            ["bash", "-c", f"_sup='{stdout}'; sup_frag=''\n{parse}\n{guard}\n"
             "printf '%s' \"$sup_frag\""], capture_output=True, text=True).stdout
    assert frag(r.stdout.strip()) == ""                       # two fields: no supersedes_msg_id
    msg = f"msg_{'d':0>32}"
    assert frag(f"{wi} {asg} {msg}") == f'"supersedes_msg_id":"{msg}",'


def test_not_found_is_empty_rc0_and_unreachable_is_rc3(tmp_path):
    root = _root(tmp_path)
    _dispatch(root, "a", "t-1-aaaa")
    nf = _run(root, "--task-id", "t-9-zzzz")
    assert nf.returncode == 0 and nf.stdout == "" and "legacy fallback" in nf.stderr
    un = _run(tmp_path / "nope", "--task-id", "t-1-aaaa")
    assert un.returncode == 3 and un.stdout == "" and "unreachable" in un.stderr


def test_superseded_event_makes_the_old_assignment_terminal(tmp_path):
    """What --supersedes now buys: the retired assignment leaves the open set."""
    root = _root(tmp_path)
    wi1, asg1, _ = _dispatch(root, "a", "t-1-aaaa")
    wi2, asg2, _ = _dispatch(root, "b", "t-2-bbbb")
    emit_batch(root, [{"event_type": "task", "emitter": "dispatch-task", "fleet": F,
                       "source_ref": "dispatch-log:t-2-bbbb",
                       "payload": {"work_item_id": wi1, "assignment_id": asg1,
                                   "event": "superseded", "successor_id": asg2}}])
    conn = connect(db_path(root))
    try:
        open_ids = [r[0] for r in conn.execute(
            "SELECT a.assignment_id FROM assignments a WHERE" + NON_TERMINAL_CLAUSE)]
    finally:
        conn.close()
    assert open_ids == [asg2]


def _report_back_lookup(root, task_id, bot, dlog):
    """Drive the REAL _plane_lookup_dispatch_ids from lib/report-back.sh (the
    function text is extracted from the shipped file, never a copy) with the
    ledger door stubbed to *dlog*; prints the two link vars."""
    src = (REPO / "lib" / "report-back.sh").read_text()
    start = src.index("_plane_lookup_dispatch_ids() {")
    fn = src[start:src.index("\n}\n", start) + 3]
    script = (
        f"dispatch_ledger_path() {{ printf '%s' '{dlog}'; }}\n"
        f"{fn}\n"
        f"TASK_ID='{task_id}' BOT='{bot}' FLEET_NAME='{F}' CLAUDLOBBY_ROOT='{root}'\n"
        "PLANE_LINK_WI='' PLANE_LINK_ASG=''\n"
        "_plane_lookup_dispatch_ids\n"
        'printf \'%s %s\' "$PLANE_LINK_WI" "$PLANE_LINK_ASG"\n')
    # BASH_SOURCE[0] inside the function resolves the lookup script's dir
    script = script.replace('"$(dirname "${BASH_SOURCE[0]}")/plane-lookup.py"',
                            f'"{LOOKUP}"')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.split()


def test_report_back_links_through_the_plane_with_the_dispatch_log_absent(tmp_path):
    """J2's hard precondition, behaviourally: no dispatch-log.jsonl anywhere,
    a plane row for the task -> the report still links. (The first version
    returned before asking the plane; a text-order pin could not see it.)"""
    root = _root(tmp_path)
    wi, asg, _ = _dispatch(root, "a", "t-1-aaaa", bot="w1")
    assert _report_back_lookup(root, "t-1-aaaa", "w1",
                               tmp_path / "absent" / "dispatch-log.jsonl") == [wi, asg]


def test_report_back_falls_back_to_the_ledger_when_the_plane_is_absent(tmp_path):
    dlog = tmp_path / "dispatch-log.jsonl"
    dlog.write_text('{"ts":"2026-09-01T00:00:00Z","manager":"m","bot":"w1",'
                    '"task_id":"t-1-aaaa","plane_msg_id":"msg_1",'
                    '"plane_work_item_id":"wi_legacy","plane_assignment_id":"asg_legacy"}\n')
    assert _report_back_lookup(tmp_path / "noplane", "t-1-aaaa", "w1", dlog) == [
        "wi_legacy", "asg_legacy"]


def test_the_two_doors_call_the_lookup_before_the_ledger():
    rb = (REPO / "lib" / "report-back.sh").read_text()
    dt = (REPO / "lib" / "dispatch-task.sh").read_text()
    assert "plane-lookup.py" in rb and rb.index("plane-lookup.py") < rb.index('grep -F "\\"task_id\\"')
    assert "plane-lookup.py" in dt and '"superseded"' in dt and "supersedes_msg_id" in dt
    for f in ("report-back.sh", "dispatch-task.sh"):
        assert subprocess.run(["bash", "-n", str(REPO / "lib" / f)]).returncode == 0
