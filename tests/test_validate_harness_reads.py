"""The validation harness's plane reads: the shipped stdlib doors, never the
sqlite3 CLI, and a read that cannot run is REFUSED rather than read as empty
(#1777).

The CLI is absent on the fleet's primary Pi, where it read as "0 rows" and
failed 22 checks with no reason given, while CI (whose image has it) passed.
Worse is the other direction: a check expecting ABSENCE read the same empty
answer as a pass. These tests run the reader block lifted verbatim from
`lib/validate-bot-change.sh`, so they exercise the shipped text, against a
real plane and a missing one.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path

from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import plane_root

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "lib" / "validate-bot-change.sh"
LIB_COMMON = REPO / "lib" / "lib-common.sh"
F = "vf"


def _reader_block() -> str:
    """The harness's reader definitions, from the ledger to val_sql's end."""
    lines = HARNESS.read_text().split("\n")
    start = lines.index('HARNESS_REFUSALS="$ROOT/.harness-refusals"')
    head = lines.index("val_sql() {", start)
    end = lines.index("}", head)
    return "\n".join(lines[start : end + 1])


def _run(root: Path, body: str, **env: str) -> subprocess.CompletedProcess:
    script = "\n".join(
        [
            '. "$1"',
            "set -euo pipefail",
            'ROOT="$2"; LIB_DIR="$3"',
            _reader_block(),
            "pass=0; fail=0; refused=0",
            body,
            'printf "COUNTS pass=%s fail=%s refused=%s\\n" "$pass" "$fail" "$refused"',
        ]
    )
    return subprocess.run(
        ["bash", "-c", script, "_", str(LIB_COMMON), str(root), str(REPO / "lib")],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=60,
    )


def _counts(stdout: str) -> tuple[int, int, int]:
    line = [ln for ln in stdout.splitlines() if ln.startswith("COUNTS ")][-1]
    return tuple(int(part.split("=")[1]) for part in line.split()[1:])


def _seeded(tmp_path: Path) -> Path:
    """A real plane with one dispatch, so the fleet and its bot have identity."""
    root = plane_root(tmp_path)
    wi, asg = "wi_" + "a" * 32, "asg_" + "a" * 32
    emit_batch(
        root,
        [
            {
                "event_type": "work_item",
                "emitter": "dispatch-task",
                "fleet": F,
                "source_ref": "dispatch-log:t-1777-0001",
                "payload": {
                    "work_item_id": wi,
                    "title": "t",
                    "created_by": f"bot:{F}/mgr",
                },
            },
            {
                "event_type": "assignment",
                "emitter": "dispatch-task",
                "fleet": F,
                "source_ref": "dispatch-log:t-1777-0001",
                "payload": {
                    "assignment_id": asg,
                    "work_item_id": wi,
                    "assignee": f"bot:{F}/w1",
                    "assigned_by": f"bot:{F}/mgr",
                    "dispatch_msg_id": "msg_" + "a" * 32,
                },
            },
        ],
    )
    return root


def test_val_sql_reads_a_real_plane_in_the_cli_list_mode(tmp_path):
    root = _seeded(tmp_path)
    r = _run(
        root,
        """
out=$(val_sql "$ROOT" "SELECT 1, NULL, 'a' UNION ALL SELECT 2, 'b', NULL")
printf 'OUT<%s>\\n' "$out"
n=$(val_sql "$ROOT" "SELECT COUNT(*) FROM assignments")
[ "$n" = 1 ] && v=yes || v=no
harness_check "the seeded assignment is read" "$v"
""",
    )
    assert r.returncode == 0, r.stderr
    assert "OUT<1||a\n2|b|>" in r.stdout
    assert _counts(r.stdout) == (1, 0, 0)


def test_a_missing_plane_refuses_and_an_absence_check_fails_naming_why(tmp_path):
    # The fail-open direction: `${n:-0} -eq 0` on an unread plane said "yes".
    root = tmp_path / "no-plane-here"
    root.mkdir()
    r = _run(
        root,
        """
n=$(val_sql "$ROOT" "SELECT COUNT(*) FROM events")
printf 'N<%s>\\n' "$n"
[ "${n:-0}" -eq 0 ] && v=yes || v=no
harness_check "no script_error row landed" "$v"
""",
    )
    assert r.returncode == 0, r.stderr
    assert "N<>" in r.stdout  # never a stand-in 0
    assert _counts(r.stdout) == (0, 1, 1)
    assert "  PASS  no script_error row landed" not in r.stdout
    assert re.search(
        r"  FAIL  no script_error row landed — REFUSED, .*"
        r"plane read \(rc 1\): PlaneUnreachable: no plane db at ",
        r.stdout,
    )


def test_a_query_the_plane_cannot_run_is_refused_not_empty(tmp_path):
    root = _seeded(tmp_path)
    r = _run(
        root,
        """
