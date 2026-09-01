"""Gauntlet-round regression pins — door/shim tier.

Every test here pins a defect found by the post-merge review gauntlet on
PR #1372 (8 reviewers, consensus-ranked): the wedge marker clock-skew pin,
the cooldown rc laundering, the report-back grammar-gate newline bypass,
the --progress 64-bit wrap, the T5 crash-window plan obligation, the
.plane-session writer/reader cross-pin, the prune single-batch emission,
and the previously assertion-free tg-post / briefing-trigger armed paths.

Harness = test_plane_door_e2e's _plane_lib pattern (real doors, real shim,
real cold-CLI ingest into a scratch plane db; transport stubbed).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
CLI = Path(sys.executable).parent / "claudlobby"

DOOR_FILES = (
    "dispatch-task.sh", "report-back.sh", "workstream-update.sh",
    "tg-post.sh", "briefing-trigger.sh", "plane-session-start.sh",
    "lib-common.sh", "plane-emit.sh", "plane-socket-client.py",
    "dispatch-overdue.py",
)


def _plane_lib(tmp_path: Path) -> tuple[Path, dict]:
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
        "CLAUDLOBBY_ROOT": str(tmp_path),
        "TMUX_BIN": str(tmux),
        "OBSERVABILITY_DISPATCH_DEADLINE": "600",
        "BOT_ID": "lead",
        "BOT_NAME": "lead",
        "FLEET_NAME": "e2e-fleet",
        "HOME": str(tmp_path),
        "PLANE_EMIT_ENABLED": "1",
        "PLANE_EMIT_CLI": str(CLI),
        "PLANE_SOCKET": str(tmp_path / "no-daemon.sock"),
        "PATH": "/usr/bin:/bin",
    }
    return libdir, env


def _bash(cmd: str, env: dict, cwd=None, stdin: str | None = None):
    return subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True,
        env=env, cwd=cwd, timeout=120, input=stdin,
    )


def _rows(tmp_path: Path, sql: str, params: tuple = ()):
    conn = connect(db_path(tmp_path))
    out = conn.execute(sql, params).fetchall()
    conn.close()
    return out


@pytest.fixture()
def armed(tmp_path: Path):
    return _plane_lib(tmp_path)


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
        every emission and survived successful CLI emits."""
        libdir, env = armed
        mark = self._marker(tmp_path)
        mark.write_text(str(int(time.time()) + 3600))
        r = _bash(f'"{libdir}/plane-emit.sh"', env, stdin=VALID_BATCH)
        assert r.returncode == 0, r.stderr
        # Socket rung was ATTEMPTED (no daemon -> disclosed fallback), never
        # the cooldown skip. The future stamp is GONE — the marker present
        # afterwards is the fresh, legitimately-clocked one this run's own
        # failed socket attempt wrote (rc 5 -> new cooldown), which is the
        # correct self-healing behavior.
        assert "wedge cooldown" not in r.stderr
        assert "daemon unavailable" in r.stderr
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
        # And the happy path through cooldown still lands via the CLI rung.
        ok = _bash(f'"{libdir}/plane-emit.sh"', env, stdin=VALID_BATCH)
        assert ok.returncode == 0, ok.stderr
        assert "wedge cooldown" in ok.stderr


