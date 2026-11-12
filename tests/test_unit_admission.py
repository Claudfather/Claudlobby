"""Native renderer invariants on the canonical Git entry; no service enrollment."""
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys

import pytest
from claudlobby import runtime_admission as r
from claudlobby.config_plan import PlanError


@pytest.mark.parametrize("unit_prefix", ["claudlobby", "canary1747"])
def test_real_renderers_bind_every_native_carrier_and_refuse_bypass(monkeypatch, tmp_path,
                                                                 record_property, unit_prefix):
    from claudlobby import composer, supervision
    from claudlobby.config import BotConfig, FleetConfig, host_unit_name
    from claudlobby.paths import Paths
    from claudlobby.config_units import unit_family
    from claudlobby.context import native_environment
    from claudlobby.installation import parse_git_unit_argv
    from tests.package_fixtures import git_package

    root = tmp_path / 'data with spaces'
    root.mkdir()
    package = git_package(root)
    paths = Paths(root, package=package)
    env = native_environment(paths)
    env["PROBE_LITERAL"] = 'literal $HOME %n \\ "quoted" = value'
    for module in (composer, supervision):
        monkeypatch.setattr(module, "native_environment", lambda _: dict(env))
    bot = BotConfig("worker", "Worker", [])
    fleet = FleetConfig("example", "com.example", "worker", bots={"worker": bot})
    spec = supervision.build_supervision_spec(bot, fleet, paths)
    destination = tmp_path / "rendered"
    destination.mkdir()
    (destination / f"{spec.label}.plist").write_text(supervision.render_launchd_plist(spec))
    (destination / f"{spec.label}.service").write_text(supervision.render_systemd_unit(spec))
    composer._write_service_units(destination, host_unit_name("plane-daemon", prefix=unit_prefix), "plane-daemon",
                                  "$CLAUDLOBBY_NATIVE_DIR/plane-daemon.sh", paths)
    composer._write_timer_units(destination, "com.example.maintenance", "maintenance",
                                {"type": "interval", "seconds": 60, "startup": 60},
                                "$CLAUDLOBBY_NATIVE_DIR/maintenance.sh --check", "oneshot",
                                "example", paths, exec_args=["literal $HOME% & 'quoted'\\path"])
    frozen = []
    for plist_path in sorted(destination.glob("*.plist")):
        files = {p.name: (p.read_bytes(), p.stat().st_mode & 0o777)
                 for p in destination.glob(plist_path.stem + ".*")}
        resident = {host_unit_name(name, prefix=unit_prefix): phase
                    for name, phase in r.RESIDENT_UNIT_PHASES.items()}
        phase = "bots" if plist_path.stem == spec.label else resident.get(plist_path.stem, "producers")
        frozen.extend(unit_family(files, destination=destination,
                                  scope="bot" if phase == "bots" else "host", phase=phase))
    # Admission must use the reviewed unit and exact native target, never a
    # later change to this process's host override.
    changed_override = tmp_path / "later-host-system.yaml"
    changed_override.write_text("host: { unit_prefix: different }\n")
    monkeypatch.setenv("CLAUDLOBBY_HOST_SYSTEM_YAML", str(changed_override))
    checked = []
    for metadata in frozen:
        source = Path(metadata["source"])
        content = source.read_bytes()
        checked.append(source.suffix)
        if metadata['admission'] is None:
            continue  # Timers schedule the guarded service; they execute no command.
        argv = metadata["admission"]["argv"]
        target = parse_git_unit_argv(argv)
        assert target.argv[0] == str(package.installation.cli)
        assert target.unit == source.stem and target.phase == metadata['phase']
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
            files = {p.name: (p.read_bytes(), p.stat().st_mode & 0o777)
                     for p in destination.glob(source.stem + '.*')}
            files[source.name] = (bypass, metadata['mode'])
            with pytest.raises(PlanError, match='Git native entry'):
                unit_family(files, destination=destination, scope=metadata['scope'], phase=metadata['phase'])
    assert sorted(checked) == [".plist"] * 3 + [".service"] * 3 + [".timer"]
