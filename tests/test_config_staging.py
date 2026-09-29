"""A host configuration proposal freezes real rendering without activating it."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from claudlobby import composer, config, config_staging, context, env_tiers, releases
from claudlobby.config_plan import PlanError, read_plan
from claudlobby.config_units import current_declarations, planned_units
from claudlobby.paths import Paths
from tests.package_fixtures import source_package


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _tree(root, *, excluding=None):
    """Snapshot existing private files, links and modes without following links."""
    return {
        str(path.relative_to(root)): (
            ("link", str(path.readlink())) if path.is_symlink() else
            ("dir", path.stat().st_mode & 0o777) if path.is_dir() else
            ("file", path.read_bytes(), path.stat().st_mode & 0o777)
        )
        for path in root.rglob("*")
        if excluding is None or not path.is_relative_to(excluding)
    }


def _fleet(root, directory, name, package):
    _write(directory / "fleet.yaml", dedent(f"""\
        fleet:
          name: {name}
          manager: {name}-manager
          service_prefix: com.{name}
          system_defaults:
            hooks: false
            guardrails: false
            protocols: false
            observability: false
          github:
            mention_allowlist: [{name}-human]
          human_telegram_id: "12345"
          telegram_group_chat_id: "-10042"
          defaults:
            channels: []
          bots:
            {name}-manager:
              expertise: [stage-role]
            {name}-worker:
              expertise: [stage-role]
              skills: [stage-skill]
              tools: [stage-tool]
              telegram:
                handle: {name}_worker
                token_env: STAGE_TOKEN
              tool_permissions:
                deny: [Write]
                allow: ["Bash(echo *)"]
        """))
    _write(directory / "library/expertise/stage-role.md", "# Stage role\nRead the assigned task.\n")
    _write(directory / "library/skills/stage-skill/SKILL.md",
           "---\nname: stage-skill\n---\n# Stage skill\nUse the attached instructions.\n")
    script = _write(directory / "library/skills/stage-skill/scripts/run.sh", "#!/bin/sh\necho original\n")
    script.chmod(0o755)
    _write(directory / "library/tools/stage-tool/tool.yaml", "type: script\n")
    _write(directory / "library/tools/stage-tool/stage-tool.sh.j2",
           "#!/bin/sh\necho '{{ bot_dir }}/data'\n")
    _write(directory / ".env", "export PLANE_EMIT_DISABLED=1\n")
    return Paths(root=root, fleet_dir=None if directory == root else directory,
                 package=package)


@pytest.fixture
def staging_case(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "state").mkdir(parents=True)
    package = source_package()
    cli = _write(root / "state/releases" / ("r-" + "a" * 64) / "venv/bin/claudlobby",
                 "#!/bin/sh\nexit 97\n")
    cli.chmod(0o755)
    # Release sealing has its own tests. Select a candidate here while keeping
    # the actual config loader, validator, resource readers and renderers.
    release = SimpleNamespace(
        release_id="r-" + "a" * 64, seal_sha256="b" * 64,
        cli_path=cli, native_path=package.native,
        inputs=SimpleNamespace(artifact_id=package.artifact_id),
    )
    monkeypatch.setattr(config_staging, "read_release", lambda *_: release)
    monkeypatch.setattr(releases, "read_release", lambda *_, **__: release)
    monkeypatch.setattr(config_staging, "get_resources", lambda: package)
    for module in (config_staging, context, composer):
        monkeypatch.setattr(module, "selected_cli", lambda: cli)

    # The native shell resolver and host probes are separate contracts. Keep
    # real dotenv parsing over these explicit fixture-owned tier paths.
    def tiers(paths, bot_name=None, fleet_name=None):
        locations = (
            ("host", Path.home() / ".env"), ("root", paths.root / ".env"),
            ("fleet", paths.fleet_config_dir / ".env"),
            ("bot", paths.bot_runtime(bot_name) / ".env" if bot_name else None),
        )
        return [env_tiers.EnvTier(name, path, "unresolved" if path is None else
                                 "present" if path.is_file() else "absent")
                for name, path in locations]

    monkeypatch.setattr(env_tiers, "read_tiers", tiers)
    monkeypatch.setattr(composer, "_host_cpu_count", lambda: 4)
    monkeypatch.setattr(composer, "_git", lambda *_: None)
    monkeypatch.setattr(composer._switches, "missing_extra", lambda *_: None)
    monkeypatch.setenv("CLAUDLOBBY_HOST_SYSTEM_YAML", str(Path.home() / "host-system.yaml"))
    paths = _fleet(root, root, "primary", package)
    return SimpleNamespace(root=root, package=package, paths=paths, cli=cli, release=release)


def _changes(plan):
    return {Path(change.target): change for change in plan.changes}


def _files(plan, change):
    assert change.after["kind"] == "tree"
    return {name: plan.blob(entry["sha256"])
            for name, entry in change.after["files"].items()}


def test_selected_account_plugins_are_frozen_as_plan_input(staging_case):
    account = Path.home() / ".claude" / "settings.json"
    _write(account, json.dumps({"enabledPlugins": {"telegram@claude-plugins-official": True}}))
    plan = config_staging.stage_configuration([staging_case.paths], staging_case.release)
    assert str(account) in plan.inputs
    generated = staging_case.paths.bot_runtime("primary-manager") / ".claude/settings.local.json"
    settings = json.loads(plan.content(_changes(plan)[generated]))
    assert settings["enabledPlugins"]["telegram@claude-plugins-official"] is False
    account.write_text(json.dumps({"enabledPlugins": {}}))
    with pytest.raises(PlanError, match="input changed"):
        plan.check_fresh()


def test_stage_validates_and_renders_retained_bytes_when_authoring_changes_during_parse(
        staging_case, monkeypatch):
    manifest = staging_case.paths.fleet_yaml
    original = manifest.read_text()
    changed = original.replace("manager: primary-manager", "manager: primary-worker")
    assert changed != original
    real_load = config.load_fleet
    real_validate = config_staging.validate
    validated_managers = []

    def alternate_during_mutable_parse(path, *, projects_yaml=None):
        if path != manifest or validated_managers:
            return real_load(path, projects_yaml=projects_yaml)
        manifest.write_text(changed)
        try:
            return real_load(path, projects_yaml=projects_yaml)
        finally:
            manifest.write_text(original)

    def observe_validation(fleet, paths):
        validated_managers.append(fleet.manager)
        return real_validate(fleet, paths)

    monkeypatch.setattr(config, "load_fleet", alternate_during_mutable_parse)
    monkeypatch.setattr(config_staging, "validate", observe_validation)
    plan = config_staging.stage_configuration([staging_case.paths], staging_case.release)
    digest = plan.effects["fleet_sources"]["primary"]["fleet"]["sha256"]
    assert plan.blob(digest) == original.encode()
    assert validated_managers == ["primary-manager"]
    worker_conf = plan.content(_changes(plan)[staging_case.paths.bot_runtime("primary-worker") / "bot.conf"])
    assert b"export MANAGER_TMUX=primary-manager\n" in worker_conf


def test_stage_renders_bots_timers_and_host_guards_without_live_writes(staging_case):
    case = staging_case
    worker = case.paths.bot_runtime("primary-worker")
    _write(worker / ".claude/settings.local.json", '{"permissions":{"allow":["old"]}}')
    _write(worker / "tools/stale.sh", "old tool\n")
    _write(worker / "memory/keep.md", "durable context\n")
    _write(worker / "data/keep.json", '{"durable":true}')
    _write(worker / "projects/wip.txt", "operator work\n")
    _write(case.root / "state/plane/plane.db", "existing registry state\n")
    _write(case.paths.runtime_fleet / "timers/old.service", "old fleet unit\n")
    _write(case.root / "runtime/_host/timers/old.service", "old host unit\n")
    _write(Path.home() / "Library/LaunchAgents/existing.plist", "existing enrollment\n")
    _write(Path.home() / ".config/systemd/user/existing.service", "existing enrollment\n")
    access = Path.home() / composer.telegram_channel_rel("primary_worker") / "access.json"
    existing_access = {"dmPolicy": "open", "allowFrom": ["runtime-user"],
                       "groups": {"-10042": {"requireMention": False}},
                       "pending": {"request": {"user": "runtime-user"}}}
    _write(access, json.dumps(existing_access))
    store = case.root / "state/config-plans"
    before = (_tree(case.root, excluding=store), _tree(Path.home()))

    plan = config_staging.stage_configuration([case.paths], case.release)

    assert (_tree(case.root, excluding=store), _tree(Path.home())) == before
    assert plan.directory.parent == store
    assert read_plan(case.root, plan.plan_id) == plan
    plan.check_fresh()
    changes = _changes(plan)
    assert plan.effects["restart_bots"] == ["primary/primary-manager", "primary/primary-worker"]
    assert plan.effects["reload_supervision"] is True
    # The proposal's enrollment list covers both native representations, not
    # whichever files happen to remain installed on the development host.
    linux = planned_units(plan, "Linux")
    darwin = planned_units(plan, "Darwin")
    assert len([item for _, item in linux if item["scope"] == "bot"]) == 2
    assert len([item for _, item in darwin if item["scope"] == "bot"]) == 2
    assert all(item["phase"] == "bots" for _, item in linux if item["scope"] == "bot")
    assert any(item["phase"] == "ingest" for _, item in linux)
    assert all(item["phase"] == "producers" for _, item in linux
               if item["scope"] == "fleet")
    with pytest.raises(PlanError, match="current generated unit"):
        current_declarations(plan, "Linux")  # candidate is not running evidence
    for bot in ("primary-manager", "primary-worker"):
        directory = case.paths.bot_runtime(bot)
        conf = plan.content(changes[directory / "bot.conf"]).decode()
        assert str(case.cli) in conf and str(case.package.native) in conf
        assert f"CLAUDLOBBY_RELEASE_ID={case.release.release_id}" in conf
        assert str(case.root) in conf and f"runtime/bots/{bot}" in conf
        assert "claudlobby-render-" not in conf
        assert plan.content(changes[directory / "CLAUDE.md"])
        assert str(directory).encode() in plan.content(changes[directory / f"com.primary.{bot}.service"])
        assert plan.content(changes[directory / f"com.primary.{bot}.plist"])
        assert changes[directory / ".cli/bin/claudlobby"].after == {
            "kind": "symlink", "target": str(case.cli)}
    settings = json.loads(plan.content(changes[worker / ".claude/settings.local.json"]))
    assert "Write" in settings["permissions"]["deny"]
    assert "Bash(echo *)" in settings["permissions"]["allow"]
    assert "Skill(stage-skill)" in settings["permissions"]["allow"]
    tools = changes[worker / "tools"]
    assert _files(plan, tools) == {"stage-tool.sh": f"#!/bin/sh\necho '{worker}/data'\n".encode()}
    assert tools.after["files"]["stage-tool.sh"]["mode"] == 0o755
    skills = _files(plan, changes[worker / ".claude/skills"])
    assert skills["stage-skill/SKILL.md"] == (case.root / "library/skills/stage-skill/SKILL.md").read_bytes()
    for destination in (case.paths.runtime_fleet / "timers", case.root / "runtime/_host/timers"):
        timers = _files(plan, changes[destination])
        assert any(name.endswith(".timer") for name in timers)
        assert any(name.endswith(".plist") for name in timers)
        assert "old.service" not in timers
        assert all(b"claudlobby-render-" not in content for content in timers.values())
    host = case.root / "runtime/_host"
    assert plan.content(changes[host / "bot-handles"]) == b"primary-manager\nprimary-worker\n"
    assert plan.content(changes[host / "mention-allowlist"]) == b"primary-human\n"
    staged_access = json.loads(plan.content(changes[access]))
    assert staged_access["pending"] == existing_access["pending"]
    assert staged_access["groups"]["-10042"]["requireMention"] is True
    assert changes[access].after["mode"] == 0o600
    assert not any(target.is_relative_to(case.root / "state/plane") for target in changes)
    assert all(changes.get(worker / directory) is None for directory in ("memory", "data", "projects"))


def test_stage_refuses_incomplete_timer_compose_before_replacing_live_tree(
        staging_case, monkeypatch):
    case = staging_case
    live = case.paths.runtime_fleet / "timers"
    _write(live / "com.primary.keepalive.timer", "previous generated unit\n")
    before = _tree(live)
    store = case.root / "state/config-plans"

    # A fleet still asks for default timers, but its declared job set was
    # torn. Ordinary generate warns and preserves old files in its own tree;
    # an empty scratch render must never become a sealed replacement tree.
    with monkeypatch.context() as patch:
        patch.setattr(config, "_load_system_defaults", lambda: {"defaults": {"jobs": {}}})
        with pytest.raises(PlanError, match="torn declaration"):
            config_staging.stage_configuration([case.paths], case.release)
    assert _tree(live) == before and not store.exists()

    # Drive the same shared guard's partial-composition branch from the
    # staging boundary, without inventing a second declaration predicate.
    reconcile = composer._reconcile_fleet_job_units

    def shortfall(directory, prefix, composed, expected, *, declaration_torn=False,
                  require_complete=False):
        return reconcile(directory, prefix, set(), max(expected, 1),
                         declaration_torn=declaration_torn,
                         require_complete=require_complete)

    with monkeypatch.context() as patch:
        patch.setattr(composer, "_reconcile_fleet_job_units", shortfall)
        with pytest.raises(PlanError, match="partial composed set"):
            config_staging.stage_configuration([case.paths], case.release)
    assert _tree(live) == before and not store.exists()

    # An explicit opt-out is a genuine teardown and may replace the timer tree.
    manifest = case.paths.fleet_yaml
    manifest.write_text(manifest.read_text().replace(
        "system_defaults:\n", "system_defaults:\n    timers: false\n", 1))
    plan = config_staging.stage_configuration([case.paths], case.release)
    staged = _files(plan, _changes(plan)[live])
    assert "com.primary.keepalive.timer" not in staged
    assert _tree(live) == before


def test_stage_freezes_overlay_skill_bytes_and_refuses_changed_inputs(staging_case):
    case = staging_case
    plan = config_staging.stage_configuration([case.paths], case.release)
    destination = case.paths.bot_runtime("primary-worker") / ".claude/skills"
    change = _changes(plan)[destination]
    frozen = _files(plan, change)
    assert change.after["files"]["stage-skill/scripts/run.sh"]["mode"] == 0o755
    plan.check_fresh()

    source = case.root / "library/skills/stage-skill/scripts/run.sh"
    source.write_text("#!/bin/sh\necho changed\n")
    assert _files(read_plan(case.root, plan.plan_id), change) == frozen
    assert frozen["stage-skill/scripts/run.sh"] == b"#!/bin/sh\necho original\n"
    assert not destination.exists()
    with pytest.raises(PlanError, match="configuration input changed"):
        plan.check_fresh()


def test_stage_requires_local_coverage_and_includes_explicit_external_fleet(staging_case, tmp_path):
    case = staging_case
    local = _fleet(case.root, case.root / "local/local-team", "local-team", case.package)
    external = _fleet(case.root, tmp_path / "vault/fleets/external-team", "external-team", case.package)
    store = case.root / "state/config-plans"
    before = (_tree(case.root, excluding=store), _tree(external.fleet_config_dir), _tree(Path.home()))
    with pytest.raises(PlanError, match="omits local fleet declarations"):
        config_staging.stage_configuration([case.paths, external], case.release)
    assert not store.exists()

    plan = config_staging.stage_configuration([external, case.paths, local], case.release)

    assert (_tree(case.root, excluding=store), _tree(external.fleet_config_dir), _tree(Path.home())) == before
    assert plan.fleets == ("external-team", "local-team", "primary")
    changes = _changes(plan)
    for paths, name in ((case.paths, "primary"), (local, "local-team"), (external, "external-team")):
        assert plan.effects["fleet_manifests"][name] == str(paths.fleet_yaml)
        assert paths.bot_runtime(f"{name}-worker") / "bot.conf" in changes
        assert paths.runtime_fleet / "timers" in changes
    host = case.root / "runtime/_host"
    handles = plan.content(changes[host / "bot-handles"]).decode().splitlines()
    assert set(handles) == {f"{name}-{role}" for name in plan.fleets for role in ("manager", "worker")}
    assert set(plan.content(changes[host / "mention-allowlist"]).decode().splitlines()) == {
        f"{name}-human" for name in plan.fleets}
    plan.check_fresh()
