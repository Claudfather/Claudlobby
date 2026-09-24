"""update-claude-code.sh — success is the staged binary RUNNING, not npm's exit.

The defect these pin: npm exited 0 while omitting the platform-native optional
dependency, leaving a stub that prints an error and exits 1. The updater read
its own could-not-measure sentinel as a version, logged the run as a successful
update, and raised nothing — while every bot that started afterwards failed to
launch. The success predicate is now "the binary the fleet launches RAN and
printed a parseable version", and a binary that cannot run is loud at BOTH ends
of a run.

THE POSITIVE CONTROL COMES FIRST. `broken_stub()` builds the real failure's
shape, taken from a live capture (a reload job that ran the stub on the affected host):
exit 1, a multi-line error, no version on any line. It is installed by an npm
stub that exits 0, so npm's status says "fine" and only the verification can
notice. A test that only proved the guard passes a healthy binary would pass
the defect too. The capture does not record which stream the stub writes to,
so both are pinned.

Hermetic by construction, because this module FIRES the alert path:
  - a CONSTRUCTED env, never an os.environ copy — a bot session carries the
    real Telegram chat id and token, and an alert fired from inside one would
    post to the real group;
  - tg-post stubbed under a throwaway root (`_signal_root`, shared with
    test_maintenance_jobs; the alert path resolves it through CLAUDLOBBY_ROOT),
    recording what would have been sent;
  - npm stubbed in $HOME/.local/bin, which the script PREPENDS to PATH, and the
    fleet PATH pinned at an empty dir, so a regression in CLAUDE_BIN resolution
    finds no binary and fails closed instead of reaching a real install;
  - the plane is the throwaway root's own (cold CLI rung, no daemon), read back
    through read_fleet_events.
"""

from __future__ import annotations

import datetime
import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import (
    _write_exec,
    constructed_env,
    plane_emit_env,
    read_fleet_events,
)
from tests.test_maintenance_jobs import _captured, _signal_root

SCRIPT = Path(__file__).resolve().parent.parent / "lib" / "update-claude-code.sh"

# The live capture's text, verbatim: the package's generic message, no host
# identifiers in it.
_BROKEN_MESSAGE = """\
Error: claude native binary not installed.

Either postinstall did not run (--ignore-scripts, some pnpm configs)
or the platform-native optional dependency was not downloaded
(--omit=optional).

Run the postinstall manually (adjust path for local vs global install):
  node node_modules/@anthropic-ai/claude-code/install.cjs

Or reinstall without --ignore-scripts / --omit=optional.
"""


def broken_stub(stream: str) -> str:
    redirect = " >&2" if stream == "stderr" else ""
    return f"#!/bin/bash\ncat{redirect} <<'MSG'\n{_BROKEN_MESSAGE}MSG\nexit 1\n"


def healthy(version: str) -> str:
    return f'#!/bin/bash\necho "{version} (Claude Code)"\n'


# Records its argv, optionally takes time (the stamp test), "installs" by
# copying the staged binary over the fleet's, and exits with the chosen status.
NPM_STUB = (
    "#!/bin/bash\n"
    'echo "npm-stub called: $*" >> "$NPM_CALLS"\n'
    'if [ -n "${NPM_SLEEP:-}" ]; then sleep "$NPM_SLEEP"; fi\n'
    'if [ -n "${NPM_STAGE:-}" ]; then cp "$NPM_STAGE" "$CLAUDE_BIN"; fi\n'
    'exit "${NPM_RC:-0}"\n'
)