x=$(val_sql "$ROOT" "SELECT no_such_column FROM events")
harness_check "a harness query with a typo" "yes"
""",
    )
    assert r.returncode == 0, r.stderr
    assert _counts(r.stdout) == (0, 1, 1)
    assert "OperationalError: no such column: no_such_column" in r.stdout


def test_val_events_refuses_when_unreachable_and_not_when_nothing_happened(tmp_path):
    # The six absence checks read through val_events ("pane_stuck NOT fired"):
    # unreachable must refuse, while a plane that answered "none" must not.
    seeded = _seeded(tmp_path / "a")
    missing = tmp_path / "b" / "root"
    missing.mkdir(parents=True)
    probe = """
val_events "{root}" "{fleet}" w1 pane_stuck | grep -q '"type":"pane_stuck"' && v=no || v=yes
harness_check "pane_stuck NOT fired" "$v"
"""
    ok = _run(seeded, probe.format(root=seeded, fleet=F))
    assert ok.returncode == 0, ok.stderr
    assert _counts(ok.stdout) == (1, 0, 0), ok.stdout

    gone = _run(missing, probe.format(root=missing, fleet=F))
    assert gone.returncode == 0, gone.stderr
    assert _counts(gone.stdout) == (0, 1, 1), gone.stdout
    assert f"events of {F}/w1 (pane_stuck) (rc 3): " in gone.stdout


def test_a_poll_miss_is_not_recorded(tmp_path):
    root = tmp_path / "no-plane-here"
    root.mkdir()
    r = _run(
        root,
        """
x=$(VAL_READ_QUIET=1 val_sql "$ROOT" "SELECT 1")
harness_check "after a quiet miss" "yes"
""",
    )
    assert r.returncode == 0, r.stderr
    assert _counts(r.stdout) == (1, 0, 0)


def test_the_user_version_stamp_lands_and_never_creates_a_db(tmp_path):
    # The one WRITE in the harness (#1485's stale-db precondition). mode=rw
    # must refuse a missing file rather than create an empty db.
    text = HARNESS.read_text()
    m = re.search(
        r'val_read "stamping user_version=999" python3 -S -E -c \'(.*?)\' "\$PL_ROOT',
        text,
        re.S,
    )
    assert m, "the stamp moved: re-point this test"
    prog = m.group(1)
    root = _seeded(tmp_path)
    db = root / "state" / "plane" / "plane.db"
    ok = subprocess.run(
        ["python3", "-S", "-E", "-c", prog, str(db)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ok.returncode == 0, ok.stderr
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 999
    absent = tmp_path / "absent" / "plane.db"
    absent.parent.mkdir()
    gone = subprocess.run(
        ["python3", "-S", "-E", "-c", prog, str(absent)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert gone.returncode != 0 and "unable to open" in gone.stderr
    assert not absent.exists()


_CLI_CALL = re.compile(r"""(?<![\w.])sqlite3\s+["'$]""")


def test_the_harness_invokes_no_sqlite3_cli():
    offenders = [
        f"{n}: {ln.strip()}"
        for n, ln in enumerate(HARNESS.read_text().split("\n"), 1)
        if _CLI_CALL.search(ln)
    ]
    assert offenders == [], "\n".join(offenders)


def test_the_cli_ratchet_matches_the_calls_it_replaced():
    # Positive control: the ratchet matches each shape #1777 removed, and
    # passes the stdlib uses that replaced them.
    for old in [
        'val_sql() { sqlite3 "$1/state/plane/plane.db" "$2" 2>/dev/null || true; }',
        '        sqlite3 "$PL_ROOT/state/plane/plane.db" \\',
        '    _pl_uv=$(sqlite3 "$PL_ROOT/state/plane/plane.db" "PRAGMA user_version")',
    ]:
        assert _CLI_CALL.search(old), old
    for new in [
        "import sqlite3, sys",
        'c = sqlite3.connect("file:%s?mode=rw" % sys.argv[1])',
        "# never the sqlite3 CLI, which a host need not have",
    ]:
        assert not _CLI_CALL.search(new), new
