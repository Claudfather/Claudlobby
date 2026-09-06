"""fleet-pulse pages each ESCALATION once (chunk M-B, #1481).

`escalated` is a manager asking the human a question about one task, and it is
NON-terminal by ruling: the row stays open while the human decides. Nothing in
the open set, the overdue set or the critical-events read can see it, so this
sweep leg is the only thing between a manager's raise and an operator who never
hears about it.

The three properties, each driven through the REAL sweep against a throwaway
plane (the events-plane suite's rig; one stub, `tg-post.sh`, which captures the
page instead of sending it):

  * paged ONCE per escalation, keyed by ASSIGNMENT — a question is not a burst,
    and re-paging it every ten minutes is how a channel gets muted;
  * the marker is dropped when the row LEAVES the read (any act clears the arm),
    so a genuine re-escalation pages again — the state follows the plane rather
    than a clock;
  * a REFUSED read is not "nothing escalated": it pages its own guard and, above
    all, touches no marker — otherwise an outage would silently drop every
    marker and re-page the whole backlog on recovery.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import F, REPO, _live_dispatch, _paths, plane_root

LIB = REPO / "lib"
CLI = Path(sys.executable).parent / "claudlobby"
needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None,
                                reason="fleet-pulse needs tmux")


def _pulse_lib(tmp_path, capture, *, lookup_stub=None):
    """The repo's lib/ with ONE stub: tg-post.sh appends its page to *capture*
    (and, for the refusal pin, a plane-lookup.py that refuses)."""
    libdir = tmp_path / "lib"
    libdir.mkdir(parents=True)
    for f in LIB.iterdir():
        if f.name == "tg-post.sh" or (lookup_stub and f.name == "plane-lookup.py"):
            continue
        (libdir / f.name).symlink_to(f)
    stub = libdir / "tg-post.sh"
    stub.write_text(f'#!/bin/bash\nprintf "%s\\n" "$1" >> "{capture}"\n')
    stub.chmod(0o755)
    if lookup_stub:
        (libdir / "plane-lookup.py").write_text(lookup_stub)
    return libdir


def _pulse(root, libdir, **extra):
    env = {"CLAUDLOBBY_ROOT": str(root), "HOME": str(root / "home"), "FLEET_NAME": F,
           "PLANE_EMIT_ENABLED": "1", "PLANE_EMIT_CLI": str(CLI),
           "PLANE_SOCKET": str(root / "no-daemon.sock"),
           "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "TMUX_TMPDIR": str(root / "tmux"),
           "FLEET_PULSE_ESCALATION_CHAT_ID": "-1001234567890",
           # above the number of dead sandbox bots, so the burst detector's own
           # pages cannot be mistaken for this leg's
           "FLEET_PULSE_ESCALATION_THRESHOLD": "9", **extra}
    return subprocess.run(["bash", str(libdir / "fleet-pulse.sh"), F],
                          capture_output=True, text=True, timeout=300, env=env)


def _scene(tmp_path):
    """One declared bot holding one dispatched row, and the plane behind it."""
    root = plane_root(tmp_path)
    paths = _paths(root)
    (paths.runtime_bots / "w1" / "data").mkdir(parents=True, exist_ok=True)
    (paths.runtime_bots / "w1" / "bot.conf").write_text("TMUX_SOCKET=esc-none-w1\n")
    (root / "tmux").mkdir()
    wi, asg, _msg = _live_dispatch(root, "1", "t-esc-0001",
                                   ts="2026-09-01T10:00:00Z", bot="w1")
    return root, paths, wi, asg


def _act(root, wi, asg, event, ts, **detail):
    out = emit_batch(root, [{
        "event_type": "task", "emitter": "task-act", "fleet": F, "occurred_at": ts,
        "payload": {"work_item_id": wi, "assignment_id": asg, "event": event,
                    "actor": f"bot:{F}/mgr", **detail}}])
    assert all(o.status == "committed" for o in out), out


def _pages(capture: Path) -> list[str]:
    return [ln for ln in capture.read_text().splitlines() if ln] if capture.exists() else []


def _escalation_pages(capture: Path) -> list[str]:
    return [ln for ln in _pages(capture) if "escalated by" in ln]


@needs_tmux
def test_an_open_escalation_pages_the_operator_once(tmp_path):
    root, paths, wi, asg = _scene(tmp_path)
    _act(root, wi, asg, "escalated", "2026-09-02T10:00:00Z", by="mgr",
         question="do we ship without the migration")
    capture = tmp_path / "tg.log"
    libdir = _pulse_lib(tmp_path, capture)

    r = _pulse(root, libdir)
    assert r.returncode == 0, r.stderr[-2000:]
    paged = _escalation_pages(capture)
    assert len(paged) == 1, _pages(capture) + [r.stderr[-1500:]]
    assert "task t-esc-0001 escalated by mgr" in paged[0]
    assert "do we ship without the migration" in paged[0]
    assert (root / "state" / "pulse" / "escalated" / asg).exists()

    # ...and a second sweep, with the question still open, says nothing new
    r2 = _pulse(root, libdir)
    assert r2.returncode == 0, r2.stderr[-2000:]
    assert len(_escalation_pages(capture)) == 1, _pages(capture)


@needs_tmux
def test_an_answered_escalation_is_forgotten_so_a_re_raise_pages_again(tmp_path):
    """The marker follows the PLANE: any act clears the arm, and a manager who
    raises the question again is a new question."""
    root, paths, wi, asg = _scene(tmp_path)
    _act(root, wi, asg, "escalated", "2026-09-02T10:00:00Z", by="mgr", question="q1")
    capture = tmp_path / "tg.log"
    libdir = _pulse_lib(tmp_path, capture)
    _pulse(root, libdir)
    assert len(_escalation_pages(capture)) == 1

    # the human answered and the manager moved the row on
    _act(root, wi, asg, "progress", "2026-09-02T11:00:00Z")
    r = _pulse(root, libdir)
    assert r.returncode == 0, r.stderr[-2000:]
    assert len(_escalation_pages(capture)) == 1                  # nothing to say
    assert not (root / "state" / "pulse" / "escalated" / asg).exists()

    _act(root, wi, asg, "escalated", "2026-09-02T12:00:00Z", by="mgr", question="q2")
    r = _pulse(root, libdir)
    assert r.returncode == 0, r.stderr[-2000:]
    paged = _escalation_pages(capture)
    assert len(paged) == 2 and "q2" in paged[1], paged


@needs_tmux
def test_a_terminal_act_never_pages(tmp_path):
    """A withdrawn row is closed; the question died with it."""
    root, paths, wi, asg = _scene(tmp_path)
    _act(root, wi, asg, "escalated", "2026-09-02T10:00:00Z", by="mgr", question="q")
    _act(root, wi, asg, "cancelled", "2026-09-02T10:30:00Z", by="mgr", reason="moot")
    capture = tmp_path / "tg.log"
    r = _pulse(root, _pulse_lib(tmp_path, capture))
    assert r.returncode == 0, r.stderr[-2000:]
    assert _escalation_pages(capture) == [], _pages(capture)


@needs_tmux
def test_a_refused_read_pages_its_own_guard_and_keeps_every_marker(tmp_path):
    """Unreachable is not empty (the sweep's standing rule) — and dropping the
    markers on a refusal would re-page the whole backlog on recovery."""
    root, paths, wi, asg = _scene(tmp_path)
    _act(root, wi, asg, "escalated", "2026-09-02T10:00:00Z", by="mgr", question="q")
    capture = tmp_path / "tg.log"
    libdir = _pulse_lib(tmp_path, capture)
    _pulse(root, libdir)
    assert len(_escalation_pages(capture)) == 1

    refusing = _pulse_lib(
        tmp_path / "refuse", capture,
        lookup_stub=('import sys\n'
                     'print("plane-lookup: UNREACHABLE (stub)", file=sys.stderr)\n'
                     'sys.exit(3)\n'))
    r = _pulse(root, refusing)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "escalated reader UNREACHABLE" in r.stderr
    assert any("escalated-task reader for f is UNREACHABLE" in p
               for p in _pages(capture)), _pages(capture)
    assert (root / "state" / "pulse" / "escalated" / asg).exists()
    # the question itself is not re-paged by the outage
    assert len(_escalation_pages(capture)) == 1
