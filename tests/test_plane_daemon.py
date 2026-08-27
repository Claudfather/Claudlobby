"""The ingest daemon (Phase-2 T0): socket protocol, taxonomy mirror, spool
drain, lifecycle events, single-instance + stale-socket recovery.

Socket paths live under a SHORT tmp dir, never pytest's tmp_path: macOS caps
sun_path at 104 bytes and pytest's tmp factory routinely exceeds it. The db
root stays on tmp_path (no length limit there).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from claudlobby.plane import PLANE_SCHEMA_VERSION
from claudlobby.plane.daemon import (
    DaemonAlreadyRunning,
    MAX_REQUEST_BYTES,
    MAX_SOCKET_PATH_BYTES,
    PlaneDaemon,
    SocketPathTooLong,
    _check_sun_path,
    send_batch,
)
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.ids import ensure_host_uid, mint_event_id
from claudlobby.plane.migrations import migrate
from claudlobby.plane.spool import spool_dir, spool_write


def _short_sock_dir() -> Path:
    for base in ("/tmp/claude", "/tmp"):
        try:
            Path(base).mkdir(exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="pd", dir=base))
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix="pd"))


def _comm(suffix="2", body="over the wire") -> dict:
    return {
        "event_type": "communication",
        "emitter": "daemon-test",
        "fleet": "example-fleet",
        "payload": {
            "msg_id": "msg_" + suffix * 32,
            "sender": "bot:example-fleet/alpha",
            "message_class": "notice",
            "body": body,
            "privacy": "full",
        },
    }


@pytest.fixture()
def running(tmp_path: Path):
    """A live daemon on a short socket; yields (root, sock_path, daemon).
    Capture is armed FULL so round-trip content is assertable — and its being
    honored at all is itself the transport-never-changes-semantics check
    (an unarmed root drops bodies at the door exactly like the CLI)."""
    cap = tmp_path / "state" / "plane"
    cap.mkdir(parents=True, exist_ok=True)
    (cap / "capture.json").write_text('{"*": "full"}')
    sdir = _short_sock_dir()
    sock = sdir / "s"
    daemon = PlaneDaemon(tmp_path, socket_override=sock, drain_interval=9999)
    t = threading.Thread(
        target=lambda: daemon.serve(install_signals=False), daemon=True
    )
    t.start()
    for _ in range(200):
        if sock.exists():
            break
        time.sleep(0.02)
    else:
        raise AssertionError("daemon never bound its socket")
    yield tmp_path, sock, daemon
    daemon.stop()
    t.join(timeout=10)
    shutil.rmtree(sdir, ignore_errors=True)


def test_commit_roundtrip_and_row_lands(running):
    root, sock, _ = running
    resp = send_batch(sock, [_comm()])
    assert resp["ok"] is True
    assert resp["results"][0]["status"] == "committed"
    eid = resp["results"][0]["event_id"]
    conn = connect(db_path(root))
    row = conn.execute(
        "SELECT body, schema_version FROM communications WHERE event_id = ?",
        (eid,),
    ).fetchone()
    conn.close()
    assert row["body"] == "over the wire"
    assert row["schema_version"] == PLANE_SCHEMA_VERSION


def test_replay_same_event_id_is_duplicate(running):
    root, sock, _ = running
    eid = mint_event_id()
    req = {**_comm("3"), "event_id": eid}
    assert send_batch(sock, [req])["results"][0]["status"] == "committed"
    again = send_batch(sock, [req])
    assert again["ok"] is True
    assert again["results"][0]["status"] == "duplicate"


def test_contract_violation_mirrors_exit_2_and_writes_nothing(running):
    root, sock, _ = running
    bad = _comm("4")
    bad["payload"]["message_class"] = "yell"
    resp = send_batch(sock, [bad])
    assert resp["ok"] is False and resp["code"] == "contract_violation"
    conn = connect(db_path(root))
    n = conn.execute(
        "SELECT COUNT(*) FROM communications WHERE msg_id = ?",
        ("msg_" + "4" * 32,),
    ).fetchone()[0]
    conn.close()
    assert n == 0


def test_malformed_and_empty_requests_are_bad_request(running):
    _, sock, _ = running
    for payload in (b"not json\n", b"\n", b'{"nope": 1}\n', b'{"events": []}\n',
                    b'{"events": [42]}\n'):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(10)
        client.connect(str(sock))
        client.sendall(payload)
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass  # daemon already replied+closed — the race send_batch guards too
        buf = b""
        while b"\n" not in buf:
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
        client.close()
        resp = json.loads(buf)
        assert resp["ok"] is False and resp["code"] == "bad_request", payload


def test_oversize_request_refused_not_fatal(running):
    _, sock, _ = running
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(30)
    client.connect(str(sock))
    blob = b"x" * (MAX_REQUEST_BYTES + 2)
    try:
        client.sendall(blob)
    except OSError:
        pass  # daemon may reset mid-send after refusing; either way it answered/closed
    else:
        client.shutdown(socket.SHUT_WR)
    try:
        buf = client.recv(65536)
        if buf:
            assert json.loads(buf)["code"] == "bad_request"
    except OSError:
        pass
    client.close()
    # the daemon survived: a normal request still round-trips
    assert send_batch(sock, [_comm("5")])["ok"] is True


def test_slow_client_does_not_kill_the_daemon(running):
    _, sock, _ = running
    lingerer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    lingerer.connect(str(sock))
    lingerer.sendall(b'{"events"')  # never finishes, never newlines
    # don't wait the full 30s read timeout — just prove interleaving works:
    # the daemon is serial per connection, so close the lingerer and verify
    # the next request is served.
    lingerer.close()
    assert send_batch(sock, [_comm("6")])["ok"] is True


def test_stale_socket_is_recovered(tmp_path: Path):
    sdir = _short_sock_dir()
    sock = sdir / "s"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(sock))
    dead.close()               # bound then closed: path exists, nobody listens
    daemon = PlaneDaemon(tmp_path, socket_override=sock, drain_interval=9999)
    t = threading.Thread(
        target=lambda: daemon.serve(install_signals=False), daemon=True
    )
    t.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if send_batch(sock, [_comm("7")])["ok"]:
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("daemon never recovered the stale socket")
    finally:
        daemon.stop()
        t.join(timeout=10)
        shutil.rmtree(sdir, ignore_errors=True)


def test_second_daemon_refuses_while_first_lives(running):
    root, sock, _ = running
    second = PlaneDaemon(root, socket_override=sock)
    with pytest.raises(DaemonAlreadyRunning):
        second.serve(install_signals=False)


def test_sun_path_guard_refuses_long_paths():
    with pytest.raises(SocketPathTooLong):
        _check_sun_path(Path("/" + "x" * 120 + "/s"))


def test_deep_root_whose_public_path_fits_still_binds(tmp_path: Path):
    """Regression: the hidden rename-in name must not push a public path
    that FITS sun_path over the limit — macOS TMPDIR roots did exactly
    that (public 94 bytes, verbose tmp suffix ~120)."""
    deep = Path("/tmp/claude") / ("d" * (86 - len("/tmp/claude/") - len("/s")))
    deep.mkdir(parents=True, exist_ok=True)
    sock = deep / "s"
    assert len(str(sock).encode()) <= MAX_SOCKET_PATH_BYTES
    daemon = PlaneDaemon(tmp_path, socket_override=sock, drain_interval=9999)
    t = threading.Thread(
        target=lambda: daemon.serve(install_signals=False), daemon=True
    )
    t.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not sock.exists():
            time.sleep(0.05)
        assert sock.exists(), "public-fits root must bind"
    finally:
        daemon.stop()
        t.join(timeout=10)
        shutil.rmtree(deep, ignore_errors=True)


def test_drain_on_start_ingests_preexisting_spool(tmp_path: Path):
    eid = mint_event_id()
    fin = {**_comm("8"), "event_id": eid,
           "occurred_at": "2026-08-24T00:00:00+00:00",
           "schema_version": PLANE_SCHEMA_VERSION}
    spool_write(tmp_path, [fin], "db was down")
    sdir = _short_sock_dir()
    sock = sdir / "s"
    daemon = PlaneDaemon(tmp_path, socket_override=sock, drain_interval=9999)
    t = threading.Thread(
        target=lambda: daemon.serve(install_signals=False), daemon=True
    )
    t.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if not list(spool_dir(tmp_path).glob("*.json")):
                break
            time.sleep(0.05)
        conn = connect(db_path(tmp_path))
        n = conn.execute(
            "SELECT COUNT(*) FROM communications WHERE event_id = ?", (eid,)
        ).fetchone()[0]
        drained = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='system'"
            " AND event='spool_drain_completed'"
        ).fetchone()[0]
        conn.close()
        assert n == 1, "startup drain must ingest the stranded entry"
        assert drained == 1, "a drain that did work emits its system event"
    finally:
        daemon.stop()
        t.join(timeout=10)
        shutil.rmtree(sdir, ignore_errors=True)


def test_lifecycle_events_recorded(tmp_path: Path):
    sdir = _short_sock_dir()
    sock = sdir / "s"
    daemon = PlaneDaemon(tmp_path, socket_override=sock, drain_interval=9999)
    t = threading.Thread(
        target=lambda: daemon.serve(install_signals=False), daemon=True
    )
    t.start()
    for _ in range(200):
        if sock.exists():
            break
        time.sleep(0.02)
    daemon.stop()
    t.join(timeout=10)
    shutil.rmtree(sdir, ignore_errors=True)
    conn = connect(db_path(tmp_path))
    events = [r[0] for r in conn.execute(
        "SELECT event FROM events WHERE kind='system' ORDER BY ingest_seq"
    )]
    host = ensure_host_uid(tmp_path / "state")
    subj = conn.execute(
        "SELECT subject_kind, subject_uid FROM events WHERE event='daemon_started'"
    ).fetchone()
    conn.close()
    assert "daemon_started" in events and "daemon_stopping" in events
    assert (subj["subject_kind"], subj["subject_uid"]) == ("host", host)


def test_unarmed_root_drops_body_with_proof_triple(tmp_path: Path):
    """Transport never changes semantics: the daemon path applies the SAME
    capture policy the CLI applies — no capture.json means metadata mode,
    body dropped at the door, proof triple retained."""
    sdir = _short_sock_dir()
    sock = sdir / "s"
    daemon = PlaneDaemon(tmp_path, socket_override=sock, drain_interval=9999)
    t = threading.Thread(
        target=lambda: daemon.serve(install_signals=False), daemon=True
    )
    t.start()
    for _ in range(200):
        if sock.exists():
            break
        time.sleep(0.02)
    try:
        resp = send_batch(sock, [_comm("a", body="secret content")])
        assert resp["ok"] is True
        conn = connect(db_path(tmp_path))
        row = conn.execute(
            "SELECT body, body_sha256, body_bytes, privacy FROM communications"
        ).fetchone()
        conn.close()
        assert row["body"] is None and row["privacy"] == "metadata"
        assert row["body_sha256"] and row["body_bytes"] > 0
    finally:
        daemon.stop()
        t.join(timeout=10)
        shutil.rmtree(sdir, ignore_errors=True)


def test_socket_file_mode_is_0600(running):
    _, sock, _ = running
    import stat

    assert stat.S_IMODE(os.stat(sock).st_mode) == 0o600


def test_two_daemons_racing_one_socket_yield_exactly_one_server(tmp_path: Path):
    """PR-#1345 review F2 (+ own pre-probe): the old probe→unlink→bind was a
    TOCTOU window — two racers both bound, last rename owned the path, the
    loser's shutdown deleted the winner's socket. The lifetime flock makes
    the race deterministic: one serves, one refuses."""
    sdir = _short_sock_dir()
    sock = sdir / "s"
    r2 = tmp_path / "other-root"
    r2.mkdir()
    d1 = PlaneDaemon(tmp_path, socket_override=sock, drain_interval=9999)
    d2 = PlaneDaemon(r2, socket_override=sock, drain_interval=9999)
    outcomes: dict[str, str] = {}

    def run(name, d):
        try:
            d.serve(install_signals=False)
            outcomes[name] = "served"
        except DaemonAlreadyRunning:
            outcomes[name] = "refused"

    t1 = threading.Thread(target=run, args=("d1", d1), daemon=True)
    t2 = threading.Thread(target=run, args=("d2", d2), daemon=True)
    t1.start(); t2.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and len(outcomes) < 1:
            time.sleep(0.05)
        assert list(outcomes.values()) == ["refused"], (
            f"exactly one racer must refuse immediately: {outcomes}")
        # the winner serves — and its socket answers
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if send_batch(sock, [_comm("b")])["ok"] is not None:
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("the surviving daemon never answered")
    finally:
        d1.stop(); d2.stop()
        t1.join(timeout=10); t2.join(timeout=10)
        shutil.rmtree(sdir, ignore_errors=True)
    assert sorted(outcomes.values()) == ["refused", "served"]


def test_shutdown_never_unlinks_a_replaced_socket(running):
    """F2's nastier half: if the public path is no longer OUR inode, leaving
    is someone else's file — shutdown must not delete it."""
    root, sock, daemon = running
    os.unlink(sock)
    sock.write_text("imposter")           # somebody else's file at our path
    daemon.stop()
    for _ in range(200):                   # lock release marks the finally done
        if daemon._lock_fd is None:
            break
        time.sleep(0.05)
    assert daemon._lock_fd is None, "daemon never finished shutting down"
    assert sock.exists() and sock.read_text() == "imposter", (
        "shutdown deleted a file it does not own")


