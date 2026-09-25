"""Raw manifest key diagnostics and the strict CLI no-write boundary."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil
import json

import pytest
import yaml

from claudlobby.__main__ import main
from claudlobby.config import DEFAULT_PLUGINS, load_fleet
from claudlobby.paths import Paths
from claudlobby.validator import validate

REPO = Path(__file__).resolve().parent.parent
SECRET = "must-never-appear-in-a-diagnostic"


@pytest.fixture
def manifest(fleet_dir, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    plugin_dir = home / ".claude" / "plugins"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "installed_plugins.json").write_text(
        json.dumps({"plugins": {name: [] for name in DEFAULT_PLUGINS}})
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(fleet_dir)
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    monkeypatch.setenv("PLANE_SOCKET", str(tmp_path / "absent.sock"))
    monkeypatch.setenv("TELEGRAM_STATE_DIR", str(tmp_path / "channel"))
    # The shared fixture creates empty runtime directories; start with none so
    # strict generation must prove it did not create even its output root.
    (fleet_dir / "runtime" / "bots").rmdir()
    (fleet_dir / "runtime").rmdir()
    doc = {"fleet": {
        "name": "diagnostic-fixture", "system_defaults": False,
        "bots": {"probe": {"expertise": ["software-engineering"], "channels": []}},
    }}
    def write(value=doc):
        (fleet_dir / "fleet.yaml").write_text(yaml.safe_dump(value, sort_keys=False))
        return load_fleet(fleet_dir / "fleet.yaml")[0]
    return fleet_dir, doc, write


def _unknown(fleet):
    return getattr(fleet, "config_warnings", [])


def _put_typo(doc, scope):
    if scope == "document":
        doc["fleets"] = SECRET
        return "fleets"
    if scope == "fleet":
        doc["fleet"]["service_prefx"] = SECRET
        return "fleet.service_prefx"
    if scope == "defaults":
        doc["fleet"]["defaults"] = {"permission_modes": SECRET}
        return "fleet.defaults.permission_modes"
    if scope == "system_defaults":
        doc["fleet"]["system_defaults"] = {"enabled": False, "timerrs": SECRET}
        return "fleet.system_defaults.timerrs"
    doc["fleet"]["bots"]["probe"]["permission_modes"] = SECRET
    return "fleet.bots.probe.permission_modes"


@pytest.mark.parametrize("scope", ["document", "fleet", "defaults", "system_defaults", "bot"])
def test_raw_key_warning_names_source_and_never_value(manifest, scope):
    root, doc, write = manifest
    path = _put_typo(doc, scope)
    fleet = write()
    warnings = _unknown(fleet)
    assert len(warnings) == 1
    assert path in warnings[0]
    assert "unknown key" in warnings[0] and "ignored" in warnings[0]
    assert SECRET not in warnings[0]
    if scope in {"defaults", "bot"}:
        assert "did you mean 'permission_mode'" in warnings[0]
    assert fleet.bots["probe"].permission_mode is None
    assert not (root / "runtime").exists()


def test_default_warning_is_not_repeated_per_inheriting_bot_or_validation(manifest):
    root, doc, write = manifest
    _put_typo(doc, "defaults")
    doc["fleet"]["bots"]["second"] = deepcopy(doc["fleet"]["bots"]["probe"])
    fleet = write()
    for _ in range(2):
        report = validate(fleet, Paths(root=root))
        unknown = [w for w in report.warnings if "unknown key" in w]
        assert unknown == _unknown(fleet)
        assert len(unknown) == 1
    assert len(_unknown(fleet)) == 1


@pytest.mark.parametrize("scope", ["document", "fleet", "defaults", "system_defaults", "bot"])
@pytest.mark.parametrize("command", ["validate", "generate"])
def test_strict_cli_refuses_before_any_composition_write(manifest, monkeypatch, caplog, scope, command):
    root, doc, write = manifest
    baseline = write()
    report = validate(baseline, Paths(root=root))
    assert report.errors == [] and report.warnings == [], report
    _put_typo(doc, scope)
    write()
    # Trap the first composition call as well as comparing every fixture byte.
    # On the unfixed parent this reaches the trap, proving refusal is the change.
    def no_compose(*args, **kwargs):
        pytest.fail("strict unknown-key validation reached composition")
    monkeypatch.setattr("claudlobby.commands.core.compose_fleet", no_compose)
    monkeypatch.setattr("claudlobby.commands.core.compose_bot", no_compose)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert main(["--root", str(root), command, "--strict"]) == 1
    assert "unknown key" in caplog.text
    assert SECRET not in caplog.text
    assert not (root / "runtime").exists()
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_normal_validation_warns_without_applying_or_renaming_key(manifest, caplog):
    root, doc, write = manifest
    _put_typo(doc, "bot")
    fleet = write()
    assert main(["--root", str(root), "validate"]) == 0
    assert "fleet.bots.probe.permission_modes" in caplog.text
    assert "ignored" in caplog.text and SECRET not in caplog.text
    assert fleet.bots["probe"].permission_mode is None


def test_aliases_and_extension_namespaces_remain_supported(manifest):
    _, doc, write = manifest
    bot = doc["fleet"]["bots"]["probe"]
    bot.pop("expertise")
    bot.update({
        "persona": "software-engineering", "brief": {"on_start": False},
        "env": {"PERMISSION_MODES": "extension-value"},
        "scope": {"custom_catalog": {"permission_modes": "extension"}},
        "model_strategy": {"custom_model_rule": "extension"},
        "hooks": {"PreToolUse": [{"command": "true", "custom_payload": "extension"}]},
        "mcp": [{"github": {"instances": [{"personal": {"custom_port": 42}}]}}],
        "tools": [{"fixture": {"params": {"permission_modes": "extension"}}}],
        "mounts": {"arbitrary-name": "/fixture/path"},
        "secret_files": {"CUSTOM_TOKEN": "private/fixture"},
    })
    doc["fleet"].update({
        "accounts": {"custom-account": "~/private-config"},
        "plugins": {"required": [], "marketplaces": {"custom": {"source": "fixture"}}},
        "defaults": {"env": {"CUSTOM_DEFAULT": "kept-as-raw-default"},
                     "jobs": {"custom-job": {"enabled": False}}, "brief": {"on_start": False}},
    })
    fleet = write()
    assert _unknown(fleet) == []
    assert fleet.bots["probe"].expertise == ["software-engineering"]
    assert fleet.bots["probe"].brief_on_start is False
    assert fleet.bots["probe"].scope.raw["custom_catalog"] == {"permission_modes": "extension"}
    assert fleet.bots["probe"].model_strategy.raw["custom_model_rule"] == "extension"
    assert fleet.defaults["env"] == {"CUSTOM_DEFAULT": "kept-as-raw-default"}
    assert fleet.bots["probe"].env == {"PERMISSION_MODES": "extension-value"}


@pytest.mark.parametrize("name", ["fleet.yaml.example", "fleet.yaml.seed"])
def test_shipped_manifest_copies_have_no_unknown_keys(tmp_path, name):
    copied = tmp_path / name
    shutil.copyfile(REPO / name, copied)
    fleet, _ = load_fleet(copied)
    assert _unknown(fleet) == []


def test_explicit_enum_error_is_still_an_error(manifest):
    _, doc, write = manifest
    doc["fleet"]["bots"]["probe"]["permission_mode"] = "not-a-mode"
    with pytest.raises(ValueError, match="permission_mode"):
        write()


def test_raw_host_consumer_key_is_recognized_and_still_applied(manifest):
    root, doc, write = manifest
    doc["fleet"]["github"] = {"mention_allowlist": ["fixture-human"]}
    fleet = write()
    assert _unknown(fleet) == []
    overlay = root / "local" / "fixture"
    overlay.mkdir(parents=True)
    shutil.copyfile(root / "fleet.yaml", overlay / "fleet.yaml")
    from claudlobby.composer import compose_host_mention_allowlist
    result = compose_host_mention_allowlist(Paths(root=root), output_dir=root / "private-output")
    assert result.read_text() == "fixture-human\n"


def test_internal_attribute_name_is_not_a_supported_source_alias(manifest):
    _, doc, write = manifest
    doc["fleet"]["bots"]["probe"]["brief_on_start"] = True
    fleet = write()
    assert len(_unknown(fleet)) == 1
    assert "fleet.bots.probe.brief_on_start" in _unknown(fleet)[0]
    assert fleet.bots["probe"].brief_on_start is False


def test_non_string_unknown_key_is_reported_without_value(manifest):
    _, doc, write = manifest
    doc["fleet"]["defaults"] = {42: {"secret": SECRET}}
    fleet = write()
    assert len(_unknown(fleet)) == 1
    assert "fleet.defaults.42" in _unknown(fleet)[0]
    assert SECRET not in _unknown(fleet)[0]
