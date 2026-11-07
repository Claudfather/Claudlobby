"""The free-memory readers and process tables behind `heavy-slot.py run`'s stop
line (#2221): the meminfo reader the render gate shares, the vm_stat reader that
gives lib-common's `avail_ram_mb` figure on macOS, the /proc and ps process
tables, and the pure debounce."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import constructed_env, load_lib_module
from tests.test_heavy_slot_run import WRAPPER, _env, _events, _run, _wait_for, se  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
# vm_stat's format as macOS 14 prints it, with numbers chosen for these tests: not a
# recording. The darwin-only test below reads the real output on CI's macOS runner
# and records it in that run's junit report.
FIXTURE = REPO / "tests/fixtures/vm_stat-darwin.txt"
# Real vm_stat output, whole, as the darwin-only test below recorded it on CI's
# macos-latest runner (#2786's first run, the junit report of its macOS shard 2).
RECORDED = REPO / "tests/fixtures/vm_stat-macos-runner.txt"
hs = load_lib_module("heavy-slot")

LINUX = sys.platform.startswith("linux")


def _meminfo(proc: Path, mib: int) -> None:
    tmp = proc / ".meminfo"
    tmp.write_text(f"MemTotal:       16384000 kB\nMemAvailable:   {mib * 1024} kB\n")
    os.replace(tmp, proc / "meminfo")


def test_host_readings_and_the_run_watch_read_one_meminfo(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "loadavg").write_text("1.00 2.00 3.00 1/100 42\n")
    _meminfo(proc, 4321)
    monkeypatch.setenv("HEAVY_SLOT_PROC", str(proc))
    assert hs.host_readings()["mem_available_mib"] == 4321
    assert hs._mem_available_mib() == 4321
    (proc / "meminfo").write_text("MemTotal:       16384000 kB\nMemFree: 512000 kB\n")
    with pytest.raises(ValueError):
        hs._mem_available_mib()
    with pytest.raises(ValueError):
        hs.host_readings()


def test_the_vm_stat_reader_counts_free_inactive_and_speculative_pages():
    text = FIXTURE.read_text()
    expected = (12345 + 198765 + 4321) * 16384 // 1048576
    assert hs._vm_stat_mib(text) == expected
    lines = text.splitlines()
    with pytest.raises(ValueError):
        hs._vm_stat_mib("\n".join(lines[1:]))  # no header
    for counter in ("Pages free", "Pages inactive", "Pages speculative"):
        with pytest.raises(ValueError):
            hs._vm_stat_mib("\n".join(x for x in lines if not x.startswith(counter)))


def test_the_vm_stat_reader_on_output_recorded_on_a_mac():
    # Pages free 44252 + inactive 251126 + speculative 161846, in 16384-byte pages.
    assert hs._vm_stat_mib(RECORDED.read_text()) == 7144


def test_where_there_is_no_meminfo_the_watch_reads_vm_stat(se):  # noqa: F811
    proc = se.tmp / "proc"
    proc.mkdir()  # no meminfo here
    vm = se.tmp / "vm_stat.txt"
    over = FIXTURE.read_text().replace("12345.", "400000.")  # ~6.2 GiB free
    under = FIXTURE.read_text().replace("12345.", "1000.").replace("198765.", "1000.")
    vm.write_text(over)
    fake = se.tmp / "fake-vm-stat"
    fake.write_text(f"#!/bin/sh\ncat {vm}\n")
    fake.chmod(0o755)
    se.state.mkdir()
    (se.state / "run-lines").write_text(
        "stop_mib = 3800\ndebounce_reads = 2\npoll_s = 0.1\ngrace_s = 1\n")
    p = subprocess.Popen(
        [str(WRAPPER), "run", "--", "pytest"],
        env=_env(se, HEAVY_SLOT_PROC=proc, HEAVY_SLOT_VM_STAT=fake, STUB_WAIT=se.tmp / "never"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _wait_for(lambda: list(se.tmp.glob("ran.*")))
    time.sleep(0.5)
    assert p.poll() is None  # over the line: not stopped
    vm.write_text(under)
    assert p.wait(30) == 79
    (stopped,) = [e for e in _events(se) if e["type"] == "heavy_slot_stopped"]
    assert stopped["data"]["line"] == "stop_mib"


@pytest.mark.skipif(not LINUX, reason="reads /proc")
def test_the_ps_table_and_the_proc_table_agree():
    child = subprocess.Popen(
        [sys.executable, "-c", "import os, time; os.setsid(); time.sleep(60)"])
    try:
        def both():
            proc, ps = hs._proc_table(), hs._ps_table()
            led = proc and ps and child.pid in proc and child.pid in ps
            return (proc, ps) if led and proc[child.pid][1] == child.pid else None
        proc, ps = _wait_for(both)
        for pid in (os.getpid(), child.pid):
            assert proc[pid][:2] == ps[pid][:2]
        assert ps[child.pid][1] == child.pid  # its own session leads its own group
    finally:
        child.kill()
        child.wait()
    table = hs._parse_ps(
        "  100     1   100 Ss   Sat Nov  7 10:00:00 2026\n"
        "  101   100   100 Z    Sat Nov  7 10:00:01 2026\n"
        "garbage\n"
        "  102   100   100 S    Sat Nov  7  10:00:02 2026\n")
    assert sorted(table) == [100, 102]
    assert table[102] == (100, 100, "Sat Nov 7 10:00:02 2026")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS vm_stat")
def test_on_macos_the_reader_and_avail_ram_mb_agree_on_this_hosts_vm_stat(tmp_path, record_property,
                                                                         monkeypatch):
    text = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
    record_property("vm_stat", text)
    fake = tmp_path / "bin"
    fake.mkdir()
    cap = tmp_path / "captured.txt"
    cap.write_text(text)
    (fake / "vm_stat").write_text(f"#!/bin/sh\ncat {cap}\n")
    (fake / "vm_stat").chmod(0o755)
    lib = REPO / "claudlobby/_runtime_scripts/lib-common.sh"
    out = subprocess.run(
        ["bash", "-c", f". {lib}; avail_ram_mb"], capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"}).stdout.strip()
    assert hs._vm_stat_mib(text) == int(out)
    monkeypatch.delenv("HEAVY_SLOT_PROC", raising=False)
    assert hs._mem_available_mib() > 0


def test_the_stop_watch():
    def watch(*readings):
        w = hs._StopWatch({"stop_mib": 3800.0, "debounce_reads": 2, "poll_s": 2.0})
        return w, [w.take(r) for r in readings]

    assert watch(3000, 5000, 3000)[1] == [False, False, False]
    w, got = watch(3000, 3000)
    assert got == [False, True]
    assert "3000" in w.reading() and "3800" in w.reading()
    w, got = watch(3000, None, 3000)
    assert got == [False, False, False]
    assert w.peak["unreadable"] == 1 and w.peak["readings"] == 2
    assert w.peak["mem_available_mib_low"] == 3000 and w.peak["mem_available_mib_start"] == 3000
