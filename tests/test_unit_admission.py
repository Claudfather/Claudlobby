"""Durable generated startup refusal; only harmless private processes run here."""

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import plistlib
import selectors
import shutil
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from claudlobby import activation_state as a, runtime_admission as r
from claudlobby.releases import read_release, release_path, seal_release
from tests.test_config_plan import proposal
from tests.test_releases import installed
from tests.test_runtime_admission import _active, _identity, _starting


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("unit")  # bounded AF_UNIX pathname


def _environment(release):
    return {"CLAUDLOBBY_ROOT": str(release.directory.parents[2]),
            "FLEET_ROOT": str(release.directory.parents[2]),
            "CLAUDLOBBY_RELEASE_ID": release.release_id,
            "CLAUDLOBBY_CLI": str(release.cli_path),
            "CLAUDLOBBY_NATIVE_DIR": str(release.native_path),
            "CLAUDLOBBY_LIBRARY_DIR": str(release.directory / "library"),
            "CLAUDLOBBY_ARTIFACT_ID": release.inputs.artifact_id,
            "PATH": "/usr/bin:/bin"}


def test_interrupted_activation_and_reboot_refuse_before_producer_effect(proposal):
    builder, _, _ = proposal
    plan = builder.seal()
    release = read_release(builder.root, plan.release_id)
    env, identity = _environment(release), _identity(builder)
    argv = r.wrap_unit_argv(env, unit="claudlobby-maintenance", phase="producers",
                            mode="oneshot", argv=["/bin/sh", "-c", "exit 0"])
    effects = []

    def producer(*args, **kwargs):
        with pytest.raises(a.ActivationError, match="holds the lock"):
            with a.locked_activation(builder.root):
                pytest.fail("one-shot producer lost its lease")
        effects.append("ran")
        return SimpleNamespace(returncode=0)

    _active(plan)
    assert r.run_unit(argv, identity=identity, environment=env, runner=producer) == 0
    with a.locked_activation(builder.root) as store:
        from tests.test_activation_state import _prepare
        _prepare(store, plan, "interrupted")
    # No coordinator/socket remains, as after process loss or reboot. Selection
    # still points to the formerly active release; durable interruption wins.
    with pytest.raises(a.ActivationError, match="unfinished activation interrupted"):
        r.run_unit(argv, identity=identity, environment=env, runner=producer)
    with pytest.raises(a.ActivationError, match="environment differs"):
        r.run_unit(argv, identity=identity, environment={**env, "CLAUDLOBBY_CLI": "/stale/cli"},
                   runner=producer)
    assert effects == ["ran"]
    with a.locked_activation(builder.root):
        pass  # normal one-shot cleanup and refusal leave no lock behind


def test_resident_exec_retains_supervised_pid_without_lifetime_shared_lock(proposal, tmp_path):
    builder, _, _ = proposal
    plan = builder.seal()
    _active(plan)
    release, identity = read_release(builder.root, plan.release_id), _identity(builder)
    env = _environment(release)
    argv = r.wrap_unit_argv(env, unit="claudlobby-plane-daemon", phase="ingest", mode="exec",
                            argv=["/bin/bash", "-c", "printf '%s\\n' $$; read -r finish"])
    # Explicit fixture identity: exercise real exec/CLOEXEC, not a fake installed
    # interpreter or native lifecycle script. Installed -I bootstrap is separate.
    driver = tmp_path / "exec_boundary.py"
    driver.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from claudlobby import runtime_admission as r\n"
        f"r.run_unit({argv!r}, environment={env!r}, identity=r.RuntimeIdentity("
        f"Path({str(identity.cli)!r}), Path({str(identity.native)!r}), {identity.artifact_id!r}))\n"
    )
    process = subprocess.Popen([sys.executable, "-B", str(driver)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               start_new_session=True)
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=5), "resident exec did not answer within five seconds"
        assert process.stdout.readline().strip() == str(process.pid)
        assert process.poll() is None
        with a.locked_activation(builder.root):
            pass  # coordinator can now acquire EX to quiesce this tracked PID
        process.stdin.write("finish\n")
        process.stdin.flush()
        assert process.wait(timeout=5) == 0, process.stderr.read()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)


def test_exact_armed_bot_unit_has_only_its_matching_start_continuation(proposal):
    builder, _, settings = proposal
    plan = builder.seal()
    release, identity = read_release(builder.root, plan.release_id), _identity(builder)
    env = _environment(release)
    argv = r.wrap_unit_argv(env, unit="com.example.worker", phase="bots", mode="exec",
                            argv=[str(release.native_path / "start-bot.sh"), str(settings.parent)])
    admission = {"kind": "unit-start-v1", "unit": "com.example.worker", "argv": list(argv)}
    effects = []

    class ExecObserved(Exception):
        pass

    def start(*_):
        effects.append("exact start reached exec")
        raise ExecObserved

    with a.locked_activation(builder.root) as store:
        _starting(store, plan)
        producer = {**admission, "unit": "claudlobby-maintenance", "argv": list(
            r.wrap_unit_argv(env, unit="claudlobby-maintenance", phase="producers",
                              mode="oneshot", argv=["/bin/true"]))}
        with pytest.raises(a.ActivationError, match="matching start step"):
            with r.activation_start(store, "candidate", unit=producer, identity=identity):
                pytest.fail("producer was armed before verification")
        with r.activation_start(store, "candidate", unit=admission, identity=identity):
            wrong = list(argv)
            wrong[8] = "com.example.other"
            with pytest.raises(a.ActivationError, match="does not admit"):
                r.run_unit(wrong, identity=identity, environment=env, execer=start)
            with pytest.raises(ExecObserved):
                r.run_unit(argv, identity=identity, environment=env, execer=start)
            continuation = r._start_request(builder.root, "start-bot", settings.parent,
                                             plan.release_id, identity)
            with pytest.raises(a.ActivationError, match="does not admit"):
                r._request_start(builder.root, {**continuation, "pid": os.getpid() + 1}, .2)
            r._request_start(builder.root, {**continuation, "pid": os.getpid()}, .2)
        with pytest.raises(a.ActivationError, match="authorization unavailable"):
            r.run_unit(argv, identity=identity, environment=env, execer=start)
    assert effects == ["exact start reached exec"]
    assert not (builder.root / "state/activation-start.sock").exists()