class Host:
    """A throwaway host: a fleet binary, an npm that swaps it, a captured alert
    channel and the root's own plane."""

    def __init__(self, tmp_path, installed, staged=None, npm_rc=0, npm_sleep=None):
        self.tmp = tmp_path
        # A fleet-less host job resolves its alert chat id from a declaring bot;
        # this root declares a fake one and stubs the sender.
        self.root = _signal_root(tmp_path)
        self.home = tmp_path / "home"
        self.capture = tmp_path / "tg-capture"
        self.calls = tmp_path / "npm.calls"
        self.bin = tmp_path / "fleetbin" / "claude"
        (self.home / ".local" / "bin").mkdir(parents=True)
        _write_exec(self.home / ".local" / "bin" / "npm", NPM_STUB)
        (tmp_path / "empty").mkdir()
        self.bin.parent.mkdir()
        _write_exec(self.bin, installed)
        self.extra = {"NPM_RC": str(npm_rc)}
        if staged is not None:
            _write_exec(tmp_path / "staged", staged)
            self.extra["NPM_STAGE"] = tmp_path / "staged"
        if npm_sleep is not None:
            self.extra["NPM_SLEEP"] = str(npm_sleep)

    def run(self):
        env = constructed_env(
            CLAUDLOBBY_ROOT=self.root,
            HOME=self.home,
            TG_CAPTURE=self.capture,
            NPM_CALLS=self.calls,
            CLAUDE_BIN=self.bin,
            CLAUDE_UPDATE_FLEET_PATH=self.tmp / "empty",
            # These tests assert on the plane, and with no daemon each event is a
            # cold `emit-batch` spawn: on a loaded host that outruns the 10s
            # production bound and the event is reaped (forced at 1s, it drops
            # binary_unrunnable). Give the cold rung room.
            FLEET_EVENT_EMIT_TIMEOUT_S="120",
            **plane_emit_env(),
            **self.extra,
        )
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=300
        )

    def log(self) -> str:
        p = self.root / "state" / "claude-update.log"
        return p.read_text() if p.exists() else ""

    def sent(self) -> list[str]:
        return _captured(self.tmp).splitlines()

    def events(self) -> str:
        return read_fleet_events(self.root)


def _event_types(events: str) -> list[str]:
    return re.findall(r'"type":"([a-z_]+)"', events)


BINARY_SIGNALS = {"binary_update_failed", "binary_unrunnable", "binary_repaired"}


def _assert_alert_path_ran_clean(h):
    """The failure paths must not trip install_error_trap: a script_error row
    would be a second, misleading alert for a condition already raised."""
    assert "script_error" not in _event_types(h.events()), h.events()


# --- the positive control ------------------------------------------------------


@pytest.mark.parametrize("stream", ["stderr", "stdout"])
def test_positive_control_npm_exit_0_leaving_a_stub_fires_update_failed(
    tmp_path, stream
):
    h = Host(tmp_path, installed=healthy("2.1.278"), staged=broken_stub(stream))
    r = h.run()

    assert h.calls.exists(), "precondition: the install ran and swapped the binary"
    assert r.returncode == 1, (r.returncode, r.stderr)
    log = h.log()
    assert "UPDATE FAILED" in log, log
    assert "npm install returned 0 but the staged binary cannot run" in log
    # The operator is told WHY, in the stub's own words.
    assert "exited 1: Error: claude native binary not installed." in log
    # Two of the alert's three channels are observable here: the plane event
    # and Telegram (no manager is declared, so there is no nudge to reach).
    assert "binary_update_failed" in _event_types(h.events())
    assert any("FLEET ALERT [binary_update_failed]" in line for line in h.sent()), (
        h.sent()
    )
    # And nothing claims the update worked.
    assert "UPDATE verified" not in log
    assert "version changed" not in log
    assert "unknown" not in log
    _assert_alert_path_ran_clean(h)


# --- each half of the predicate, pinned on its own -----------------------------


def test_a_binary_that_exits_nonzero_is_not_measured_even_when_it_prints_a_version(
    tmp_path,
):
    # "RAN" is half the predicate. A parse-only check reads 2.1.281 here and
    # calls it a healthy upgrade.
    staged = '#!/bin/bash\necho "2.1.281 (Claude Code)"\nexit 1\n'
    h = Host(tmp_path, installed=healthy("2.1.278"), staged=staged)
    r = h.run()
    assert r.returncode == 1, r.stderr
    assert "exited 1" in h.log()
    assert "binary_update_failed" in _event_types(h.events())
    assert "version changed" not in h.log()