# ---------------------------------------------------------------------------
# report-back: grammar gates + progress wrap (C5, C10)
# ---------------------------------------------------------------------------
class TestReportBackGates:
    def test_newline_task_id_never_reaches_the_join(self, tmp_path, armed):
        """Measured pre-fix on bash 3.2: a task id carrying an embedded
        newline passed BOTH gates (case-glob * spans newlines; grep-on-stdin
        anchors per line) and reached grep -F as pattern-OR, linking an
        unrelated dispatch row. The whole-string [[ =~ ]] gates refuse it:
        the report lands UNLINKED, rc 0 (fail-open)."""
        libdir, env = armed
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        # A row the OLD pattern-OR would have matched via its second line.
        (state / "dispatch-log.jsonl").write_text(
            '{"ts":"2026-08-27T00:00:00Z","bot":"w1","task_id":"t-9-ffff",'
            '"plane_msg_id":"msg_' + "a" * 32 + '",'
            '"plane_work_item_id":"wi_' + "a" * 32 + '",'
            '"plane_assignment_id":"asg_' + "a" * 32 + '"}\n'
            'junk\n'
        )
        rbenv = dict(env, BOT_DIR=str(tmp_path / "botdir"))
        r = _bash(
            f'"{libdir}/report-back.sh" w1 completed "done" '
            "--task $'t-9-ffff\\njunk'",
            rbenv,
        )
        assert r.returncode == 0, r.stderr
        comm = _rows(tmp_path, "SELECT work_item_id FROM communications")
        assert len(comm) == 1
        assert comm[0]["work_item_id"] is None  # unlinked, never pattern-OR

    def test_progress_wraparound_refused(self, tmp_path, armed):
        """Probed pre-fix: a 20-digit --progress passed the digit gate,
        wrapped $((10#...)) negative, passed -le 100, and the plane refused
        the batch at pydantic ge=0 — dropping the report's communication AND
        task fact. The length cap refuses it at the door, exit 2."""
        libdir, env = armed
        r = _bash(
            f'"{libdir}/report-back.sh" w1 progress "x" '
            "--progress 9223372036854775808",
            env,
        )
        assert r.returncode == 2, (r.returncode, r.stderr)
        assert "0-100" in r.stderr

    def test_valid_progress_still_lands(self, tmp_path, armed):
        libdir, env = armed
        r = _bash(f'"{libdir}/report-back.sh" w1 progress "x" --progress 40', env)
        assert r.returncode == 0, r.stderr
        assert _rows(tmp_path, "SELECT msg_id FROM communications")


# ---------------------------------------------------------------------------
# T5 plan obligation: the crash window is VISIBLE (spec-lens Major)
# ---------------------------------------------------------------------------
def test_crash_between_intent_and_send_leaves_visible_intent(tmp_path, armed):
    """The phase-2 plan names this the canary sharp edge: intent-FIRST means
    a crash between the plane record and the tmux send must leave a VISIBLE
    intent-without-transmission (the deliberate flip from the legacy shape,
    a sent-report-without-ledger-row). Mechanism: the tmux stub BLOCKS on
    send-keys; once the intent row is visible in the db, SIGKILL the door
    process group — a real kill inside the window — then assert the shape."""
    import os
    import signal

    libdir, env = armed
    blocker = tmp_path / "tmux-blocker"
    blocker.write_text(
        '#!/bin/bash\ncase "$*" in *send-keys*) sleep 30 ;; esac\nexit 0\n'
    )
    blocker.chmod(0o755)
    env = dict(env, TMUX_BIN=str(blocker))
    proc = subprocess.Popen(
        ["bash", "-c", f'"{libdir}/report-back.sh" w1 completed "done"'],
        env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        comms: list = []
        while time.monotonic() < deadline:
            if db_path(tmp_path).exists():
                # the db FILE lands before migrations create the tables —
                # on a loaded CI runner this poll raced into that window
                # ("no such table: communications", twice on #1421's CI,
                # never locally). Mid-creation is simply not-ready-yet.
                try:
                    comms = _rows(tmp_path,
                                  "SELECT msg_id FROM communications")
                except sqlite3.OperationalError:
                    comms = []
                if comms:
                    break
            time.sleep(0.1)
        assert comms, "intent row never appeared — cannot exercise the window"
        os.killpg(proc.pid, signal.SIGKILL)  # the crash, inside the window
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)
    tx = _rows(
        tmp_path,
        "SELECT 1 FROM events WHERE kind='transmission' AND msg_id = ?",
        (comms[0]["msg_id"],),
    )
    assert tx == [], "no transmission may exist — that IS the visible window"


