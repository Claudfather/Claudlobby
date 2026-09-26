"""Portable plist contracts plus native macOS utility/lifecycle evidence.

The service smoke is opt-in and runs only on an ephemeral GitHub-hosted Mac.
It composes a real plist whose launcher is a scratch executable, observes that
process, and proves cleanup on success and assertion failure. No fleet service
installer, real bot session, credentials, or Telegram transport is involved.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
import platform
import plistlib
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from claudlobby.composer import compose_launchd_plist
from claudlobby.config import BotConfig, FleetConfig
from claudlobby.paths import Paths

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"

# Reserved for what the platform genuinely withholds — a launchctl binary that
# does not exist off Darwin. The reason names `launchctl` specifically rather
# than "macOS", so a reader of a skipped run learns exactly which rung is
# missing instead of guessing at the whole platform. Paired with
# `addopts = "-rsfE"` (pyproject.toml) this prints on every run rather than
# collapsing into a bare `s`. The `s` is the half that does that; `fE` restores
# the failure names the bare `-rs` had replaced (#1509).
needs_launchctl = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason=(
        "needs macOS `launchctl` — the binary does not exist on Linux, so this "
        "rung runs in the supported macOS CI lane (#1810)"
    ),
)


# --- runs everywhere: pure POSIX / portable stdlib ---------------------------


def test_launchd_bot_enroller_is_executable():
    """The most basic thing no test asserted about the launchd path.

    A `stat` needs no launchd. If this enroller loses its exec bit, macOS
    supervision breaks — and that is detectable from Linux, so it is checked
    from Linux.
    """
    enroller = LIB / "install-bot.sh"
    assert enroller.is_file(), f"{enroller} missing"
    assert enroller.stat().st_mode & 0o111, f"{enroller} is not executable"


def test_composed_plist_is_structurally_valid(tmp_path):
    """The composed plist parses as a plist, and carries the right Label.

    `plistlib` is portable stdlib, so this is a real structural assertion on
    every platform — it catches malformed XML and a wrong Label wherever it
    runs. It is NOT the platform's acceptance verdict: only `plutil`/launchd on
    a Mac can say launchd would load it, and nothing here claims otherwise.
    """
    bot = BotConfig(bot_id="w", name="w", expertise=["eng"])
    fleet = FleetConfig(name="t", service_prefix="p", bots={"w": bot})
    (tmp_path / "runtime" / "bots" / "w").mkdir(parents=True)

    text = compose_launchd_plist(bot, fleet, Paths(root=tmp_path, fleet_dir=tmp_path))
    parsed = plistlib.loads(text.encode())

    assert parsed["Label"] == "p.w"
    assert parsed["ProgramArguments"], "plist declares no ProgramArguments"


# --- genuinely gated: needs a binary Linux does not have ---------------------


@needs_launchctl
def test_launchctl_is_reachable():
    """If this fails on a Mac, every launchd rung below it is moot."""
    rc = subprocess.run(["launchctl", "version"], capture_output=True).returncode
    assert rc == 0, "launchctl not reachable on a Darwin host"


def test_the_gate_names_the_binary_not_just_the_platform():
    """Guard the guard: a skip reason that only says "macOS" makes the reader
    guess which rung is missing, and a thin reason is barely louder than a pass
    — the failure mode #1012 is about. Runs everywhere."""
    reason = needs_launchctl.kwargs["reason"]
    assert "launchctl" in reason
    assert len(reason) > 40, "reason too thin to tell a reader what is missing"


@needs_launchctl
def test_native_plutil_accepts_composed_plist(tmp_path):
    bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
    fleet = FleetConfig(name="ci", service_prefix="claudlobby-ci", bots={"worker": bot})
    plist = tmp_path / "worker.plist"
    plist.write_text(compose_launchd_plist(bot, fleet, Paths(root=tmp_path)))
    result = subprocess.run(["/usr/bin/plutil", "-lint", str(plist)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@contextmanager
def _native_scratch_job(tmp_path):
    prefix = "claudlobby-ci-" + uuid.uuid4().hex
    bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
    fleet = FleetConfig(name="ci", service_prefix=prefix, bots={"worker": bot})
    paths = Paths(root=tmp_path)
    bot_dir = paths.bot_runtime(bot.bot_id)
    bot_dir.mkdir(parents=True)
    (paths.lib / "logs").mkdir(parents=True)
    launcher = paths.lib / "start-bot.sh"
    launcher.write_text('#!/bin/bash\nprintf "%s\\n" "$$" > "$1/native.pid"\nexec /bin/sleep 60\n')
    launcher.chmod(0o755)
    plist = tmp_path / "native.plist"
    plist.write_text(compose_launchd_plist(bot, fleet, paths))
    lint = subprocess.run(["/usr/bin/plutil", "-lint", str(plist)],
                          capture_output=True, text=True)
    assert lint.returncode == 0, lint.stdout + lint.stderr
    domain = None
    diagnostics = []
    for candidate in (f"gui/{os.getuid()}", f"user/{os.getuid()}"):
        probe = subprocess.run(["/bin/launchctl", "print", candidate],
                               capture_output=True, text=True)
        if probe.returncode == 0:
            domain = candidate
            break
        diagnostics.append(f"{candidate}: {probe.stderr.strip()}")
    assert domain, "No usable per-user launchd domain: " + "; ".join(diagnostics)
    target = f"{domain}/{prefix}.worker"
    try:
        start = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist)],
                               capture_output=True, text=True)
        assert start.returncode == 0, f"bootstrap {target}: {start.stdout}{start.stderr}"
        pidfile = bot_dir / "native.pid"
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        assert pidfile.exists(), f"job {target} never started"
        pid = int(pidfile.read_text().strip())
        state = subprocess.run(["/bin/launchctl", "print", target],
                               capture_output=True, text=True)
        assert state.returncode == 0, state.stderr
        assert f"pid = {pid}" in state.stdout, state.stdout
        command = subprocess.check_output(["ps", "-p", str(pid), "-o", "comm="], text=True)
        assert command.strip().endswith("sleep"), command
        yield target
    finally:
        stop = subprocess.run(["/bin/launchctl", "bootout", target],
                              capture_output=True, text=True)
        for _ in range(100):
            absent = subprocess.run(["/bin/launchctl", "print", target],
                                    capture_output=True, text=True)
            if absent.returncode != 0:
                break
            time.sleep(0.05)
        assert absent.returncode != 0, f"cleanup failed for {target}: {stop.stderr}"


@pytest.mark.skipif(
    platform.system() != "Darwin"
    or os.environ.get("CLAUDLOBBY_CI_NATIVE_SMOKE") != "1"
    or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted",
    reason="launchctl service mutation requires an opted-in GitHub-hosted macOS runner",
)
@pytest.mark.parametrize("fail_after_start", [False, True], ids=["success", "assertion-failure"])
def test_native_launchd_scratch_lifecycle(tmp_path, fail_after_start):
    if fail_after_start:
        with pytest.raises(AssertionError, match="exercise cleanup"):
            with _native_scratch_job(tmp_path):
                raise AssertionError("exercise cleanup")
    else:
        with _native_scratch_job(tmp_path):
            pass