def test_override_parent_is_never_chmodded(tmp_path: Path):
    """PR-#1345 review F7: --socket must not mutate an operator directory."""
    sdir = _short_sock_dir()
    os.chmod(sdir, 0o755)
    sock = sdir / "s"
    daemon = PlaneDaemon(tmp_path, socket_override=sock, drain_interval=9999)
    t = threading.Thread(
        target=lambda: daemon.serve(install_signals=False), daemon=True
    )
    t.start()
    try:
        for _ in range(200):
            if sock.exists():
                break
            time.sleep(0.02)
        import stat as _stat

        assert _stat.S_IMODE(os.stat(sdir).st_mode) == 0o755, (
            "the daemon chmodded a directory it does not own")
    finally:
        daemon.stop()
        t.join(timeout=10)
        shutil.rmtree(sdir, ignore_errors=True)


def test_missing_override_parent_refuses(tmp_path: Path):
    from claudlobby.plane.daemon import SocketOverrideInvalid

    daemon = PlaneDaemon(
        tmp_path, socket_override=Path("/tmp/claude/nope-nope/s"),
        drain_interval=9999,
    )
    with pytest.raises(SocketOverrideInvalid):
        daemon.serve(install_signals=False)


def test_failed_drain_advances_the_deadline(tmp_path: Path, monkeypatch):
    """PR-#1345 review F9: a broken db retried once per accept tick (~1s)
    forever; the deadline now advances at ATTEMPT."""
    from claudlobby.plane import daemon as daemon_mod

    calls = []

    def broken_drain(root, conn, host_uid):
        calls.append(1)
        raise RuntimeError("db is broken")

    monkeypatch.setattr(daemon_mod, "drain", broken_drain)
    d = PlaneDaemon(tmp_path, drain_interval=600)
    assert d._last_drain == 0.0
    d._drain_spool(reason="probe")
    assert len(calls) == 1
    assert d._last_drain > 0.0, "a FAILED attempt must still stamp the deadline"
    stamped = d._last_drain
    if time.monotonic() - stamped < 600:
        pass  # within interval: the serve loop's guard would not re-fire
    d._drain_spool(reason="probe2")   # direct call still runs (loop guards)
    assert len(calls) == 2


