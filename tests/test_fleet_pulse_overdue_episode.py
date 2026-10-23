"""fleet-pulse pages a manager once per overdue assignment (#2457).

An assignment past its deadline with no terminal linked report pages its bot's
manager once. The page stands while the assignment stays open and past due:
the progress grace taking it off the overdue list and then lapsing, a manager
restart and the passage of time are no new fact about it, so none of them
pages it again (`task recheck` re-asks the manager about a task past its
deadline once a day). Another assignment past its deadline is a new fact and
pages, naming that assignment. A row the reader defers is past due and not
overdue, and the sweep pages it only once it is overdue: one inside the
progress grace pages when the grace lapses, and one past the expiry cap or
orphaned by its worker's restart never pages. A page no manager session took
records nothing, so it goes out once a manager is there, and a reader that
cannot answer changes nothing.

Each case drives the REAL sweep (the repo's claudlobby/_runtime_scripts/,
linked file by file) and the REAL matcher over a throwaway plane. The manager
is the stand-in input box in tmux, which logs each line it is submitted; the
workers have live sessions, so the sweep raises nothing else about them.
Stand-ins: tg-post.sh, plane-lookup.py (no escalation to read) and systemctl;
the sweep records nothing (PLANE_EMIT_DISABLED=1), so the events it records
about a row are read in harness/validate-bot-change.sh.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.conftest import _write_exec, load_lib_module
from tests.plane_fixtures import F, FLEET_YAML, REPO, _live_dispatch, _report, plane_root

LIB = REPO / "claudlobby/_runtime_scripts"
BOX = REPO / "tests/fixtures/input-box-stub.py"
pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="fleet-pulse needs tmux")
STUBS = {
    "tg-post.sh": "#!/bin/bash\nexit 0\n",
    "plane-lookup.py": "import sys\nsys.exit(0)\n",
}
REFUSING = ('import sys\nprint("dispatch-overdue: --all: the plane is UNREACHABLE (stub)",'
            " file=sys.stderr)\nsys.exit(3)\n")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class Fleet:
    """Fleet f: workers w1 and w2, their manager a stand-in box in tmux, and
    the plane the real matcher reads."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.root = plane_root(tmp)
        (self.root / "home").mkdir()
        fleet_dir = self.root / "local" / F
        fleet_dir.mkdir(parents=True)
        (fleet_dir / "fleet.yaml").write_text(FLEET_YAML)
        # a short path: a tmux socket's path is limited to about 100 bytes
        self.tmux = Path(tempfile.mkdtemp(prefix="t2457-", dir="/tmp"))
        self.tmux_env = {**os.environ, "TMUX_TMPDIR": str(self.tmux)}
        self.bin = tmp / "bin"
        self.bin.mkdir()
        _write_exec(self.bin / "systemctl", "#!/bin/sh\nexit 3\n")
        self.proc = tmp / "proc"
        self.proc.mkdir()
        self.lib = self.pulse_lib(tmp / "lib")
        self.managers = 0
        self.bots = fleet_dir / "runtime" / "bots"
        for bot in ("w1", "w2"):
            bot_dir = self.bots / bot
            (bot_dir / "data").mkdir(parents=True)
            (bot_dir / "bot.conf").write_text(
                f"BOT_NAME={bot}\nBOT_ID={bot}\nFLEET_NAME={F}\nTMUX_SOCKET=t2457-{bot}\n"
                "MANAGER_TMUX=mgr\nMANAGER_TMUX_SOCKET=t2457-mgr\n")
            r = self.tmux_cmd("-L", f"t2457-{bot}", "new-session", "-d", "-s", bot, "sleep 3600")
            assert r.returncode == 0, r.stderr

    def pulse_lib(self, lib: Path, **extra: str) -> Path:
        """The repo's runtime scripts, linked file by file, with the stand-ins."""
        lib.mkdir()
        stubs = {**STUBS, **extra}
        for f in LIB.iterdir():
            if f.name not in stubs:
                (lib / f.name).symlink_to(f)
        for name, body in stubs.items():
            _write_exec(lib / name, body)
        return lib

    def tmux_cmd(self, *argv):
        return subprocess.run(["tmux", *argv], env=self.tmux_env, capture_output=True, text=True, timeout=30)

    def _box(self, script: str):
        return subprocess.run(["bash", "-c", f". {shlex.quote(str(LIB / 'lib-common.sh'))} && {script}"],
                              env=self.tmux_env, capture_output=True, text=True, timeout=120)

    def start_manager(self) -> Path:
        """A new manager session; returns the log of the lines it is submitted."""
        self.managers += 1
        log = self.tmp / f"manager-{self.managers}.log"
        log.touch()
        box = f"{shlex.quote(sys.executable)} {shlex.quote(str(BOX))} --log {shlex.quote(str(log))}"
        r = self._box(f"harness_box_up t2457-mgr mgr {shlex.quote(box)}")
        assert r.stdout.strip() == "drawn", r.stdout + r.stderr
        return log

    def stop_manager(self):
        self._box("harness_box_down t2457-mgr mgr")

    def assign(self, n: str, bot: str, *, due_ago: int, dispatched_ago: int = 3600,
               task_id: str = "") -> tuple[str, str]:
        """An assignment dispatched *dispatched_ago* seconds ago and due
        *due_ago* seconds ago, made with `task assign` (a request ref, no legacy
        task id), or by the legacy dispatch door when *task_id* is given."""
        now = time.time()
        wi, asg, _msg = _live_dispatch(self.root, n, task_id, ts=_iso(now - dispatched_ago), bot=bot,
                                       expected_by=_iso(now - due_ago),
                                       ref=None if task_id else f"request:{n:0>36}")
        return wi, asg

    def report(self, wi: str, asg: str, event: str, *, ago: int = 1):
        """w1's report on the assignment, *ago* seconds ago."""
        _report(self.root, wi, asg, _iso(time.time() - ago), bot="w1", event=event)

    def overdue(self) -> dict:
        """bot -> overdue rows, as the matcher reads the plane now."""
        doors = load_lib_module("dispatch-overdue")
        return doors.overdue_all(int(time.time()), fleet=F, root=str(self.root))

    def verdicts(self, **env) -> dict:
        """assignment id -> the verdict `--all` gives its row now, read as the
        sweep reads it: with the bots dir (the orphan split needs it), and with
        only the knobs given, since the sweep's env takes none from the caller."""
        r = subprocess.run(
            [sys.executable, str(LIB / "dispatch-overdue.py"), "--all", "--fleet", F,
             "--root", str(self.root), "--bots-dir", str(self.bots)],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", **env},
            capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return {asg: verdict for _bot, _da, _exp, _elapsed, asg, verdict, _tid
                in map(str.split, r.stdout.splitlines())}

    def sweep(self, lib: Path | None = None, **env):
        result = subprocess.run(
            ["bash", str((lib or self.lib) / "fleet-pulse.sh"), F],
            env={"PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin", "HOME": str(self.root / "home"),
                 "LANG": "C.UTF-8", "CLAUDLOBBY_ROOT": str(self.root), "FLEET_NAME": F,
                 "TMUX_TMPDIR": str(self.tmux), "BROWSER_SCAN_PROC": str(self.proc),
                 "PLANE_EMIT_DISABLED": "1", **env},
            capture_output=True, text=True, errors="replace", timeout=300)
        assert result.returncode == 0, result.stderr[-3000:]
        return result

    def close(self):
        for sock in ("t2457-mgr", "t2457-w1", "t2457-w2"):
            self.tmux_cmd("-L", sock, "kill-server")
        shutil.rmtree(self.tmux, ignore_errors=True)


def pages(log: Path, bot: str = "w1") -> list[str]:
    """The overdue pages about *bot* that the manager was submitted."""
    return [line for line in log.read_text().splitlines()
            if line.startswith(f"[FLEET-PULSE] {bot} overdue_dispatch")]


@pytest.fixture
def fleet(tmp_path):
    f = Fleet(tmp_path)
    try:
        yield f
    finally:
        f.close()


def test_the_page_stands_while_the_progress_grace_defers_the_assignment(fleet, monkeypatch):
    """The issue's night: w1's assignment pages, then w1 reports progress on
    other work, so the grace takes the row off the overdue list while w2's row
    keeps the list from being empty, and then the grace lapses. The row was
    never resolved, so its return is no new fact."""
    log = fleet.start_manager()
    wi, asg = fleet.assign("1", "w1", due_ago=600)
    fleet.assign("2", "w2", due_ago=600)
    fleet.sweep()
    assert len(pages(log)) == 1, log.read_text()
    fleet.report(wi, asg, "progress")
    over = fleet.overdue()
    assert "w1" not in over and "w2" in over, over     # deferred, and the list is not empty
    fleet.sweep()
    time.sleep(2)
    monkeypatch.setenv("DISPATCH_PROGRESS_GRACE_S", "1")
    assert "w1" in fleet.overdue()                     # the grace has lapsed
    fleet.sweep(DISPATCH_PROGRESS_GRACE_S="1")
    assert len(pages(log)) == 1, log.read_text()


def test_a_row_inside_the_grace_at_its_deadline_pages_once_the_grace_lapses(fleet):
    """w1 reported progress a minute ago, before its assignment fell due half
    a minute ago, so the first sweep to see the row past due finds the grace
    deferring it: no page. Once the grace lapses the row is overdue, and it
    pages once."""
    log = fleet.start_manager()
    wi, asg = fleet.assign("1", "w1", due_ago=30)
    fleet.report(wi, asg, "progress", ago=60)
    assert fleet.verdicts() == {asg: "progress_grace"}
    fleet.sweep()
    assert pages(log) == [], log.read_text()
    lapsed = {"DISPATCH_PROGRESS_GRACE_S": "30"}  # the progress is over a minute old
    assert fleet.verdicts(**lapsed) == {asg: "overdue"}
    fleet.sweep(**lapsed)
    fleet.sweep(**lapsed)
    got = pages(log)
    assert len(got) == 1 and asg in got[0], log.read_text()


def test_a_row_past_the_expiry_cap_never_pages(fleet):
    """An assignment dispatched 25 hours ago is past the expiry cap (#460) the
    first time the sweep sees it, as every such row is at rollout: past due,
    never paged. w2's overdue row pages, so the sweep did read the rows."""
    log = fleet.start_manager()
    _wi, asg = fleet.assign("1", "w1", due_ago=89400, dispatched_ago=90000)
    _wi, control = fleet.assign("2", "w2", due_ago=60)
    assert fleet.verdicts() == {asg: "max_age_cap", control: "overdue"}
    fleet.sweep()
    fleet.sweep()
    assert pages(log) == [] and len(pages(log, "w2")) == 1, log.read_text()


def test_a_row_whose_worker_respawned_never_pages(fleet):
    """w1 restarted after the dispatch (its data/.spawn is newer), so the
    session that held the task is gone and the row is orphaned (#835): past
    due, never paged. Only a dispatch with a legacy task id is orphan-split.
    w2's overdue row pages, so the sweep did read the rows."""
    log = fleet.start_manager()
    _wi, asg = fleet.assign("1", "w1", due_ago=600, task_id="t-835-0001")
    _wi, control = fleet.assign("2", "w2", due_ago=60)
    (fleet.bots / "w1" / "data" / ".spawn").touch()
    assert fleet.verdicts() == {asg: "orphaned", control: "overdue"}
    fleet.sweep()
    fleet.sweep()
    assert pages(log) == [] and len(pages(log, "w2")) == 1, log.read_text()


def test_a_manager_restart_does_not_page_the_assignment_again(fleet):
    """The new session is told about what changes after it starts: an
    assignment it was already paged about stays quiet, a new one pages."""
    first = fleet.start_manager()
    fleet.assign("1", "w1", due_ago=600)
    fleet.sweep()
    assert len(pages(first)) == 1, first.read_text()
    fleet.stop_manager()
    second = fleet.start_manager()
    fleet.sweep()
    assert pages(second) == [], second.read_text()
    _wi, asg = fleet.assign("2", "w1", due_ago=60)
    fleet.sweep()
    got = pages(second)
    assert len(got) == 1 and asg in got[0], second.read_text()


def test_time_alone_does_not_page_the_assignment_again(fleet):
    """The re-notify window (one second here) still re-pages the sweep's
    other alerts; an overdue assignment is not re-paged for its age."""
    log = fleet.start_manager()
    fleet.assign("1", "w1", due_ago=600)
    fleet.sweep(FLEET_PULSE_RENOTIFY_AFTER_S="1")
    time.sleep(2)
    fleet.sweep(FLEET_PULSE_RENOTIFY_AFTER_S="1")
    assert len(pages(log)) == 1, log.read_text()


def test_each_assignment_past_its_deadline_pages_once_naming_itself(fleet):
    """A second assignment pages while the first is still past due, and a
    third pages after both closed and nothing in the fleet was past due. Each
    page names its own assignment and says what is missing."""
    log = fleet.start_manager()
    wi1, first = fleet.assign("1", "w1", due_ago=600)
    fleet.sweep()
    wi2, second = fleet.assign("2", "w1", due_ago=60)
    fleet.sweep()
    fleet.report(wi1, first, "completed")
    fleet.report(wi2, second, "completed")
    fleet.sweep()
    assert fleet.overdue() == {}
    _wi, third = fleet.assign("3", "w1", due_ago=60)
    fleet.sweep()
    got = pages(log)
    assert len(got) == 3, log.read_text()
    for page, asg in zip(got, (first, second, third)):
        assert asg in page and "no terminal linked report" in page, page
    assert first not in got[1] and first not in got[2] and second not in got[2], got


def test_a_page_no_manager_took_goes_out_once_one_is_there(fleet):
    """With no manager session the push types nothing, so nothing is
    recorded, and the first sweep with a manager sends the page (#831)."""
    fleet.assign("1", "w1", due_ago=600)
    fleet.sweep()
    log = fleet.start_manager()
    fleet.sweep()
    fleet.sweep()
    assert len(pages(log)) == 1, log.read_text()


def test_a_reader_that_cannot_answer_changes_nothing(fleet):
    """A refused overdue read is not "nothing past due": the record stands."""
    log = fleet.start_manager()
    fleet.assign("1", "w1", due_ago=600)
    fleet.sweep()
    fleet.sweep(lib=fleet.pulse_lib(fleet.tmp / "lib-refusing", **{"dispatch-overdue.py": REFUSING}))
    fleet.sweep()
    assert len(pages(log)) == 1, log.read_text()
