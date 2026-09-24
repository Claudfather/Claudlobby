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


# --- a refusal reaches every check that consumes the read (#1796 review) -----


def test_a_read_that_feeds_two_absence_checks(tmp_path):
    # The review's repro, verbatim. The first check emptied the ledger, so the
    # second consumer of the same read PASSED on a plane that could not be read.
    missing = tmp_path / "root"
    missing.mkdir()
    r = _run(
        missing,
        f"""
ev=$(val_events "{missing}" "{F}" bp)
printf '%s' "$ev" | grep -q '"type":"service_down"' && r=no || r=yes
harness_check "fleet-pulse emits no service_down across the boot" "$r"
printf '%s' "$ev" | grep -q '"type":"session_missing"' && r=no || r=yes
harness_check "  ...and no session_missing either" "$r"
""",
    )
    assert _counts(r.stdout) == (0, 2, 2), r.stdout


def test_val_scenario_prints_the_header_and_ends_a_reported_refusal(tmp_path):
    missing = tmp_path / "root"
    missing.mkdir()
    r = _run(
        missing,
        f"""
val_scenario "scenario A"
ev=$(val_events "{missing}" "{F}" a)
[ -z "$ev" ] && r=yes || r=no
harness_check "A: nothing fired" "$r"
val_scenario "scenario B"
harness_check "B: a check that reads nothing" "yes"
""",
    )
    assert "\n\n=== scenario A ===\n" in "\n" + r.stdout
    assert "  FAIL  A: nothing fired — REFUSED" in r.stdout
    assert "  PASS  B: a check that reads nothing" in r.stdout
    assert _counts(r.stdout) == (1, 1, 1), r.stdout


def test_val_diag_says_why_and_refuses_nothing(tmp_path):
    # A diagnostic dump is scored by no check. Recorded, its refusal would
    # carry into the next scenario, onto checks that never read it.
    missing = tmp_path / "root"
    missing.mkdir()
    r = _run(
        missing,
        f"""
val_diag val_events "{missing}" "{F}" a
harness_check "the next check scores" "yes"
""",
    )
    assert "    [stderr] plane-lookup: no plane db at " in r.stdout
    assert _counts(r.stdout) == (1, 0, 0), r.stdout


def test_a_refusal_keeps_the_end_of_a_long_stderr(tmp_path):
    # A traceback puts its exception LAST: a reason cut to its first 400 bytes
    # kept the frames and dropped the one line that says what went wrong.
    r = _run(
        tmp_path,
        """
val_read "a long read" python3 -c 'import sys; sys.stderr.write("frame " * 200 + "\\nOperationalError: the actual cause\\n"); sys.exit(1)'
harness_check "after it" "yes"
""",
    )
    assert "OperationalError: the actual cause" in r.stdout, r.stdout
    assert _counts(r.stdout) == (0, 1, 1)


def test_val_restarts_counts_through_the_reader_and_refuses_when_unreachable(tmp_path):
    # It answered 0 when it could not read (`except Exception: print(0)` under
    # `|| echo 0`), the shape #1777 names.
    seeded = _seeded(tmp_path / "a")
    missing = tmp_path / "b" / "root"
    missing.mkdir(parents=True)
    r = _run(
        seeded,
        f"""
n=$(val_restarts "{seeded}" "{F}" w1)
[ "$n" = 0 ] && v=yes || v=no
harness_check "a bot with no restart counts 0" "$v"
val_scenario "unreachable"
m=$(val_restarts "{missing}" "{F}" w1)
printf 'M<%s>\\n' "$m"
harness_check "counted on an unreachable plane" "yes"
""",
    )
    assert "  PASS  a bot with no restart counts 0" in r.stdout, r.stdout
    assert "M<>" in r.stdout
    assert re.search(
        r"  FAIL  counted on an unreachable plane — REFUSED, .*"
        rf"keepalive entries of {F}/w1 \(rc 1\): PlaneUnreachable: no plane db at ",
        r.stdout,
    ), r.stdout
    assert _counts(r.stdout) == (1, 1, 1)