# ---------------------------------------------------------------------------
# .plane-session cross-pin: the hook WRITES what report-back READS (S16)
# ---------------------------------------------------------------------------
def test_session_hook_writer_and_report_back_reader_agree(tmp_path, armed):
    libdir, env = armed
    botdir = tmp_path / "botdir"
    (botdir / "data").mkdir(parents=True)
    hook = _bash(
        f'"{libdir}/plane-session-start.sh"',
        dict(env, BOT_DIR=str(botdir)),
        stdin='{"session_id": "abc-123"}',
    )
    assert hook.returncode == 0, hook.stderr
    expected = "sess_" + hashlib.sha256(b"abc-123").hexdigest()[:32]
    assert expected in (botdir / "data" / ".plane-session").read_text()

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "dispatch-log.jsonl").write_text(
        '{"ts":"2026-08-27T00:00:00Z","bot":"w1","task_id":"t-9-ffff",'
        '"plane_msg_id":"msg_' + "b" * 32 + '",'
        '"plane_work_item_id":"wi_' + "b" * 32 + '",'
        '"plane_assignment_id":"asg_' + "b" * 32 + '"}\n'
    )
    r = _bash(
        f'"{libdir}/report-back.sh" w1 completed "done" --task t-9-ffff',
        dict(env, BOT_DIR=str(botdir)),
    )
    assert r.returncode == 0, r.stderr
    ev = _rows(
        tmp_path,
        "SELECT session_uid FROM events WHERE kind='task' AND event='completed'",
    )
    assert len(ev) == 1
    assert ev[0]["session_uid"] == expected


# ---------------------------------------------------------------------------
# Join parity: casefold + newest-wins matches dispatch-overdue semantics (C7)
# ---------------------------------------------------------------------------
def test_link_join_is_casefold_newest_wins(tmp_path, armed):
    libdir, env = armed
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    old_ids = ("msg_" + "c" * 32, "wi_" + "c" * 32, "asg_" + "c" * 32)
    new_ids = ("msg_" + "d" * 32, "wi_" + "d" * 32, "asg_" + "d" * 32)
    rows = []
    for bot, ids in (("W1", old_ids), ("w1", new_ids)):
        # Compact separators — the real ledger writer printfs compact JSON,
        # and the join greps the compact form.
        rows.append(json.dumps({
            "ts": "2026-08-27T00:00:00Z", "bot": bot, "task_id": "t-9-ffff",
            "plane_msg_id": ids[0], "plane_work_item_id": ids[1],
            "plane_assignment_id": ids[2],
        }, separators=(",", ":")))
    (state / "dispatch-log.jsonl").write_text("\n".join(rows) + "\n")
    r = _bash(
        f'"{libdir}/report-back.sh" W1 completed "done" --task t-9-ffff', env
    )
    assert r.returncode == 0, r.stderr
    ev = _rows(
        tmp_path,
        "SELECT assignment_id FROM events WHERE kind='task' AND event='completed'",
    )
    # dispatch-overdue.py's join is case-insensitive and newest-row-wins;
    # the bash mirror must agree: the LAST matching row's ids link.
    assert ev and ev[0]["assignment_id"] == new_ids[2]


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

    def test_accepted_post_lands_comm_and_carrier_ref(self, tmp_path, armed):
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
        r = _bash(f'"{libdir}/tg-post.sh" "line1\ttabbed"', tge)
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

    def test_nonnumeric_message_id_never_reaches_carrier_ref(self, tmp_path, armed):
        libdir, env = armed
        tge = self._tg_env(
            tmp_path, env, '{"ok":true,"result":{"message_id":"weird"}}'
        )
        r = _bash(f'"{libdir}/tg-post.sh" "hello"', tge)
        assert r.returncode == 0, r.stderr
        tx = _rows(
            tmp_path,
            "SELECT carrier_ref FROM events WHERE kind='transmission'",
        )
        assert tx and tx[0]["carrier_ref"] is None

    def test_rejected_post_lands_failed_transmission(self, tmp_path, armed):
        libdir, env = armed
        tge = self._tg_env(
            tmp_path, env, '{"ok":false,"description":"chat not found"}'
        )
        r = _bash(f'"{libdir}/tg-post.sh" "hello"', tge)
        assert r.returncode == 3
        tx = _rows(
            tmp_path,
            "SELECT event FROM events WHERE kind='transmission'",
        )
        assert tx and tx[0]["event"] == "failed"