def test_a_binary_that_runs_but_prints_no_version_is_not_measured(tmp_path):
    # "Parseable version" is the other half. An exit-status-only check passes
    # this binary.
    staged = '#!/bin/bash\necho "Claude Code"\nexit 0\n'
    h = Host(tmp_path, installed=healthy("2.1.278"), staged=staged)
    r = h.run()
    assert r.returncode == 1, r.stderr
    assert "printed no parseable version: Claude Code" in h.log()
    assert "binary_update_failed" in _event_types(h.events())


# --- the healthy paths stay quiet ----------------------------------------------


def test_a_healthy_upgrade_raises_nothing(tmp_path):
    h = Host(tmp_path, installed=healthy("2.1.278"), staged=healthy("2.1.281"))
    r = h.run()
    assert r.returncode == 0, r.stderr
    log = h.log()
    assert "current: 2.1.278" in log
    assert "UPDATE verified: the staged binary ran and reported 2.1.281" in log
    assert "version changed: 2.1.278 → 2.1.281" in log
    assert h.sent() == []
    types = _event_types(h.events())
    assert not BINARY_SIGNALS & set(types), types
    _assert_alert_path_ran_clean(h)


def test_an_already_current_binary_is_a_no_op(tmp_path):
    h = Host(tmp_path, installed=healthy("2.1.281"))
    r = h.run()
    assert r.returncode == 0, r.stderr
    assert "no-op: already on 2.1.281" in h.log()
    assert h.sent() == []


def test_an_npm_failure_reports_what_the_binary_measures_now(tmp_path):
    # The binary is measured after a failed install, never assumed unchanged:
    # a failed install may still have replaced it.
    h = Host(tmp_path, installed=healthy("2.1.278"), npm_rc=1)
    r = h.run()
    assert r.returncode == 1, r.stderr
    assert "npm install returned 1 — the fleet's binary runs 2.1.278" in h.log()
    assert "binary_update_failed" in _event_types(h.events())


# --- a run that STARTS on an unrunnable binary ---------------------------------


def test_starting_on_an_unrunnable_binary_is_the_alarm_raised_before_the_install(
    tmp_path,
):
    h = Host(tmp_path, installed=broken_stub("stderr"), staged=healthy("2.1.281"))
    r = h.run()
    assert r.returncode == 0, r.stderr
    log = h.log()
    assert "current: CANNOT RUN (" in log
    # Raised BEFORE the install, which can run for many minutes.
    assert log.index("UPDATE ALERT") < log.index("UPDATE running")
    sent = h.sent()
    assert "FLEET ALERT [binary_unrunnable]" in sent[0], sent
    # The same run repaired it, and says so rather than leaving the alert open.
    assert "UPDATE repaired" in log
    assert any("FLEET NOTICE [binary_repaired]" in line for line in sent), sent
    types = _event_types(h.events())
    assert "binary_unrunnable" in types and "binary_repaired" in types, types
    assert "binary_update_failed" not in types
    assert "unknown" not in log
    _assert_alert_path_ran_clean(h)


def test_a_reinstall_that_does_not_repair_raises_both_alerts(tmp_path):
    h = Host(tmp_path, installed=broken_stub("stderr"), staged=broken_stub("stderr"))
    r = h.run()
    assert r.returncode == 1, r.stderr
    types = _event_types(h.events())
    assert "binary_unrunnable" in types and "binary_update_failed" in types, types
    assert "binary_repaired" not in types
    assert "UPDATE repaired" not in h.log()


# --- stamps --------------------------------------------------------------------


def _stamp(log: str, marker: str) -> datetime.datetime:
    line = next(line for line in log.splitlines() if marker in line)
    return datetime.datetime.fromisoformat(line.split(" ", 1)[0])


def test_each_log_line_is_stamped_when_it_is_written(tmp_path):
    # Stamped at write time: the install's duration shows in the log. Stamps
    # have whole-second resolution and the gap is at least the npm sleep, so
    # 1s is enough to move the seconds field.
    h = Host(
        tmp_path, installed=healthy("2.1.278"), staged=healthy("2.1.281"), npm_sleep=1
    )
    assert h.run().returncode == 0
    log = h.log()
    assert _stamp(log, "UPDATE install finished") > _stamp(log, "UPDATE running"), log
