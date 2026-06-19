"""Verify per-bot tmux socket resolution agrees across the lifecycle.

Companion to test_lifecycle_names.py. Where that test asserts unit name
(BOT_SERVICE) and session name (dir slug) stay distinct, this one asserts the
THIRD identity axis — the per-bot tmux server socket — is resolved identically
everywhere it is needed:

  * the bash SSOT helper tmux_socket_for_bot() (lib-common.sh) resolves a bot's
    socket from its dir, preferring TMUX_SOCKET and falling back to BOT_SERVICE;
  * the production guard fails fast on an empty socket while FLEET_NAME is set
    (an empty bare socket would collide across fleets and reintroduce the
    shared-server SPOF the whole change exists to remove);
  * bot_tmux() refuses `tmux -L ""`; bot_tmux_send() logs a send_miss instead of
    silently dropping a cross-socket send;
  * the composer emits TMUX_SOCKET, MANAGER_TMUX_SOCKET, and a pinned
    TMUX_TMPDIR into bot.conf and the service units.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import textwrap

import pytest

from claudlobby.composer import (
    compose_bot_conf,
    compose_launchd_plist,
    compose_systemd_unit,
)
from claudlobby.config import load_fleet
from claudlobby.paths import Paths


# Resolve relative to this file so the test exercises the checkout's
# lib-common.sh, not the shared install's.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB_DIR = os.path.join(_REPO_ROOT, "lib")


# --- bash-helper harness (mirrors test_lifecycle_names.py) ------------------


def _run_bash(script, env=None):
    """Run a bash snippet against lib-common.sh; return (stdout, stderr, rc)."""
    merged_env = {**os.environ, **(env or {})}
    r = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=merged_env,
        timeout=10,
    )
    return r.stdout.strip(), r.stderr.strip(), r.returncode


def _write_bot_conf(bot_dir, *, bot_service="com.test.eng.alpha", tmux_socket="MIRROR"):
    """Write a minimal bot.conf for socket-resolution tests.

    tmux_socket="MIRROR" emits TMUX_SOCKET == bot_service (the real default);
    None omits the TMUX_SOCKET line entirely (simulates an un-regenerated bot).
    """
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "data" / "events").mkdir(parents=True, exist_ok=True)
    lines = [
        "export BOT_ID=alpha",
        "BOT_NAME=alpha",
        f"BOT_SERVICE={bot_service}",
        "BOT_LABEL=ALPHA",
        f'BOT_DIR="{bot_dir}"',
    ]
    if tmux_socket == "MIRROR":
        lines.append(f"TMUX_SOCKET={bot_service}")
    elif tmux_socket is not None:
        lines.append(f"TMUX_SOCKET={tmux_socket}")
    (bot_dir / "bot.conf").write_text("\n".join(lines) + "\n")
    return bot_dir


def _src(snippet):
    return f'. "{LIB_DIR}/lib-common.sh"; {snippet}'


class TestSocketResolution:
    def test_resolves_from_tmux_socket_field(self, tmp_path):
        d = _write_bot_conf(tmp_path / "alpha")
        out, _, rc = _run_bash(_src(f'tmux_socket_for_bot "{d}"'))
        assert rc == 0
        assert out == "com.test.eng.alpha"

    def test_falls_back_to_bot_service_when_unregenerated(self, tmp_path):
        # No TMUX_SOCKET line — an old bot.conf that predates the field.
        d = _write_bot_conf(tmp_path / "alpha", tmux_socket=None)
        out, _, rc = _run_bash(_src(f'tmux_socket_for_bot "{d}"'))
        assert rc == 0
        assert out == "com.test.eng.alpha"

    def test_socket_differs_from_session_name(self, tmp_path):
        """The socket (= BOT_SERVICE) and the tmux session (= dir slug) differ —
        the core 'three distinct axes' invariant."""
        d = _write_bot_conf(tmp_path / "alpha")
        socket, _, _ = _run_bash(_src(f'tmux_socket_for_bot "{d}"'))
        session, _, _ = _run_bash(_src(f'tmux_session_name "{d}"'))
        assert socket == "com.test.eng.alpha"
        assert session == "alpha"
        assert socket != session

    def test_empty_service_with_fleet_name_fails_fast(self, tmp_path):
        """Guard: empty BOT_SERVICE while FLEET_NAME is set must NOT silently
        fall back to a bare socket — it would collide across fleets."""
        d = _write_bot_conf(tmp_path / "alpha", bot_service="", tmux_socket=None)
        out, err, rc = _run_bash(
            _src(f'tmux_socket_for_bot "{d}"'), env={"FLEET_NAME": "prod-fleet"}
        )
        assert rc != 0
        assert out == ""
        assert "refusing" in err.lower()

    def test_empty_service_without_fleet_name_uses_test_fallback(self, tmp_path):
        """Test harness (no FLEET_NAME): a bare dir-slug socket is permitted."""
        d = _write_bot_conf(tmp_path / "alpha", bot_service="", tmux_socket=None)
        # Ensure FLEET_NAME is absent from the child env.
        out, _, rc = _run_bash(_src(f'unset FLEET_NAME; tmux_socket_for_bot "{d}"'))
        assert rc == 0
        assert out == "tmux-alpha"

    def test_reverse_lookup_resolves_sibling_socket(self, tmp_path):
        """tmux_socket_for_session derives a peer's socket from its session name
        via the sibling bot dir — the cross-socket dispatch path."""
        bots = tmp_path / "bots"
        _write_bot_conf(bots / "alpha", bot_service="com.test.eng.alpha")
        _write_bot_conf(bots / "beta", bot_service="com.test.eng.beta")
        # Caller is alpha; resolve beta's socket from its session name alone.
        out, _, rc = _run_bash(
            _src("tmux_socket_for_session beta"),
            env={"BOT_DIR": str(bots / "alpha")},
        )
        assert rc == 0
        assert out == "com.test.eng.beta"

    def test_cross_fleet_reverse_lookup_resolves_peer_in_sibling_fleet(self, tmp_path):
        """When the peer is absent from the caller's OWN fleet, resolution falls
        back to a fleet-wide search under local/*/runtime/bots — the cross-fleet
        dispatch path (a top-level manager reaching a worker in another fleet)."""
        a = tmp_path / "local" / "fleet-a" / "runtime" / "bots"
        b = tmp_path / "local" / "fleet-b" / "runtime" / "bots"
        _write_bot_conf(a / "mgr", bot_service="com.test.a.mgr")
        _write_bot_conf(b / "worker", bot_service="com.test.b.worker")
        out, _, rc = _run_bash(
            _src("tmux_socket_for_session worker"),
            env={
                "BOT_DIR": str(a / "mgr"),
                "CLAUDLOBBY_ROOT": str(tmp_path),
                "FLEET_NAME": "fleet-a",
            },
        )
        assert rc == 0
        assert out == "com.test.b.worker"

    def test_own_fleet_fast_path_wins_over_cross_fleet_namesake(self, tmp_path):
        """The own-fleet peer resolves directly even when a sibling fleet owns a
        bot of the same name — the fast path runs first, no cross-fleet scan."""
        a = tmp_path / "local" / "fleet-a" / "runtime" / "bots"
        b = tmp_path / "local" / "fleet-b" / "runtime" / "bots"
        _write_bot_conf(a / "mgr", bot_service="com.test.a.mgr")
        _write_bot_conf(a / "dup", bot_service="com.test.a.dup")
        _write_bot_conf(b / "dup", bot_service="com.test.b.dup")
        out, _, rc = _run_bash(
            _src("tmux_socket_for_session dup"),
            env={
                "BOT_DIR": str(a / "mgr"),
                "CLAUDLOBBY_ROOT": str(tmp_path),
                "FLEET_NAME": "fleet-a",
            },
        )
        assert rc == 0
        assert out == "com.test.a.dup"

    def test_cross_fleet_collision_without_live_server_is_deterministic(self, tmp_path):
        """A bot name owned by two sibling fleets, neither with a live tmux
        server, resolves to the sorted-first match and warns — stable across
        calls rather than filesystem-glob-order dependent."""
        z = tmp_path / "local" / "fleet-z" / "runtime" / "bots"
        b = tmp_path / "local" / "fleet-b" / "runtime" / "bots"
        c = tmp_path / "local" / "fleet-c" / "runtime" / "bots"
        _write_bot_conf(z / "mgr", bot_service="com.test.z.mgr")
        _write_bot_conf(b / "dup", bot_service="com.test.b.dup")
        _write_bot_conf(c / "dup", bot_service="com.test.c.dup")
        out, err, rc = _run_bash(
            _src("tmux_socket_for_session dup"),
            env={
                "BOT_DIR": str(z / "mgr"),
                "CLAUDLOBBY_ROOT": str(tmp_path),
                "FLEET_NAME": "fleet-z",
                "TMUX_TMPDIR": str(tmp_path),  # no live servers here
            },
        )
        assert rc == 0
        assert out == "com.test.b.dup"  # fleet-b sorts before fleet-c
        assert "deterministically" in err.lower()


class TestSocketWrappers:
    def test_bot_tmux_refuses_empty_socket_in_production(self, tmp_path):
        out, err, rc = _run_bash(
            _src('bot_tmux "" has-session -t whatever'),
            env={"FLEET_NAME": "prod-fleet"},
        )
        assert rc != 0
        assert "refusing" in err.lower()

    def test_bot_tmux_send_logs_send_miss_on_empty_socket(self, tmp_path):
        """A cross-socket send with no resolvable socket must emit a send_miss
        event (observable) and return non-zero — never a silent drop."""
        d = _write_bot_conf(tmp_path / "alpha")
        out, err, rc = _run_bash(
            _src('bot_tmux_send "" lead "hello there"'),
            env={"BOT_DIR": str(d), "BOT_ID": "alpha"},
        )
        assert rc != 0
        assert "dropped" in err.lower()
        # The send_miss event landed in the caller's ledger.
        events = list((d / "data" / "events").glob("fleet-*.jsonl"))
        assert events, "expected a fleet-*.jsonl event file"
        body = events[0].read_text()
        assert '"type":"send_miss"' in body
        assert '"caller":"alpha"' in body


# --- composition assertions -------------------------------------------------

_FLEET = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      system_defaults: false
      teams:
        eng:
          manager: lead
          workers: [worker-1]
      bots:
        lead:
          expertise: [eng]
        worker-1:
          expertise: [eng]
"""


def _make_paths(root):
    return Paths(root=root, fleet_dir=root)


def _conf_val(conf, key):
    """Shell-unquoted value of `[export ]KEY=...`, or None. Quote-agnostic."""
    for line in conf.splitlines():
        line = line.strip()
        for prefix in (f"export {key}=", f"{key}="):
            if line.startswith(prefix):
                rhs = line[len(prefix) :]
                # Drop a trailing inline comment (managers carry one on
                # MANAGER_TMUX); split() handles quoting.
                return " ".join(shlex.split(rhs, comments=True))
    return None


def _fleet(tmp_path):
    root = tmp_path / "f"
    root.mkdir(parents=True, exist_ok=True)
    (root / "fleet.yaml").write_text(textwrap.dedent(_FLEET))
    fleet, _md = load_fleet(root / "fleet.yaml")
    return fleet, _make_paths(root)


class TestComposerSocketFields:
    def test_emits_tmux_socket_equal_to_bot_service(self, tmp_path):
        fleet, paths = _fleet(tmp_path)
        conf = compose_bot_conf(fleet.bots["worker-1"], fleet, paths)
        assert _conf_val(conf, "BOT_SERVICE") == "com.test.worker-1"
        assert _conf_val(conf, "TMUX_SOCKET") == "com.test.worker-1"

    def test_worker_manager_socket_points_at_manager(self, tmp_path):
        fleet, paths = _fleet(tmp_path)
        conf = compose_bot_conf(fleet.bots["worker-1"], fleet, paths)
        assert _conf_val(conf, "MANAGER_TMUX") == "lead"
        assert _conf_val(conf, "MANAGER_TMUX_SOCKET") == "com.test.lead"

    def test_manager_socket_is_self(self, tmp_path):
        fleet, paths = _fleet(tmp_path)
        conf = compose_bot_conf(fleet.bots["lead"], fleet, paths)
        # Manager's MANAGER_TMUX is itself (with an inline comment); its socket
        # is its own BOT_SERVICE.
        assert _conf_val(conf, "MANAGER_TMUX") == "lead"
        assert _conf_val(conf, "MANAGER_TMUX_SOCKET") == "com.test.lead"

    def test_pins_tmux_tmpdir(self, tmp_path):
        fleet, paths = _fleet(tmp_path)
        conf = compose_bot_conf(fleet.bots["worker-1"], fleet, paths)
        assert _conf_val(conf, "TMUX_TMPDIR") == "/tmp"

    def test_units_pin_tmux_tmpdir(self, tmp_path):
        fleet, paths = _fleet(tmp_path)
        systemd = compose_systemd_unit(fleet.bots["worker-1"], fleet, paths)
        launchd = compose_launchd_plist(fleet.bots["worker-1"], fleet, paths)
        assert "Environment=TMUX_TMPDIR=/tmp" in systemd
        assert "<key>TMUX_TMPDIR</key><string>/tmp</string>" in launchd

    def test_systemd_execstop_is_socket_aware(self, tmp_path):
        """The generated ExecStop must tear down the bot's OWN tmux server with
        kill-server (-L <socket>) — deterministic teardown that doesn't rely on
        tmux's exit-empty default to reap the emptied server; a default-socket
        kill is blind."""
        fleet, paths = _fleet(tmp_path)
        systemd = compose_systemd_unit(fleet.bots["worker-1"], fleet, paths)
        assert (
            "ExecStop=/bin/sh -c 'tmux -L com.test.worker-1 kill-server "
            "2>/dev/null || true'" in systemd
        )


class TestLifecycleScriptExitGuards:
    """keepalive.sh and pre-stop-handoff.sh must fail fast (like start-bot.sh)
    when the socket can't be resolved — an empty BOT_SERVICE while FLEET_NAME is
    set — emitting a clear diagnostic instead of aborting silently on errexit."""

    def _misconfigured_bot(self, tmp_path):
        # bot.conf with BOT_NAME but no BOT_SERVICE/TMUX_SOCKET: an un-regenerated,
        # misconfigured bot. With FLEET_NAME set, tmux_socket_for_bot returns 1.
        d = tmp_path / "badbot"
        (d / "data" / "events").mkdir(parents=True, exist_ok=True)
        (d / "bot.conf").write_text(f'BOT_NAME=badbot\nBOT_DIR="{d}"\n')
        return d

    @pytest.mark.parametrize("script", ["keepalive.sh", "pre-stop-handoff.sh"])
    def test_fails_fast_on_unresolvable_socket(self, tmp_path, script):
        d = self._misconfigured_bot(tmp_path)
        _, err, rc = _run_bash(
            f'bash "{LIB_DIR}/{script}" "{d}"',
            env={"FLEET_NAME": "test-fleet"},
        )
        assert rc != 0
        assert "cannot resolve tmux socket" in err.lower()