# ---------------------------------------------------------------------------
# briefing-trigger armed path (previously zero plane assertions)
# ---------------------------------------------------------------------------
def test_briefing_trigger_armed_lands_briefing_comm(tmp_path, armed):
    libdir, env = armed
    cfg = tmp_path / "state" / "plane"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "capture.json").write_text('{"*": "full"}')
    botdir = tmp_path / "local" / "brf-fleet" / "runtime" / "bots" / "w1"
    (botdir / "logs").mkdir(parents=True)
    (botdir / "data").mkdir(parents=True)
    (botdir / "bot.conf").write_text('export FLEET_NAME="brf-fleet"\n')
    r = _bash(f'"{libdir}/briefing-trigger.sh" brf-fleet w1 morning', env)
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
def test_prune_emits_one_batch_for_all_archived(tmp_path, armed):
    libdir, env = armed
    counter = tmp_path / "emit-count"
    wrapper = tmp_path / "counting-cli"
    wrapper.write_text(
        "#!/bin/bash\n"
        f"echo x >> {counter}\n"
        f'exec "{CLI}" "$@"\n'
    )
    wrapper.chmod(0o755)
    wenv = dict(env, PLANE_EMIT_CLI=str(wrapper))
    ws = f'"{libdir}/workstream-update.sh"'
    for i in (1, 2):
        r = _bash(f'{ws} open "ws {i}" --project alpha', wenv)
        assert r.returncode == 0, r.stderr
    ids = [
        row["workstream_id"]
        for row in _rows(tmp_path, "SELECT workstream_id FROM workstreams")
    ]
    assert len(ids) == 2
    for wid in ids:
        r = _bash(f'{ws} close {wid} --status done', wenv)
        assert r.returncode == 0, r.stderr
    counter.write_text("")  # count only the prune
    r = _bash(f'{ws} prune', wenv)
    assert r.returncode == 0, r.stderr
    archived = _rows(
        tmp_path,
        "SELECT workstream_id FROM events WHERE kind='workstream'"
        " AND event='archived'",
    )
    assert {row["workstream_id"] for row in archived} == set(ids)
    # ONE shim invocation for the whole prune — not one per pruned id.
    assert counter.read_text().count("x") == 1


# ---------------------------------------------------------------------------
# lib-common plane helpers (C3 consolidation)
# ---------------------------------------------------------------------------
class TestPlaneHelpers:
    def _run(self, snippet: str, env: dict) -> subprocess.CompletedProcess:
        return _bash(f'source "{LIB_DIR}/lib-common.sh"; set +e; {snippet}', env)

    BASE = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}

    def test_plane_armed_matrix(self):
        e = dict(self.BASE)
        r = self._run("plane_armed d; echo rc=$?", e)
        assert "rc=1" in r.stdout  # not enabled
        e["PLANE_EMIT_ENABLED"] = "1"
        r = self._run("plane_armed d; echo rc=$?", e)
        assert "rc=0" in r.stdout
        r = self._run("plane_armed d --require-fleet; echo rc=$?", e)
        assert "rc=1" in r.stdout and "FLEET_NAME is empty" in r.stderr
        e["FLEET_NAME"] = "f"
        r = self._run("plane_armed d --require-fleet; echo rc=$?", e)
        assert "rc=0" in r.stdout
        e["PLANE_EMIT_DISABLED"] = "1"
        r = self._run("plane_armed d; echo rc=$?", e)
        assert "rc=1" in r.stdout  # DISABLED wins

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
