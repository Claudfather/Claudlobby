"""F18 closure R2a — three door rules the plane-only matcher made visible,
each driven through the REAL doors (the e2e harness: real shim, cold-CLI
rung, a real plane db):

1. an id-less PROGRESS report defers the overdue alarm (the legacy grace was
   per bot; the plane's read progress task events only, and a progress report
   resolves no id — so the report door now lands a `report_status` marker with
   status `progress` and the overdue reader counts it);
2. `--supersedes` retires the assignment of THIS worker, never a same-id
   assignment another bot holds (#518's scoping);
3. every TERMINAL report — id'd, or naming an id the plane cannot link —
   closes the bot's open id-less dispatches, as the ledger rule always did.
"""
from __future__ import annotations

import subprocess
import sys

from claudlobby.plane.queries import TASK_STATUS_SQL
from tests.test_plane_door_e2e import _bash, _plane_lib, _plane_row, _rows, _seed_assignment

F = "e2e-fleet"


def _matcher(tmp_path, libdir, env, *args):
    return subprocess.run([sys.executable, str(libdir / "dispatch-overdue.py"), *args,
                           "--fleet", F, "--root", str(tmp_path)],
                          capture_output=True, text=True, env=env, timeout=120)


def test_an_idless_progress_report_defers_the_overdue_alarm(tmp_path):
    libdir, env = _plane_lib(tmp_path)
    env = {**env, "OBSERVABILITY_DISPATCH_DEADLINE": "1"}       # due in a second
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "a long task"', env)
    assert r.returncode == 0, r.stderr
    assert _plane_row(tmp_path)["task_id"].startswith("t-")
    # a progress report WITHOUT --task: no task event can link, the marker lands
    r = _bash(f'"{libdir}/report-back.sh" w1 progress "halfway" --progress 50', env)
    assert r.returncode == 0, r.stderr
    marker = _rows(tmp_path, "SELECT json_extract(detail, '$.status') FROM events"
                             " WHERE kind='system' AND event='report_status'")
    assert [m[0] for m in marker] == ["progress"]
    import time
    now = int(time.time()) + 5                                  # past the 1s deadline
    late = _matcher(tmp_path, libdir, env, "--all", str(now))
    assert late.returncode == 0, late.stderr
    assert late.stdout == "", ("a bot reporting progress inside the grace is alive, not overdue", late.stdout)
    # ...and only inside the grace: shrink it to nothing and the alarm is back
    dead = _matcher(tmp_path, libdir, {**env, "DISPATCH_PROGRESS_GRACE_S": "1"}, "--all", str(now + 3600))
    assert dead.returncode == 0 and dead.stdout.startswith("w1 "), (dead.stdout, dead.stderr)


def test_supersedes_retires_this_workers_assignment_not_a_same_id_twin(tmp_path):
    libdir, env = _plane_lib(tmp_path)
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "first"', env)
    assert r.returncode == 0, r.stderr
    mine = _plane_row(tmp_path)
    twin = _seed_assignment(tmp_path, task_id=mine["task_id"], bot="w2", tag="7")   # newer, same id, another bot
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand --supersedes {mine["task_id"]} w1 "second"', env)
    assert r.returncode == 0, r.stderr
    statuses = dict(_rows(tmp_path, TASK_STATUS_SQL))
    assert statuses[mine["plane_assignment_id"]] == "superseded", statuses
    assert statuses[twin[2]] != "superseded", "another bot's same-id assignment was retired"


def test_every_terminal_report_closes_the_bots_open_idless_dispatches(tmp_path):
    libdir, env = _plane_lib(tmp_path)
    r = _bash(f'"{libdir}/dispatch-task.sh" --type query w1 "what is the retry logic"', env)   # id-less
    assert r.returncode == 0, r.stderr
    idless = _plane_row(tmp_path)
    assert idless["task_id"] == ""
    # an id'd terminal report naming an id the plane cannot link
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "done" --task t-999999-beef', env)
    assert r.returncode == 0, r.stderr
    statuses = dict(_rows(tmp_path, TASK_STATUS_SQL))
    assert statuses[idless["plane_assignment_id"]] == "completed", statuses


def test_a_stale_callers_trailing_arguments_are_a_usage_error(tmp_path):
    """`--open w1 --source jsonl` once answered at rc 0 (the junk ignored); a
    caller still passing the retired seam or the ledger paths must hear it."""
    libdir, env = _plane_lib(tmp_path)
    r = _matcher(tmp_path, libdir, env, "--open", "w1", "--source", "jsonl")
    assert r.returncode == 2 and "takes no '--source'" in r.stderr, (r.returncode, r.stderr)
    r = _matcher(tmp_path, libdir, env, "--all", "1700000000", "/tmp/dispatch-log.jsonl")
    assert r.returncode == 2, (r.returncode, r.stderr)
