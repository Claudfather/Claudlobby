"""fleet-pulse's browser leg (#2332): a browser that runs outside a render scope
is reported once, with the bot whose session started it, and nothing stops it.

Each case drives the REAL sweep (the repo's claudlobby/_runtime_scripts/, linked
file by file) over a fleet whose bots have no session, against a /proc of the
test's own (BROWSER_SCAN_PROC). Stand-ins: plane-emit.sh appends each batch to a
file (no daemon and no install), tg-post.sh appends its page, systemctl answers
nothing, and dispatch-overdue.py and plane-lookup.py read an empty plane.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.test_browser_scan import APP, FakeProc

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "claudlobby/_runtime_scripts"
pytestmark = [
    pytest.mark.skipif(shutil.which("tmux") is None, reason="fleet-pulse needs tmux"),
    pytest.mark.skipif(platform.system() != "Linux", reason="drives the systemd branch"),
]
STUBS = {
    "plane-emit.sh": '#!/bin/bash\ncat >> "$EMIT_CAPTURE"\n',
    "tg-post.sh": '#!/bin/bash\nprintf "%s\\n" "$1" >> "$TG_CAPTURE"\n',
    "dispatch-overdue.py": "import sys\nsys.exit(0)\n",
    "plane-lookup.py": "import sys\nsys.exit(0)\n",
}


class Host:
    """A root with fleets of bots, their pulse state, and a /proc to scan."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.root = tmp / "root"
        (self.root / "state" / "pulse").mkdir(parents=True)
        (self.root / "home").mkdir()
        self.lib = tmp / "lib"
        self.lib.mkdir()
        for f in LIB.iterdir():
            if f.name not in STUBS:
                (self.lib / f.name).symlink_to(f)
        for name, body in STUBS.items():
            (self.lib / name).write_text(body)
            (self.lib / name).chmod(0o755)
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "systemctl").write_text("#!/bin/sh\nexit 3\n")
        (self.bin / "systemctl").chmod(0o755)
        self.capture = tmp / "emitted.jsonl"
        self.tg = tmp / "tg.log"
        # a short path: tmux's socket path is limited to about 100 bytes
        self.tmux = Path(tempfile.mkdtemp(prefix="t2332-", dir="/tmp"))
        self.proc = FakeProc(tmp / "proc")

    def fleet(self, name: str, prefix: str, bots, manager=None):
        fleet_dir = self.root / "local" / name
        fleet_dir.mkdir(parents=True)
        listing = "".join(f"    {b}:\n      expertise: [software-engineering]\n" for b in bots)
        (fleet_dir / "fleet.yaml").write_text(
            f"fleet:\n  manager: {bots[-1]}\n  name: {name}\n  service_prefix: {prefix}\n  bots:\n{listing}")
        for b in bots:
            bot = fleet_dir / "runtime" / "bots" / b
            (bot / "data").mkdir(parents=True)
            conf = (f"BOT_NAME={b}\nBOT_ID={b}\nFLEET_NAME={name}\nBOT_SERVICE={prefix}.{b}\n"
                    f"TMUX_SOCKET=t2332-none-{b}\n")
            if manager:
                conf += f"MANAGER_TMUX={manager[1]}\nMANAGER_TMUX_SOCKET={manager[0]}\n"
            (bot / "bot.conf").write_text(conf)

    def sweep(self, fleet: str):
        env = {"PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin", "HOME": str(self.root / "home"),
               "LANG": "C.UTF-8", "CLAUDLOBBY_ROOT": str(self.root), "FLEET_NAME": fleet,
               "PLANE_EMIT_DISABLED": "0", "EMIT_CAPTURE": str(self.capture), "TG_CAPTURE": str(self.tg),
               "TMUX_TMPDIR": str(self.tmux), "BROWSER_SCAN_PROC": str(self.proc.root)}
        result = subprocess.run(["bash", str(self.lib / "fleet-pulse.sh"), fleet], env=env,
                                capture_output=True, text=True, errors="replace", timeout=300)
        assert result.returncode == 0, result.stderr[-3000:]
        return result

    def reports(self):
        """Every browser_outside_render row recorded so far: (fleet, subject, data)."""
        rows = []
        for line in self.capture.read_text().splitlines() if self.capture.exists() else []:
            for event in json.loads(line)["events"]:
                payload = event["payload"]
                if payload["event"] == "browser_outside_render":
                    rows.append((event["fleet"], payload["subject"], payload["data"]["data"]))
        return rows

    def seen(self, fleet: str):
        path = self.root / "state" / "pulse" / f"{fleet}.browsers"
        return path.read_text().split() if path.exists() else []

    def summary(self, fleet: str):
        return (self.root / "state" / "pulse" / f"{fleet}.pulse-summary.txt").read_text()

    def tmux_cmd(self, *argv):
        return subprocess.run(["tmux", *argv], env={**os.environ, "TMUX_TMPDIR": str(self.tmux)},
                              capture_output=True, text=True, timeout=30)

    def close(self):
        for sock in ("t2332-mgr",):
            self.tmux_cmd("-L", sock, "kill-server")
        shutil.rmtree(self.tmux, ignore_errors=True)


