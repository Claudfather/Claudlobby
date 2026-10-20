"""browser-scan.py: the browsers running outside a render scope (#2332).

Each case builds a /proc of its own (stat, exe, cgroup, smaps_rollup per pid) and
runs the script over it through its --proc seam; one case reads the real /proc,
with a copy of sleep named chromium standing in for a browser.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "claudlobby/_runtime_scripts/browser-scan.py"
LIB_COMMON = REPO / "claudlobby/_runtime_scripts/lib-common.sh"
UPTIME = 100000.0
TICK = os.sysconf("SC_CLK_TCK")
APP = "/user.slice/user-1000.slice/user@1000.service/app.slice"


def _pattern() -> str:
    """The shipped list of browser names, read from lib-common.sh."""
    match = re.search(r"^BROWSER_EXE_PATTERN='([^']+)'$", LIB_COMMON.read_text(), re.MULTILINE)
    assert match, "BROWSER_EXE_PATTERN not found in lib-common.sh"
    return match.group(1)


class FakeProc:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir()
        (root / "uptime").write_text(f"{UPTIME:.2f} 1.00\n")

    def add(self, pid, ppid, exe, cgroup, age=3600, pss_kib=1024, comm=None):
        d = self.root / str(pid)
        d.mkdir()
        start = int((UPTIME - age) * TICK)
        comm = comm or os.path.basename(exe)[:15]
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} " + " ".join(["0"] * 17) + f" {start} 0 0\n")
        os.symlink(exe, d / "exe")
        (d / "comm").write_text(comm + "\n")
        (d / "cgroup").write_text(f"0::{cgroup}\n")
        (d / "smaps_rollup").write_text(f"Rss: {pss_kib * 2} kB\nPss: {pss_kib} kB\n")
        return start


def _scan(proc: Path, *extra: str) -> list[list[str]]:
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--pattern", _pattern(), "--proc", str(proc), *extra],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return [line.split("\t") for line in result.stdout.splitlines()]


def _bot_session(proc: FakeProc, unit="com.example.alpha.service"):
    """A bot's subreaper in its unit, adopting what the session orphans."""
    proc.add(900, 1, "/usr/bin/python3.11", f"{APP}/{unit}", comm="bot-subreaper")
    return 900


def test_a_browser_a_bot_session_left_running_is_reported_with_its_bot(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    parent = _bot_session(proc)
    scope = f"{APP}/app-org.chromium.Chromium-1000.scope"
    start = proc.add(1000, parent, "/usr/lib/chromium/chromium", scope, age=1800, pss_kib=200 * 1024)
    proc.add(1001, 1000, "/usr/lib/chromium/chromium", scope, pss_kib=100 * 1024)
    proc.add(1002, 1001, "/usr/lib/chromium/chromium", scope, pss_kib=100 * 1024)
    proc.add(1003, parent, "/usr/lib/chromium/chrome_crashpad_handler", scope, pss_kib=2 * 1024)
    rows = _scan(proc.root, "--unit", "com.example.alpha=alpha")
    assert rows == [["1000", str(start), "1800", "4", "402", "alpha",
                     "app-org.chromium.Chromium-1000.scope", "chromium"]]


def test_a_browser_younger_than_the_floor_is_not_reported(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    parent = _bot_session(proc)
    proc.add(1000, parent, "/usr/lib/chromium/chromium", f"{APP}/app-org.chromium.Chromium-1000.scope", age=899)
    assert _scan(proc.root) == []
    assert len(_scan(proc.root, "--min-age", "0")) == 1


@pytest.mark.parametrize("where", ["own", "ancestor"])
def test_a_browser_in_a_render_scope_is_not_reported(tmp_path, where):
    proc = FakeProc(tmp_path / "proc")
    render = f"{APP}/heavy-render-alpha-render-1760000000-4242.scope"
    proc.add(800, 1, "/usr/bin/node", render, comm="node")
    # a browser that moved itself into a scope of its own is still its render's
    cgroup = render if where == "own" else f"{APP}/app-org.chromium.Chromium-1000.scope"
    proc.add(1000, 800, "/usr/lib/chromium/headless_shell", cgroup, age=1800)
    assert _scan(proc.root) == []


def test_a_browser_no_bot_started_has_no_owner(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    proc.add(948, 1, "/usr/lib/systemd/systemd", "/user.slice/user-1000.slice/user@1000.service/init.scope")
    proc.add(1000, 948, "/usr/lib/chromium/chromium", "/user.slice/user-1000.slice/session-2.scope")
    rows = _scan(proc.root, "--unit", "com.example.alpha=alpha")
    assert [(row[0], row[5], row[6]) for row in rows] == [("1000", "-", "session-2.scope")]


def test_the_owner_is_found_through_the_parent_chain(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    unit = f"{APP}/com.example.beta.service"
    proc.add(900, 1, "/usr/bin/tmux", unit, comm="tmux: server")
    proc.add(901, 900, "/usr/bin/bash", unit, comm="bash")
    proc.add(902, 901, "/usr/bin/xdg-open", unit, comm="xdg-open")
    proc.add(1000, 902, "/usr/lib/chromium/chromium", f"{APP}/app-org.chromium.Chromium-1000.scope")
    rows = _scan(proc.root, "--unit", "com.example.alpha=alpha", "--unit", "com.example.beta=beta")
    assert [row[5] for row in rows] == ["beta"]


def test_a_crash_handler_alone_is_not_a_browser(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    parent = _bot_session(proc)
    proc.add(1003, parent, "/usr/lib/chromium/chrome_crashpad_handler", f"{APP}/com.example.alpha.service")
    assert _scan(proc.root, "--min-age", "0") == []


def test_the_name_is_the_executables_not_its_cut_comm(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    parent = _bot_session(proc)
    # the kernel cuts comm at 15 characters: "google-chrome-s"; a replaced binary reads "(deleted)"
    proc.add(1000, parent, "/opt/google/chrome/google-chrome-stable", f"{APP}/x.scope")
    proc.add(1100, parent, "/usr/lib/chromium/chromium (deleted)", f"{APP}/y.scope")
    proc.add(1200, parent, "/home/u/bin/not-chromium", f"{APP}/z.scope")
    names = sorted(row[7] for row in _scan(proc.root))
    assert names == ["chromium", "google-chrome-stable"]


def test_rows_are_oldest_first(tmp_path):
    proc = FakeProc(tmp_path / "proc")
    parent = _bot_session(proc)
    proc.add(1000, parent, "/usr/lib/chromium/chromium", f"{APP}/a.scope", age=1000)
    proc.add(2000, parent, "/usr/lib/chromium/chromium", f"{APP}/b.scope", age=5000)
    assert [row[0] for row in _scan(proc.root)] == ["2000", "1000"]


def test_with_no_proc_to_read_it_prints_nothing(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert _scan(empty) == []


def test_a_malformed_unit_pair_is_a_usage_error(tmp_path):
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), "--pattern", _pattern(),
                             "--unit", "no-owner"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2 and "LABEL=OWNER" in result.stderr


@pytest.mark.skipif(not Path("/proc/self/exe").exists(), reason="reads a Linux /proc")
def test_it_reads_the_real_proc_and_signals_nothing(tmp_path):
    browser = tmp_path / "chromium"
    shutil.copy("/bin/sleep", browser)
    child = subprocess.Popen([str(browser), "60"])
    try:
        time.sleep(0.3)
        rows = _scan(Path("/proc"), "--min-age", "0")
        assert str(child.pid) in [row[0] for row in rows]
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()
