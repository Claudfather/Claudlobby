"""Phase-2 T1: composed host-service units + the launcher.

The load-bearing assertion is compose-time dormancy: an UNARMED service job
composes NO files, because setup-system's macOS leg enrolls every
claudlobby-*.plist it finds — a composed-but-dormant service plist would be
started by the next setup run (a root pull silently activating a resident
process, the no-silent-switches violation)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import claudlobby.composer as composer_mod
from tests.package_fixtures import source_package
from claudlobby.paths import Paths


REPO = Path(__file__).resolve().parent.parent


def _paths(tmp_path: Path) -> Paths:
    return Paths(root=tmp_path, package=source_package())


def _compose(tmp_path: Path, monkeypatch, jobs: dict) -> Path:
    monkeypatch.setattr(composer_mod, "load_host_jobs", lambda: jobs)
    return composer_mod.compose_host_timers(_paths(tmp_path))


SERVICE_JOB = {
    "unit": "service",
    "script": "$CLAUDLOBBY_NATIVE_DIR/plane-daemon.sh",
}


def test_armed_service_composes_service_and_plist_no_timer(tmp_path, monkeypatch):
    out = _compose(
        tmp_path, monkeypatch, {"plane-daemon": {**SERVICE_JOB, "enroll": True}}
    )
    service = out / "claudlobby-plane-daemon.service"
    plist = out / "claudlobby-plane-daemon.plist"
    assert service.exists() and plist.exists()
    assert not (out / "claudlobby-plane-daemon.timer").exists()
    body = service.read_text()
    assert "Restart=always" in body
    assert f"Environment=CLAUDLOBBY_ROOT={tmp_path}" in body
    assert str(source_package().native / "plane-daemon.sh") in body
    pbody = plist.read_text()
    assert "<key>KeepAlive</key>" in pbody and "<true/>" in pbody
    assert "<key>RunAtLoad</key>" in pbody


def test_service_relaunches_on_a_NONZERO_exit(tmp_path, monkeypatch):
    """#1485. The ingest daemon exits 4 when the db outruns its loaded code,
    so the composed units are what turns that exit into a repair. Both forms
    already relaunch on ANY exit; this pins them against a narrowing.

    launchd: `KeepAlive` must be the bare <true/>, not the dictionary form.
    `<key>KeepAlive</key><dict><key>SuccessfulExit</key><true/></dict>` would
    relaunch ONLY on a clean exit and strand the daemon on exactly the exit
    this fix introduces — and it is one word away from the correct dictionary
    form, which is reason enough to pin the shape rather than the key."""
    out = _compose(
        tmp_path, monkeypatch, {"plane-daemon": {**SERVICE_JOB, "enroll": True}}
    )
    body = (out / "claudlobby-plane-daemon.service").read_text()
    assert "Restart=always" in body, "on-success/no would strand the exit"
    # RestartSec keeps a permanent-condition loop under systemd's default
    # start limit (5 starts / 10s); without it the unit latches `failed` and
    # stops relaunching, which is the incident again with extra steps.
    rsec = [ln for ln in body.splitlines() if ln.startswith("RestartSec=")]
    assert len(rsec) == 1, rsec
    assert int(rsec[0].split("=", 1)[1]) >= 2, rsec

    pbody = (out / "claudlobby-plane-daemon.plist").read_text()
    squashed = "".join(pbody.split())
    assert "<key>KeepAlive</key><true/>" in squashed, (
        "KeepAlive must be unconditional — a SuccessfulExit dict would skip"
        f" the downgrade exit: {pbody}"
    )
    assert "SuccessfulExit" not in pbody


def test_the_exit_line_lands_somewhere_a_person_can_read(tmp_path, monkeypatch):
    """#1485 fold. The daemon's ONE exit line is the only record a stale exit
    leaves — a process that refuses the db cannot write a row about refusing
    it — and launchd sends an unredirected service's stdio to /dev/null, so on
    macOS a relaunch loop was invisible: no plane row, no log, nothing.

    Pinned against `plane.daemon.DAEMON_LOG_NAME` rather than a literal,
    because the exit line PRINTS that path: if the two drift, the daemon sends
    an operator to a file that holds nothing, which is worse than silence."""
    from claudlobby.plane.daemon import DAEMON_LOG_NAME

    out = _compose(
        tmp_path, monkeypatch, {"plane-daemon": {**SERVICE_JOB, "enroll": True}}
    )
    log = tmp_path / "state" / DAEMON_LOG_NAME
    squashed = "".join((out / "claudlobby-plane-daemon.plist").read_text().split())
    assert f"<key>StandardOutPath</key><string>{log}</string>" in squashed
    assert f"<key>StandardErrorPath</key><string>{log}</string>" in squashed
    # launchd drops output when the DIRECTORY is missing, so compose must
    # provision it — a path pinned into a plist nobody can write to is the
    # same silence with extra steps.
    assert log.parent.is_dir(), "state/ must exist for launchd to open the log"

    # systemd carries no redirect on purpose: an unredirected unit's stderr is
    # already in the journal, and a StandardOutput= would move it out of
    # `journalctl -u` where every other unit's output lives.
    body = (out / "claudlobby-plane-daemon.service").read_text()
    assert "StandardOutput=" not in body and "StandardError=" not in body


def test_unarmed_service_composes_nothing(tmp_path, monkeypatch):
    out = _compose(
        tmp_path, monkeypatch, {"plane-daemon": {**SERVICE_JOB, "enroll": False}}
    )
    leftovers = (
        [p.name for p in out.glob("claudlobby-plane-daemon.*")] if out.exists() else []
    )
    assert leftovers == [], f"dormant service leaked units: {leftovers}"


def test_enroll_absent_means_dormant_for_services(tmp_path, monkeypatch):
    """Timers default enroll to TRUE; services must default to FALSE — the
    asymmetry is the safety property, so pin it."""
    out = _compose(tmp_path, monkeypatch, {"plane-daemon": dict(SERVICE_JOB)})
    leftovers = (
        [p.name for p in out.glob("claudlobby-plane-daemon.*")] if out.exists() else []
    )
    assert leftovers == []


def test_service_script_source_guard_applies(tmp_path, monkeypatch):
    with pytest.raises(Exception, match="source"):
        _compose(
            tmp_path,
            monkeypatch,
            {
                "plane-daemon": {
                    "unit": "service",
                    "enroll": True,
                    "script": "/etc/passwd",
                }
            },
        )


def test_sibling_timer_jobs_still_compose_around_a_service(tmp_path, monkeypatch):
    out = _compose(
        tmp_path,
        monkeypatch,
        {
            "plane-daemon": {**SERVICE_JOB, "enroll": True},
            "disk-monitor": {
                "script": "$CLAUDLOBBY_ROOT/lib/disk-monitor.sh",
                "schedule": "*-*-* 09:00:00",
                "type": "oneshot",
            },
        },
    )
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
    out = _compose(
        tmp_path,
        monkeypatch,
        {
            "plane-daemon": {**SERVICE_JOB, "enroll": True},
            "disk-monitor": {
                "script": "$CLAUDLOBBY_ROOT/lib/disk-monitor.sh",
                "schedule": "*-*-* 09:00:00",
                "type": "oneshot",
            },
        },
    )
    _compose(
        tmp_path,
        monkeypatch,
        {
            "plane-daemon": {**SERVICE_JOB, "enroll": False},
            "disk-monitor": {
                "script": "$CLAUDLOBBY_ROOT/lib/disk-monitor.sh",
                "schedule": "*-*-* 09:00:00",
                "type": "oneshot",
            },
        },
    )
    assert not (out / "claudlobby-plane-daemon.service").exists()
    assert (out / "claudlobby-disk-monitor.timer").exists(), (
        "the prune must never reach sibling timer jobs"
    )


def test_example_system_yaml_ships_the_daemon_ARMED():
    """The defaults flip (chunk N): the ingest daemon ships ON.

    The plane has been the estate's only record since the F18 closure, so a
    dormant daemon meant every emit paid an interpreter spawn while the
    behavior itself ran regardless — a default that bought nothing and cost
    latency on the cheapest hardware the north star names. The COMPOSE-time
    dormancy machinery is untouched and still pinned by the tests above; only
    the shipped value moved."""
    text = (REPO / "system.yaml.example").read_text()
    assert "plane-daemon:" in text
    block = text.split("plane-daemon:", 1)[1]
    assert block.splitlines()[1].strip() == "enroll: true"
    assert "unit: service" in block


@pytest.mark.parametrize("script,verb", [("plane-daemon.sh", "serve"), ("plane-view.sh", "view")])
def test_launcher_execs_selected_cli_without_path_probes(tmp_path, script, verb):
    """The selected executable receives exact argv and the supervisor's PID."""
    root = tmp_path / "data root"
    root.mkdir()
    stale_bin = tmp_path / "stale-bin"
    stale_bin.mkdir()
    for name in ("claudlobby", "python3"):
        stale = stale_bin / name
        stale.write_text("#!/bin/bash\necho STALE; exit 99\n")
        stale.chmod(0o755)
    selected = tmp_path / "selected cli"
    selected.write_text('#!/bin/bash\nprintf "PID:%s\\n" "$$"\nprintf "ARG:%s\\n" "$@"\n')
    selected.chmod(0o755)
    env = {
        "PATH": str(stale_bin),
        "CLAUDLOBBY_ROOT": str(root),
        "CLAUDLOBBY_CLI": str(selected),
    }
    if verb == "serve":
        socket = str(tmp_path / "owned.sock")
        env.update(PLANE_SOCKET=socket, PLANE_DRAIN_INTERVAL="0.25")
        expected = ["--socket", socket, "--drain-interval", "0.25"]
    else:
        env.update(PLANE_VIEW_HOST="127.0.0.1", PLANE_VIEW_PORT="4567")
        expected = ["--host", "127.0.0.1", "--port", "4567"]
    with subprocess.Popen(
        ["/bin/bash", str(REPO / "claudlobby/_runtime_scripts" / script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        assert proc.returncode == 0, stderr
        assert stdout.splitlines() == [f"PID:{proc.pid}"] + [
            f"ARG:{arg}" for arg in ["--root", str(root), "plane", verb, *expected]
        ]


@pytest.mark.parametrize("script", ["plane-daemon.sh", "plane-view.sh"])
def test_launcher_refuses_unselected_cli_despite_stale_path(tmp_path, script):
    """Neither PATH nor an old data-root virtualenv may choose a release."""
    root = tmp_path / "root"
    stale_bin = root / ".venv" / "bin"
    stale_bin.mkdir(parents=True)
    for name in ("claudlobby", "python3"):
        stale = stale_bin / name
        stale.write_text("#!/bin/bash\necho STALE; exit 99\n")
        stale.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", str(REPO / "claudlobby/_runtime_scripts" / script)],
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": str(stale_bin), "CLAUDLOBBY_ROOT": str(root)},
    )
    assert result.returncode == 127
    assert "CLAUDLOBBY_CLI" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("root", [None, "relative/data"])
@pytest.mark.parametrize("script", ["plane-daemon.sh", "plane-view.sh"])
def test_launcher_requires_explicit_absolute_data_root(tmp_path, root, script):
    selected = tmp_path / "selected"
    selected.write_text("#!/bin/bash\necho MUST-NOT-RUN; exit 99\n")
    selected.chmod(0o755)
    env = {"PATH": str(tmp_path), "CLAUDLOBBY_CLI": str(selected)}
    if root is not None:
        env["CLAUDLOBBY_ROOT"] = root
    result = subprocess.run(
        ["/bin/bash", str(REPO / "claudlobby/_runtime_scripts" / script)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode == 127
    assert "CLAUDLOBBY_ROOT" in result.stderr
    assert result.stdout == ""