def test_val_poll_scores_one_read_not_every_miss(tmp_path):
    # A poll's misses are not refusals: tries that never answer record ONE
    # refusal, from the read after them, and a poll that gets its answer
    # records none and hands the answer back.
    r = _run(
        tmp_path,
        """
n=$(val_poll 3 0 val_read "never answers" false)
harness_check "after a poll that never got an answer" "yes"
val_scenario "next"
m=$(val_poll 3 0 val_read "answers" echo 2)
printf 'M<%s>\\n' "$m"
harness_check "after a poll that got one" "yes"
""",
    )
    assert "M<2>" in r.stdout
    assert _counts(r.stdout) == (1, 1, 1), r.stdout
    line = [ln for ln in r.stdout.splitlines() if "never got an answer" in ln][0]
    assert line.count("never answers (rc 1)") == 1, line


def test_val_probe_captures_a_refusal_and_records_nothing(tmp_path):
    # A door called the wrong way on purpose (#1187): its rc 2 IS the answer.
    r = _run(
        tmp_path,
        """
val_probe python3 "$LIB_DIR/dispatch-overdue.py" --open /x/dispatch-log.jsonl /x/report-back.jsonl 1
printf 'RC<%s> OUT<%s>\\n' "$PROBE_RC" "$PROBE_OUT"
grep -q "expects <bot_id> first" "$VAL_READ_ERR" && v=yes || v=no
harness_check "the refusal names the grammar split" "$v"
""",
    )
    assert "RC<2> OUT<>" in r.stdout, r.stdout
    assert _counts(r.stdout) == (1, 0, 0), r.stdout


# --- ratchets on the harness's shape ----------------------------------------


def _code_lines(text: str) -> list[tuple[int, int, str]]:
    """(first line, last line, command) per command: continuations joined,
    comment lines dropped."""
    out, buf, start, n = [], [], 0, 0
    for n, ln in enumerate(text.split("\n"), 1):
        if not buf:
            start = n
        if ln.endswith("\\"):
            buf.append(ln[:-1])
            continue
        buf.append(ln)
        out.append((start, n, " ".join(buf)))
        buf = []
    if buf:
        out.append((start, n, " ".join(buf)))
    return [c for c in out if not c[2].lstrip().startswith("#")]


# The CLI as a command word: not a module (`sqlite3.connect`, `import sqlite3,`)
# and not a path fragment. Any flag or argument shape after it matches.
_CLI_CALL = re.compile(r"(?<![\w./-])sqlite3(?=\s|$)")


def _cli_calls(text: str) -> list[str]:
    return [f"{a}: {cmd.strip()[:120]}" for a, _, cmd in _code_lines(text) if _CLI_CALL.search(cmd)]


def test_the_harness_invokes_no_sqlite3_cli():
    offenders = _cli_calls(HARNESS.read_text())
    assert offenders == [], "\n".join(offenders)


def test_the_cli_ratchet_matches_the_calls_it_replaced():
    # Positive control: the ratchet matches each shape #1777 removed, and each
    # revert the #1796 review found it missed (a flag first; a continuation),
    # and passes the stdlib uses that replaced them.
    for old in [
        'val_sql() { sqlite3 "$1/state/plane/plane.db" "$2" 2>/dev/null || true; }',
        '        sqlite3 "$PL_ROOT/state/plane/plane.db" \\',
        '    _pl_uv=$(sqlite3 "$PL_ROOT/state/plane/plane.db" "PRAGMA user_version")',
        '    _pl_count() { sqlite3 -readonly "$PL_ROOT/state/plane/plane.db" "SELECT 1"; }',
        "    _pl_count() { sqlite3 \\",
    ]:
        assert _cli_calls(old), old
    for new in [
        "import sqlite3, sys",
        'c = sqlite3.connect("file:%s?mode=rw" % sys.argv[1])',
        "# never the sqlite3 CLI, which a host need not have",
    ]:
        assert not _cli_calls(new), new


# The doors that read the plane: every lib script that loads plane-readers
# (the reader itself included), found rather than listed, and the CLI verbs
# that print plane rows.
_DOORS = sorted(p.stem for p in (REPO / "lib").glob("*.py") if "plane-readers" in p.read_text())
_DOOR = r"\b(?:" + "|".join(map(re.escape, _DOORS)) + r")\.py\b"
_DOOR_CALL = re.compile(
    rf"python3\b[^|;]*{_DOOR}"
    r'|"\$VAL_CLI"[^|;]*\b(?:checkins|events|report-back|workstreams|brief|uptime)\b'
)
_HEREDOC = re.compile(r"<<-?'?(\w+)'?[^\n]*\n(.*?)\n\1\n", re.S)


