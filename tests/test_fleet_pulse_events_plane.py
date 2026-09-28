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
import time

import pytest

from tests.plane_fixtures import F, REPO, _live_dispatch, _scene, ro

LIB = REPO / "lib"
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


def _pulse(root, libdir, *, scratch_plane_env, **extra):
    env = {**scratch_plane_env(root), "HOME": str(root / "home"), "FLEET_NAME": F,
           "PLANE_EMIT_ENABLED": "1",

           "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMUX_TMPDIR": str(root / "tmux"),
           "FLEET_PULSE_ESCALATION_CHAT_ID": "-1001234567890",
           "FLEET_PULSE_ESCALATION_STATE_DIR": str(root / "escalation-sender"),
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
    return (root / "state" / "pulse" / f"{F}.pulse-summary.txt").read_text()


@needs_tmux
def test_the_escalation_and_the_summary_read_the_plane_with_no_flag(tmp_path, *, scratch_plane_env):
    root, paths = _two_dead_bots(tmp_path)
    capture = tmp_path / "tg.log"
    libdir = _pulse_lib(tmp_path, capture)
    env_without_flags = {k: "" for k in ("PLANE_READ_EVENTS",)}       # explicitly unset, never read
    r = _pulse(root, libdir, **env_without_flags, scratch_plane_env=scratch_plane_env)
    assert r.returncode == 0, r.stderr[-2000:]
    assert PAGE in capture.read_text(), capture.read_text() + r.stderr[-2000:]
    assert "UNREACHABLE" not in r.stderr and "cutover_declared" not in r.stderr and "keep the files" not in r.stderr
    assert _await(root, "SELECT COUNT(*) FROM events WHERE event = 'session_missing'", 2) >= 2
    summary = _summary(root)
    assert "session_missing" in summary and "unknown" not in summary
    assert not list(paths.runtime_bots.glob("*/data/events/*")), "no file is written or read"


@needs_tmux
def test_an_unreachable_plane_is_unknown_per_bot_and_paged_never_none(tmp_path, *, scratch_plane_env):
    """The db path made unopenable (a directory — the shape a wedged disk
    presents), so the sweep's own doors spool and its readers refuse."""
    root, paths = _two_dead_bots(tmp_path)
    capture = tmp_path / "tg.log"
    libdir = _pulse_lib(tmp_path, capture)
    for p in (root / "state" / "plane").glob("plane.db*"):
        p.unlink()
    (root / "state" / "plane" / "plane.db").mkdir()
    r = _pulse(root, libdir, scratch_plane_env=scratch_plane_env)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "UNREACHABLE" in r.stderr and "cannot be judged this pass" in r.stderr
    # a MISSING SCRIPT would say "No such file" too — but so does Linux's socket
    # client for the absent daemon socket ("[Errno 2] No such file or directory"),
    # which is the expected transport fallback here, not a broken rig
    assert not re.search(r"(bash|python3?|dispatch-overdue\.py|plane-lookup\.py):.*No such file", r.stderr), r.stderr[-1500:]
    # the loop never reads the window a refused read removed (the cache is the
    # pass's own temp file, so match the redirect error, not a file name)
    assert not re.search(r"fleet-pulse\.sh: line \d+: .*: No such file", r.stderr), r.stderr[-1500:]
    paged = capture.read_text()
    assert "events reader for f is UNREACHABLE" in paged and PAGE not in paged, paged
    summary = _summary(root)
    assert summary.count("unknown (events reader unreachable)") == 2 and " none" not in summary


@needs_tmux
def test_a_refused_overdue_reader_is_unknown_per_bot_in_the_summary(tmp_path, *, scratch_plane_env):
    """The plane answers the events readers; only the overdue reader refuses
    (rc 3, as it does when the plane cannot serve it) — the summary once
    printed `none` per bot here."""
    root, paths = _two_dead_bots(tmp_path)
    capture = tmp_path / "tg.log"
    stub = ('import sys\nprint("dispatch-overdue: --all: the plane is UNREACHABLE (stub)", file=sys.stderr)\n'
            'sys.exit(3)\n')
    libdir = _pulse_lib(tmp_path, capture, matcher_stub=stub)
    r = _pulse(root, libdir, scratch_plane_env=scratch_plane_env)
    assert r.returncode == 0, r.stderr[-2000:]
    summary = _summary(root)
    assert summary.count("unknown (overdue reader unreachable)") == 2, summary
    assert "unknown (events reader unreachable)" not in summary          # the events half answered
    assert "session_missing" in summary                                   # ...and said so
    assert "overdue reader for f is UNREACHABLE" in capture.read_text()


@needs_tmux
def test_no_event_file_is_read_or_reaped(tmp_path, *, scratch_plane_env):
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
    r = _pulse(root, libdir, scratch_plane_env=scratch_plane_env)
    assert r.returncode == 0, r.stderr[-2000:]
    assert stale.exists() and stale.read_text() == body and int(stale.stat().st_mtime) == 946684800
    assert "service_down" not in _summary(root)


# --- two fleets' passes at once, on one HOST-GLOBAL state/pulse (#1901) -----

G = "g"

# The tg-post.sh stub for the interleave: it captures every page like the
# plain stub, and at fleet f's FIRST page it starts fleet g's pass and holds f
# there until g is parked inside ITS first page -- g's window written, g's read
# loop mid-flight. f then reads the rest of its own window and ends; g resumes
# only once f's pass has ended (the test touches f.done). The background
# subshell's stdio is detached, or f's `$(...)` around this stub would wait
# for g's whole pass.
_INTERLEAVE_STUB = """#!/bin/bash
printf '%s\\n' "$1" >> "{capture}"
case "$CLAUDLOBBY_FLEET:$1" in
  "f:FLEET ALERT: session_missing"*)
    ( CLAUDLOBBY_FLEET=g FLEET_NAME=g bash "{lib}/fleet-pulse.sh" g >"{sync}/g.out" 2>"{sync}/g.err"
      echo $? >"{sync}/g.rc" ) </dev/null >/dev/null 2>&1 &
    for _ in $(seq 2400); do [ -e "{sync}/g.parked" ] && break; sleep 0.1; done ;;
  "g:FLEET ALERT: session_missing"*)
    : >"{sync}/g.parked"
    for _ in $(seq 2400); do [ -e "{sync}/f.done" ] && break; sleep 0.1; done ;;
esac
"""


def _second_fleet_beside(root):
    """Fleet g on the SAME root as f -- so the same state/pulse -- its two bots
    dead the way f's are. One open-ended dispatch gives the plane g's identity
    before g's first sweep, or g's overdue reader refuses and pages."""
    fleet_dir = root / "local" / G
    for b in ("v1", "v2"):
        (fleet_dir / "runtime" / "bots" / b / "data").mkdir(parents=True, exist_ok=True)
        (fleet_dir / "runtime" / "bots" / b / "bot.conf").write_text(f"TMUX_SOCKET=r1901-none-{b}\n")
    (fleet_dir / "fleet.yaml").write_text(
        "fleet:\n  manager: v2\n  name: g\n  service_prefix: com.test\n  bots:\n"
        "    v1:\n      expertise: [software-engineering]\n"
        "    v2:\n      expertise: [software-engineering]\n")
    _live_dispatch(root, "9", "t-1901-g001", ts="2026-09-01T10:00:00Z", bot="v1", fleet=G)


@needs_tmux
def test_two_fleets_passes_at_once_never_read_or_delete_each_others_window(
    tmp_path, *, scratch_plane_env
):
    """`state/pulse` is HOST-GLOBAL and every fleet's pulse timer fires in the
    same second, so passes overlap as a matter of routine. The critical-window
    cache once sat at one fixed path there: a sibling's write landing inside
    this pass's read loop replaced its rows (its page silently never sent), and
    this pass's end-of-pass `rm` landing inside the sibling's loop failed the
    sibling's next `<` redirect (a script_error, and the sibling aborted).

    The interleave hits both points deterministically (the stub above). f's
    second critical type is read AFTER g wrote its window, so f paging it with
    f's own bots is the pin that f read its own rows; g finishing clean after
    f's pass ended is the pin that f's cleanup never deleted g's."""
    root, paths = _two_dead_bots(tmp_path)
    _second_fleet_beside(root)
    env = {**scratch_plane_env(root), "HOME": str(root / "home"), "FLEET_NAME": F,
           "PLANE_EMIT_ENABLED": "1", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for b in ("w1", "w2"):
        seed = subprocess.run(
            ["bash", "-c", f'. "{LIB}/lib-common.sh"; emit_fleet_event bridge_down pulse "{{}}" "{paths.runtime_bots / b}" {b}'],
            capture_output=True, text=True, timeout=180, env=env)
        assert seed.returncode == 0, seed.stderr[-1000:]
    assert _await(root, "SELECT COUNT(*) FROM events WHERE event = 'bridge_down'", 2) == 2

    capture, sync = tmp_path / "tg.log", tmp_path / "sync"
    sync.mkdir()
    libdir = _pulse_lib(tmp_path, capture)
    (libdir / "tg-post.sh").write_text(_INTERLEAVE_STUB.format(capture=capture, lib=libdir, sync=sync))
    try:
        # a cold emit reaped under two concurrent passes would starve g of its rows
        r_f = _pulse(root, libdir, FLEET_EVENT_EMIT_TIMEOUT_S="120",
                     scratch_plane_env=scratch_plane_env)
    finally:
        (sync / "f.done").touch()
    deadline = time.monotonic() + 300
    while not (sync / "g.rc").exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    g_err = (sync / "g.err").read_text() if (sync / "g.err").exists() else "(g never started)"
    assert (sync / "g.parked").exists(), "no interleave: g never reached its first page\n" + g_err[-2000:]
    assert r_f.returncode == 0, r_f.stderr[-2000:]
    assert (sync / "g.rc").exists() and (sync / "g.rc").read_text().strip() == "0", g_err[-2000:]

    paged = capture.read_text()
    assert "FLEET ALERT: session_missing on 2 bots (w1 w2)." in paged, paged
    assert "FLEET ALERT: bridge_down on 2 bots (w1 w2)." in paged, paged          # f's own window, after g wrote g's
    assert "FLEET ALERT: session_missing on 2 bots (v1 v2)." in paged, paged      # g's own window
    for err in (r_f.stderr, g_err):                                                # a redirect onto a deleted window
        assert not re.search(r"fleet-pulse\.sh: line \d+: .*: No such file", err), err[-2000:]
    assert _await(root, "SELECT COUNT(*) FROM events WHERE event = 'script_error'", 0) == 0
