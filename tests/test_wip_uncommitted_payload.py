"""`wip_uncommitted` says what it SAW, not how many (#1728).

The alert is the one thing standing between a manager and destroying a worker's
in-flight work -- the composed decision table says of it, in these words, "Do
NOT restart - task is in flight". It was emitted from a bare count, and a count
cannot tell `M lib/foo.py` from `?? .venv/`: both are 1, and only one of them is
work. So it fired forever on repos holding a virtualenv, and a permanently-true
alert is indistinguishable from a correctly-firing one. The reader it trained to
skip it was the manager holding the restart.

MEASURED BEFORE DESIGNING, which is what picked this fix over the obvious one:
5,284 rows in the 2.4 days this host's plane retains, **95.2% of them from eight
(bot,repo) pairs whose count never changed across the whole window**, and a live
sweep of 108 checkouts finding 9 dirty of which **9 were untracked-only and none
was work in flight**.

THE OBVIOUS FIX IS REFUSED ON PURPOSE and one test here pins that refusal.
Excluding untracked paths silences the alert in the one case where the work is
UNRECOVERABLE -- a new file has no copy anywhere until it is added -- and
40-65% of the last fortnight of commits per repo add at least one file. So the
tracked/untracked split rides the payload as a FACT and never as a gate.

The real script is run end to end (no tmux needed: a missing session emits
session_missing and the sweep carries on to check 4), and every assertion reads
the row the plane actually holds.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import constructed_env, plane_emit_env, read_fleet_events

REPO_ROOT = Path(__file__).resolve().parent.parent
FLEET_PULSE = REPO_ROOT / "lib" / "fleet-pulse.sh"
FLEET = "wipfleet"
BOT = "wipbot"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, timeout=60)


@pytest.fixture()
def wip_root(tmp_path):
    """A fleet of one declared bot with one real git checkout in projects/."""
    root = tmp_path / "root"
    bots = root / "local" / FLEET / "runtime" / "bots"
    bot = bots / BOT
    (bot / "data").mkdir(parents=True)
    (bot / "bot.conf").write_text("TMUX_SOCKET=wip-none\n")
    (root / "local" / FLEET / "fleet.yaml").write_text(
        f"fleet:\n  bots:\n    {BOT}:\n")
    repo = bot / "projects" / "demo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "tracked.md").write_text("original\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    return root, repo


def _run(root: Path, **extra) -> subprocess.CompletedProcess:
    """A CONSTRUCTED child env (conftest's ratified default) plus the two keys
    that make the shim record into this root's own plane via its cold rung."""
    env = constructed_env(HOME=str(root / "home"), CLAUDLOBBY_ROOT=str(root),
                          **plane_emit_env(), **extra)
    return subprocess.run(["bash", str(FLEET_PULSE), FLEET],
                          capture_output=True, text=True, env=env, timeout=180)


def _wip_rows(root: Path) -> list[dict]:
    rows = [json.loads(line) for line in read_fleet_events(root).splitlines()
            if line.strip()]
    return [r for r in rows if r.get("type") == "wip_uncommitted"]


def _one_row(root: Path) -> dict:
    proc = _run(root)
    assert proc.returncode == 0, f"pulse aborted\n{proc.stdout}\n{proc.stderr}"
    rows = _wip_rows(root)
    assert rows, f"no wip_uncommitted row emitted\n{proc.stdout}\n{proc.stderr}"
    return rows[-1]["data"]


def test_an_untracked_only_repo_is_reported_AS_untracked(wip_root):
    """The `.venv/` case: still emitted (never filtered), and now legible."""
    root, repo = wip_root
    (repo / ".venv").mkdir()
    (repo / ".venv" / "pyvenv.cfg").write_text("x\n")

    data = _one_row(root)

    assert data["dirty_untracked"] == 1
    assert data["dirty_tracked"] == 0
    assert any(".venv" in p and p.startswith("??") for p in data["paths"]), data
    # The old field survives, because the shipped readers name it.
    assert data["dirty_files"] == 1


def test_a_tracked_edit_is_reported_AS_tracked(wip_root):
    """The case the alert exists for."""
    root, repo = wip_root
    (repo / "tracked.md").write_text("edited\n")

    data = _one_row(root)

    assert data["dirty_tracked"] == 1
    assert data["dirty_untracked"] == 0
    assert any(p.strip().startswith("M") and "tracked.md" in p
               for p in data["paths"]), data


def test_the_two_cases_a_COUNT_cannot_separate_are_now_separable(wip_root):
    """THE DEFECT, stated as a test: a venv and a mid-edit are both
    `dirty_files == 1`, and the whole decision turns on which one it is."""
    root, repo = wip_root
    (repo / ".venv").mkdir()
    (repo / ".venv" / "pyvenv.cfg").write_text("x\n")
    venv = _one_row(root)

    (repo / ".venv" / "pyvenv.cfg").unlink()
    (repo / ".venv").rmdir()
    (repo / "tracked.md").write_text("edited\n")
    edit = _one_row(root)

    assert venv["dirty_files"] == edit["dirty_files"] == 1, (
        "the premise of the bug: identical counts")
    assert (venv["dirty_tracked"], venv["dirty_untracked"]) == (0, 1)
    assert (edit["dirty_tracked"], edit["dirty_untracked"]) == (1, 0)
    assert venv["paths"] != edit["paths"]


def test_an_untracked_only_repo_is_NEVER_silently_filtered(wip_root):
    """The refusal, pinned. Excluding untracked paths is the obvious way to
    quieten this alert and it is the one change that must not be made: a new
    source file is untracked and IS work in flight, with no copy anywhere.

    A future "fix" that drops untracked-only repos turns this red."""
    root, repo = wip_root
    (repo / "brand-new-source.py").write_text("def f():\n    return 1\n")

    data = _one_row(root)

    assert data["dirty_untracked"] == 1, (
        "an untracked NEW FILE is work in flight and must still be reported")
    assert any("brand-new-source.py" in p for p in data["paths"]), data


def test_the_path_list_STATES_ITS_BOUND(wip_root):
    """#1742 estate-wide: a capped list must never read as the whole of the
    dirt, so the cap is emitted beside the total."""
    root, repo = wip_root
    for i in range(7):
        (repo / f"extra-{i}.txt").write_text("x\n")

    proc = _run(root, OBSERVABILITY_WIP_PATHS_MAX="3")
    assert proc.returncode == 0, proc.stderr
    data = _wip_rows(root)[-1]["data"]

    assert data["paths_total"] == 7
    assert data["paths_shown"] == 3
    assert len(data["paths"]) == 3, "the list must match what it claims to show"
    assert data["paths_shown"] < data["paths_total"], "this run IS truncated"


def test_how_long_the_condition_has_been_true_is_reported_and_RESETS(wip_root):
    """A condition true for hours reads differently from one that appeared a
    tick ago — and the hash is what gates it, which is the half a broken
    implementation would get wrong while still printing a big number."""
    root, repo = wip_root
    (repo / ".venv").mkdir()
    (repo / ".venv" / "pyvenv.cfg").write_text("x\n")

    first = _one_row(root)
    assert first["unchanged_for_s"] == 0, "the first sighting is a floor of 0"

    # Backdate only the timestamp the previous run wrote. The hash comparison is
    # untouched and real: it is what decides whether this stamp counts at all.
    state = root / "state" / "pulse"
    stamps = list(state.glob("*.wip_ts"))
    assert len(stamps) == 1, [p.name for p in stamps]
    stamps[0].write_text(str(int(time.time()) - 7200))

    held = _one_row(root)
    assert held["unchanged_for_s"] >= 7200, held

    # CONTROL: change the dirty state and the clock must restart, even though
    # the backdated stamp is still on disk. Without the hash gate this would
    # keep reporting two hours.
    (repo / "another.txt").write_text("x\n")
    stamps[0].write_text(str(int(time.time()) - 7200))
    moved = _one_row(root)
    assert moved["unchanged_for_s"] == 0, (
        "a CHANGED status must reset the clock; a stale stamp must not survive "
        "it")
