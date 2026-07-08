"""Task-ID'd dispatch (goal-aware plan P4, locked fork F3).

Whatever admits work mints the id (`mint_task_id` in lib-common →
`t-<epochsecs>-<4hex>`); the dispatch-ledger row is the SSOT; the id
threads [BOTCOMMAND] → worker → [BOTREPORT] → report ledger; the overdue
matcher joins on it. Join matrix (ironclad ruling): id↔id joins exactly;
an id-less terminal report closes ONLY id-less (pre-migration) dispatch
rows — never id'd ones (LLM echo non-compliance is normal, #447; erosion
is surfaced by a missing-id counter, not silent degradation).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "lib"

_spec = importlib.util.spec_from_file_location(
    "dispatch_overdue", REPO_ROOT / "lib" / "dispatch-overdue.py"
)
dispatch_overdue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dispatch_overdue)

TASK_ID_RE = re.compile(r"^t-[0-9]+-[0-9a-f]{4}$")


def _bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        timeout=10,
    )


def _sourced(fn_call: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return _bash(f'. "{LIB_DIR}/lib-common.sh"; {fn_call}', env=env)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _dispatch(bot, dispatched_at, expected_by, task_id=None):
    row = {
        "ts": "2026-05-27T10:00:00Z",
        "manager": "lead",
        "bot": bot,
        "task": "do x",
        "dispatched_at": dispatched_at,
        "expected_by": expected_by,
    }
    if task_id is not None:
        row["task_id"] = task_id
    return row


def _report(bot, ts, status="completed", task_id=None):
    row = {
        "ts": ts,
        "bot": bot,
        "status": status,
        "summary": "done",
        "pr_url": "",
        "issues": "",
        "skill": "",
    }
    if task_id is not None:
        row["task_id"] = task_id
    return row


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


# --- join matrix (dispatch-overdue.py) --------------------------------------------


class TestJoinMatrix:
    NOW = 2000

    def _run(self, tmp_path, dispatches, reports):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, dispatches)
        _write_jsonl(rlog, reports)
        return dispatch_overdue.overdue_all(str(dlog), str(rlog), self.NOW)

    def test_id_report_closes_exactly_its_own_dispatch(self, tmp_path):
        out = self._run(
            tmp_path,
            [
                _dispatch("w1", 100, 1000, task_id="t-100-aaaa"),
                _dispatch("w1", 200, 1000, task_id="t-200-bbbb"),
            ],
            [_report("w1", "1970-01-01T00:05:00Z", task_id="t-100-aaaa")],
        )
        assert [d[0] for d in out.get("w1", [])] == [200], (
            "the un-reported sibling dispatch must stay open"
        )

    def test_idless_terminal_never_closes_an_id_dispatch(self, tmp_path):
        # The #447 fix, preserved: one id-less report must not blanket-close.
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
            [_report("w1", "1970-01-01T00:05:00Z")],  # no task_id
        )
        assert out.get("w1"), "id'd dispatch must remain overdue"

    def test_idless_terminal_closes_idless_dispatch(self, tmp_path):
        # Legacy rows keep the pre-migration (bot, ts) semantics — no flag-day.
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000)],
            [_report("w1", "1970-01-01T00:05:00Z")],
        )
        assert not out.get("w1")

    def test_id_report_closes_idless_dispatch_too(self, tmp_path):
        # An id-carrying terminal report still satisfies a legacy dispatch
        # (it is strictly more informative than the old contract required).
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000)],
            [_report("w1", "1970-01-01T00:05:00Z", task_id="t-999-ffff")],
        )
        assert not out.get("w1")

    def test_report_from_wrong_bot_does_not_close(self, tmp_path):
        # Review finding (#518): the id join must be scoped by (bot, id) —
        # a peer echoing (or mishearing) another bot's task id must not
        # silence the watchdog on the real owner's still-open dispatch.
        out = self._run(
            tmp_path,
            [_dispatch("worker-a", 100, 1000, task_id="t-100-aaaa")],
            [_report("worker-b", "1970-01-01T00:05:00Z", task_id="t-100-aaaa")],
        )
        assert out.get("worker-a"), (
            "wrong-bot report must not close the dispatch"
        )

    def test_wrong_id_does_not_close(self, tmp_path):
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
            [_report("w1", "1970-01-01T00:05:00Z", task_id="t-777-dddd")],
        )
        assert out.get("w1")

    def test_progress_report_with_id_does_not_close(self, tmp_path):
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
            [
                _report(
                    "w1", "1970-01-01T00:05:00Z", status="progress",
                    task_id="t-100-aaaa",
                )
            ],
        )
        assert out.get("w1"), "non-terminal progress reuses the id, never closes"


class TestMissingIdCounter:
    def test_counts_idless_terminal_reports(self, tmp_path):
        rlog = tmp_path / "r.jsonl"
        _write_jsonl(
            rlog,
            [
                _report("w1", "1970-01-01T00:05:00Z"),
                _report("w1", "1970-01-01T00:06:00Z", task_id="t-1-aaaa"),
                _report("w2", "1970-01-01T00:07:00Z", status="progress"),
                _report("w2", "1970-01-01T00:08:00Z", status="failed"),
            ],
        )
        # terminal + id-less: w1's first report and w2's failed report
        assert dispatch_overdue.missing_id_count(str(rlog)) == 2


# --- shell integration: dispatch-task mints, report-back echoes -------------------


class TestShellRoundTrip:
    def _stub_env(self, tmp_path: Path) -> dict:
        """Stub dispatch.sh + tmux so no real session is needed."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "tmux").write_text("#!/bin/bash\nexit 0\n")
        (bindir / "tmux").chmod(0o755)
        stub_dispatch = tmp_path / "dispatch.sh"
        stub_dispatch.write_text(
            f'#!/bin/bash\nprintf \'%s\\n\' "$2" > "{tmp_path}/sent.txt"\n'
        )
        stub_dispatch.chmod(0o755)
        return {
            "CLAUDLOBBY_ROOT": str(tmp_path),
            "TMUX_BIN": str(bindir / "tmux"),
            "OBSERVABILITY_DISPATCH_DEADLINE": "600",
            "BOT_ID": "lead",
        }

    def test_dispatch_task_mints_ledgers_and_envelopes(self, tmp_path):
        env = self._stub_env(tmp_path)
        # Point dispatch-task at the stub transport by copying it beside a
        # linked dispatch.sh (the script invokes "$LIB_DIR/dispatch.sh").
        libdir = tmp_path / "lib"
        libdir.mkdir()
        for f in LIB_DIR.glob("*"):
            (libdir / f.name).symlink_to(f)
        (libdir / "dispatch.sh").unlink()
        (libdir / "dispatch.sh").symlink_to(tmp_path / "dispatch.sh")
        r = _bash(
            f'"{libdir}/dispatch-task.sh" --botcommand w1 "fix the widget"',
            env=env,
        )
        assert r.returncode == 0, r.stderr
        ledger = (tmp_path / "state" / "dispatch-log.jsonl").read_text()
        row = json.loads(ledger.splitlines()[-1])
        assert TASK_ID_RE.match(row["task_id"]), row
        sent = (tmp_path / "sent.txt").read_text()
        assert f"task:{row['task_id']}" in sent, (
            "envelope must carry the minted id"
        )

    def test_report_back_echoes_task_id_to_ledger(self, tmp_path):
        env = {
            "CLAUDLOBBY_ROOT": str(tmp_path),
            "MANAGER_TMUX": "mgr",
            "MANAGER_TMUX_SOCKET": "mgr-sock",
            "TMUX_BIN": "/usr/bin/true",
        }
        r = _bash(
            f'"{LIB_DIR}/report-back.sh" w1 completed "widget shipped" '
            f"--task t-123-abcd",
            env=env,
        )
        assert r.returncode == 0, r.stderr
        ledger = (tmp_path / "runtime" / "fleet" / "report-back.jsonl").read_text()
        row = json.loads(ledger.splitlines()[-1])
        assert row["task_id"] == "t-123-abcd", row


