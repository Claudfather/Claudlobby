"""Per-bot validator consumers use the runtime tier door, never live host state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from claudlobby import validator
from claudlobby.config import (
    BotConfig, FleetConfig, GithubAppConfig, McpEntry, SystemDefaultsConfig,
    TelegramConfig, ToolEntry, load_fleet,
)
from claudlobby.paths import Paths
from tests.conftest import constructed_env

SOURCE = Path(__file__).resolve().parents[1]
KEYS = ("SAMPLE_TOKEN", "TOOL_TOKEN", "TG_TOKEN", "PAT", "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH")


@pytest.fixture
def estate(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "source"
    home.mkdir()
    root.mkdir()
    fleet_dir = root / "local" / "example"
    fleet_dir.mkdir(parents=True)
    paths = Paths(root=root, fleet_dir=fleet_dir)
    lib = root / "lib"
    lib.mkdir()
    for name in ("env-tiers.sh", "lib-common.sh", "supervisor.sh"):
        shutil.copy2(SOURCE / "lib" / name, lib / name)
    (root / "library" / "mcp").mkdir(parents=True)
    (root / "library" / "mcp" / "sample.json").write_text(json.dumps({
        "sample": {"command": "fixture-no-server", "env": {"TOKEN": "${SAMPLE_TOKEN}"}},
        "_env_contract": {"SAMPLE_TOKEN": {"secret": True}},
    }))
    tool = root / "library" / "tools" / "sample-tool"
    tool.mkdir(parents=True)
    (tool / "tool.yaml").write_text("type: script\nenv: [TOOL_TOKEN]\n")
    (tool / "sample.sh.j2").write_text("#!/bin/sh\nexit 0\n")
    bot = BotConfig(bot_id="one", name="One", expertise=["absent-expertise"],
                    channels=[], mcp=[McpEntry("sample")], tools=[ToolEntry("sample-tool")],
                    telegram=TelegramConfig(token_env="TG_TOKEN"),
                    git_credentials={"Example": "PAT"}, github_app=GithubAppConfig())
    fleet = FleetConfig(name="example", service_prefix="fixture", bots={"one": bot},
                        system_defaults=SystemDefaultsConfig(enabled=False, protocols=False))
    # Construct a process environment rather than subtracting possible secrets.
    env = constructed_env(HOME=str(home), CLAUDLOBBY_ROOT=str(root),
                          PLANE_SOCKET=str(tmp_path / "absent.sock"),
                          TELEGRAM_STATE_DIR=str(tmp_path / "channel"),
                          XDG_CONFIG_HOME=str(tmp_path / "xdg"))
    for name in list(os.environ):
        monkeypatch.delenv(name)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(validator, "_operator_git_identity_problem", lambda: None)
    monkeypatch.setattr(validator, "_operator_reverse_insteadof", lambda: None)
    # Only the real, fixture-owned tier door may launch a process.
    real_run = subprocess.run
    def isolated_run(args, *a, **kw):
        assert args[:2] in (["bash", str(lib / "env-tiers.sh")],
                             ["bash", str(root / "runtime-probe.sh")]), args
        assert kw["env"]["HOME"] == str(home)
        assert kw["env"]["CLAUDLOBBY_ROOT"] == str(root)
        assert kw["env"]["BOT_DIR"].startswith(str(root) + "/")
        return real_run(args, *a, **kw)
    monkeypatch.setattr(subprocess, "run", isolated_run)
    monkeypatch.setattr(validator, "_validate_mcp_packages", lambda *a: None)
    captured = {}
    real_mcp = validator._validate_bot_mcp
    def capture(bot_name, bot, paths, effective_env, *args):
        captured[bot_name] = effective_env
        return real_mcp(bot_name, bot, paths, effective_env, *args)
    monkeypatch.setattr(validator, "_validate_bot_mcp", capture)
    return home, paths, fleet, captured


def assign(estate, tier, value, *, bot="one", keys=KEYS):
    home, paths, _, _ = estate
    target = {"host": home / ".env", "root": paths.root / ".env",
              "fleet": paths.fleet_dir / ".env", "bot": paths.bot_runtime(bot) / ".env"}[tier]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(f"{key}={value}\n" for key in keys))


def report(estate, *, unavailable=False):
    _, paths, fleet, _ = estate
    result = validator.validate(fleet, paths)
    if not unavailable:
        assert all(value is not None for value in estate[3].values())
        assert not any("environment checks unavailable" in w for w in result.warnings)
    return result


def env_warnings(result):
    return [w for w in result.warnings if any(s in w for s in (
        "requires SAMPLE_TOKEN", "requires TOOL_TOKEN", "telegram.token_env", "names 'PAT'",
        "routing requires GITHUB_APP_", "GH_TOKEN is set", "GITHUB_TOKEN is set"))]


@pytest.mark.parametrize("tier", ["host", "root", "fleet", "bot"])
def test_each_runtime_tier_satisfies_all_actual_consumers(estate, tier):
    assign(estate, tier, "synthetic-present")
    # Existing fleet file must not hide a root or host assignment.
    if tier in {"host", "root"}:
        (estate[1].fleet_dir / ".env").write_text("UNRELATED=1\n")
    assert env_warnings(report(estate)) == []
    assert estate[3]["one"]["SAMPLE_TOKEN"] == "synthetic-present"


@pytest.mark.parametrize("specific", ["root", "fleet", "bot"])
@pytest.mark.parametrize("value", ["", "synthetic-more-specific"])
def test_later_assignment_wins_even_when_empty(estate, monkeypatch, specific, value):
    monkeypatch.setenv("SAMPLE_TOKEN", "synthetic-ambient")
    assign(estate, "host", "synthetic-host")
    if specific != "root":
        assign(estate, "root", "synthetic-root")
    if specific == "bot":
        assign(estate, "fleet", "synthetic-fleet")
    assign(estate, specific, value)
    result = report(estate)
    assert estate[3]["one"]["SAMPLE_TOKEN"] == value
    assert bool(env_warnings(result)) == (value == "")
    if value == "":
        assert any("SET BUT EMPTY" in w for w in env_warnings(result))


@pytest.mark.parametrize("ambient,tier,expected", [
    ("", "synthetic-root", "synthetic-root"),
    ("synthetic-ambient", "", ""),
    ("synthetic-ambient", None, "synthetic-ambient"),
    ("", None, ""),
])
def test_ambient_is_fallback_only_when_no_tier_assigns(estate, monkeypatch, ambient, tier, expected):
    monkeypatch.setenv("SAMPLE_TOKEN", ambient)
    if tier is not None:
        assign(estate, "root", tier, keys=["SAMPLE_TOKEN"])
        (estate[1].fleet_dir / ".env").write_text("UNRELATED=1\n")
    result = report(estate)
    assert estate[3]["one"]["SAMPLE_TOKEN"] == expected
    assert any("requires SAMPLE_TOKEN" in w for w in result.warnings) == (expected == "")


@pytest.mark.parametrize("value", ["synthetic-bot-conf", "", "0", "false"])
def test_explicit_bot_env_is_last_assignment_matching_composer_stringification(estate, value):
    assign(estate, "bot", "synthetic-tier")
    estate[2].bots["one"].env.update({key: value for key in KEYS})
    result = report(estate)
    assert estate[3]["one"]["SAMPLE_TOKEN"] == str(value)
    assert bool(env_warnings(result)) == (str(value) == "")


def test_other_bot_and_callers_bot_dir_never_supply_values(estate, monkeypatch):
    _, paths, fleet, captured = estate
    fleet.bots["two"] = BotConfig(bot_id="two", name="Two", expertise=["absent"],
                                  channels=[], mcp=[McpEntry("sample")])
    assign(estate, "bot", "synthetic-one")
    assign(estate, "bot", "", bot="two")
    monkeypatch.setenv("BOT_DIR", str(paths.bot_runtime("one")))
    result = report(estate)
    assert captured["one"]["SAMPLE_TOKEN"] == "synthetic-one"
    assert captured["two"]["SAMPLE_TOKEN"] == ""
    assert any("bot 'two'" in w and "SET BUT EMPTY" in w for w in result.warnings)
    assert not any("bot 'one'" in w and "requires SAMPLE_TOKEN" in w for w in result.warnings)


def test_nested_fleet_uses_runtime_resolver(estate):
    _, paths, _, _ = estate
    nested = paths.root / "local" / "group" / "example"
    paths.fleet_dir.rename(nested.parent)  # group initially contains the fleet contents
    nested.mkdir()
    estate = (estate[0], Paths(root=paths.root, fleet_dir=nested), estate[2], estate[3])
    # Runtime nested discovery uses fleet.yaml as the ownership marker.
    (nested / "fleet.yaml").write_text("fleet: {name: example}\n")
    assign(estate, "fleet", "synthetic-nested")
    assert env_warnings(report(estate)) == []


def test_root_only_install_uses_root_and_bot_tiers(estate):
    estate = (estate[0], Paths(root=estate[1].root), estate[2], estate[3])
    (estate[1].root / ".env").write_text("".join(f"{k}=synthetic-root\n" for k in KEYS))
    assert env_warnings(report(estate)) == []


def test_no_fleet_or_default_env_merge_is_invented(estate):
    _, paths, _, captured = estate
    (paths.fleet_dir / "fleet.yaml").write_text("""fleet:
  name: example
  service_prefix: fixture
  env: {SAMPLE_TOKEN: unsupported-fleet-value}
  defaults:
    env: {SAMPLE_TOKEN: unsupported-default-value}
  bots:
    one:
      expertise: [absent]
      channels: []
      mcp: [sample]