def _bare_door_calls(text: str) -> list[str]:
    """Plane reads that bypass val_read. A val_probe is not a read: it calls a
    door the wrong way on purpose, and the refusal is the answer."""
    bare = [
        f"{a}: {cmd.strip()[:120]}"
        for a, _, cmd in _code_lines(text)
        if _DOOR_CALL.search(cmd) and not re.search(r"\bval_(?:read|probe)\b", cmd)
    ]
    bare += [
        f"heredoc: {m.group(2).strip()[:120]}"
        for m in _HEREDOC.finditer(text)
        if re.search(_DOOR, m.group(2))
    ]
    return bare


def test_every_plane_read_in_the_harness_goes_through_val_read():
    assert {"dispatch-overdue", "plane-lookup", "plane-readers"} <= set(_DOORS), _DOORS
    offenders = _bare_door_calls(HARNESS.read_text())
    assert offenders == [], "\n".join(offenders)


def test_the_read_ratchet_flags_the_shapes_the_review_found():
    # Positive control: each bare shape the #1796 review listed. Two absence
    # checks read their swallowed refusal as 0 through the first two.
    for old in [
        'aged_out=$(python3 "$LIB_DIR/dispatch-overdue.py" --all "$(date +%s)" \\\n'
        '    --fleet "$FLEET" --root "$ROOT" 2>/dev/null | grep -c "^valaged " || true)\n',
        'ta_esc_after=$(python3 -S -E "$LIB_DIR/plane-lookup.py" --root "$ROOT" --escalated \\\n'
        "    --fleet \"$FLEET\" 2>/dev/null | grep -c 't-1481-0003' || true)\n",
        'CLAUDLOBBY_ROOT="$ROOT" "$VAL_CLI" --root "$ROOT" checkins --fleet "$F" --json \\\n'
        '    > "$ROOT/ck-read.out" 2> "$ROOT/ck-read.err" || true\n',
        '    dead_restarts=$(python3 - "$LIB_DIR" "$ROOT" <<\'PY\' 2>/dev/null || echo 0\n'
        'spec = importlib.util.spec_from_file_location("pr", sys.argv[1] + "/plane-readers.py")\n'
        "PY\n",
    ]:
        assert _bare_door_calls(old), old
    for new in [
        't=$(val_read "x" python3 "$LIB_DIR/dispatch-overdue.py" --open "$B" \\\n'
        '    --fleet "$FLEET" --root "$ROOT" | grep -c x || true)\n',
        'val_probe python3 "$LIB_DIR/dispatch-overdue.py" --open \\\n    "$a" "$b" "$now"\n',
    ]:
        assert not _bare_door_calls(new), new


_READERS = ("val_read", "val_events", "val_sql")
_WRITES_TO = re.compile(r'(?:>|\bVAL_READ_ERR=)\s*("[^"]+")')


def _word(names) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(map(re.escape, names)) + r")\b")