# --- dispatch-log rotation (#467 live half) ----------------------------------------


def test_dispatch_log_self_rotates(tmp_path):
    stub_tmux = tmp_path / "tmux"
    stub_tmux.write_text("#!/bin/bash\nexit 0\n")
    stub_tmux.chmod(0o755)
    env = {
        "CLAUDLOBBY_ROOT": str(tmp_path),
        "TMUX_BIN": str(stub_tmux),
        "OBSERVABILITY_DISPATCH_DEADLINE": "600",
        "BOT_ID": "lead",
    }
    state = tmp_path / "state"
    state.mkdir()
    old = json.dumps(
        _dispatch("w9", 1, 2) | {"ts": "2020-01-01T00:00:00Z"}
    )
    (state / "dispatch-log.jsonl").write_text(old + "\n")
    libdir = tmp_path / "lib"
    libdir.mkdir()
    for f in LIB_DIR.glob("*"):
        (libdir / f.name).symlink_to(f)
    (libdir / "dispatch.sh").unlink()
    stub = tmp_path / "dispatch.sh"
    stub.write_text("#!/bin/bash\nexit 0\n")
    stub.chmod(0o755)
    (libdir / "dispatch.sh").symlink_to(stub)
    r = _bash(f'"{libdir}/dispatch-task.sh" w1 "new task"', env=env)
    assert r.returncode == 0, r.stderr
    text = (state / "dispatch-log.jsonl").read_text()
    assert "2020-01-01" not in text, "entries past retention must rotate out"
    assert "new task" in text