""")
    fleet, _ = load_fleet(paths.fleet_dir / "fleet.yaml")
    assert fleet.bots["one"].env == {}
    validator.validate(fleet, paths)
    assert "SAMPLE_TOKEN" not in captured["one"]


@pytest.mark.parametrize("failure", ["missing", "nonzero", "malformed"])
def test_resolver_refusal_is_value_free_unknown_and_other_checks_continue(estate, failure):
    _, paths, fleet, captured = estate
    assign(estate, "fleet", "synthetic-private-sentinel")
    door = paths.lib / "env-tiers.sh"
    if failure == "missing":
        door.unlink()
    else:
        door.write_text("#!/bin/bash\n" + (
            "printf 'synthetic-private-sentinel' >&2; exit 3\n" if failure == "nonzero"
            else "printf 'synthetic-private-sentinel\\n'\n"))
    # Static App/source/tool defects must not disappear with the env lookup.
    fleet.bots["one"].github_app.slug = "sample-app"
    fleet.bots["one"].tools.append(ToolEntry("missing-tool"))
    result = report(estate, unavailable=True)
    assert captured["one"] is None
    assert env_warnings(result) == []
    assert sum("environment checks unavailable" in w for w in result.warnings) == 1
    assert any("declares slug without bot_user_id" in w for w in result.warnings)
    assert any("missing-tool" in e for e in result.errors)
    assert any("absent-expertise" in e for e in result.errors)
    assert "synthetic-private-sentinel" not in repr(result)


def test_absent_and_empty_are_distinct_and_values_are_never_reported(estate):
    first = report(estate)
    assert any("no .env tier sets it" in w for w in first.warnings)
    assign(estate, "host", "synthetic-private-sentinel")
    assign(estate, "bot", "", keys=["SAMPLE_TOKEN"])
    second = report(estate)
    assert any("SET BUT EMPTY" in w for w in second.warnings)
    assert "synthetic-private-sentinel" not in repr(second)


def test_git_shadow_uses_cascade_with_bot_override(estate, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "synthetic-ambient")
    assign(estate, "bot", "", keys=["GH_TOKEN"])
    assert not any("GH_TOKEN is set" in w for w in report(estate).warnings)
    estate[2].bots["one"].env["GH_TOKEN"] = "synthetic-bot"
    assert any("GH_TOKEN is set" in w for w in report(estate).warnings)


@pytest.mark.parametrize("bot_value", [None, "synthetic-config", ""])
def test_presence_matches_native_bash_tiers_then_actual_composed_bot_conf(estate, bot_value):
    from claudlobby.composer import compose_bot_conf

    _, paths, fleet, captured = estate
    for tier in ("host", "root", "fleet", "bot"):
        assign(estate, tier, "synthetic-" + tier)
    if bot_value is not None:
        fleet.bots["one"].env["SAMPLE_TOKEN"] = bot_value
    conf = paths.bot_runtime("one") / "bot.conf"
    # compose_bot_conf stays a pure renderer here; cascade suppresses its
    # unrelated fleet-scoped resolver call. No bot is generated or launched.
    conf.write_text(compose_bot_conf(fleet.bots["one"], fleet, paths, cascade={}))
    script = paths.root / "runtime-probe.sh"
    script.write_text('set -eu\n. "$CLAUDLOBBY_ROOT/lib/lib-common.sh"\n'
                      'source_env_tiered "$BOT_DIR" "$FLEET_NAME"\n'
                      '. "$BOT_DIR/bot.conf"\n'
                      'printf "%s" "$SAMPLE_TOKEN"\n')
    result = subprocess.run(["bash", str(script)], text=True, capture_output=True,
        env=constructed_env(HOME=str(estate[0]), CLAUDLOBBY_ROOT=str(paths.root),
                            BOT_DIR=str(paths.bot_runtime("one")), FLEET_NAME="example"))
    assert result.returncode == 0, result.stderr
    report(estate)
    assert captured["one"]["SAMPLE_TOKEN"] == result.stdout
