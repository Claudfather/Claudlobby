"""F18 closure R2b-2 — fleet-pulse's events read-back reads the plane and
nothing else: no `PLANE_READ_EVENTS`, no `cutover_declared`, no dated event
files, no reaper of files nothing writes. A plane that cannot answer is a
THIRD state, never a quiet fleet: the per-bot ALERTS column says `unknown`
and the guard pages. The same third state is rendered for a REFUSED overdue
reader (rc 3), which once printed `none` per bot while the events reader
said unknown (filed on #1467 by the R2a adversarial lens).

Each pin drives the REAL sweep (the repo's lib/, one stub: tg-post.sh
captures its page) against a throwaway plane through the real doors.
"""
from __future__ import annotations

import re

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.plane_fixtures import F, REPO, _scene, ro

LIB = REPO / "lib"
CLI = Path(sys.executable).parent / "claudlobby"
needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="fleet-pulse needs tmux")
PAGE = "FLEET ALERT: session_missing on 2 bots (w1 w2)."


def _pulse_lib(tmp_path, capture, *, matcher_stub=None):
    """The repo's lib/ with ONE stub: tg-post.sh appends its page to *capture*
    (and, for the refused-overdue pin, a dispatch-overdue.py that refuses)."""
    libdir = tmp_path / "lib"
    libdir.mkdir()
    for f in LIB.iterdir():
        if f.name == "tg-post.sh" or (matcher_stub and f.name == "dispatch-overdue.py"):
            continue
        (libdir / f.name).symlink_to(f)
    stub = libdir / "tg-post.sh"
    stub.write_text(f'#!/bin/bash\nprintf "%s\\n" "$1" >> "{capture}"\n')
    stub.chmod(0o755)
    if matcher_stub:
        (libdir / "dispatch-overdue.py").write_text(matcher_stub)
    return libdir


def _pulse(root, libdir, **extra):
    env = {"CLAUDLOBBY_ROOT": str(root), "HOME": str(root / "home"), "FLEET_NAME": F,
           "PLANE_EMIT_ENABLED": "1", "PLANE_EMIT_CLI": str(CLI),
           "PLANE_SOCKET": str(root / "no-daemon.sock"),
           "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMUX_TMPDIR": str(root / "tmux"),
           "FLEET_PULSE_ESCALATION_CHAT_ID": "-1001234567890",
           "FLEET_PULSE_ESCALATION_THRESHOLD": "2", **extra}
    return subprocess.run(["bash", str(libdir / "fleet-pulse.sh"), F], capture_output=True,
                          text=True, timeout=300, env=env)


