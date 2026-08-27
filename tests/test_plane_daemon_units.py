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


def test_armed_to_unarmed_transition_prunes_the_composed_units(tmp_path, monkeypatch):
    """PR-#1345 review F3: recomposing unarmed left the armed run's units on
    disk, and setup-system enrolls every claudlobby-*.plist it finds — the
    supposedly dormant service silently reactivated. Disarm must PRUNE."""
    armed = {"plane-daemon": {**SERVICE_JOB, "enroll": True}}
    out = _compose(tmp_path, monkeypatch, armed)
    assert (out / "claudlobby-plane-daemon.service").exists()
    unarmed = {"plane-daemon": {**SERVICE_JOB, "enroll": False}}
    _compose(tmp_path, monkeypatch, unarmed)
    leftovers = sorted(p.name for p in out.glob("claudlobby-plane-daemon.*"))
    assert leftovers == [], f"disarming left units for setup to re-enroll: {leftovers}"


def test_prune_only_touches_the_disarmed_service_units(tmp_path, monkeypatch):
    out = _compose(tmp_path, monkeypatch, {
        "plane-daemon": {**SERVICE_JOB, "enroll": True},
        "disk-monitor": {
            "script": "$CLAUDLOBBY_ROOT/lib/disk-monitor.sh",
            "schedule": "*-*-* 09:00:00", "type": "oneshot",
        },
    })
    _compose(tmp_path, monkeypatch, {
        "plane-daemon": {**SERVICE_JOB, "enroll": False},
        "disk-monitor": {
            "script": "$CLAUDLOBBY_ROOT/lib/disk-monitor.sh",
            "schedule": "*-*-* 09:00:00", "type": "oneshot",
        },
    })
    assert not (out / "claudlobby-plane-daemon.service").exists()
    assert (out / "claudlobby-disk-monitor.timer").exists(), (
        "the prune must never reach sibling timer jobs")


def test_service_enroller_refuses_foreign_owner_without_adopt(tmp_path):
    """PR-#1345 review F5: enrolling from a second tree silently captured the
    first tree's installed unit — the timer enroller's ownership gate now
    guards the service enroller too, with --adopt as the explicit override."""
    home = tmp_path / "home"
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    (stub_bin / "systemctl").write_text("#!/bin/bash\nexit 0\n")
    os.chmod(stub_bin / "systemctl", 0o755)

    def composed_unit(root: str) -> str:
        return (
            "# Generated by claudlobby — do not hand-edit.\n[Unit]\n"
            "Description=claudlobby plane-daemon (host service)\n\n[Service]\n"
            f"Environment=CLAUDLOBBY_ROOT={root}\nExecStart={root}/lib/plane-daemon.sh\n"
        )

    installed = home / ".config" / "systemd" / "user" / "claudlobby-plane-daemon.service"
    installed.write_text(composed_unit("/srv/root-A"))
    units_b = tmp_path / "units-b"
    units_b.mkdir()
    (units_b / "claudlobby-plane-daemon.service").write_text(
        composed_unit("/srv/root-B"))

    def run(*extra):
        return subprocess.run(
            ["bash", str(REPO / "lib" / "install-host-service-systemd.sh"),
             "plane-daemon", *extra],
            capture_output=True, text=True,
            env={"PATH": f"{stub_bin}:/usr/bin:/bin", "HOME": str(home),
                 "TIMER_DIR": str(units_b),
                 "UNIT_NAME": "claudlobby-plane-daemon"},
        )

    refused = run()
    assert refused.returncode != 0, "foreign-owner capture must refuse"
    assert "root-A" in installed.read_text(), "the refusal must not overwrite"
    adopted = run("--adopt")
    assert adopted.returncode == 0, adopted.stderr
    assert "adopting" in adopted.stdout
    assert "root-B" in installed.read_text()


def test_launcher_127_when_python3_cannot_import_claudlobby(tmp_path):
    """PR-#1345 review F6: on a bare host /bin/python3 exists and cannot
    import claudlobby — `python3 -m claudlobby` then exits 1, masquerading
    as a daemon failure. The launcher must probe the import and fall through
    to the honest 127."""
    root = tmp_path / "root"
    root.mkdir()
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    fake_py = stub_dir / "python3"
    fake_py.write_text(
        "#!/bin/bash\n"
        'case "$*" in *"import claudlobby"*) exit 1;; esac\n'
        'echo "MUST NOT EXEC: $*"; exit 99\n'
    )
    os.chmod(fake_py, 0o755)
    r = subprocess.run(
        ["/bin/bash", str(REPO / "lib" / "plane-daemon.sh")],
        capture_output=True, text=True,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin", "CLAUDLOBBY_ROOT": str(root)},
    )
    assert r.returncode == 127, (r.returncode, r.stdout, r.stderr)
    assert "MUST NOT EXEC" not in r.stdout
    assert "no claudlobby CLI resolvable" in r.stderr


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

    # SYMLINK, never copy (#1372 review verification note): a copied macOS
    # system binary can wedge uninterruptibly under SIP/quarantine and hung
    # the whole suite on the reviewer's host.
    os.symlink(_shutil.which("dirname"), stub_dir / "dirname")
    r = subprocess.run(
        ["/bin/bash", str(REPO / "lib" / "plane-daemon.sh")],
        capture_output=True, text=True,
        env={"PATH": f"{stub_dir}:/bin", "CLAUDLOBBY_ROOT": str(root)},
    )
    assert r.returncode == 127, (r.returncode, r.stdout, r.stderr)
    assert "no claudlobby CLI resolvable" in r.stderr