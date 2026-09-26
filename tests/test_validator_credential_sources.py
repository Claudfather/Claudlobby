"""Parsed public-path coverage for credential source diagnostics (#1817)."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from claudlobby.commands.core import cmd_validate
from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from claudlobby.validator import validate


PLUGIN_WARNING = (
    "plugins.include_defaults is false — default plugins (claudna) will not be installed"
)
REGISTRY = "cli:gh-token, literal, mint:github-app"
SOURCE_ERRORS = [
    "bot 'first': credential_sources['A_TOKEN'] = 'literl' is not in the "
    f"closed source registry, one of: {REGISTRY} — did you mean 'literal'?",
    "bot 'first': credential_sources['Z_TOKEN'] = 'cli:gh-toke' is not in the "
    f"closed source registry, one of: {REGISTRY} — did you mean 'cli:gh-token'?",
]
MISSING_EXPERTISE = (
    "bot 'first': expertise 'enginering' not found in overlay or base library"
    " — did you mean 'engineering'?"
)
LATER_BOT_ERROR = (
    "bot 'later': expertise 'absent' not found in overlay or base library"
)
RESERVED_WARNING = (
    "bot 'first': credential_sources['PROBE_TOKEN'] = 'mint:github-app' is "
    "RESERVED — no boot-time resolver reads it (deliberate; App-auth mints at "
    "use time via lib/git-credential-github-app, see mcp: [github-app] and "
    "lib/mint-github-token.sh). Supply PROBE_TOKEN in a .env tier or adopt "
    "App mode; the resolver arm belongs to #252's per-bot sidecar"
)


@pytest.fixture
def credential_paths(tmp_path: Path, monkeypatch) -> Paths:
    """Only synthetic config, library, runtime and HOME; no credential probes.

    Keep the real source audit, settings builder and per-bot validation intact.
    The fixture declares no GitHub App, resolver, vault, MCP or manager, so none
    needs a host probe; a subprocess attempt is an error rather than a chance
    to read operator config or execute a credential helper.
    """
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PLANE_EMIT_DISABLED": "1",
        "PLANE_SOCKET": str(tmp_path / "no-daemon.sock"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(root)

    def unexpected_probe(*args, **kwargs):
        pytest.fail("credential validation must not launch a host/credential probe")

    monkeypatch.setattr(subprocess, "run", unexpected_probe)
    # This source-registry fixture declares no env consumers or assignments.
    # Pin the query result while retaining its ban on unrelated subprocesses;
    # test_validator_env_cascade exercises the real runtime door separately.
    monkeypatch.setattr(Paths, "env_resolved", lambda self, **kwargs: {})
    monkeypatch.setattr("claudlobby.validator.shutil.which", lambda name: None)
    (root / "library" / "expertise").mkdir(parents=True)
    (root / "library" / "expertise" / "engineering.md").write_text(
        "---\ntitle: Engineering\n---\n# Engineering\nBuild things.\n"
    )
    (root / "runtime" / "bots").mkdir(parents=True)
    return Paths(root=root)


def _write_manifest(
    paths: Paths,
    sources: dict[str, str],
    *,
    expertise: str = "engineering",
    later_bot: bool = False,
) -> None:
    bots = {
        "first": {
            "expertise": [expertise],
            "credential_sources": sources,
        }
    }
    if later_bot:
        bots["later"] = {"expertise": ["absent"]}
    paths.fleet_yaml.write_text(
        yaml.safe_dump(
            {
                "fleet": {
                    "name": "credential-diagnostics",
                    "system_defaults": False,
                    "plugins": {"include_defaults": False},
                    "bots": bots,
                }
            },
            sort_keys=False,
        )
    )


@pytest.mark.parametrize("expertise", ["engineering", "enginering"])
def test_unknown_sources_return_ordered_errors_and_continue(credential_paths, expertise):
    # Reverse insertion order deliberately: diagnostics must sort variable names.
    _write_manifest(
        credential_paths,
        {"Z_TOKEN": "cli:gh-toke", "A_TOKEN": "literl"},
        expertise=expertise,
        later_bot=True,
    )
    fleet, _ = load_fleet(credential_paths.fleet_yaml)
    report = validate(fleet, credential_paths)

    preceding = [MISSING_EXPERTISE] if expertise == "enginering" else []
    assert report.errors == [*preceding, *SOURCE_ERRORS, LATER_BOT_ERROR]
    assert report.warnings == [PLUGIN_WARNING]


def test_unknown_source_cli_returns_normal_validation_error(credential_paths, caplog):
    _write_manifest(credential_paths, {"A_TOKEN": "literl"}, later_bot=True)
    args = argparse.Namespace(root=str(credential_paths.root), fleet=None, strict=False)

    with caplog.at_level(logging.WARNING, logger="claudlobby"):
        result = cmd_validate(args)

    assert result == 1
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.ERROR, SOURCE_ERRORS[0]),
        (logging.ERROR, LATER_BOT_ERROR),
        (logging.WARNING, PLUGIN_WARNING),
    ]
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("source", ["literal", "cli:gh-token", "mint:github-app"])
def test_registered_sources_preserve_exact_diagnostics(credential_paths, source):
    _write_manifest(credential_paths, {"PROBE_TOKEN": source})
    fleet, _ = load_fleet(credential_paths.fleet_yaml)
    report = validate(fleet, credential_paths)

    assert report.errors == []
    reserved = [RESERVED_WARNING] if source == "mint:github-app" else []
    assert report.warnings == [*reserved, PLUGIN_WARNING]


def test_registered_source_preserves_earlier_suggestion(credential_paths):
    _write_manifest(
        credential_paths, {"PROBE_TOKEN": "literal"}, expertise="enginering"
    )
    fleet, _ = load_fleet(credential_paths.fleet_yaml)
    report = validate(fleet, credential_paths)

    assert report.errors == [MISSING_EXPERTISE]
    assert report.warnings == [PLUGIN_WARNING]
