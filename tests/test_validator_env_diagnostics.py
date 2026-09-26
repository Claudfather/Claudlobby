"""Public package-check warnings never repeat an env resolver's private data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

from claudlobby import env_tiers, mcp_packages, validator
from claudlobby.config import BotConfig, FleetConfig, McpEntry, SystemDefaultsConfig
from claudlobby.paths import Paths
from tests.conftest import constructed_env

SOURCE = Path(__file__).resolve().parents[1]
FLAG = mcp_packages.PROBE_FLAG


@pytest.fixture
def private_fleet(tmp_path, monkeypatch):
    root, home = tmp_path / "source", tmp_path / "home"
    (root / "lib").mkdir(parents=True)
    (root / "library/mcp").mkdir(parents=True)
    (root / "library/expertise").mkdir(parents=True)
    home.mkdir()
    for name in ("env-tiers.sh", "lib-common.sh", "supervisor.sh", "mcp-package-grammar.py"):
        shutil.copy2(SOURCE / "lib" / name, root / "lib" / name)
    (root / "library/expertise/engineering.md").write_text("# Engineering\n")
    (root / "library/mcp/sample.json").write_text(json.dumps({
        "sample": {"command": "npx", "args": ["-y", "@example/fixture-mcp@1.2.3"]},
    }))
    env = constructed_env(HOME=str(home), TMPDIR=str(tmp_path),
        XDG_CONFIG_HOME=str(tmp_path / "xdg"), TELEGRAM_STATE_DIR=str(tmp_path / "channel"),
        CLAUDLOBBY_ROOT=str(root), PLANE_SOCKET=str(tmp_path / "absent.sock"))
    for name in list(os.environ):
        monkeypatch.delenv(name)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    real_run = subprocess.run
    def private_query(args, *a, **kw):
        assert args[:2] == ["bash", str(root / "lib/env-tiers.sh")], args
        assert kw["env"]["HOME"] == str(home)
        assert kw["env"]["CLAUDLOBBY_ROOT"] == str(root)
        assert kw["env"]["BOT_DIR"] == "" or kw["env"]["BOT_DIR"].startswith(str(root) + "/")
        return real_run(args, *a, **kw)
    monkeypatch.setattr(subprocess, "run", private_query)
    probes = []
    def no_probe(argv):
        probes.append(argv)
        pytest.fail("registry/provider subprocess must not run in this fixture")
    monkeypatch.setattr(mcp_packages, "_probe", no_probe)
    fleet = FleetConfig(name="example", service_prefix="fixture", bots={
        "one": BotConfig(bot_id="one", name="One", expertise=["engineering"],
                         channels=[], mcp=[McpEntry("sample")])},
        system_defaults=SystemDefaultsConfig(enabled=False, protocols=False))
    return fleet, Paths(root=root), probes


def query_warning(result):
    return [w for w in result.warnings if f"could not read whether {FLAG}" in w]


def assert_disclosed_without_value(result, marker, capsys, caplog):
    warnings = query_warning(result)
    assert len(warnings) == 1
    # Keep failure loud and actionable without forwarding opaque exception data.
    assert "registry check did NOT run" in warnings[0]
    assert "declared packages resolve" in warnings[0]
    captured = capsys.readouterr()
    assert marker not in repr(result) + captured.out + captured.err + caplog.text
    assert "lib/env-tiers.sh" in warnings[0]
    assert "dependencies" in warnings[0]
    assert not result.has_errors


@pytest.mark.parametrize("failure", ["stderr", "malformed-row", "os-error"])
def test_real_resolver_failures_do_not_disclose_values(private_fleet, failure, monkeypatch, capsys, caplog):
    fleet, paths, probes = private_fleet
    marker = "SYNTHETIC_PRIVATE_" + failure.upper().replace("-", "_")
    door = paths.lib / "env-tiers.sh"
    if failure == "stderr":
        door.write_text("#!/bin/bash\nprintf '%s' " + shlex.quote("TOKEN=" + marker) + " >&2\nexit 3\n")
    elif failure == "malformed-row":
        door.write_text("#!/bin/bash\nprintf '%s\\n' " + shlex.quote("TOKEN=" + marker) + "\n")
    else:
        # The real read_tiers catch wraps an OS failure's opaque text.
        def failed_launch(*a, **kw):
            raise OSError(marker)
        monkeypatch.setattr(subprocess, "run", failed_launch)
    result = validator.validate(fleet, paths)
    assert_disclosed_without_value(result, marker, capsys, caplog)
    assert probes == []


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_other_resolution_errors_still_warn_without_raw_text(private_fleet, error_type, monkeypatch, capsys, caplog):
    fleet, paths, probes = private_fleet
    marker = "SYNTHETIC_PRIVATE_" + error_type.__name__
    original = env_tiers.resolve
    def failure(paths, **kwargs):
        # Own only the package rung; leave per-bot callers unaffected when
        # this fix is combined with the separate #1661 consumer change.
        if "fleet_name" in kwargs:
            raise error_type(marker)
        return original(paths, **kwargs)
    monkeypatch.setattr(env_tiers, "resolve", failure)
    result = validator.validate(fleet, paths)
    assert_disclosed_without_value(result, marker, capsys, caplog)
    assert probes == []


@pytest.mark.parametrize("failure", ["missing", "empty-output"])
def test_missing_or_empty_resolver_remains_unknown(private_fleet, failure):
    fleet, paths, probes = private_fleet
    door = paths.lib / "env-tiers.sh"
    if failure == "missing":
        door.unlink()
    else:
        door.write_text("#!/bin/bash\nexit 0\n")
    result = validator.validate(fleet, paths)
    warnings = query_warning(result)
    assert len(warnings) == 1
    assert "registry check did NOT run" in warnings[0]
    assert "declared packages resolve" in warnings[0]
    assert probes == []
    assert not result.has_errors


@pytest.mark.parametrize("setting", [None, "0", ""])
def test_successfully_disarmed_query_has_no_failure_warning(private_fleet, setting):
    fleet, paths, probes = private_fleet
    if setting is not None:
        (paths.root / ".env").write_text(f"{FLAG}={setting}\n")
    result = validator.validate(fleet, paths)
    assert query_warning(result) == []
    assert probes == []
    assert not result.has_errors


def test_successfully_armed_query_keeps_original_probe_result(private_fleet, monkeypatch):
    fleet, paths, probes = private_fleet
    (paths.root / ".env").write_text(f"{FLAG}=1\n")
    def synthetic_probe(argv):
        probes.append(argv)
        return "missing", "synthetic package absence"
    monkeypatch.setattr(mcp_packages, "_probe", synthetic_probe)
    result = validator.validate(fleet, paths)
    assert query_warning(result) == []
    assert probes == [["npx", "-y", "@example/fixture-mcp@1.2.3", "--help"]]
    assert any("synthetic package absence" in w for w in result.warnings)
    assert not result.has_errors


def test_grammar_unavailability_retains_its_separate_warning(private_fleet):
    fleet, paths, probes = private_fleet
    (paths.lib / "mcp-package-grammar.py").unlink()
    result = validator.validate(fleet, paths)
    assert query_warning(result) == []
    assert any("shared package grammar" in w and "UNKNOWN" in w for w in result.warnings)
    assert probes == []