def _read_wrappers(text: str) -> set[str]:
    """The readers, and every harness function whose body calls one."""
    lines = text.split("\n")
    bodies = {}
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)(\w+)\(\)\s*\{(.*)$", ln)
        if not m:
            continue
        indent, name, rest = m.groups()
        if rest.rstrip().endswith("}"):
            bodies[name] = rest
            continue
        body = []
        for nxt in lines[i + 1 :]:
            if re.match(rf"^{indent}\}}\s*$", nxt):
                break
            body.append(nxt)
        bodies[name] = "\n".join(body)
    names = set(_READERS)
    grew = True
    while grew:
        grew = False
        for name, body in bodies.items():
            if name not in names and _word(names).search(body):
                names.add(name)
                grew = True
    return names


def _cross_boundary_consumers(text: str) -> tuple[list[str], int, int]:
    """Reads consumed past the next val_scenario, where a check has already
    reported their refusal and the boundary dropped it. A read lands in a
    variable (x=$(read)) or a file (read > "file", or its VAL_READ_ERR). Also
    returns how many reads and boundaries it saw, so a pass is never a pass
    over nothing."""
    lines = text.split("\n")
    readers = _word(_read_wrappers(text))
    bounds = [n for n, ln in enumerate(lines, 1) if re.match(r"^\s*val_scenario\s", ln)]
    reads = []  # (first line, last line, key, a use of it, a rewrite of it)
    for start, end, cmd in _code_lines(text):
        if not readers.search(cmd):
            continue
        m = re.match(r"^\s*(?:local\s+)?([A-Za-z_]\w*)=(.*)$", cmd, re.S)
        if m and "$(" in m.group(2):
            var = re.escape(m.group(1))
            reads.append((start, end, m.group(1), rf"\$\{{?{var}\b", rf"(?:^|[;\s])(?:local\s+)?{var}=(?!=)"))
        for target in _WRITES_TO.findall(cmd):
            path = re.escape(target)
            reads.append((start, end, target, path, rf"(?:>|\bVAL_READ_ERR=)\s*{path}"))
    offenders = []
    for start, end, key, use, rewrite in reads:
        after = [b for b in bounds if b > end]
        if not after:
            continue
        use_re, rewrite_re = re.compile(use), re.compile(rewrite)
        for n in range(end + 1, len(lines) + 1):
            ln = lines[n - 1]
            if key not in ln or ln.lstrip().startswith("#"):
                continue
            if n > after[0] and use_re.search(ln):
                offenders.append(f"{key} read at {start}, consumed at {n}, past the boundary at {after[0]}")
            if rewrite_re.search(ln):
                break
    return offenders, len(reads), len(bounds)


def test_no_read_is_consumed_past_the_next_scenario_boundary():
    # A boundary drops every refusal a check has reported, so a check after it
    # that consumes an earlier read would score an unreadable read again.
    offenders, reads, bounds = _cross_boundary_consumers(HARNESS.read_text())
    assert reads > 40 and bounds > 30, (reads, bounds)
    assert offenders == [], "\n".join(offenders)


def test_the_boundary_ratchet_flags_a_read_consumed_across_one():
    # Positive controls. The task-id section reads the first scenario's rows,
    # so a val_scenario at its header is exactly the shape this exists for;
    # and a read written to a file is a read too.
    text = HARNESS.read_text()
    plain = 'echo ""\necho "=== validate task-id end-to-end (P4: event + nudge carry the id) ==="\n'
    assert text.count(plain) == 1
    moved = text.replace(
        plain, 'val_scenario "validate task-id end-to-end (P4: event + nudge carry the id)"\n'
    )
    offenders, _, _ = _cross_boundary_consumers(moved)
    assert any(o.startswith("events_rows read at ") for o in offenders), offenders
    to_file = (
        'val_scenario "a"\n'
        'val_read "checkins" "$VAL_CLI" --root "$ROOT" checkins > "$ROOT/out"\n'
        'harness_check "x" "yes"\n'
        'val_scenario "b"\n'
        'grep -q id "$ROOT/out" && r=yes || r=no\n'
    )
    offenders, _, _ = _cross_boundary_consumers(to_file)
    assert offenders == ['"$ROOT/out" read at 2, consumed at 5, past the boundary at 4'], offenders


_ASSIGN_SUB = re.compile(r'^\s*(?:local\s+)?\w+="?\$\(')


def _unguarded_greps(text: str) -> list[str]:
    """Assignments that end the run under set -e when a grep in them matches
    nothing: under pipefail the substitution fails, and the bare assignment
    with it. A `||` or `&&` on the line guards it."""
    return [
        f"{a}: {cmd.strip()[:120]}"
        for a, _, cmd in _code_lines(text)
        if _ASSIGN_SUB.match(cmd)
        and re.search(r"\bgrep\b", cmd)
        and "||" not in cmd
        and "&&" not in cmd
    ]


def test_no_grep_in_an_assignment_can_end_the_run():
    # An empty read once ended the whole run at its sixth check, with no
    # summary line, so the checks after it never ran.
    offenders = _unguarded_greps(HARNESS.read_text())
    assert offenders == [], "\n".join(offenders)


def test_the_grep_ratchet_flags_a_shape_that_really_ends_the_run(tmp_path):
    # Positive control, and its premise: the unguarded form of the #1728 read
    # really does end the run on an empty read, and the guarded form does not.
    guarded = next(ln for ln in HARNESS.read_text().split("\n") if ln.startswith("_wip_mixed="))
    bare = guarded.replace(" || true)", ")")
    assert bare != guarded
    assert _unguarded_greps(bare) and not _unguarded_greps(guarded)
    for line, survives in ((guarded, True), (bare, False)):
        r = _run(tmp_path, f'events_rows=""\n{line}\necho REACHED')
        assert ("REACHED" in r.stdout) is survives, (line, r.stdout, r.stderr)
