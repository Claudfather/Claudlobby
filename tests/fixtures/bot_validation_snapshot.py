"""Deterministic public-validator characterization; no fleet or credential access.

Run each source revision in a fresh process against the same scratch path. Only
unrelated network/ignition probes and operator host facts are pinned. Library,
overlay, effective equipment, source/grant, dotenv and vault marker readers stay
real. The fixture refuses subprocesses and checks its complete file inventory.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import patch


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _document(path: Path, **metadata) -> None:
    import yaml
    _write(path, "---\n" + yaml.safe_dump({"title": path.stem, **metadata}) + "---\nFixture.\n")


def _inventory(root: Path) -> dict:
    return {
        str(path.relative_to(root)): (
            "directory" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mode,
        )
        for path in sorted(root.rglob("*"))
    }


def _setup(root: Path):
    from claudlobby.paths import Paths
    base, overlay = root / "source", root / "source/local/sample"
    paths = Paths(root=base, fleet_dir=overlay)
    _document(base / "library/expertise/engineering.md")
    _document(base / "library/expertise/software-engineering.md")
    _document(base / "library/expertise/collision.md", permissions={"allow": ["Bash"]})
    _document(overlay / "library/expertise/collision.md", permissions={"allow": ["Read"]})
    _document(base / "library/expertise/risky.md", permissions={
        "allow": ["Bash", "Bash(*)", "MadeUpTool", "Read(/fixture/input/**)"],
        "deny": ["Write(/fixture/output/**)"], "allow_all": True,
    })
    _document(base / "library/guardrails/guard.md", permissions={"allow": ["Bash"], "deny": ["MadeUpTool"]})
    _document(base / "library/skills/bundle/one/SKILL.md", tool_grants=["Bash"])
    _document(overlay / "library/skills/bundle/one/SKILL.md", tool_grants=["Read"])
    _document(base / "library/skills/required-skill/SKILL.md", tool_grants=["Bash(*)"])
    _document(base / "library/skills/checkin/SKILL.md")
    _document(base / "library/skills/risky-skill/SKILL.md", tool_grants=["Bash", "Read(/fixture/skill/**)"])
    _document(base / "library/protocols/required.md", requires={"skills": ["required-skill"]})
    _document(base / "library/protocols/checkin.md", requires={"skills": ["checkin"]})
    _document(base / "library/integrations/bundle/one.md", tool_grants=["Bash"])
    _document(overlay / "library/integrations/bundle/one.md", tool_grants=["Read"])
    _document(base / "library/integrations/sample.md")
    _document(base / "library/integrations/risky-integration.md", tool_grants=["Bash", "Read(/fixture/integration/**)"])
    _write(base / "library/mcp/sample.json", json.dumps({
        "sample": {"command": "fixture-mcp", "env": {"TOKEN": "${TOKEN}", "LABEL": "${LABEL}"}},
        "_env_contract": {
            "TOKEN": {"description": "Fixture token", "default_tier": "bot", "scope": "instance", "secret": True},
            "LABEL": {"description": "Fixture label", "default_tier": "fleet", "secret": False},
        },
        "_permissions_contract": {"tools": ["read"]},
    }))
    for name, target in (("one", "same.sh"), ("two", "same.sh"), ("gh-tool", "gh")):
        _write(base / f"library/tools/{name}/tool.yaml", "type: script\nenv: [TOOL_TOKEN]\n")
        _write(base / f"library/tools/{name}/{target}.j2", "#!/bin/sh\nexit 0\n")
    _write(base / "library/tools/parameter/tool.yaml", "type: script\nparams:\n  required:\n    required: true\n")
    _write(base / "library/tools/parameter/parameter.sh.j2", "#!/bin/sh\nexit 0\n")
    _write(paths.env_file, "")
    (root / "home").mkdir()
    (root / "vault/shared").mkdir(parents=True)
    (root / "vault-two/_shared").mkdir(parents=True)
    (root / "markerless").mkdir()
    return paths


def _cases(root: Path):
    from claudlobby.config import (
        AutonomousRunnerBypass, AutonomousRunnerConfig, AutonomousRunnerPicker,
        BotConfig, BriefingConfig, GithubAppConfig, McpEntry, ModelStrategyConfig,
        ObservabilityConfig, TelegramConfig, ToolEntry, ToolPermissionsConfig,
    )
    def bot(name="first", **kwargs):
        return BotConfig(bot_id=name, name=name, expertise=kwargs.pop("expertise", ["engineering"]),
                         channels=[], remote_control=False, bench=name == "first", **kwargs)
    dense = bot(
        expertise=["risky", "software-engineering", "enginering"], voice="missing-voice", skills=["bundle/", "risky-skill", "empty/", "missing-skill"],
        mcp=[McpEntry("sample", ["work"]), McpEntry("sampl")], integrations=["bundle/", "risky-integration", "empty/", "missing-integration"],
        credential_sources={"Z_TOKEN": "literl", "A_TOKEN": "mint:github-app"},
        guardrails=["guard", "missing-guard"], protocols=["required", "checkin", "missing-protocol"],
        resources=["missing-resource"], lessons=["empty/"], post_actions=["missing-action"],
        tools=[ToolEntry("missing-tool"), ToolEntry("one"), ToolEntry("two"), ToolEntry("gh-tool"), ToolEntry("parameter")],
        telegram=TelegramConfig(token_env="TG_TOKEN"), git_credentials={"Zulu": "Z_PAT", "Alpha": "A_PAT"},
        github_app=GithubAppConfig(slug="fixture-app"),
        observability=ObservabilityConfig(pulse_interval=0, bridge_heal_max_attempts=11, retired=("reap_days",)),
        model="opuss", model_strategy=ModelStrategyConfig(base="sonnett", escalate_to="mystery"),
        hooks={"SessionStars": [{"command": "true"}]},
        env={"DISABLE_TELEMETRY": "1", "GITHUB_APP_ID": "fixture", "FOREIGN_PATH": "/fixture/foreign"},
        extra_flags=["--remote-control"], claudron_session_loop=True, account="missing-account",
        tool_permissions=ToolPermissionsConfig(allow=["Write", "Read(/fixture/input/**)"], deny=["Write"]),
        autonomous_runner=AutonomousRunnerConfig(
            skill="unknown", cadence="tomorrow", target_repo="invalid", picker=AutonomousRunnerPicker(),
            bypass=AutonomousRunnerBypass(on_bypass="unknown"), on_outcome={"unknown": "unknown"},
            pre_hooks=["echo fixture"], post_hooks=["/wrong:skill"],
        ),
    )
    yield "minimal", [bot()], {}, "", {}, False
    yield "dense", [dense, bot("later", expertise=[], voice="later-voice",
                                briefing=BriefingConfig(slots={"morning": "*-*-* 08:00:00"}),
                                observability=ObservabilityConfig(pulse_interval=3601, bridge_heal_max_attempts=0))], {
        "GH_TOKEN": "fixture-process", "GITHUB_TOKEN": "fixture-process",
    }, "", {}, False
    yield "overlay", [bot(expertise=["collision"], skills=["bundle/"], integrations=["bundle/"], protocols=["required"])], {}, "", {}, False
    for variant, process, fleet_env, bot_env in (
        ("absent", {}, "", {}),
        ("process", {"SAMPLE_WORK_TOKEN": "fixture-process", "LABEL": "fixture-label", "TOOL_TOKEN": "fixture-process"}, "", {}),
        ("fleet-empty", {"SAMPLE_WORK_TOKEN": "fixture-process"}, "SAMPLE_WORK_TOKEN=\n", {}),
        ("bot-empty", {"SAMPLE_WORK_TOKEN": "fixture-process"}, "SAMPLE_WORK_TOKEN=fixture-fleet\n", {"first": "SAMPLE_WORK_TOKEN=\n"}),
        ("bot-present", {}, "SAMPLE_WORK_TOKEN=\n", {"first": "SAMPLE_WORK_TOKEN=fixture-bot\nLABEL=fixture-label\nTOOL_TOKEN=fixture-tool\n"}),
    ):
        yield "env-" + variant, [bot(mcp=[McpEntry("sample", ["work"])], tools=[ToolEntry("one")])], process, fleet_env, bot_env, False
    for name, locations, cli in (
        ("shared-vault", ["vault", "vault"], True),
        ("distinct-vaults", ["vault", "vault-two"], True),
        ("invalid-vaults", ["markerless", "missing"], False),
    ):
        yield name, [bot("first" if i == 0 else "later", claudron_vault_path=str(root / loc),
                         claudron_session_loop=False) for i, loc in enumerate(locations)], {}, "", {}, cli
    yield "credentials", [bot(git_credentials={"Example": "PAT"}, telegram=TelegramConfig(token_env="TELEGRAM_BOT_TOKEN")),
                           bot("later", git_credentials={"Example": "PAT"})], {}, "PAT=fixture-pat\n", {}, False
    yield "roles", [bot(manages=["lead"], protocols=["checkin"]), bot("lead", manages=["worker"], protocols=["checkin"]),
                     bot("worker", protocols=["checkin"])], {}, "", {}, False
    yield "settings-value-error", [bot()], {}, "", {}, False
    yield "settings-runtime-error", [bot()], {}, "", {}, False


def snapshot(source: Path, fixture_root: Path) -> dict:
    from claudlobby import composer, paths as paths_module, validator
    from claudlobby.config import FleetConfig, SystemDefaultsConfig
    for module in (validator, composer, paths_module):
        assert Path(module.__file__).resolve().is_relative_to(source.resolve()), module.__file__
    owner = fixture_root / ".bot-validation-fixture"
    if (fixture_root / "case").exists() and not owner.is_file():
        raise ValueError("refusing to replace an unowned fixture case directory")
    fixture_root.mkdir(parents=True, exist_ok=True)
    owner.write_text("synthetic bot-validation fixture\n")
    results = {}
    for name, bots, process, fleet_env, bot_env, cli_present in _cases(fixture_root):
        # This directory belongs solely to this fixture. Recreate each case at
        # the identical path so path-bearing diagnostics remain comparable.
        case_root = fixture_root / "case"
        if case_root.exists():
            shutil.rmtree(case_root)
        case_root.mkdir()
        # Vault paths use fixture_root, not case_root, so keep their synthetic
        # markers outside the per-case reset and never inspect any host vault.
        for marker in ("vault/shared", "vault-two/_shared", "markerless"):
            (fixture_root / marker).mkdir(parents=True, exist_ok=True)
        paths = _setup(case_root)
        _write(paths.env_file, fleet_env)
        for bot_name, text in bot_env.items():
            _write(paths.bot_runtime(bot_name) / ".env", text)
        fleet = FleetConfig(name="sample", service_prefix="fixture", bots={b.bot_id: b for b in bots},
                            system_defaults=SystemDefaultsConfig(enabled=False, protocols=False))
        env = {"HOME": str(case_root / "home"), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
               "PLANE_EMIT_DISABLED": "1", "PLANE_SOCKET": str(case_root / "unbound.sock"), **process}
        counts = Counter()
        equipment = {}
        real_names, real_vault = validator._available_names, validator.detect_vault
        real_dirs = paths_module.Paths.library_dir_names
        real_skills, real_integrations = composer.resolve_effective_skills, composer.resolve_effective_integrations
        def names(*args, **kwargs):
            counts["available_" + args[1]] += 1
            return real_names(*args, **kwargs)
        def dirs(instance, kind, *args, **kwargs):
            if kind == "tools":
                counts["available_tools"] += 1
            return real_dirs(instance, kind, *args, **kwargs)
        def detect(path):
            counts["vault:" + str(path)] += 1
            return real_vault(path)
        def which(name):
            counts["which:" + name] += 1
            return "/fixture/bin/claudron" if cli_present else None
        def identity():
            counts["git_identity"] += 1
            return "fixture operator has no identity"
        def reverse():
            counts["git_rewrite"] += 1
            return "fixture operator forces ssh"
        def skills(bot, *args, **kwargs):
            counts["skills:" + bot.bot_id] += 1
            result = real_skills(bot, *args, **kwargs)
            equipment.setdefault(bot.bot_id, {})["skills"] = result
            return result
        def integrations(bot, *args, **kwargs):
            counts["integrations:" + bot.bot_id] += 1
            result = real_integrations(bot, *args, **kwargs)
            equipment.setdefault(bot.bot_id, {})["integrations"] = result
            return result
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            stack.enter_context(patch.object(validator, "shutil", SimpleNamespace(which=which)))
            stack.enter_context(patch.object(validator, "_available_names", names))
            stack.enter_context(patch.object(paths_module.Paths, "library_dir_names", dirs))
            stack.enter_context(patch.object(validator, "detect_vault", detect))
            stack.enter_context(patch.object(paths_module, "_HAS_CLAUDRON", False))
            stack.enter_context(patch.object(validator, "_operator_git_identity_problem", identity))
            stack.enter_context(patch.object(validator, "_operator_reverse_insteadof", reverse))
            stack.enter_context(patch.object(composer, "resolve_effective_skills", skills))
            stack.enter_context(patch.object(composer, "resolve_effective_integrations", integrations))
            for unrelated in ("_validate_mcp_packages", "_validate_ignition", "_validate_goal_binding"):
                stack.enter_context(patch.object(validator, unrelated, lambda *a, **kw: None))
            stack.enter_context(patch("claudlobby.ignition.ignition_doors", return_value={}))
            stack.enter_context(patch("subprocess.run", side_effect=AssertionError("unexpected external probe")))
            if name.startswith("settings-"):
                error = ValueError if name == "settings-value-error" else RuntimeError
                stack.enter_context(patch.object(composer, "compose_settings_local", side_effect=error("fixture settings failure")))
            before = _inventory(fixture_root)
            try:
                report = validator.validate(fleet, paths)
                outcome = {"errors": report.errors, "warnings": report.warnings,
                           "has_errors": report.has_errors, "has_issues": report.has_issues, "exception": None}
            except Exception as exc:
                outcome = {"exception": {"type": type(exc).__name__, "message": str(exc)}}
            assert _inventory(fixture_root) == before, f"validator wrote fixture files in {name}"
        results[name] = {**outcome, "probes": dict(counts), "equipment": equipment}
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--fixture-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    result = snapshot(args.source, args.fixture_root)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(result)} cases; source={args.source.resolve()}")


if __name__ == "__main__":
    main()
