"""Every job a computer turns on by default runs once, cleanly, with no team.

The computer is the one tests/no_team_start.py starts. Each job runs its own
command: the host shell scripts run for real against the started root, with the
recorder serving it and a private Telegram stub; the jobs whose script only
execs the CLI run that command in this process as the started runtime.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

from tests.conftest import _write_exec
from tests.no_team_start import REPO, Case, default_on_jobs
from tests.test_maintenance_jobs import _native_fixture

JOBS = yaml.safe_load((REPO / "claudlobby/system.yaml").read_text())["host"]["jobs"]
#: Jobs whose script is a thin launcher that execs one CLI command; that command runs here.
CLI = {
    "claude-update",
    "plane-prune",
    "plane-expire",
    "plane-daemon",
    "plane-view",
    "plane-owner",
}
SHELL = sorted(default_on_jobs() - CLI)
#: Private peers a script runs beside it, beyond those every native job has.
HELPERS = {"plane-host-probe": ("memory-thrash.py",)}
#: What bash and its tools print when a script breaks partway and still exits zero.
BROKEN = ("unbound variable", "command not found", "can't open file", "syntax error",
          "No such file or directory", "Traceback")


def test_every_default_job_is_run_here():
    assert SHELL and set(SHELL) | (CLI & default_on_jobs()) == default_on_jobs()
    for name in SHELL:
        assert JOBS[name]["script"].startswith("$CLAUDLOBBY_NATIVE_DIR/"), name
    for name in CLI - {"plane-owner"}:
        script = REPO / "claudlobby/_runtime_scripts" / Path(JOBS[name]["script"]).name
        # update-claude-code.sh is reached only through `host update runtime`.
        assert (
            name == "claude-update" or 'exec "$CLAUDLOBBY_CLI"' in script.read_text()
        ), name


@pytest.fixture
def started(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    case.start()
    case.admit()
    return case


@contextmanager
def _recorder(root, scratch_plane_env):
    """The plane-daemon job's own command, serving the started root."""
    from claudlobby.plane.daemon import PlaneDaemon

    socket = scratch_plane_env.socket_dir() / "s"
    daemon = PlaneDaemon(root, socket_override=socket, drain_interval=0.2)
    failed = []

    def serve():
        try:
            daemon.serve(install_signals=False)
        except BaseException as exc:  # reported below, in the test's thread
            failed.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not socket.exists() and not failed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not failed and socket.exists(), failed
    try:
        yield socket
    finally:
        daemon.stop()
        thread.join(timeout=10)
    assert not failed and not thread.is_alive(), failed


def _events(root):
    from claudlobby.plane.db import connect, db_path

    with closing(connect(db_path(root))) as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT event, emitter, fleet_uid, detail FROM events ORDER BY ingest_seq")]


def _run_host_scripts(case, tmp_path, scratch_plane_env, test_cli, jobs):
    """Run each (job, args) as its unit does, with the recorder serving the root."""
    stubs = tmp_path / "stub-bin"
    stubs.mkdir(exist_ok=True)
    # No process on this test machine is a team's, so none is listed to act on,
    # and no terminal session is nudged.
    _write_exec(stubs / "ps", "#!/bin/sh\nexit 0\n")
    _write_exec(stubs / "tmux", "#!/bin/sh\nexit 1\n")
    capture = tmp_path / "tg-capture"
    before = len(_events(case.root))
    results = {}
    with _recorder(case.root, scratch_plane_env) as socket:
        for name, args in jobs:
            script = Path(JOBS[name]["script"]).name
            native = _native_fixture(tmp_path, script, *HELPERS.get(name, ()))
            env = {
                "HOME": str(tmp_path / "home"),
                "PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "C",
                "CLAUDLOBBY_NATIVE_DIR": str(native),
                "CLAUDLOBBY_LIBRARY_DIR": str(native),
                "CLAUDLOBBY_CLI": str(test_cli),
                "TG_CAPTURE": str(capture),
                **scratch_plane_env(case.root, socket=socket),
            }
            results[name] = subprocess.run(["bash", str(native / script), *args], env=env,
                                           capture_output=True, text=True, timeout=120)
        time.sleep(0.5)  # one drain interval past the last emit
    assert not capture.exists() or not capture.read_text().strip()
    return results, _events(case.root)[before:]


