"""Cold setup commands delegate effects to the sealed release and activation owners."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby.command_result import CommandFailure, CommandOutput
from claudlobby.commands import setup


@pytest.mark.parametrize("repository", [None, "", "shop", "https://github.com/example-org/shop", "a/b/c",
                                       "../shop", "a/..", "a/b\n", "a_/shop", "a-/shop", "a/" + "x" * 101])
def test_team_template_requires_an_explicit_repository(repository):
    with pytest.raises(CommandFailure, match="explicit OWNER/REPO"):
        setup._template_config("revenue-web-app", "shop", repository, "Improve checkout tests")


@pytest.mark.parametrize("goal", [None, "", " ", " leading", "trailing ", "two\nlines", "tabs\there", "null\0", "x" * 2001])
def test_team_template_requires_a_bounded_approved_goal(goal):
    with pytest.raises(CommandFailure, match="single-paragraph"):
        setup._template_config("revenue-web-app", "shop", "example-org/shop", goal)


def test_team_template_materializes_exact_scope_and_preserves_policy():
    from claudlobby.config import load_fleet_snapshot

    goal = 'Improve "checkout": café conversion tests'
    content = setup._template_config("revenue-web-app", "shop", "example-org/shop", goal)
    fleet, defaults = load_fleet_snapshot(Path("/unused/fleet.yaml"), content, None)
    assert defaults["jobs"]["manager-checkin"]["enroll"] is True
    assert defaults["jobs"]["reload-fleet"]["enroll"] is False
    assert fleet.plugins.startup_policy == "verify-existing"
    assert "claudna@Claudfather" in fleet.plugins.required
    assert content == setup._template_config("revenue-web-app", "shop", "example-org/shop", goal)
    assert fleet.name == "shop" and fleet.service_prefix == "com.claudlobby.shop"
    assert fleet.mission.startswith(f"Approved goal: {goal}.")
    assert "Require explicit human permission" in fleet.mission
    assert "reviewer approval alone never authorizes a merge" in fleet.bots["lead"].mission
    assert {key: bot.expertise for key, bot in fleet.bots.items()} == {
        "lead": ["orchestration"], "engineer": ["software-engineering"], "reviewer": ["code-review"]}
    assert all(bot.scope.org == "example-org" and bot.scope.repos == ["example-org/shop"]
               for bot in fleet.bots.values())
    assert all("task-scoped-effects" in bot.guardrails and not bot.dangerously_skip_permissions
               and not bot.channels and not bot.remote_control for bot in fleet.bots.values())
    assert fleet.projects_derived and all(p.validation.tier == "review" for p in fleet.projects.values())


def test_team_template_missing_resource_refuses_without_checkout_fallback(tmp_path, monkeypatch):
    from claudlobby import resources

    monkeypatch.setattr(resources, "get_resources", lambda: SimpleNamespace(seeds=tmp_path))
    with pytest.raises(CommandFailure, match="installed team template"):
        setup._template_config("revenue-web-app", "shop", "example-org/shop", "Improve checkout tests")
    with pytest.raises(CommandFailure, match="unsupported team template"):
        setup._template_config("other", "shop", "example-org/shop", "Improve checkout tests")


def test_setup_parser_requires_one_source():
    import argparse
    from claudlobby.commands._setup_parsers import register_fleet_setup

    parser = argparse.ArgumentParser()
    register_fleet_setup(parser.add_subparsers())
    assert parser.parse_args(["setup", "--config", "authored.yaml", "--install-directory", "/units"]).template is None
    args = parser.parse_args(["setup", "--template", "revenue-web-app", "--repository", "example-org/shop",
                              "--goal", "Improve checkout tests", "--install-directory", "/units"])
    assert args.config is None and args.repository == "example-org/shop"
    for flags in ([], ["--config", "file", "--template", "revenue-web-app"], ["--template", "other"]):
        with pytest.raises(SystemExit) as refused:
            parser.parse_args(["setup", "--install-directory", "/units", *flags])
        assert refused.value.code == 2


@pytest.fixture
def team_setup(tmp_path, monkeypatch):
    from claudlobby.commands import host, releases
    from claudlobby import activation_state
    from claudlobby.config_plan import ConfigPlanBuilder

    root = tmp_path / "data"
    root.mkdir()
    args = SimpleNamespace(public_command="fleet.setup", root=str(root), fleet="shop", seed=False,
                           config=None, template="revenue-web-app", repository="example-org/shop",
                           goal="Improve checkout tests", replace_config=False,
                           install_directory=str(tmp_path / "units"), activation_id="a1")
    monkeypatch.setattr(releases, "_executing_release", lambda _: "r-" + "a" * 64)
    monkeypatch.setattr(releases, "_release", lambda *_: object())
    monkeypatch.setattr(activation_state, "read_selection", lambda _: None)
    effects = []

    def stage(stage_args, stage_root):
        plan = ConfigPlanBuilder(stage_root, "r-" + "a" * 64, "b" * 64, ("shop",), effects={})
        plan.input(stage_root / "local/shop/fleet.yaml")
        plan.input(stage_root / "local/shop/projects.yaml")
        sealed = plan.seal()
        return CommandOutput(releases._plan_data(sealed), sealed.release_id)

    def activate(activation_args, activation_root):
        effects.append((activation_args, activation_root))
        return CommandOutput({"activation_id": activation_args.activation_id}, "r-" + "a" * 64, ("Activation fixture.",))

    monkeypatch.setattr(releases, "_config_plan", stage)
    monkeypatch.setattr(host, "_activate", activate)
    return args, root, effects


def test_template_setup_reuses_plan_activation_and_idempotent_copy(team_setup):
    args, root, effects = team_setup
    first = setup.dispatch(args)
    content = (root / "local/shop/fleet.yaml").read_bytes()
    second = setup.dispatch(args)
    assert first.data["config_copied"] and not second.data["config_copied"]
    assert (root / "local/shop/fleet.yaml").read_bytes() == content
    assert all(not call.adopt_existing and target == root.resolve() for call, target in effects)
    args.goal = "A different approved goal"
    with pytest.raises(CommandFailure, match="--replace-config"):
        setup.dispatch(args)
    assert len(effects) == 2 and (root / "local/shop/fleet.yaml").read_bytes() == content


@pytest.mark.parametrize("kind", ["file", "directory", "dangling-link"])
def test_template_setup_never_adopts_existing_project_declaration(team_setup, kind):
    args, root, effects = team_setup
    projects = root / "local/shop/projects.yaml"
    projects.parent.mkdir(parents=True)
    if kind == "file":
        projects.write_text("projects: {}\n")
    elif kind == "directory":
        projects.mkdir()
    else:
        projects.symlink_to(root / "missing")
    with pytest.raises(CommandFailure, match="existing project declaration"):
        setup.dispatch(args)
    assert not effects and not (projects.parent / "fleet.yaml").exists()


def test_template_setup_refuses_project_added_during_staging_and_restores_declaration(team_setup, monkeypatch):
    from claudlobby.commands import releases

    args, root, effects = team_setup
    stage = releases._config_plan

    def changed(stage_args, stage_root):
        (root / "local/shop/projects.yaml").write_text("projects: {}\n")
        return stage(stage_args, stage_root)

    monkeypatch.setattr(releases, "_config_plan", changed)
    with pytest.raises(CommandFailure, match="changed while staging") as refused:
        setup.dispatch(args)
    assert refused.value.data["config_declaration"] == "removed"
    assert "correct --repository/--goal or use an authored --config FILE" in refused.value.error.hint
    assert "correct the selected installed template" not in refused.value.error.hint
    assert not effects and not (root / "local/shop/fleet.yaml").exists()
    assert (root / "local/shop/projects.yaml").read_text() == "projects: {}\n"


def test_template_setup_generated_bot_context_refuses_before_declaration(team_setup, monkeypatch):
    args, root, effects = team_setup
    monkeypatch.setenv("BOT_ID", "worker")
    with pytest.raises(CommandFailure, match="operator shell"):
        setup.dispatch(args)
    assert not effects and not (root / "local").exists()


def test_authorized_fleet_file_copy_is_atomic_and_requires_explicit_replacement(tmp_path):
    source = tmp_path / "operator" / "fleet.yaml"
    source.parent.mkdir()
    source.write_bytes(b"fleet: one\n")
    target = tmp_path / "data" / "local" / "one" / "fleet.yaml"
    (tmp_path / "data").mkdir()

    assert setup._copy_config(source.read_bytes(), target, replace=False)[:2] == (True, None)
    assert target.read_bytes() == source.read_bytes()
    assert not setup._copy_config(source.read_bytes(), target, replace=False)[0]
    source.write_bytes(b"fleet: changed\n")
    with pytest.raises(CommandFailure, match="--replace-config"):
        setup._copy_config(source.read_bytes(), target, replace=False)
    assert target.read_bytes() == b"fleet: one\n"
    assert setup._copy_config(source.read_bytes(), target, replace=True)[:2] == (True, b"fleet: one\n")
    assert target.read_bytes() == b"fleet: changed\n"

    target.unlink()
    target.symlink_to(source)
    with pytest.raises(CommandFailure, match="not a regular file"):
        setup._copy_config(source.read_bytes(), target, replace=True)
    assert source.read_bytes() == b"fleet: changed\n"


def test_fleet_setup_stages_and_activates_copied_external_config(tmp_path, monkeypatch):
    from claudlobby.commands import host, releases
    from claudlobby import activation_state

    root = tmp_path / "data"
    root.mkdir()
    source = tmp_path / "authored-fleet.yaml"
    source.write_text("fleet:\n  name: example\n")
    monkeypatch.setattr(releases, "_executing_release", lambda _: "r-" + "a" * 64)
    monkeypatch.setattr(releases, "_release", lambda *_: object())
    monkeypatch.setattr(activation_state, "read_selection", lambda _: None)
    monkeypatch.setattr(host, "_operator_shell", lambda: None)
    observed = {}

    def stage(args, stage_root):
        observed["content"] = (stage_root / "local/example/fleet.yaml").read_bytes()
        observed["external"] = args.fleet_path
        return CommandOutput({"plan_id": "p-" + "b" * 64, "fleets": ["example"]})

    def activate(args, activation_root):
        observed["plan_id"] = args.plan_id
        observed["root"] = activation_root
        observed["install_directory"] = args.install_directory
        return CommandOutput({"activation_id": args.activation_id}, "r-" + "a" * 64,
                             ("Activation active.",))

    monkeypatch.setattr(releases, "_config_plan", stage)
    monkeypatch.setattr(host, "_activate", activate)
    result = setup.dispatch(SimpleNamespace(public_command="fleet.setup", root=str(root), fleet="example",
                                            seed=False, config=str(source), replace_config=False,
                                            install_directory=str(tmp_path / "native"), activation_id="a1"))
    assert observed == {"content": source.read_bytes(), "external": [], "plan_id": "p-" + "b" * 64,
                        "root": root, "install_directory": str(tmp_path / "native")}
    assert result.data["config_copied"] is True


def test_fleet_setup_refuses_mismatched_authored_name_before_activation(tmp_path, monkeypatch):
    from claudlobby.commands import host, releases
    from claudlobby import activation_state

    root = tmp_path / "data"
    root.mkdir()
    source = tmp_path / "authored-fleet.yaml"
    source.write_text("fleet:\n  name: another\n")
    monkeypatch.setattr(releases, "_executing_release", lambda _: "r-" + "a" * 64)
    monkeypatch.setattr(releases, "_release", lambda *_: object())
    monkeypatch.setattr(releases, "_config_plan", lambda *_: pytest.fail("staging reached"))
    monkeypatch.setattr(activation_state, "read_selection", lambda _: None)
    monkeypatch.setattr(host, "_operator_shell", lambda: None)
    monkeypatch.setattr(host, "_activate", lambda *_: pytest.fail("activation reached"))

    def run(fleet="example", replace=False):
        return setup.dispatch(SimpleNamespace(public_command="fleet.setup", root=str(root), fleet=fleet,
                                              seed=False, config=str(source), replace_config=replace,
                                              install_directory=str(tmp_path / "native"), activation_id="a1"))

    # The name is checked before copying: no stray declaration blocks later plans.
    with pytest.raises(CommandFailure, match="authored fleet name differs"):
        run()
    assert not (root / "local").exists()
    source.write_text("fleet: [unterminated\n  name: SECRET-value\n")
    with pytest.raises(CommandFailure, match="not readable YAML") as refused:
        run()
    assert "SECRET-value" not in str(refused.value) and not (root / "local").exists()

    # Staging refusal after the copy restores the prior state exactly.
    source.write_text("fleet:\n  name: example\n")
    monkeypatch.setattr(releases, "_config_plan", lambda *_: (_ for _ in ()).throw(
        CommandFailure("conflict", "conflict: configuration could not be staged from these inputs")))
    with pytest.raises(CommandFailure, match="fleet declaration removed") as refused:
        run()
    assert refused.value.data["config_declaration"] == "removed"
    assert f"correct {source} and rerun fleet setup" in refused.value.error.hint
    assert "config validate" in refused.value.error.hint and not (root / "local").exists()
    declared = root / "local/example/fleet.yaml"
    declared.parent.mkdir(parents=True)
    declared.write_bytes(b"fleet:\n  name: example\n  prior: true\n")
    with pytest.raises(CommandFailure, match="fleet declaration restored"):
        run(replace=True)
    assert declared.read_bytes() == b"fleet:\n  name: example\n  prior: true\n"


def test_fleet_setup_refuses_system_container_and_nested_fleet_before_flat_write(tmp_path, monkeypatch):
    from claudlobby.commands import host, releases
    from claudlobby import activation_state

    root = tmp_path / "data"
    source = tmp_path / "authored-fleet.yaml"
    source.write_text("fleet:\n  name: example\n")
    monkeypatch.setattr(releases, "_executing_release", lambda _: "r-" + "a" * 64)
    monkeypatch.setattr(releases, "_release", lambda *_: object())
    monkeypatch.setattr(releases, "_config_plan", lambda *_: pytest.fail("staging reached"))
    monkeypatch.setattr(activation_state, "read_selection", lambda _: None)
    monkeypatch.setattr(host, "_operator_shell", lambda: None)

    def run():
        return setup.dispatch(SimpleNamespace(public_command="fleet.setup", root=str(root), fleet="example",
                                              seed=False, config=str(source), replace_config=False,
                                              install_directory=str(tmp_path / "native"), activation_id="a1"))

    nested = root / "local/system/example"
    nested.mkdir(parents=True)
    (nested / "fleet.yaml").write_text("fleet:\n  name: example\n")
    with pytest.raises(CommandFailure, match="already declared nested"):
        run()
    assert not (root / "local/example").exists()

    container = root / "local/example"
    (container / "child").mkdir(parents=True)
    (container / ".claudron-system").write_text("# claudlobby nested-system container\n")
    with pytest.raises(CommandFailure, match="is a system container"):
        run()
    assert not (container / "fleet.yaml").exists()


def test_host_setup_checks_native_prerequisites_and_assembles_without_selection(tmp_path, monkeypatch):
    from claudlobby import release_install, resources, supervision_inventory

    root = tmp_path / "new-data"
    package = SimpleNamespace(artifact_id="artifact")
    monkeypatch.setattr(resources, "get_resources", lambda: package)
    monkeypatch.setattr(supervision_inventory, "Adapter", lambda _: SimpleNamespace(read=lambda _: (
        f"manager\tLinux\ndirectory\t{tmp_path / 'native'}\n")))
    monkeypatch.setattr(setup.shutil, "which", lambda _: "/fixture/bin")
    recorded = {}

    def assemble(*arguments):
        recorded["arguments"] = arguments
        return SimpleNamespace(release_id="r-" + "a" * 64, cli_path=root / "state/releases/cli",
                               inputs=SimpleNamespace(artifact_id="artifact"))

    monkeypatch.setattr(release_install, "assemble_release", assemble)
    output = setup.dispatch(SimpleNamespace(public_command="host.setup", root=str(root), fleet=None,
                                            seed=False, wheel="wheel", dependency_lock="lock",
                                            wheelhouse="wheelhouse", interpreter="python"))
    assert root.is_dir()
    assert recorded["arguments"] == (root, Path("wheel"), Path("lock"), Path("wheelhouse"), Path("python"))
    assert output.data["selection"] == "unchanged"
    assert output.data["install_directories"] == [str(tmp_path / "native")]

    # Startup silently skips consent/trust seeding without jq, so setup requires it.
    monkeypatch.setattr(setup.shutil, "which", lambda name: None if name == "jq" else "/fixture/bin")
    with pytest.raises(CommandFailure, match="required bot runtime executable: jq"):
        setup.dispatch(SimpleNamespace(public_command="host.setup", root=str(root), fleet=None,
                                       seed=False, wheel="wheel", dependency_lock="lock",
                                       wheelhouse="wheelhouse", interpreter="python"))


def test_host_setup_names_assembly_cause_and_retained_directory_without_subprocess_output(tmp_path, monkeypatch):
    from claudlobby import release_install, resources, supervision_inventory
    from claudlobby.releases import ReleaseError

    root = tmp_path / "new-data"
    monkeypatch.setattr(resources, "get_resources", lambda: SimpleNamespace(artifact_id="artifact"))
    monkeypatch.setattr(supervision_inventory, "Adapter", lambda _: SimpleNamespace(read=lambda _: (
        f"manager\tLinux\ndirectory\t{tmp_path / 'native'}\n")))
    monkeypatch.setattr(setup.shutil, "which", lambda _: "/fixture/bin")

    def fail(data_root, *_):
        directory = data_root / "state/releases" / ("r-" + "c" * 64)
        raise ReleaseError(f"assembly failed; incomplete release retained at {directory}: "
                           "release assembly subprocess failed (1): SECRET-pip-output")

    monkeypatch.setattr(release_install, "assemble_release", fail)
    args = SimpleNamespace(public_command="host.setup", root=str(root), fleet=None, seed=False,
                           wheel="wheel", dependency_lock="lock", wheelhouse="wheelhouse", interpreter="python")
    with pytest.raises(CommandFailure) as refused:
        setup.dispatch(args)
    retained = root.resolve() / "state/releases" / ("r-" + "c" * 64)
    message = refused.value.error.message
    assert "release assembly subprocess failed (1)" in message and str(retained) in message
    assert "manually remove" in refused.value.error.hint and str(retained) in refused.value.error.hint
    assert "SECRET" not in message + refused.value.error.hint

    monkeypatch.setattr(release_install, "assemble_release", lambda *_: (_ for _ in ()).throw(
        ReleaseError("invalid candidate wheel: SECRET-detail")))
    with pytest.raises(CommandFailure) as refused:
        setup.dispatch(args)
    assert "SECRET" not in refused.value.error.message
    assert "retained" not in refused.value.error.hint


@pytest.mark.parametrize("source_kind", ["template", "config"])
def test_plan_only_retains_exact_plan_and_freshness_without_activation(team_setup, monkeypatch, source_kind):
    from claudlobby.commands import host
    from claudlobby.config_plan import read_plan, PlanError
    from claudlobby import releases

    args, root, effects = team_setup
    args.plan_only = True
    args.install_directory = None
    if source_kind == "config":
        source = root.parent / "authored.yaml"
        source.write_bytes(setup._template_config(args.template, args.fleet, args.repository, args.goal))
        args.config = str(source)
        args.template = args.repository = args.goal = None
    monkeypatch.setattr(host, "_activate", lambda *_: pytest.fail("plan-only invoked activation"))
    first = setup.dispatch(args)
    plan = read_plan(root, first.data["plan_id"])
    assert first.release_id == plan.release_id == first.data["release_id"]
    assert first.data["release_seal"] == plan.release_seal
    assert first.data["changes"] == [] and first.data["fleets"] == ["shop"]
    assert first.data["activation"] == "not_requested" and first.data["config_copied"]
    assert first.data["config"] == str(root / "local/shop/fleet.yaml")
    assert str(root / "local/shop/fleet.yaml") in plan.inputs
    assert plan.inputs[str(root / "local/shop/projects.yaml")]["state"]["node"] == {"kind": "absent"}
    second = setup.dispatch(args)
    assert second.data["plan_id"] == plan.plan_id and not second.data["config_copied"]
    monkeypatch.setattr(releases, "read_release", lambda *_: SimpleNamespace(seal_sha256=plan.release_seal))
    plan.check_fresh()
    (root / "local/shop/projects.yaml").write_text("projects: {}\n")
    with pytest.raises(PlanError, match="configuration input changed"):
        plan.check_fresh()
    assert not effects and not (root / "state/activation").exists()


def test_plan_only_preplan_refusal_rolls_back_declaration(team_setup, monkeypatch):
    from claudlobby.commands import releases, host

    args, root, effects = team_setup
    args.plan_only = True
    args.install_directory = None
    monkeypatch.setattr(host, "_activate", lambda *_: pytest.fail("plan-only invoked activation"))
    monkeypatch.setattr(releases, "_config_plan", lambda *_: (_ for _ in ()).throw(
        CommandFailure("conflict", "conflict: configuration could not be staged from these inputs")))
    with pytest.raises(CommandFailure) as refused:
        setup.dispatch(args)
    assert refused.value.data["config_declaration"] == "removed"
    assert not (root / "local").exists() and not effects


def test_activation_requires_install_directory_before_declaration(team_setup):
    args, root, effects = team_setup
    args.install_directory = None
    with pytest.raises(CommandFailure, match="--install-directory is required"):
        setup.dispatch(args)
    assert not (root / "local").exists() and not effects


def test_plan_only_parser_needs_no_native_directory_and_dispatch_has_no_activation_id(monkeypatch):
    import argparse
    from claudlobby.commands import _setup_parsers

    parser = argparse.ArgumentParser()
    _setup_parsers.register_fleet_setup(parser.add_subparsers())
    args = parser.parse_args(["setup", "--config", "authored.yaml", "--plan-only"])
    assert args.plan_only and args.install_directory is None
    observed = {}
    monkeypatch.setattr(_setup_parsers, "execute", lambda command, fn, **kwargs: observed.update(kwargs))
    args.json = False
    _setup_parsers._dispatch(args)
    assert args.activation_id is None and observed["request_id"] is None
