"""Candidate source reads must never escape into installed runtime references."""

from dataclasses import replace
import json

import pytest

from claudlobby.composer import (
    compose_bot_conf, compose_fleet_timers, compose_launchd_plist,
    compose_settings_local, compose_systemd_unit,
)
from claudlobby.config import load_fleet
from claudlobby.context import native_environment
from claudlobby.installation import Installation
from claudlobby.paths import Paths
from tests.package_fixtures import source_package


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
