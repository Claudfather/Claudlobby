"""Gauntlet-round regression pins — door/shim tier.

Remaining tests pin the shim wedge marker and cooldown verdicts, plus
the tg-post and briefing-trigger armed paths on private Plane storage.

The real shim uses the private durable spool when its daemon is down.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from tests.test_plane_events_door import _serving

LIB_DIR = Path(__file__).resolve().parent.parent / "claudlobby/_runtime_scripts"
CLI = Path(sys.executable).parent / "claudlobby"

DOOR_FILES = (
    "tg-post.sh", "briefing-trigger.sh", "plane-session-start.sh",
    "lib-common.sh", "supervisor.sh", "plane-emit.sh", "plane-socket-client.py",
    "dispatch-overdue.py", "plane-readers.py", "plane-lookup.py",   # the doors' plane joins (R1: no ledger fallback)
)


def _plane_lib(tmp_path: Path, *, scratch_plane_env, initialize=False) -> tuple[Path, dict]:
    libdir = tmp_path / "lib"
    libdir.mkdir()
    for name in DOOR_FILES:
        (libdir / name).symlink_to(LIB_DIR / name)
    stub = libdir / "dispatch.sh"
    stub.write_text("#!/bin/bash\nexit 0\n")
    stub.chmod(0o755)
    tmux = tmp_path / "tmux"
    tmux.write_text("#!/bin/bash\nexit 0\n")
    tmux.chmod(0o755)
    env = {
        **scratch_plane_env(tmp_path, initialize=initialize),
        "TMUX_BIN": str(tmux),
        "OBSERVABILITY_DISPATCH_DEADLINE": "600",
        "BOT_ID": "lead",
        "BOT_NAME": "lead",
        "FLEET_NAME": "e2e-fleet",
        "HOME": str(tmp_path),
        "PLANE_EMIT_ENABLED": "1",


        "PATH": "/usr/bin:/bin",
    }
    return libdir, env


def _bash(cmd: str, env: dict, cwd=None, stdin: str | None = None):
    return subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True,
        env=env, cwd=cwd, timeout=120, input=stdin,
    )


def _bash_committed(root: Path, scratch_plane_env, cmd: str, env: dict):
    with _serving(root, scratch_plane_env) as socket:
        return _bash(cmd, {**env, "PLANE_SOCKET": str(socket)})


def _rows(tmp_path: Path, sql: str, params: tuple = ()):
    conn = connect(db_path(tmp_path))
    out = conn.execute(sql, params).fetchall()
    conn.close()
    return out


@pytest.fixture()
def armed(tmp_path: Path, *, scratch_plane_env):
    return _plane_lib(tmp_path, scratch_plane_env=scratch_plane_env, initialize=True)


VALID_BATCH = json.dumps({"events": [{
    "event_type": "system", "emitter": "gauntlet-test",
    "payload": {"event": "daemon_started"},
}]})


# ---------------------------------------------------------------------------
# Shim: wedge marker + cooldown rc (adversarial Major #2, consensus C1)
# ---------------------------------------------------------------------------
class TestWedgeMarker:
    def _marker(self, tmp_path: Path) -> Path:
        d = tmp_path / "state" / "plane"
        d.mkdir(parents=True, exist_ok=True)
        return d / ".socket-wedged"

    def test_future_marker_is_expired_not_pinning(self, tmp_path, armed):
        """A marker stamped AHEAD of the clock (the RTC-less-Pi boot class)
        must read expired-and-deleted, not pin the socket rung off for the
        whole skew. Probed pre-fix: marker at now+3600 skipped the socket on
        every emission."""
        libdir, env = armed
        mark = self._marker(tmp_path)
        mark.write_text(str(int(time.time()) + 3600))
        r = _bash(f'"{libdir}/plane-emit.sh"', env, stdin=VALID_BATCH)
        assert r.returncode == 6, r.stderr
        # Socket rung was ATTEMPTED (no daemon -> disclosed fallback), never
        # the cooldown skip. The future stamp is GONE — the marker present
        # afterwards is the fresh, legitimately-clocked one this run's own
        # failed socket attempt wrote (rc 5 -> new cooldown), which is the
        # correct self-healing behavior.
        assert "wedge cooldown" not in r.stderr
        # #1657/#1690: this is a genuine transport attempt (no daemon, no
        # cooldown skip), so the shim's contract now names it "transport
        # failed" -- "daemon unavailable" is the old, pre-split wording and
        # must not reappear here.
        assert "transport failed" in r.stderr
        assert "daemon unavailable" not in r.stderr
        assert int(mark.read_text()) <= int(time.time())

    def test_cooldown_passes_client_verdicts_through(self, tmp_path, armed):
        """5-reviewer consensus: the cooldown branch hardcoded rc=5, so
        malformed stdin DURING cooldown exited 3 (total failure) with a false
        "daemon unavailable" — a contract violation wearing a transport
        failure, exactly during incident windows. The finalize-only client
        already returns 2 for bad stdin; rc must pass through."""
        libdir, env = armed
        self._marker(tmp_path).write_text(str(int(time.time())))
        bad = _bash(f'"{libdir}/plane-emit.sh"', env, stdin="not json")
        assert bad.returncode == 2, (bad.returncode, bad.stderr)
        assert "total failure" not in bad.stderr
        # A valid cooldown batch is durable pending, never falsely committed.
        ok = _bash(f'"{libdir}/plane-emit.sh"', env, stdin=VALID_BATCH)
        assert ok.returncode == 6, ok.stderr
        assert "wedge cooldown" in ok.stderr
        assert len(list((tmp_path / "state" / "plane" / "staged").glob("*.batch"))) == 1


# ---------------------------------------------------------------------------
# report-back: grammar gates + progress wrap (C5, C10)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# T5 plan obligation: the crash window is VISIBLE (spec-lens Major)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# .plane-session cross-pin: the hook WRITES what report-back READS (S16)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Join parity: casefold + newest-wins matches dispatch-overdue semantics (C7)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# tg-post armed path (previously zero plane assertions — general-lens #2)
# ---------------------------------------------------------------------------
class TestTgPostArmed:
    def _tg_env(self, tmp_path: Path, env: dict, resp: str) -> dict:
        fakebin = tmp_path / "fakebin"
        fakebin.mkdir(exist_ok=True)
        curl = fakebin / "curl"
        curl.write_text(f"#!/bin/bash\nprintf '%s' '{resp}'\n")
        curl.chmod(0o755)
        jq = shutil.which("jq")
        assert jq, "jq required for tg-post tests"
        path = f"{fakebin}:{Path(jq).parent}:/usr/bin:/bin"
        return dict(
            env, PATH=path,
            TELEGRAM_GROUP_CHAT_ID="-100123", TELEGRAM_BOT_TOKEN="tok",
        )

    def test_accepted_post_lands_comm_and_carrier_ref(self, tmp_path, armed, scratch_plane_env):
        libdir, env = armed
        # Full capture so the body CONTENT is assertable (default metadata
        # mode drops it at the door, correctly).
        cfg = tmp_path / "state" / "plane"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "capture.json").write_text('{"*": "full"}')
        tge = self._tg_env(
            tmp_path, env, '{"ok":true,"result":{"message_id":42}}'
        )
        # Tab in the body — the F14 class the per-string escaper was added
        # for; the single jq -nc assembly must encode it correctly.
        r = _bash_committed(tmp_path, scratch_plane_env,
                            f'"{libdir}/tg-post.sh" "line1\ttabbed"', tge)
        assert r.returncode == 0, r.stderr
        comm = _rows(
            tmp_path,
            "SELECT body, message_class, recipient_raw FROM communications",
        )
        assert comm and comm[0]["body"] == "line1\ttabbed"
        assert comm[0]["message_class"] == "notice"
        tx = _rows(
            tmp_path,
            "SELECT event, carrier, carrier_ref FROM events"
            " WHERE kind='transmission'",
        )
        assert tx and tx[0]["event"] == "carrier_accepted"
        assert tx[0]["carrier"] == "telegram-tgpost"
        assert tx[0]["carrier_ref"] == "tg:42"

    def test_nonnumeric_message_id_never_reaches_carrier_ref(self, tmp_path, armed, scratch_plane_env):
        libdir, env = armed
        tge = self._tg_env(
            tmp_path, env, '{"ok":true,"result":{"message_id":"weird"}}'
        )
        r = _bash_committed(tmp_path, scratch_plane_env, f'"{libdir}/tg-post.sh" "hello"', tge)
        assert r.returncode == 0, r.stderr
        tx = _rows(
            tmp_path,
            "SELECT carrier_ref FROM events WHERE kind='transmission'",
        )
        assert tx and tx[0]["carrier_ref"] is None

    def test_rejected_post_lands_failed_transmission(self, tmp_path, armed, scratch_plane_env):
        libdir, env = armed
        tge = self._tg_env(
            tmp_path, env, '{"ok":false,"description":"chat not found"}'
        )
        r = _bash_committed(tmp_path, scratch_plane_env, f'"{libdir}/tg-post.sh" "hello"', tge)
        assert r.returncode == 3
        tx = _rows(
            tmp_path,
            "SELECT event FROM events WHERE kind='transmission'",
        )
        assert tx and tx[0]["event"] == "failed"


# ---------------------------------------------------------------------------
# briefing-trigger armed path (previously zero plane assertions)
# ---------------------------------------------------------------------------
def test_briefing_trigger_armed_lands_briefing_comm(tmp_path, armed, scratch_plane_env):
    libdir, env = armed
    cfg = tmp_path / "state" / "plane"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "capture.json").write_text('{"*": "full"}')
    botdir = tmp_path / "local" / "brf-fleet" / "runtime" / "bots" / "w1"
    (botdir / "logs").mkdir(parents=True)
    (botdir / "data").mkdir(parents=True)
    (botdir / ".claude" / "skills" / "briefing").mkdir(parents=True)  # composed skill
    (botdir / "bot.conf").write_text('export FLEET_NAME="brf-fleet"\n')
    r = _bash_committed(tmp_path, scratch_plane_env,
                        f'"{libdir}/briefing-trigger.sh" brf-fleet w1 morning', env)
    assert r.returncode == 0, r.stderr
    comm = _rows(
        tmp_path,
        "SELECT sender_alias, message_class, body FROM communications",
    )
    assert comm and comm[0]["message_class"] == "briefing"
    assert comm[0]["sender_alias"] == "system:briefing-trigger"
    assert comm[0]["body"] == "/briefing morning"
    tx = _rows(
        tmp_path,
        "SELECT event FROM events WHERE kind='transmission'",
    )
    assert tx and tx[0]["event"] == "pane_submitted"


# ---------------------------------------------------------------------------
# workstream prune: ONE batch, emitted once (C6)
# ---------------------------------------------------------------------------
class TestPlaneHelpers:
    def _run(self, snippet: str, env: dict) -> subprocess.CompletedProcess:
        return _bash(f'source "{LIB_DIR}/lib-common.sh"; set +e; {snippet}', env)

    BASE = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}

    def test_plane_armed_matrix(self):
        """The always-on contract (F18 closure R1): armed with NO flag;
        PLANE_EMIT_ENABLED is not read (0 changes nothing); the identity
        preconditions are disclosed skips worded without the old flag;
        PLANE_EMIT_DISABLED=1 is the one thing that disarms, and it wins."""
        e = dict(self.BASE)
        r = self._run("plane_armed d; echo rc=$?", e)
        assert "rc=0" in r.stdout  # no flag at all: armed
        r = self._run("plane_armed d; echo rc=$?", {**e, "PLANE_EMIT_ENABLED": "0"})
        assert "rc=0" in r.stdout  # the old flag has no meaning
        r = self._run("plane_armed d --require-fleet; echo rc=$?", e)
        assert "rc=1" in r.stdout and "FLEET_NAME is empty" in r.stderr
        assert "PLANE_EMIT_ENABLED" not in r.stderr
        r = self._run("plane_armed d --require-bot; echo rc=$?", e)
        assert "rc=1" in r.stdout and "BOT_NAME is empty" in r.stderr
        e["FLEET_NAME"] = "f"
        e["BOT_NAME"] = "b"
        r = self._run("plane_armed d --require-fleet --require-bot; echo rc=$?", e)
        assert "rc=0" in r.stdout
        e["PLANE_EMIT_DISABLED"] = "1"
        r = self._run("plane_armed d; echo rc=$?", {**e, "PLANE_EMIT_ENABLED": "1"})
        assert "rc=1" in r.stdout  # DISABLED wins, even beside the old flag

    def test_plane_mint_id_grammar(self):
        import re
        r = self._run("plane_mint_id msg; echo; plane_mint_id wi", self.BASE)
        minted = r.stdout.strip().splitlines()
        assert re.fullmatch(r"msg_[0-9a-f]{32}", minted[0])
        assert re.fullmatch(r"wi_[0-9a-f]{32}", minted[1])

    def test_plane_tx_event_is_valid_json(self):
        r = self._run(
            'plane_tx_event my-door "fl\\"eet" tmux msg_ab dest-1 pane_submitted',
            self.BASE,
        )
        obj = json.loads(r.stdout)
        assert obj["event_type"] == "transmission"
        assert obj["fleet"] == 'fl"eet'
        assert obj["payload"]["state"] == "pane_submitted"
        assert obj["payload"]["attempt_no"] == 1

    def test_epoch_to_iso_utc(self):
        r = self._run("epoch_to_iso_utc 1756300000", self.BASE)
        assert r.stdout.strip() == "2025-08-27T13:06:40Z"