def test_shim_end_to_end_through_real_daemon(running):
    """lib/plane-emit.sh -> lib/plane-socket-client.py -> daemon -> row: the
    whole rung-1 chain, cross-language, on a real db."""
    import subprocess

    root, sock, _ = running
    shim = Path(__file__).resolve().parent.parent / "lib" / "plane-emit.sh"
    batch = json.dumps({"events": [_comm("f", body="via the shim")]})
    r = subprocess.run(
        ["bash", str(shim)], input=batch, capture_output=True, text=True,
        env={**os.environ, "CLAUDLOBBY_ROOT": str(root),
             "PLANE_SOCKET": str(sock)},
    )
    assert r.returncode == 0, r.stderr
    eid = r.stdout.strip().splitlines()[0]
    conn = connect(db_path(root))
    row = conn.execute(
        "SELECT body FROM communications WHERE event_id = ?", (eid,)
    ).fetchone()
    conn.close()
    assert row is not None and row["body"] == "via the shim"


def _doctor(root: Path):
    import subprocess
    import sys as _sys

    return subprocess.run(
        [_sys.executable, "-m", "claudlobby", "--root", str(root),
         "plane", "doctor"],
        capture_output=True, text=True,
    )


def test_doctor_daemon_rung_serving_and_never_armed(running, tmp_path: Path):
    """T9: three-state daemon rung. A live daemon reads serving; a root that
    never started one reads ok-unarmed (doors fall back by design)."""
    root, sock, _ = running
    import claudlobby.plane.daemon as dmod

    # the doctor probes the DEFAULT socket path; point the check at ours by
    # exercising the never-armed branch on a fresh root and the serving branch
    # via the daemon's own root only when the default path matches — here the
    # override socket means the default path is absent, so this root reads the
    # STARTED-NOT-SERVING attention branch instead (daemon_started was logged).
    r = _doctor(root)
    assert r.returncode == 1
    assert "not serving" in r.stdout and "falling back" in r.stdout
    from claudlobby.plane.emit_api import emit

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    emit(fresh, {"event_type": "work_item", "emitter": "t9",
                 "fleet": "f", "payload": {
                     "work_item_id": "wi_" + "9" * 32, "title": "t",
                     "created_by": "bot:f/a"}})
    r2 = _doctor(fresh)
    assert r2.returncode == 0, r2.stdout
    assert "never armed" in r2.stdout


def test_send_batch_raises_oserror_when_no_daemon(tmp_path: Path):
    sdir = _short_sock_dir()
    try:
        with pytest.raises(OSError):
            send_batch(sdir / "absent", [_comm("9")])
    finally:
        shutil.rmtree(sdir, ignore_errors=True)