def test_the_host_scripts_run_clean_on_a_computer_with_no_team(started, tmp_path, scratch_plane_env, test_cli):
    results, recorded = _run_host_scripts(started, tmp_path, scratch_plane_env, test_cli,
                                          [(name, []) for name in SHELL])
    failed = {name: (result.returncode, result.stderr[-400:])
              for name, result in results.items() if result.returncode}
    assert not failed
    broken = {name: [line for line in result.stderr.splitlines() if any(text in line for text in BROKEN)]
              for name, result in results.items()}
    assert not {name: lines for name, lines in broken.items() if lines}
    assert not [row for row in recorded if row[0] == "script_error"], recorded
    # Whatever this machine's own disk and memory raise, it is the host's: one
    # fleet alias, the recorder's `_host`, carries every event a job records.
    assert len({row[2] for row in recorded if row[2] is not None}) <= 1, recorded


def test_a_host_alert_with_no_team_is_recorded_with_nowhere_to_go(started, tmp_path, scratch_plane_env, test_cli):
    """An alert on a computer with no team is kept, and its failed delivery is kept beside it."""
    results, recorded = _run_host_scripts(started, tmp_path, scratch_plane_env, test_cli,
                                          [("fleet-memory-check", ["--threshold", "0"])])
    assert results["fleet-memory-check"].returncode == 0
    events = [row[0] for row in recorded]
    assert "memory_high" in events and "alert_delivery_failed" in events, recorded
    failure = next(row for row in recorded if row[0] == "alert_delivery_failed")
    data = json.loads(failure[3])["data"]
    assert (data["for_event"], data["exit"], data["target"]) == ("memory_high", 2, "none"), data
    assert "ALERT-DELIVERY-FAILED memory_high" in results["fleet-memory-check"].stderr


def test_the_plane_maintenance_commands_run_clean_on_a_computer_with_no_team(started):
    from claudlobby.commands import plane_expire, plane_maintenance

    pruned = plane_maintenance.prune(
        SimpleNamespace(
            root=started.root, fleet=None, seed=False, days=None, dry_run=False
        )
    )
    assert pruned.data["source_revision"] == started.target
    expired = plane_expire.dispatch(
        SimpleNamespace(
            root=started.root, fleet=None, seed=False, after_days=None, dry_run=False
        )
    )
    assert expired.data["expired"] == 0 and expired.data["fleets"] == []


def test_claude_update_runs_clean_on_a_computer_with_no_team(started, monkeypatch):
    from claudlobby.commands import host_update

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    result = host_update.dispatch(
        SimpleNamespace(
            public_command="host.update.runtime",
            fleet=None,
            seed=None,
            root=str(started.root),
            dry_run=False,
        )
    )
    assert result.data["outcome"] == "no_fleet"


def test_the_plane_view_serves_a_computer_with_no_team(started):
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app
    from tests.test_plane_two_fleets import _Sampler

    client = TestClient(
        create_app(
            started.root, sampler=_Sampler([]), package=started.package(started.target)
        )
    )
    for path in ("/api/fleets", "/api/overview", "/api/tasks"):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)


def test_the_owner_server_serves_a_computer_with_no_team(tmp_path, monkeypatch):
    """plane-owner, when this computer's settings turn it on."""
    from fastapi.testclient import TestClient
    from claudlobby.plane.owner_access import OwnerAccess
    from claudlobby.plane.owner_browser import create_owner_browser_app
    from claudlobby.plane.owner_source import inspect_owner_setup
    from tests.test_plane_owner_browser import ORIGIN, OWNER, _pair_locally, _post
    from tests.test_plane_two_fleets import _Sampler

    case = Case(tmp_path, monkeypatch, owner="on")
    case.start()
    case.admit()

    async def verify_principal(_scope):
        return OWNER

    # owner_server.serve's own startup checks, then its application.
    OwnerAccess(case.root).current_grant()
    assert inspect_owner_setup(case.root) == case.host_uid
    app = create_owner_browser_app(
        case.root,
        verify_principal=verify_principal,
        expected_origin=ORIGIN,
        sampler=_Sampler([]),
        package=case.package(case.target),
    )
    client = TestClient(app, base_url=ORIGIN)
    _pair_locally(client, OwnerAccess(case.root))
    assert _post(client, "login").status_code == 200
    for path in ("/api/owner/status", "/api/fleets", "/api/tasks"):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