def test_real_renderers_bind_every_native_carrier_and_refuse_bypass(installed, monkeypatch, tmp_path,
                                                                 record_property):
    from claudlobby import composer, supervision
    from claudlobby.config import BotConfig, FleetConfig
    from claudlobby.paths import Paths
    from claudlobby.config_units import unit_family
    from claudlobby.supervision_inventory import UnitDeclaration
    from tests.package_fixtures import source_package

    root, inputs, selected, _, _ = installed
    spaced_root = root.with_name("data with spaces")
    root.rename(spaced_root)  # fixture is not sealed yet; no release relocation
    directory = release_path(spaced_root, inputs.release_id)
    (directory / selected.cli).write_text(f"#!{directory / selected.interpreter}\n")
    release = seal_release(spaced_root, inputs, selected)
    env = _environment(release)
    env.pop("PATH")  # native_environment owns identity, renderer adds scheduler PATH
    env["PROBE_LITERAL"] = 'literal $HOME %n \\ "quoted" = value'
    for module in (composer, supervision):
        monkeypatch.setattr(module, "native_environment", lambda _: dict(env))
    paths = Paths(spaced_root.resolve(), package=replace(source_package(), native=release.native_path))
    bot = BotConfig("worker", "Worker", [])
    fleet = FleetConfig("example", "com.example", "worker", bots={"worker": bot})
    spec = supervision.build_supervision_spec(bot, fleet, paths)
    destination = tmp_path / "rendered"
    destination.mkdir()
    (destination / f"{spec.label}.plist").write_text(supervision.render_launchd_plist(spec))
    (destination / f"{spec.label}.service").write_text(supervision.render_systemd_unit(spec))
    composer._write_service_units(destination, "claudlobby-plane-daemon", "plane-daemon",
                                  "$CLAUDLOBBY_NATIVE_DIR/plane-daemon.sh", paths)
    composer._write_timer_units(destination, "com.example.maintenance", "maintenance",
                                {"type": "interval", "seconds": 60},
                                "$CLAUDLOBBY_NATIVE_DIR/maintenance.sh --check", "oneshot",
                                "example", paths, exec_args=["literal $HOME% & 'quoted'\\path"])
    frozen = []
    for plist_path in sorted(destination.glob("*.plist")):
        files = {p.name: (p.read_bytes(), p.stat().st_mode & 0o777)
                 for p in destination.glob(plist_path.stem + ".*")}
        phase = "bots" if plist_path.stem == spec.label else r.RESIDENT_UNIT_PHASES.get(plist_path.stem, "producers")
        frozen.extend(unit_family(files, destination=destination,
                                  scope="bot" if phase == "bots" else "host", phase=phase,
                                  release_id=release.release_id))
    checked = []
    for metadata in frozen:
        source = Path(metadata["source"])
        content = source.read_bytes()
        argv = metadata["admission"]["argv"]
        target = r.parse_unit_argv(argv)
        declaration = UnitDeclaration(source, metadata["scope"], Path(metadata["working_directory"]),
                                      metadata["release_id"], tuple(metadata["environment"].items()),
                                      service=metadata["service"])
        assert r.validate_unit_admission(release, declaration, metadata, content) == target
        checked.append(source.suffix)
        if source.suffix == ".service":
            assert b"ExecStopPost=" not in content  # failed admission has no cleanup write
            if sys.platform.startswith("linux"):
                analyze = shutil.which("systemd-analyze")
                assert analyze, "Linux carrier evidence requires the systemd parser"
                # The real systemd parser's offline debug dump, not a shell or
                # test-authored unquoter. verify never enrolls or starts a unit.
                process = subprocess.Popen(
                    [analyze, "--man=no", "--generators=no", "--recursive-errors=no", "verify", str(source)],
                    env={**os.environ, "SYSTEMD_LOG_LEVEL": "debug", "SYSTEMD_LOG_COLOR": "0"},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                try:
                    stdout, stderr = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
                    raise
                assert process.returncode == 0, stderr
                observed = dict(line.strip()[len("Environment: "):].split("=", 1)
                                for line in stdout.splitlines() if line.strip().startswith("Environment: "))
                plist_env = plistlib.loads(source.with_suffix(".plist").read_bytes())["EnvironmentVariables"]
                expected = spec.environment if target.phase == "bots" else plist_env
                assert observed == expected
                record_property("systemd_environment", "actual offline systemd parser, all rendered services")
            else:
                record_property("systemd_environment", "not run on this platform; Linux CI required")
            # Hashing the changed payload does not legitimize a bypass.
            bypass = content.replace(r.unit_systemd_command(argv).encode(), b"/bin/true")
            with pytest.raises(a.ActivationError, match="bypasses"):
                r.validate_unit_admission(release, declaration,
                    {**metadata, "sha256": hashlib.sha256(bypass).hexdigest()}, bypass)
    assert sorted(checked) == [".plist"] * 3 + [".service"] * 3 + [".timer"]
