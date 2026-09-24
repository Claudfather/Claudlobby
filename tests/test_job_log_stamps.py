"""Each log line is stamped when it is WRITTEN (#1773), not once at the top of a run.

weekly-worker-restart, update-siblings and notify-behind took one `ts_iso` at the
start and stamped every later line with it. A run lasting minutes to an hour
therefore logged as a single instant, and per-step durations could not be
recovered: the evidence an operator needs when a run goes wrong. #1770 fixed the
same shape in update-claude-code.sh, and the three now share its `log()`.

Each test drives the REAL script, puts a known 2 s gap between two of its log
lines (a slow stub on the path the run takes), and asserts the later line is
stamped later. `ts_iso` has one-second resolution, so a 2 s gap cannot round to
an equal stamp; the hoisted stamp gives an equal one every time.
"""

from __future__ import annotations

import datetime
import shutil
import subprocess
from pathlib import Path

from tests.conftest import _write_exec, constructed_env
from tests.test_notify_behind import Harness

LIB = Path(__file__).resolve().parent.parent / "lib"


def _stamp(log: str, needle: str) -> datetime.datetime:
    """The stamp of the first line containing `needle`: the token before its first space."""
    line = next((l for l in log.splitlines() if needle in l), None)
    assert line is not None, f"no line containing {needle!r} in:\n{log}"
    return datetime.datetime.fromisoformat(line.split(" ", 1)[0])


def _slow_git(tmp: Path, word: str) -> Path:
    """A dir holding a `git` that takes 2 s whenever `word` is one of its arguments,
    and is the real git otherwise."""
    d = tmp / "slowbin"
    d.mkdir()
    _write_exec(
        d / "git",
        (
            "#!/bin/sh\n"
            f'case " $* " in *" {word} "*) sleep 2 ;; esac\n'
            f'exec {shutil.which("git")} "$@"\n'
        ),
    )
    return d


def _run_source_job(tmp_path, script: str, slow_on: str) -> str:
    h = Harness(tmp_path, behind=2)
    env = h.env()
    env["PATH"] = f"{_slow_git(tmp_path, slow_on)}:{env['PATH']}"
    r = subprocess.run(
        ["bash", str(LIB / script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert r.returncode == 0, (r.returncode, r.stderr)
    return (Path(h.root) / "state" / script.replace(".sh", ".log")).read_text()


def test_notify_behind_stamps_the_verdict_after_the_fetch_it_waited_for(tmp_path):
    # WATCHING is written before the loop; the verdict only after its fetch.
    log = _run_source_job(tmp_path, "notify-behind.sh", "fetch")
    gap = _stamp(log, "] BEHIND") - _stamp(log, "WATCHING")
    assert gap >= datetime.timedelta(seconds=1), (gap, log)


def test_update_siblings_stamps_a_repo_line_after_the_git_call_before_it(tmp_path):
    # START is written after discovery; the root's own line after the
    # `rev-parse --show-toplevel` that names it.
    log = _run_source_job(tmp_path, "update-siblings.sh", "--show-toplevel")
    gap = _stamp(log, "] SKIP") - _stamp(log, "START")
    assert gap >= datetime.timedelta(seconds=1), (gap, log)


def test_weekly_worker_restart_stamps_each_step_of_a_bounce(tmp_path):
    # The shape lib/validate-bot-change.sh uses: a copied lib/, a worker whose
    # pre-stop handoff takes 2 s, and a spin-up that fails (so no bridge wait).
    root = tmp_path / "root"
    lib = root / "lib"
    lib.mkdir(parents=True)
    for name in ("lib-common.sh", "supervisor.sh", "weekly-worker-restart.sh"):
        shutil.copy(LIB / name, lib / name)
    _write_exec(lib / "pre-stop-handoff.sh", "#!/bin/bash\nsleep 2\nexit 0\n")
    _write_exec(lib / "spin-up-bot.sh", "#!/bin/bash\nexit 7\n")
    bot = root / "local" / "F" / "runtime" / "bots" / "wworker"
    (bot / "data").mkdir(parents=True)
    (bot / "bot.conf").write_text("BOT_ID=wworker\nMANAGER_TMUX=wmgr\n")
    env = constructed_env(
        CLAUDLOBBY_ROOT=root,
        HOME=tmp_path / "home",
        PLANE_EMIT_DISABLED="1",
        TMUX_TMPDIR=tmp_path / "tmux",
    )
    (tmp_path / "tmux").mkdir()
    subprocess.run(
        ["bash", str(lib / "weekly-worker-restart.sh"), "F"],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    log = (root / "state" / "weekly-worker-restart.log").read_text()
    assert "RESTART FAILED: wworker" in log, log  # precondition: the bounce ran
    gap = _stamp(log, "RESTART FAILED: wworker") - _stamp(
        log, "RESTART worker: wworker"
    )
    assert gap >= datetime.timedelta(seconds=1), (gap, log)
