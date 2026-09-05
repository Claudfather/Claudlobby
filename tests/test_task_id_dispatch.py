"""Task-ID'd dispatch, shell side (goal-aware plan P4, locked fork F3).

Covers the mint (`mint_task_id` in lib-common) and the shell round trip
(dispatch-task mints, records on the PLANE and envelopes; report-back records
its report). Join-matrix semantics live with the matcher:
tests/test_dispatch_overdue.py.

F18 closure (R1): the doors write NO ledger any more — the plane is the only
record — so the round trip is read back from the plane db the real shim lands
(cold-CLI rung; no daemon in this harness, the socket rung's failure is
disclosed on stderr). The two rotation tests (`test_dispatch_log_self_rotates`,
`test_report_back_rotation_honors_reap_days`) went with the ledgers: there is
nothing left to rotate.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from tests.conftest import _scrubbed_env
from tests.plane_fixtures import ro as _ro
from tests.test_plane_shadow import F as FLEET

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "lib"
CLI = Path(sys.executable).parent / "claudlobby"

TASK_ID_RE = re.compile(r"^t-[0-9]+-[0-9a-f]{4}$")

# The lines the shim itself writes on a daemon-less host (measured in this
# harness): the socket rung fails and says so, then the cold-CLI rung records.
# Every other stderr byte on a clean dispatch is a defect.
SHIM_STDERR_RE = re.compile(
    r"^(plane-emit: (daemon unavailable \(rc=\d+\) — falling back to cold CLI"
    r"|socket in wedge cooldown \(\d+s\) — straight to cold CLI)"
    r"|plane-socket-client: transport failed: .*)$"   # the socket rung's own voice (Linux: ENOENT; macOS: path too long)
)

# The lib files a door reaches through $LIB_DIR: the plane shim (THE record),
# the stdlib readers the doors consult, and the hint helper — a
# `2>/dev/null || true` call site, so a harness missing it is a silent no-op
# rather than an error, which is why it is symlinked rather than left out.
DOOR_FILES = (
    "dispatch-task.sh", "report-back.sh", "lib-common.sh",
    "plane-emit.sh", "plane-socket-client.py", "plane-lookup.py", "plane-readers.py",
    "dispatch-overdue.py", "dispatch-supersede-hint.py",
)


def _bash(script: str, env: dict | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    # Build the subprocess env from the scrubbed base so an inherited
    # FLEET_NAME / CLAUDLOBBY_* / BOT_* / TELEGRAM* (leaked from a live bot
    # session) can't reroute the script's path resolution — hermetic
    # regardless of the runner's env. Tests supply what they need via `env`.
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_scrubbed_env(**(env or {})),
        timeout=timeout,
    )


def _sourced(fn_call: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return _bash(f'. "{LIB_DIR}/lib-common.sh"; {fn_call}', env=env)


def plane_env(root: Path) -> dict:
    """The env that makes a door RECORD into <root>/state/plane/plane.db: the
    fleet (a door records nothing without one — plane_armed --require-fleet),
    the venv's real CLI as the shim's cold rung, and a socket path no daemon
    listens on. The capture policy keeps bodies so tests can read them back."""
    (root / "state" / "plane").mkdir(parents=True, exist_ok=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    return {
        "CLAUDLOBBY_ROOT": str(root),
        "FLEET_NAME": FLEET,
        "PLANE_EMIT_CLI": str(CLI),
        "PLANE_SOCKET": str(root / "no-daemon.sock"),
    }


def _fake_lib(tmp_path: Path, dispatch_stub: str) -> tuple[Path, dict]:
    """A minimal lib/ for driving the real doors with a stubbed transport and a
    REAL plane: the doors + lib-common + the shim are symlinked (BASH_SOURCE
    resolves LIB_DIR through the symlink dir, so "$LIB_DIR/dispatch.sh" hits
    our stub), dispatch.sh and tmux are stubs. Returns (libdir, env)."""
    libdir = tmp_path / "lib"
    libdir.mkdir(exist_ok=True)          # idempotent: a test may dispatch twice into one root
    for name in DOOR_FILES:
        if not (libdir / name).exists():
            (libdir / name).symlink_to(LIB_DIR / name)
    stub = libdir / "dispatch.sh"
    stub.write_text(dispatch_stub)
    stub.chmod(0o755)
    tmux = tmp_path / "tmux"
    tmux.write_text("#!/bin/bash\nexit 0\n")
    tmux.chmod(0o755)
    env = plane_env(tmp_path)
    env.update({
        "TMUX_BIN": str(tmux),
        "OBSERVABILITY_DISPATCH_DEADLINE": "600",
        "BOT_ID": "lead",
        "BOT_NAME": "lead",
    })
    return libdir, env


def _epoch(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def plane_dispatch_row(root: Path) -> dict | None:
    """The NEWEST dispatch the plane holds, in the retired ledger row's field
    names (task_id, expected_by, bot, task, ...): task_id is the id the
    assignment's source_ref carries (`dispatch-log:t-...`), "" for an id-less
    dispatch (`dispatch-log:sha:...`); expected_by is the assignment's deadline
    as an epoch, None when withheld; task is the message AS SENT (the envelope
    around the task text — the plane records what the worker received). None
    when the plane holds no dispatch — including when no plane db exists at
    all (nothing was ever recorded)."""
    if not (root / "state" / "plane" / "plane.db").exists():
        return None
    with _ro(root) as conn:
        row = conn.execute(
            "SELECT c.msg_id, c.body, c.message_class, c.command_type, c.recipient_raw,"
            " a.source_ref, a.expected_by, c.assignment_id, c.work_item_id"
            " FROM communications c LEFT JOIN assignments a ON a.assignment_id = c.assignment_id"
            " WHERE c.emitter = 'dispatch-task' ORDER BY c.ingest_seq DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    msg, body, mclass, ctype, recipient, ref, expected_by, asg, wi = row
    ref = ref or ""
    task_id = ref[len("dispatch-log:"):] if ref.startswith("dispatch-log:t-") else ""
    return {
        "task_id": task_id, "expected_by": _epoch(expected_by), "bot": recipient,
        "task": body, "message_class": mclass, "command_type": ctype,
        "plane_msg_id": msg, "plane_assignment_id": asg or "", "plane_work_item_id": wi or "",
    }


def plane_report_rows(root: Path) -> list[dict]:
    """Every report the plane holds, oldest first: the report communication's
    body is the [BOTREPORT] line as sent — the summary the door was given
    rides inside it."""
    if not (root / "state" / "plane" / "plane.db").exists():
        return []
    with _ro(root) as conn:
        rows = conn.execute(
            "SELECT msg_id, sender_alias, body FROM communications"
            " WHERE emitter = 'report-back' AND message_class = 'report' ORDER BY ingest_seq"
        ).fetchall()
    return [{"plane_msg_id": m, "sender": s, "summary": b} for m, s, b in rows]


# --- mint_task_id (lib-common SSOT) ----------------------------------------------


class TestMintTaskId:
    def test_shape_matches_pinned_grammar(self):
        r = _sourced("mint_task_id")
        assert r.returncode == 0, r.stderr
        assert TASK_ID_RE.match(r.stdout.strip()), r.stdout

    def test_two_mints_differ(self):
        r = _sourced("mint_task_id; mint_task_id")
        a, b = r.stdout.split()
        assert a != b, "collision-safety: same-second mints must differ"

    def test_survives_sanitize_tmux_input(self):
        r = _sourced('tid=$(mint_task_id); sanitize_tmux_input "$tid"')
        assert TASK_ID_RE.match(r.stdout.strip()), (
            "id must pass the envelope sanitizer untouched"
        )


# --- shell round trip -------------------------------------------------------------


def test_dispatch_task_mints_records_on_the_plane_and_envelopes(tmp_path):
    libdir, env = _fake_lib(
        tmp_path,
        f'#!/bin/bash\nprintf \'%s\\n\' "$2" > "{tmp_path}/sent.txt"\n',
    )
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "fix the widget"', env=env)
    assert r.returncode == 0, r.stderr
    row = plane_dispatch_row(tmp_path)
    assert row is not None, "the dispatch must be recorded on the plane — there is no other record"
    assert TASK_ID_RE.match(row["task_id"]), row
    assert row["bot"] == "w1" and "fix the widget" in row["task"], row
    assert row["plane_assignment_id"] and row["plane_work_item_id"], row
    sent = (tmp_path / "sent.txt").read_text()
    assert f"task:{row['task_id']}" in sent, "envelope must carry the minted id"


def test_a_clean_dispatch_writes_only_the_shims_disclosure_to_stderr(tmp_path):
    """The happy path must be SILENT but for the shim's own voice.

    This asserts a channel, not a behaviour, and it exists because the suite was
    structurally blind to that channel: every call in tests/test_dispatch_task.sh
    redirects `2>/dev/null`, and the assertions here read stderr only as a failure
    MESSAGE (`assert r.returncode == 0, r.stderr`), which is invisible while the
    run passes. A defect that is noisy but exits 0 therefore had nowhere to fail.

    One did. `--supersedes` landed with no default init beside its five siblings,
    so under `set -u` the ledger printf's `$(json_escape "$DISPATCH_SUPERSEDES")`
    faulted on EVERY dispatch that omitted the flag. It stayed invisible because
    the fault is confined to the command substitution's subshell: that subshell
    dies, expands to empty, and the parent printf still succeeds — the row is
    correct, the exit code is 0, and only stderr ever knew. Exactly the state a
    return-code assertion cannot distinguish.

    Since the F18 closure a clean dispatch on a daemon-less host is never byte-
    silent: the shim discloses its socket rung's failure before the cold-CLI
    rung records (the fallback disclosure IS the contract). So the assertion
    is that every stderr line is one of the shim's own — nothing else, and
    never the door's "did NOT record" line.

    Scoped honestly: this closes the channel for ONE path. The shell suite's
    blanket `2>/dev/null` is untouched and still hides the same class elsewhere.
    """
    libdir, env = _fake_lib(tmp_path, "#!/bin/bash\nexit 0\n")
    r = _bash(f'"{libdir}/dispatch-task.sh" w1 "fix the widget"', env=env)
    assert r.returncode == 0, r.stderr
    foreign = [ln for ln in r.stderr.splitlines() if not SHIM_STDERR_RE.match(ln)]
    assert foreign == [], (
        f"a successful dispatch wrote to stderr beyond the shim's disclosure: {foreign!r} — "
        "an exit-0 run that complains is a defect the return code cannot report"
    )
    assert plane_dispatch_row(tmp_path) is not None, "and the dispatch was recorded"


def test_missing_flag_value_is_a_loud_error(tmp_path):
    # ${2:?} guards exit 0 through the EXIT trap on bash 3.2 — the explicit
    # _flag_val guard must fail loudly instead (review 6b).
    libdir, env = _fake_lib(tmp_path, "#!/bin/bash\nexit 0\n")
    r = _bash(f'"{libdir}/dispatch-task.sh" --repo', env=env)
    assert r.returncode != 0
    assert "needs a value" in r.stderr


def test_report_back_records_the_report_on_the_plane(tmp_path):
    env = plane_env(tmp_path)
    env.update({
        "MANAGER_TMUX": "mgr",
        "MANAGER_TMUX_SOCKET": "mgr-sock",
        "TMUX_BIN": "/usr/bin/true",
    })
    r = _bash(
        f'"{LIB_DIR}/report-back.sh" w1 completed "widget shipped" --task t-123-abcd',
        env=env,
    )
    assert r.returncode == 0, r.stderr
    rows = plane_report_rows(tmp_path)
    assert len(rows) == 1 and "widget shipped" in rows[0]["summary"], rows
    assert "task:t-123-abcd" in rows[0]["summary"], "the report carries the id it was given"
    assert rows[0]["sender"] == f"bot:{FLEET}/w1", rows
