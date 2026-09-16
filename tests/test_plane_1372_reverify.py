"""PR-#1372 RE-VERIFICATION residuals, pinned (numbered per the re-verify)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib"


def _client(*extra, stdin='{"events": [{"event_type": "x"}]}', timeout=20):
    return subprocess.run(
        ["python3", "-S", "-E", str(LIB / "plane-socket-client.py"), *extra],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )


def test_timeout_inf_and_zero_are_usage_refusals(tmp_path):
    for bad in ("inf", "nan", "0", "-3", "99999"):
        r = _client("--socket", "/nonexistent", "--finalize-to",
                    str(tmp_path / "f"), "--timeout", bad)
        assert r.returncode == 2, (bad, r.returncode, r.stderr)
        assert "finite positive" in r.stderr, bad


def test_finalize_only_mints_and_skips_the_socket(tmp_path):
    fin = tmp_path / "fin"
    r = _client("--socket", "/nonexistent", "--finalize-to", str(fin),
                "--finalize-only",
                stdin=json.dumps({"events": [{"event_type": "task",
                                              "emitter": "t", "fleet": "f",
                                              "payload": {}}]}))
    assert r.returncode == 5
    batch = json.loads(fin.read_text())
    assert batch["events"][0]["event_id"].startswith("ev_")


def test_probe_daemon_survives_list_reply_and_trickle(tmp_path):
    """Re-verify F15 residuals: a listener replying `[]` crashed doctor on
    AttributeError; a trickle listener exceeded the requested timeout."""
    import socket as s
    import tempfile
    import threading

    from claudlobby.plane.daemon import probe_daemon

    d = Path(tempfile.mkdtemp(prefix="rp", dir="/tmp/claude"))

    def serve(path, behavior):
        srv = s.socket(s.AF_UNIX, s.SOCK_STREAM)
        srv.bind(str(path))
        srv.listen(1)
        srv.settimeout(6)

        def run():
            try:
                c, _ = srv.accept()
                behavior(c)
                c.close()
            except OSError:
                pass
            finally:
                srv.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    lst = d / "list"
    serve(lst, lambda c: c.sendall(b"[]\n"))
    assert probe_daemon(lst) is False, "a JSON list is not the daemon shape"

    def trickle(c):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            try:
                c.sendall(b"x")
            except OSError:
                return
            time.sleep(0.2)

    tr = d / "trickle"
    serve(tr, trickle)
    t0 = time.monotonic()
    assert probe_daemon(tr, timeout=1.0) is False
    assert time.monotonic() - t0 < 3, "trickle must not outlive the deadline"
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def test_bench_negative_shim_is_a_usage_error():
    r = subprocess.run(
        [sys.executable, str(LIB.parent / "bin" / "plane-bench.py"),
         "--shim", "-1"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 2
    assert "0 or a positive integer" in r.stderr


def test_wedge_cooldown_marker_short_circuits_the_socket(tmp_path):
    """Re-verify F5 blocking residual: doors emit twice, so the per-emission
    deadline compounded. A fresh wedge marker sends the SECOND emission
    straight to the CLI rung with disclosure."""
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / ".socket-wedged").write_text(
        str(int(time.time())))
    recorder = tmp_path / "rec.sh"
    recorder.write_text("#!/bin/bash\nexit 0\n")
    recorder.chmod(0o755)
    t0 = time.monotonic()
    r = subprocess.run(
        ["bash", str(LIB / "plane-emit.sh")],
        input='{"events": [{"event_type": "task", "emitter": "t",'
              ' "fleet": "f", "payload": {}}]}',
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "CLAUDLOBBY_ROOT": str(root),
             "PLANE_SOCKET": str(root / "no.sock"),
             "PLANE_EMIT_CLI": str(recorder)},
    )
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, r.stderr
    assert "wedge cooldown" in r.stderr
    assert elapsed < 1.5, f"cooldown path must not touch the socket ({elapsed:.1f}s)"


def test_expired_wedge_marker_is_cleared(tmp_path):
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    mark = root / "state" / "plane" / ".socket-wedged"
    mark.write_text(str(int(time.time()) - 3600))
    recorder = tmp_path / "rec.sh"
    recorder.write_text("#!/bin/bash\nexit 0\n")
    recorder.chmod(0o755)
    r = subprocess.run(
        ["bash", str(LIB / "plane-emit.sh")],
        input='{"events": [{"event_type": "task", "emitter": "t",'
              ' "fleet": "f", "payload": {}}]}',
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "CLAUDLOBBY_ROOT": str(root),
             "PLANE_SOCKET": str(root / "no.sock"),
             "PLANE_EMIT_CLI": str(recorder)},
    )
    assert r.returncode == 0, r.stderr
    assert "wedge cooldown" not in r.stderr, "expired marker must not gate"