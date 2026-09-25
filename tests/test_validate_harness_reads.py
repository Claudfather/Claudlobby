"""The validation harness's plane reads: the shipped stdlib doors, never the
sqlite3 CLI, and a read that cannot run is REFUSED rather than read as empty
(#1777).

The CLI is absent on the fleet's primary Pi, where it read as "0 rows" and
failed 22 checks with no reason given, while CI (whose image has it) passed.
Worse is the other direction: a check expecting ABSENCE read the same empty
answer as a pass. These tests run the reader block lifted verbatim from
`lib/validate-bot-change.sh`, so they exercise the shipped text.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

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