@pytest.fixture
def host(tmp_path):
    h = Host(tmp_path)
    try:
        yield h
    finally:
        h.close()


def _browser(host, pid, parent, cgroup, age=1800, pss_mib=300):
    return host.proc.add(pid, parent, "/usr/lib/chromium/chromium", cgroup, age=age, pss_kib=pss_mib * 1024)


def _left_by(host, unit, pid=900):
    """A bot's subreaper in its unit: the parent of what its session orphaned."""
    host.proc.add(pid, 1, "/usr/bin/python3.11", f"{APP}/{unit}", comm="bot-subreaper")
    return pid


def test_a_browser_a_bot_left_running_is_recorded_and_reported_once(host):
    host.fleet("F", "com.t", ["b1", "b2"])
    start = _browser(host, 1000, _left_by(host, "com.t.b1.service"), f"{APP}/app-org.chromium.Chromium-1000.scope")
    host.sweep("F")
    assert host.reports() == [("F", "bot:F/b1", {
        "pid": 1000, "start": start, "age_s": 1800, "processes": 1, "pss_mib": 300, "owner": "b1",
        "unit": "app-org.chromium.Chromium-1000.scope", "name": "chromium"})]
    assert host.seen("F") == [f"1000-{start}"]
    assert "Browsers outside a render scope: chromium pid 1000 (30 min, 300 MiB, b1);" in host.summary("F")
    host.sweep("F")
    assert len(host.reports()) == 1
    assert host.seen("F") == [f"1000-{start}"]


def test_a_browser_that_has_gone_drops_its_key(host):
    host.fleet("F", "com.t", ["b1", "b2"])
    _browser(host, 1000, _left_by(host, "com.t.b1.service"), f"{APP}/app-org.chromium.Chromium-1000.scope")
    host.sweep("F")
    assert len(host.seen("F")) == 1
    shutil.rmtree(host.proc.root / "1000")
    host.sweep("F")
    assert host.seen("F") == []
    assert "Browsers outside a render scope" not in host.summary("F")


def test_render_scopes_and_young_browsers_are_not_reported(host):
    host.fleet("F", "com.t", ["b1", "b2"])
    parent = _left_by(host, "com.t.b1.service")
    _browser(host, 1000, parent, f"{APP}/heavy-render-F-b1-1760000000-4242.scope")
    _browser(host, 1100, parent, f"{APP}/app-org.chromium.Chromium-1100.scope", age=600)
    host.sweep("F")
    assert host.reports() == [] and host.seen("F") == []


def test_one_report_per_browser_on_a_host_of_two_fleets(host):
    host.fleet("F", "com.t", ["b1", "b2"])
    host.fleet("G", "com.u", ["g1", "g2"])
    _browser(host, 1000, _left_by(host, "com.u.g1.service"), f"{APP}/app-org.chromium.Chromium-1000.scope")
    host.proc.add(948, 1, "/usr/lib/systemd/systemd", "/user.slice/user-1000.slice/user@1000.service/init.scope")
    _browser(host, 2000, 948, "/user.slice/user-1000.slice/session-2.scope")
    host.sweep("F")
    host.sweep("G")
    # g1's browser is G's to report; the one no bot started is the first fleet's (F)
    got = sorted((fleet, subject, data["pid"], data["owner"]) for fleet, subject, data in host.reports())
    assert got == [("F", "F", 2000, None), ("G", "bot:G/g1", 1000, "g1")]


def test_a_push_the_manager_did_not_take_keeps_no_key(host):
    host.fleet("F", "com.t", ["b1", "b2"], manager=("t2332-mgr", "mgr"))
    start = _browser(host, 1000, _left_by(host, "com.t.b1.service"), f"{APP}/app-org.chromium.Chromium-1000.scope")
    assert host.tmux_cmd("-L", "t2332-mgr", "new-session", "-d", "-s", "mgr", "sleep 600").returncode == 0
    token = host.tmux_cmd("-L", "t2332-mgr", "display-message", "-p", "-t", "mgr",
                          "#{session_created}-#{pane_pid}").stdout.strip()
    # the manager did not take a push moments ago: its floor holds this one back (rc 3)
    (host.root / "state" / "pulse" / "held-push.t2332-mgr_mgr").write_text(token)
    host.sweep("F")
    host.sweep("F")
    assert [data["pid"] for _, _, data in host.reports()] == [1000, 1000]
    assert host.seen("F") == []
    assert f"1000-{start}" not in host.seen("F")


def test_a_push_with_no_manager_session_keeps_no_key(host):
    """A manager is declared and its session is not there: nothing was typed,
    so the key waits for a push that goes through (#2457)."""
    host.fleet("F", "com.t", ["b1", "b2"], manager=("t2332-mgr", "mgr"))
    _browser(host, 1000, _left_by(host, "com.t.b1.service"), f"{APP}/app-org.chromium.Chromium-1000.scope")
    host.sweep("F")
    assert [data["pid"] for _, _, data in host.reports()] == [1000]
    assert host.seen("F") == []
