"""Task-ID'd dispatch, shell side (goal-aware plan P4, locked fork F3).

Covers the mint (`mint_task_id` in lib-common) and the shell round trip
(dispatch-task mints/ledgers/envelopes; report-back echoes). Join-matrix
semantics live with the matcher: tests/test_dispatch_overdue.py.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from tests.conftest import _scrubbed_env, dispatch_row as _dispatch

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "lib"

TASK_ID_RE = re.compile(r"^t-[0-9]+-[0-9a-f]{4}$")


def _bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    # Build the subprocess env from the scrubbed base so an inherited
    # FLEET_NAME / CLAUDLOBBY_* / BOT_* / TELEGRAM* (leaked from a live bot
    # session) can't reroute the script's path resolution — hermetic
    # regardless of the runner's env. Tests supply what they need via `env`.
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_scrubbed_env(**(env or {})),
        timeout=10,
    )


def _sourced(fn_call: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return _bash(f'. "{LIB_DIR}/lib-common.sh"; {fn_call}', env=env)


def _fake_lib(tmp_path: Path, dispatch_stub: str) -> tuple[Path, dict]:
    """A minimal lib/ for driving the real dispatch-task.sh with a stubbed
    transport: dispatch-task + lib-common are symlinked (BASH_SOURCE resolves
    LIB_DIR through the symlink dir, so "$LIB_DIR/dispatch.sh" hits our stub),
    dispatch.sh and tmux are stubs. Returns (libdir, env)."""
    libdir = tmp_path / "lib"
    libdir.mkdir()
    for name in ("dispatch-task.sh", "lib-common.sh"):
        (libdir / name).symlink_to(LIB_DIR / name)
    stub = libdir / "dispatch.sh"
    stub.write_text(dispatch_stub)
    stub.chmod(0o755)
    tmux = tmp_path / "tmux"
    tmux.write_text("#!/bin/bash\nexit 0\n")
    tmux.chmod(0o755)
    env = {
        "CLAUDLOBBY_ROOT": str(tmp_path),
        "TMUX_BIN": str(tmux),
        "OBSERVABILITY_DISPATCH_DEADLINE": "600",
        "BOT_ID": "lead",
    }
    return libdir, env


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


def test_dispatch_task_mints_ledgers_and_envelopes(tmp_path):
    libdir, env = _fake_lib(
        tmp_path,
        f'#!/bin/bash\nprintf \'%s\\n\' "$2" > "{tmp_path}/sent.txt"\n',
    )
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "fix the widget"', env=env)
    assert r.returncode == 0, r.stderr
    row = json.loads(
        (tmp_path / "state" / "dispatch-log.jsonl").read_text().splitlines()[-1]
    )
    assert TASK_ID_RE.match(row["task_id"]), row
    sent = (tmp_path / "sent.txt").read_text()
    assert f"task:{row['task_id']}" in sent, "envelope must carry the minted id"


def test_a_clean_dispatch_writes_nothing_to_stderr(tmp_path):
    """The happy path must be SILENT, not merely successful.

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

    Scoped honestly: this closes the channel for ONE path. The shell suite's
    blanket `2>/dev/null` is untouched and still hides the same class elsewhere.
    """
    libdir, env = _fake_lib(tmp_path, "#!/bin/bash\nexit 0\n")
    r = _bash(f'"{libdir}/dispatch-task.sh" w1 "fix the widget"', env=env)
    assert r.returncode == 0, r.stderr
    assert r.stderr == "", (
        f"a successful dispatch wrote to stderr: {r.stderr!r} — an exit-0 run that "
        "complains is a defect the return code cannot report"
    )


def test_missing_flag_value_is_a_loud_error(tmp_path):
    # ${2:?} guards exit 0 through the EXIT trap on bash 3.2 — the explicit
    # _flag_val guard must fail loudly instead (review 6b).
    libdir, env = _fake_lib(tmp_path, "#!/bin/bash\nexit 0\n")
    r = _bash(f'"{libdir}/dispatch-task.sh" --repo', env=env)
    assert r.returncode != 0
    assert "needs a value" in r.stderr


def test_report_back_echoes_task_id_to_ledger(tmp_path):
    env = {
        "CLAUDLOBBY_ROOT": str(tmp_path),
        "MANAGER_TMUX": "mgr",
        "MANAGER_TMUX_SOCKET": "mgr-sock",
        "TMUX_BIN": "/usr/bin/true",
    }
    r = _bash(
        f'"{LIB_DIR}/report-back.sh" w1 completed "widget shipped" --task t-123-abcd',
        env=env,
    )
    assert r.returncode == 0, r.stderr
    row = json.loads(
        (tmp_path / "runtime" / "fleet" / "report-back.jsonl")
        .read_text()
        .splitlines()[-1]
    )
    assert row["task_id"] == "t-123-abcd", row


def test_dispatch_log_self_rotates(tmp_path):
    libdir, env = _fake_lib(tmp_path, "#!/bin/bash\nexit 0\n")
    state = tmp_path / "state"
    state.mkdir()
    old = json.dumps(_dispatch("w9", 1, 2) | {"ts": "2020-01-01T00:00:00Z"})
    (state / "dispatch-log.jsonl").write_text(old + "\n")
    r = _bash(f'"{libdir}/dispatch-task.sh" w1 "new task"', env=env)
    assert r.returncode == 0, r.stderr
    text = (state / "dispatch-log.jsonl").read_text()
    assert "2020-01-01" not in text, "entries past retention must rotate out"
    assert "new task" in text


def test_report_back_rotation_honors_reap_days(tmp_path):
    # The shared rotate_jsonl_by_ts made report-back honor
    # OBSERVABILITY_REAP_DAYS (it hard-coded 7d before the consolidation).
    env = {
        "CLAUDLOBBY_ROOT": str(tmp_path),
        "MANAGER_TMUX": "mgr",
        "MANAGER_TMUX_SOCKET": "mgr-sock",
        "TMUX_BIN": "/usr/bin/true",
        "OBSERVABILITY_REAP_DAYS": "3650",  # ~10y window keeps a 2020 row
    }
    ledger_dir = tmp_path / "runtime" / "fleet"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "report-back.jsonl").write_text(
        '{"ts":"2020-01-01T00:00:00Z","bot":"w9","task_id":"","status":"completed",'
        '"summary":"old","pr_url":"","issues":"","skill":"","progress":"","artifact":""}\n'
    )
    r = _bash(f'"{LIB_DIR}/report-back.sh" w1 completed "new"', env=env)
    assert r.returncode == 0, r.stderr
    text = (ledger_dir / "report-back.jsonl").read_text()
    assert "2020-01-01" in text, "wide reap window must retain the old row"
