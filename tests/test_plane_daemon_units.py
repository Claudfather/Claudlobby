"""Phase-2 T1: composed host-service units + the launcher.

The load-bearing assertion is compose-time dormancy: an UNARMED service job
composes NO files, because setup-system's macOS leg enrolls every
claudlobby-*.plist it finds — a composed-but-dormant service plist would be
started by the next setup run (a root pull silently activating a resident
process, the no-silent-switches violation)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import claudlobby.composer as composer_mod
from claudlobby.paths import Paths


REPO = Path(__file__).resolve().parent.parent


def _paths(tmp_path: Path) -> Paths:
    return Paths(root=tmp_path)


def _compose(tmp_path: Path, monkeypatch, jobs: dict) -> Path:
    monkeypatch.setattr(composer_mod, "load_host_jobs", lambda: jobs)
    return composer_mod.compose_host_timers(_paths(tmp_path))


SERVICE_JOB = {
    "unit": "service",
    "script": "$CLAUDLOBBY_ROOT/lib/plane-daemon.sh",
}


def test_armed_service_composes_service_and_plist_no_timer(tmp_path, monkeypatch):
    out = _compose(tmp_path, monkeypatch,
                   {"plane-daemon": {**SERVICE_JOB, "enroll": True}})
    service = out / "claudlobby-plane-daemon.service"
    plist = out / "claudlobby-plane-daemon.plist"
    assert service.exists() and plist.exists()
    assert not (out / "claudlobby-plane-daemon.timer").exists()
    body = service.read_text()
    assert "Restart=always" in body
    assert f"Environment=CLAUDLOBBY_ROOT={tmp_path}" in body
    assert f"{tmp_path}/lib/plane-daemon.sh" in body
    pbody = plist.read_text()
    assert "<key>KeepAlive</key>" in pbody and "<true/>" in pbody
    assert "<key>RunAtLoad</key>" in pbody


def test_unarmed_service_composes_nothing(tmp_path, monkeypatch):
    out = _compose(tmp_path, monkeypatch,
                   {"plane-daemon": {**SERVICE_JOB, "enroll": False}})
    leftovers = [p.name for p in out.glob("claudlobby-plane-daemon.*")] if out.exists() else []
    assert leftovers == [], f"dormant service leaked units: {leftovers}"


def test_enroll_absent_means_dormant_for_services(tmp_path, monkeypatch):
    """Timers default enroll to TRUE; services must default to FALSE — the
    asymmetry is the safety property, so pin it."""
    out = _compose(tmp_path, monkeypatch, {"plane-daemon": dict(SERVICE_JOB)})
    leftovers = [p.name for p in out.glob("claudlobby-plane-daemon.*")] if out.exists() else []
    assert leftovers == []


def test_service_script_source_guard_applies(tmp_path, monkeypatch):
    with pytest.raises(Exception, match="source"):
        _compose(tmp_path, monkeypatch, {"plane-daemon": {
            "unit": "service", "enroll": True,
            "script": "/etc/passwd",
        }})


def test_sibling_timer_jobs_still_compose_around_a_service(tmp_path, monkeypatch):
    out = _compose(tmp_path, monkeypatch, {
        "plane-daemon": {**SERVICE_JOB, "enroll": True},
        "disk-monitor": {
            "script": "$CLAUDLOBBY_ROOT/lib/disk-monitor.sh",
            "schedule": "*-*-* 09:00:00", "type": "oneshot",
        },
    })
    assert (out / "claudlobby-disk-monitor.timer").exists()
    assert (out / "claudlobby-plane-daemon.service").exists()


def test_example_system_yaml_ships_the_daemon_dormant():
    text = (REPO / "system.yaml.example").read_text()
    assert "plane-daemon:" in text
    block = text.split("plane-daemon:", 1)[1]
    assert block.splitlines()[1].strip() == "enroll: false"
    assert "unit: service" in block


def test_launcher_execs_resolved_cli_with_serve_args(tmp_path):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "claudlobby"
    stub.write_text("#!/bin/bash\necho \"CLI-ARGS:$*\"\n")
    os.chmod(stub, 0o755)
    root = tmp_path / "root"
    root.mkdir()
    r = subprocess.run(
        ["bash", str(REPO / "lib" / "plane-daemon.sh")],
        capture_output=True, text=True,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin",
             "CLAUDLOBBY_ROOT": str(root),
             "PLANE_SOCKET": "/tmp/x.sock"},
    )
    assert r.returncode == 0, r.stderr
    # --root is GLOBAL (precedes the subcommand) — the smoke run caught the
    # inverted order as a real argparse error where this stub accepted it.
    assert f"CLI-ARGS:--root {root} plane serve --socket /tmp/x.sock" in r.stdout


def test_launcher_prefers_the_root_venv(tmp_path):
    root = tmp_path / "root"
    (root / ".venv" / "bin").mkdir(parents=True)
    venv_cli = root / ".venv" / "bin" / "claudlobby"
    venv_cli.write_text("#!/bin/bash\necho VENV-CLI\n")
    os.chmod(venv_cli, 0o755)
    r = subprocess.run(
        ["bash", str(REPO / "lib" / "plane-daemon.sh")],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "CLAUDLOBBY_ROOT": str(root)},
    )
    assert r.returncode == 0, r.stderr
    assert "VENV-CLI" in r.stdout


def test_launcher_127_when_nothing_resolves(tmp_path):
    """PATH carries bash + coreutils (/bin and a dirname stub) but neither a
    claudlobby CLI nor python3 (macOS keeps python3 in /usr/bin, excluded)."""
    root = tmp_path / "root"
    root.mkdir()
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    import shutil as _shutil

    _shutil.copy(_shutil.which("dirname"), stub_dir / "dirname")
    os.chmod(stub_dir / "dirname", 0o755)
    r = subprocess.run(
        ["/bin/bash", str(REPO / "lib" / "plane-daemon.sh")],
        capture_output=True, text=True,
        env={"PATH": f"{stub_dir}:/bin", "CLAUDLOBBY_ROOT": str(root)},
    )
    assert r.returncode == 127, (r.returncode, r.stdout, r.stderr)
    assert "no claudlobby CLI resolvable" in r.stderr