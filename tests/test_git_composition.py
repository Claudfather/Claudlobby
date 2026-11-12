"""Candidate source reads must never escape into installed runtime references."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from claudlobby.composer import (
    compose_bot_conf, compose_fleet_timers, compose_launchd_plist,
    compose_settings_local, compose_systemd_unit,
)
from claudlobby.config import load_fleet
from claudlobby.context import native_environment
from claudlobby.installation import Installation
from claudlobby.paths import Paths
from claudlobby import active_config, config_staging, config_units, composer, context
from claudlobby.config_plan import PlanError
from claudlobby.resources import selected_cli
from tests.package_fixtures import source_package
from tests.test_config_staging import staging_case  # noqa: F401 — isolated real-render fixture


@pytest.fixture
def candidate(tmp_path):
    installation = Installation(tmp_path / "installation", tmp_path / "data")
    installation.bin.mkdir(parents=True)
    installation.cli.write_text("#!/bin/sh\nexit 99\n")
    installation.cli.chmod(0o755)
    installation.data_root.mkdir()
    manifest = installation.data_root / "fleet.yaml"
    manifest.write_text(json.dumps({"fleet": {
        "name": "example", "manager": "lead", "service_prefix": "com.example",
        "system_defaults": False,
        "bots": {"lead": {"expertise": ["software-engineering"], "channels": [],
                          "brief": {"on_start": True},
                          "briefing": {"slots": {"morning": "*-*-* 08:30:00"}}}},
    }}))
    fleet, defaults = load_fleet(manifest)
    package = replace(source_package(), installation=installation, artifact_id="",
                      source_revision="a" * 40)
    paths = Paths(installation.data_root, package=package)
    return installation, paths, fleet, defaults


def test_candidate_render_uses_final_installation_paths(candidate):
    installation, paths, fleet, defaults = candidate
    bot = fleet.bots["lead"]
    env = native_environment(paths)
    assert env["CLAUDLOBBY_CLI"] == str(installation.cli)
    assert env["CLAUDLOBBY_NATIVE_DIR"] == str(installation.native)
    assert env["CLAUDLOBBY_LIBRARY_DIR"] == str(installation.source / "library")
    assert env["CLAUDLOBBY_INSTALLATION"] == str(installation.directory)
    assert "CLAUDLOBBY_RELEASE_ID" not in env
    assert paths.lib == paths.package.native  # Candidate tools still read from candidate.
    texts = [compose_bot_conf(bot, fleet, paths), compose_systemd_unit(bot, fleet, paths),
             compose_launchd_plist(bot, fleet, paths),
             json.dumps(compose_settings_local(bot, fleet, paths))]
    timers = compose_fleet_timers(fleet, paths, defaults)
    texts.extend(path.read_text() for path in timers.iterdir() if path.is_file())
    assert any(str(installation.cli) in text and "brief" in text for text in texts)
    for text in texts:
        assert str(paths.package.native) not in text
        assert str(paths.package.library) not in text
    assert str(installation.native / "start-bot.sh") in texts[1]
    assert str(installation.native / "start-bot.sh") in texts[2]


def test_installation_binding_and_code_are_not_writable_configuration(candidate):
    installation, paths, _, _ = candidate
    with pytest.raises(ValueError, match="different persistent data root"):
        Paths(installation.data_root / "other", package=paths.package)
    for destination in (installation.source / "library/edit", installation.cli,
                        installation.environment / "bin/python"):
        with pytest.raises(ValueError, match="package-owned"):
            paths.assert_writable(destination, output_root=installation.directory)


def test_git_host_render_freezes_native_bindings_without_a_release(staging_case, monkeypatch):  # noqa: F811
    case = staging_case
    installation = Installation(case.root.parent / "installation with spaces", case.root)
    installation.bin.mkdir(parents=True)
    installation.cli.write_text("#!/bin/sh\nexit 99\n")
    installation.cli.chmod(0o755)
    package = replace(case.package, installation=installation, artifact_id="", source_revision="a" * 40)
    paths = Paths(case.root, package=package)
    for module in (config_staging, composer, context):
        monkeypatch.setattr(module, "selected_cli", selected_cli)
    rendered = config_staging.render_git_configuration([paths], revision="a" * 40)
    assert rendered.revision == "a" * 40
    assert not (case.root / "state/config-plans").exists()
    assert not paths.bot_runtime("primary-worker").exists()
    snapshot = active_config.snapshot_from_plan(rendered, installation, "a" * 40)
    units = snapshot["units"]
    assert units and all("release_id" not in unit for unit in units)
    worker = next(unit for unit in units if unit["bot"] == "primary-worker" and unit["platform"] == "Linux")
    assert worker["admission"]["command"] == [str(installation.native / "start-bot.sh"),
                                               str(paths.bot_runtime("primary-worker"))]
    assert worker["environment"]["CLAUDLOBBY_CLI"] == str(installation.cli)
    assert worker["working_directory"] == str(paths.bot_runtime("primary-worker"))
    service = rendered.blob(worker["sha256"]).decode()
    assert f'ExecStart="{installation.cli}" "--root" "{case.root}" "_native" "--unit"' in service
    assert {unit["platform"] for unit in units} == {"Linux", "Darwin"}
    assert str(case.package.native) not in json.dumps(units)
    # Publishing configuration does not enroll, launch or grant authority.
    active_config.publish_current_config(snapshot, installation=installation, expected_previous=None)
    corrupt = json.loads(json.dumps(snapshot))
    native = next(unit for unit in corrupt["units"] if unit["admission"] is not None)
    native["admission"]["command"] = ["/bin/false"]
    corrupt["fingerprint"] = active_config._fingerprint(corrupt)
    with pytest.raises(PlanError, match="native command"):
        active_config.publish_current_config(corrupt, installation=installation,
                                             expected_previous=snapshot["fingerprint"])
    with pytest.raises(PlanError):
        config_units.current_git_declarations(snapshot, "Linux")  # exact generated bytes are not published yet
    for unit in units:
        source = Path(unit["source"])
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(rendered.blob(unit["sha256"]))
        source.chmod(unit["mode"])
    declarations = config_units.current_git_declarations(snapshot, "Linux")
    assert len(declarations) == sum(unit["platform"] == "Linux" for unit in units)
    assert all(declaration.release_id == "" for declaration in declarations)
    Path(worker["source"]).write_text("changed native command\n")
    with pytest.raises(PlanError, match="current generated unit"):
        config_units.current_git_declarations(snapshot, "Linux")