def _await(root, sql, want, *, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        with ro(root) as conn:
            got = conn.execute(sql).fetchone()[0]
        if got == want or time.monotonic() > deadline:
            return got
        time.sleep(0.25)


def _two_dead_bots(tmp_path):
    """Two declared bots with no tmux session anywhere: the sweep lands
    session_missing for both on the plane, through the real door."""
    root, paths, _, _ = _scene(tmp_path)
    for b in ("w1", "w2"):
        (paths.runtime_bots / b / "data").mkdir(parents=True, exist_ok=True)
        (paths.runtime_bots / b / "bot.conf").write_text(f"TMUX_SOCKET=r2b2-none-{b}\n")
    (root / "tmux").mkdir()
    return root, paths


def _summary(root):
    return (root / "state" / "pulse" / "pulse-summary.txt").read_text()


@needs_tmux
def test_the_escalation_and_the_summary_read_the_plane_with_no_flag(tmp_path):
    root, paths = _two_dead_bots(tmp_path)
    capture = tmp_path / "tg.log"
    libdir = _pulse_lib(tmp_path, capture)
    env_without_flags = {k: "" for k in ("PLANE_READ_EVENTS",)}       # explicitly unset, never read
    r = _pulse(root, libdir, **env_without_flags)
    assert r.returncode == 0, r.stderr[-2000:]
    assert PAGE in capture.read_text(), capture.read_text() + r.stderr[-2000:]
    assert "UNREACHABLE" not in r.stderr and "cutover_declared" not in r.stderr and "keep the files" not in r.stderr
    assert _await(root, "SELECT COUNT(*) FROM events WHERE event = 'session_missing'", 2) >= 2
    summary = _summary(root)
    assert "session_missing" in summary and "unknown" not in summary
    assert not list(paths.runtime_bots.glob("*/data/events/*")), "no file is written or read"


@needs_tmux
def test_an_unreachable_plane_is_unknown_per_bot_and_paged_never_none(tmp_path):
    """The db path made unopenable (a directory — the shape a wedged disk
    presents), so the sweep's own doors spool and its readers refuse."""
    root, paths = _two_dead_bots(tmp_path)
    capture = tmp_path / "tg.log"
    libdir = _pulse_lib(tmp_path, capture)
    for p in (root / "state" / "plane").glob("plane.db*"):
        p.unlink()
    (root / "state" / "plane" / "plane.db").mkdir()
    r = _pulse(root, libdir)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "UNREACHABLE" in r.stderr and "cannot be judged this pass" in r.stderr
    # a MISSING SCRIPT would say "No such file" too — but so does Linux's socket
    # client for the absent daemon socket ("[Errno 2] No such file or directory"),
    # which is the expected transport fallback here, not a broken rig
    assert not re.search(r"(bash|python3?|dispatch-overdue\.py|plane-lookup\.py):.*No such file", r.stderr), r.stderr[-1500:]
    assert ".critical-window" not in r.stderr
    paged = capture.read_text()
    assert "events reader for f is UNREACHABLE" in paged and PAGE not in paged, paged
    summary = _summary(root)
    assert summary.count("unknown (events reader unreachable)") == 2 and " none" not in summary


@needs_tmux
def test_a_refused_overdue_reader_is_unknown_per_bot_in_the_summary(tmp_path):
    """The plane answers the events readers; only the overdue reader refuses
    (rc 3, as it does when the plane cannot serve it) — the summary once
    printed `none` per bot here."""
    root, paths = _two_dead_bots(tmp_path)
    capture = tmp_path / "tg.log"
    stub = ('import sys\nprint("dispatch-overdue: --all: the plane is UNREACHABLE (stub)", file=sys.stderr)\n'
            'sys.exit(3)\n')
    libdir = _pulse_lib(tmp_path, capture, matcher_stub=stub)
    r = _pulse(root, libdir)
    assert r.returncode == 0, r.stderr[-2000:]
    summary = _summary(root)
    assert summary.count("unknown (overdue reader unreachable)") == 2, summary
    assert "unknown (events reader unreachable)" not in summary          # the events half answered
    assert "session_missing" in summary                                   # ...and said so
    assert "overdue reader for f is UNREACHABLE" in capture.read_text()


@needs_tmux
def test_no_event_file_is_read_or_reaped(tmp_path):
    """A stale dated file under a bot's data/events — the shape the retired
    ledgers had, with a reap window of 0 days that the old reaper would have
    deleted it under — is neither read (its service_down never reaches the
    summary) nor touched (same bytes, same mtime)."""
    root, paths = _two_dead_bots(tmp_path)
    capture = tmp_path / "tg.log"
    libdir = _pulse_lib(tmp_path, capture)
    (paths.runtime_bots / "w1" / "bot.conf").write_text("TMUX_SOCKET=r2b2-none-w1\n")
    stale = paths.runtime_bots / "w1" / "data" / "events" / "fleet-2020-01-01.jsonl"
    stale.parent.mkdir(parents=True, exist_ok=True)
    body = '{"ts":"2020-01-01T00:00:00Z","type":"service_down","source":"pulse","bot":"w1","data":{}}\n'
    stale.write_text(body)
    os.utime(stale, (946684800, 946684800))
    r = _pulse(root, libdir)
    assert r.returncode == 0, r.stderr[-2000:]
    assert stale.exists() and stale.read_text() == body and int(stale.stat().st_mtime) == 946684800
    assert "service_down" not in _summary(root)
