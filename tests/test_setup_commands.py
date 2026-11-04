"""Public setup routes delegate to their canonical preparation and lifecycle owners."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby.command_result import CommandFailure
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
    from copy import deepcopy
    from claudlobby import git_update, resources
    from claudlobby.installation import Installation

    root = tmp_path / "data"
    root.mkdir()
    installation = Installation(tmp_path / "installation", root)
    seeds = resources.get_resources().seeds
    package = SimpleNamespace(installation=installation, source_revision="a" * 40, seeds=seeds)
    args = SimpleNamespace(public_command="fleet.setup", root=str(root), fleet="shop", seed=False,
                           config=None, template="revenue-web-app", repository="example-org/shop",
                           goal="Improve checkout tests", replace_config=False, plan_only=False,
                           install_directory=str(tmp_path / "units"), retained={"record": None, "locked": False},
                           dispatches=[], real_store=git_update.UpdateStore)
    effects = []
    class Store:
        def __init__(self, observed):
            assert observed == installation
            self.installation = observed
        def __enter__(self):
            assert not args.retained["locked"]
            args.retained["locked"] = True
            return self
        def __exit__(self, *_):
            args.retained["locked"] = False
        def _assert_locked(self):
            assert args.retained["locked"]
        def load(self):
            return deepcopy(args.retained["record"])
    def prepare(store, *, target, initialization, prepare_only):
        store._assert_locked()
        assert store.installation == installation and prepare_only is True
        effects.append({"target": target, "initialization": initialization, "prepare_only": prepare_only})
        args.retained["record"] = {"target": target, "config_fingerprint": "b" * 64,
                                   "initialize": True, "phase": "quiesce",
                                   "request_id": "00000000-0000-4000-8000-000000000001"}
        return deepcopy(args.retained["record"])
    def apply(observed, *, expected_request_id):
        assert expected_request_id == args.retained["record"]["request_id"]
        assert observed == installation and not args.retained["locked"]
        args.dispatches.append(deepcopy(args.retained["record"]))
        return {"update": deepcopy(args.retained["record"]), "dispatch": "submitted"}
    monkeypatch.setattr(resources, "get_resources", lambda: package)
    monkeypatch.setattr(git_update, "UpdateStore", Store)
    monkeypatch.setattr(git_update, "read_update", lambda _: deepcopy(args.retained["record"]))
    monkeypatch.setattr(git_update, "_prepare_update_locked", prepare)
    monkeypatch.setattr(git_update, "apply_or_continue", apply)
    return args, root, effects


def test_template_setup_submits_exact_initialization_and_refuses_retarget(team_setup, monkeypatch):
    args, root, effects = team_setup
    first = setup.dispatch(args)
    declaration = root / "local/shop/fleet.yaml"
    content = declaration.read_bytes()
    assert first.data["config_copied"] and first.release_id is None
    assert first.data["source_revision"] == "a" * 40 and first.data["config_fingerprint"] == "b" * 64
    assert effects == [{"target": "a" * 40, "initialization": {
        "directory": args.install_directory, "manifests": [str(declaration)]}, "prepare_only": True}]
    assert len(args.dispatches) == 1
    args.goal = "A different approved goal"
    monkeypatch.setattr(setup, "_template_config", lambda *_: pytest.fail("read new input for retained operation"))
    with pytest.raises(CommandFailure, match="existing update operation"):
        setup.dispatch(args)
    assert declaration.read_bytes() == content and len(effects) == 1


def test_first_setup_dispatch_refuses_replacement_operation(team_setup, monkeypatch):
    from copy import deepcopy
    from claudlobby import git_update
    args, root, effects = team_setup
    original = "00000000-0000-4000-8000-000000000001"
    replacement = {"request_id": "00000000-0000-4000-8000-000000000002", "phase": "prepare"}
    def replaced(installation, *, expected_request_id):
        assert not args.retained["locked"] and expected_request_id == original
        args.retained["record"] = deepcopy(replacement)
        git_update._require_expected_request(args.retained["record"], expected_request_id)
        pytest.fail("replacement dispatch must refuse")
    monkeypatch.setattr(git_update, "apply_or_continue", replaced)
    with pytest.raises(CommandFailure) as refused:
        setup.dispatch(args)
    assert refused.value.data["request_id"] == original
    assert args.retained["record"] == replacement and not args.dispatches
    assert (root / "local/shop/fleet.yaml").is_file() and len(effects) == 1


@pytest.mark.parametrize("fail_before_record", [False, True])
def test_first_setup_holds_real_update_lock_through_copy_and_preparation(team_setup, monkeypatch, fail_before_record):
    from claudlobby import git_update
    from claudlobby.resources import get_resources
    args, root, effects = team_setup
    args.plan_only = True
    installation = get_resources().installation
    real_store = args.real_store
    prepare, copy = git_update._prepare_update_locked, setup._copy_config
    observed = []
    def competing_setup_is_blocked(stage):
        with pytest.raises(git_update.UpdateBusyError):
            with real_store(installation):
                pytest.fail("another first setup acquired authoring ownership")
        observed.append(stage)
    def author(*values, **options):
        competing_setup_is_blocked("copy")
        return copy(*values, **options)
    def freeze(store, **options):
        competing_setup_is_blocked("freeze")
        assert options["initialization"]["manifests"] == [str(root / "local/shop/fleet.yaml")]
        if fail_before_record:
            raise git_update.UpdateError("first preparation refused before record")
        return prepare(store, **options)
    def rollback(*values):
        competing_setup_is_blocked("rollback")
        return restore(*values)
    restore = setup._restore_config
    monkeypatch.setattr(git_update, "UpdateStore", real_store)
    monkeypatch.setattr(git_update, "_prepare_update_locked", freeze)
    monkeypatch.setattr(setup, "_copy_config", author)
    monkeypatch.setattr(setup, "_restore_config", rollback)
    if fail_before_record:
        with pytest.raises(CommandFailure) as refused:
            setup.dispatch(args)
        assert refused.value.data["config_declaration"] == "removed"
        assert not (root / "local").exists()
        assert observed == ["copy", "freeze", "rollback"]
    else:
        assert setup.dispatch(args).data["dispatch"] == "prepared"
        assert observed == ["copy", "freeze"]
    assert not args.dispatches


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


def test_template_setup_pre_record_refusal_preserves_concurrent_project_and_removes_copy(team_setup, monkeypatch):
    from claudlobby import git_update
    args, root, effects = team_setup
    def refused(*_args, **_kwargs):
        (root / "local/shop/projects.yaml").write_text("projects: {}\n")
        raise git_update.UpdateError("inputs changed before record")
    monkeypatch.setattr(git_update, "_prepare_update_locked", refused)
    with pytest.raises(CommandFailure) as failure:
        setup.dispatch(args)
    assert failure.value.data["config_declaration"] == "removed"
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


def test_fleet_setup_passes_copied_external_config_to_initialization(team_setup):
    args, root, effects = team_setup
    source = root.parent / "authored-fleet.yaml"
    source.write_text("fleet:\n  name: shop\n")
    args.config, args.template, args.repository, args.goal = str(source), None, None, None
    result = setup.dispatch(args)
    destination = root / "local/shop/fleet.yaml"
    assert destination.read_bytes() == source.read_bytes() and result.data["config_copied"]
    assert effects[0]["initialization"] == {"directory": args.install_directory,
                                             "manifests": [str(destination)]}


def test_fleet_setup_refuses_mismatched_or_malformed_authored_name_before_copy(team_setup):
    args, root, effects = team_setup
    source = root.parent / "authored-fleet.yaml"
    args.config, args.template, args.repository, args.goal = str(source), None, None, None
    source.write_text("fleet:\n  name: another\n")
    with pytest.raises(CommandFailure, match="authored fleet name differs"):
        setup.dispatch(args)
    assert not (root / "local").exists() and not effects
    source.write_text("fleet: [unterminated\n  name: SECRET-value\n")
    with pytest.raises(CommandFailure, match="not readable YAML") as refused:
        setup.dispatch(args)
    assert "SECRET-value" not in str(refused.value) and not (root / "local").exists()


@pytest.mark.parametrize("recorded", [False, True])
def test_fleet_setup_rolls_back_only_before_operation_record(team_setup, monkeypatch, recorded):
    from claudlobby import git_update
    args, root, effects = team_setup
    args.replace_config = True
    destination = root / "local/shop/fleet.yaml"
    destination.parent.mkdir(parents=True)
    prior = b"fleet:\n  name: shop\n  prior: true\n"
    destination.write_bytes(prior)
    def refused(store, **_kwargs):
        store._assert_locked()
        if recorded:
            args.retained["record"] = {"target": "a" * 40, "phase": "prepare"}
        raise git_update.UpdateError("unknown retained preparation" if recorded else "preparation refused")
    monkeypatch.setattr(git_update, "_prepare_update_locked", refused)
    with pytest.raises(CommandFailure) as failure:
        setup.dispatch(args)
    assert failure.value.data["config_declaration"] == ("unchanged" if recorded else "restored")
    assert (destination.read_bytes() != prior) is recorded
    assert not effects


@pytest.mark.parametrize("kind", ["nested", "system-container"])
def test_fleet_setup_refuses_nonflat_target_before_write(team_setup, kind):
    args, root, effects = team_setup
    if kind == "nested":
        nested = root / "local/system/shop"
        nested.mkdir(parents=True)
        (nested / "fleet.yaml").write_text("fleet:\n  name: shop\n")
        message = "already declared nested"
    else:
        container = root / "local/shop"
        (container / "child").mkdir(parents=True)
        (container / ".claudron-system").write_text("# nested-system container\n")
        message = "is a system container"
    with pytest.raises(CommandFailure, match=message):
        setup.dispatch(args)
    assert not (root / "local/shop/fleet.yaml").exists() and not effects


@pytest.mark.parametrize("retained", ["operation", "current-config"])
def test_fleet_setup_refuses_retained_state_before_reading_authored_input(team_setup, monkeypatch, retained):
    args, root, effects = team_setup
    if retained == "operation":
        args.retained["record"] = {"phase": "prepare"}
        message = "existing update operation"
    else:
        (root / "state").mkdir()
        (root / "state/config.json").write_bytes(b"retained current configuration")
        message = "only the first installation"
    monkeypatch.setattr(setup, "_template_config", lambda *_: pytest.fail("authored input read"))
    with pytest.raises(CommandFailure, match=message):
        setup.dispatch(args)
    assert not (root / "local").exists() and not effects


@pytest.fixture
def host_git_setup(tmp_path, monkeypatch):
    from claudlobby import resources, supervision_inventory
    root = tmp_path / "new-data"
    monkeypatch.setattr(resources, "get_resources", lambda: SimpleNamespace())
    monkeypatch.setattr(supervision_inventory, "Adapter", lambda _: SimpleNamespace(read=lambda _: (
        f"manager\tLinux\ndirectory\t{tmp_path / 'native'}\n")))
    monkeypatch.setattr(setup.shutil, "which", lambda _: "/fixture/bin")
    args = SimpleNamespace(public_command="host.setup", root=str(root), fleet=None, seed=False,
                           installation=str(tmp_path / "installation"), remote="https://example.invalid/repo.git",
                           ref="main", interpreter="/fixture/python")
    return args, root


def test_host_setup_checks_native_prerequisites_and_prepares_git_without_activation(host_git_setup, monkeypatch):
    from claudlobby import bootstrap
    args, root = host_git_setup
    recorded = []
    def prepare(directory, data_root, **inputs):
        recorded.append((directory, data_root, inputs))
        return {"installation": str(directory), "data_root": str(data_root), "source_revision": "a" * 40,
                "cli": str(directory / "bin/claudlobby"), "prepared": True, "activated": False}
    monkeypatch.setattr(bootstrap, "prepare_git_installation", prepare)
    output = setup.dispatch(args)
    assert root.is_dir()
    assert recorded == [(Path(args.installation), root,
                         {"remote": args.remote, "ref": "main", "interpreter": Path(args.interpreter)})]
    assert output.release_id is None and output.data["source_revision"] == "a" * 40
    assert output.data["prepared"] and not output.data["activated"]
    assert output.data["cli"] == str(Path(args.installation) / "bin/claudlobby")
    assert output.data["install_directories"] == [str(root.parent / "native")]
    assert not (root / "state").exists() and not (root / "local").exists()
    monkeypatch.setattr(setup.shutil, "which", lambda name: None if name == "jq" else "/fixture/bin")
    with pytest.raises(CommandFailure, match="required bot runtime executable: jq"):
        setup.dispatch(args)
    assert len(recorded) == 1


@pytest.mark.parametrize("failure,code", [(ValueError("SECRET retained metadata"), "conflict"),
                                         (OSError("SECRET filesystem output"), "unavailable"),
                                         (BlockingIOError("SECRET concurrent output"), "conflict")])
def test_host_setup_sanitizes_preparation_failures_without_cleanup(host_git_setup, monkeypatch, failure, code):
    from claudlobby import bootstrap
    args, root = host_git_setup
    retained = Path(args.installation) / "preparation.json"
    def fail(*_args, **_kwargs):
        retained.parent.mkdir()
        retained.write_bytes(b"retained receipt")
        raise failure
    monkeypatch.setattr(bootstrap, "prepare_git_installation", fail)
    with pytest.raises(CommandFailure) as refused:
        setup.dispatch(args)
    assert refused.value.error.code == code
    assert "SECRET" not in refused.value.error.message + refused.value.error.hint
    assert "same explicit inputs" in refused.value.error.hint
    assert retained.read_bytes() == b"retained receipt"
    assert not (root / "state").exists()


def test_host_setup_names_only_fixed_input_refusal(host_git_setup, monkeypatch):
    from claudlobby import bootstrap
    args, _ = host_git_setup
    def fail(*_args, **_kwargs):
        raise bootstrap.BundleError("Git preparation requires an explicit trusted remote and ref")
    monkeypatch.setattr(bootstrap, "prepare_git_installation", fail)
    with pytest.raises(CommandFailure) as refused:
        setup.dispatch(args)
    assert refused.value.error.code == "invalid_argument"
    assert "explicit trusted remote and ref" in refused.value.error.message


def test_host_setup_parser_requires_explicit_git_inputs_and_rejects_removed_flags():
    import argparse
    from claudlobby.commands._setup_parsers import register_host_setup
    parser = argparse.ArgumentParser()
    register_host_setup(parser.add_subparsers())
    flags = ["setup", "--installation", "/installation", "--remote", "https://example.invalid/repo.git",
             "--ref", "main", "--interpreter", "/host/python"]
    args = parser.parse_args(flags)
    assert args.installation == "/installation" and args.ref == "main"
    for invalid in (flags[:-2], [*flags, "--wheel", "/wheel"],
                    ["setup", "--wheel", "/wheel", "--dependency-lock", "/lock", "--wheelhouse", "/wheels"]):
        with pytest.raises(SystemExit) as refused:
            parser.parse_args(invalid)
        assert refused.value.code == 2


@pytest.mark.parametrize("source_kind", ["template", "config"])
def test_plan_only_prepares_same_initial_operation_without_dispatch(team_setup, source_kind):
    args, root, effects = team_setup
    args.plan_only = True
    if source_kind == "config":
        source = root.parent / "authored.yaml"
        source.write_bytes(setup._template_config(args.template, args.fleet, args.repository, args.goal))
        args.config = str(source)
        args.template = args.repository = args.goal = None
    result = setup.dispatch(args)
    declaration = root / "local/shop/fleet.yaml"
    assert result.release_id is None and result.data["dispatch"] == "prepared"
    assert result.data["update"] == args.retained["record"]
    assert result.data["update"]["initialize"] is True
    assert effects == [{"target": "a" * 40, "initialization": {
        "directory": args.install_directory, "manifests": [str(declaration)]}, "prepare_only": True}]
    assert result.data["config"] == str(declaration) and result.data["config_copied"]
    assert not (root / "state/activations").exists()


@pytest.mark.parametrize("plan_only", [False, True])
def test_first_apply_requires_install_directory_before_declaration(team_setup, plan_only):
    args, root, effects = team_setup
    args.plan_only, args.install_directory = plan_only, None
    with pytest.raises(CommandFailure, match="--install-directory is required"):
        setup.dispatch(args)
    assert not (root / "local").exists() and not effects


def test_plan_only_parser_requires_native_directory_and_no_activation_id(monkeypatch):
    import argparse
    from claudlobby.commands import _setup_parsers
    parser = argparse.ArgumentParser()
    _setup_parsers.register_fleet_setup(parser.add_subparsers())
    with pytest.raises(SystemExit) as refused:
        parser.parse_args(["setup", "--config", "authored.yaml", "--plan-only"])
    assert refused.value.code == 2
    args = parser.parse_args(["setup", "--config", "authored.yaml", "--plan-only", "--install-directory", "/units"])
    assert args.plan_only and args.install_directory == "/units"
    observed = {}
    monkeypatch.setattr(_setup_parsers, "execute", lambda command, fn, **kwargs: observed.update(kwargs))
    args.json = False
    _setup_parsers._dispatch(args)
    assert args.activation_id is None and observed["request_id"] is None


@pytest.fixture
def git_preparation(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import sys
    from claudlobby import bootstrap, git_update

    remote = tmp_path / 'trusted-source'
    (remote / 'claudlobby').mkdir(parents=True)
    package = Path(bootstrap.__file__).parent
    for name in ('installation.py', 'git_update.py'):
        shutil.copyfile(package / name, remote / 'claudlobby' / name)
    (remote / 'requirements-runtime.lock').write_text('example==1.0 --hash=sha256:' + 'a' * 64 + '\n')

    def git(*args):
        result = subprocess.run(['git', '-c', 'user.name=Example', '-c',
            'user.email=example@example.invalid', '-C', str(remote), *args],
            check=True, capture_output=True, text=True)
        return result.stdout.strip()

    git('init', '--quiet')
    git('add', '.')
    git('commit', '--quiet', '-m', 'initial')
    git('branch', '-M', 'main')
    revision = git('rev-parse', 'HEAD')
    calls = []

    def dependency(arguments):
        arguments = [str(value) for value in arguments]
        calls.append(arguments)
        if 'venv' in arguments:
            python = Path(arguments[-1]) / 'bin/python'
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text('fixture interpreter')
            python.chmod(0o755)
        if '--report' in arguments:
            return b'{"version":"1","install":[]}'
        if '-c' in arguments:
            import json
            python = Path(arguments[0])
            return json.dumps({'prefix': str(python.parent.parent), 'base_prefix': '/host-python',
                               'executable': str(python), 'version': [3, 11]}).encode()
        return b''

    monkeypatch.setattr(git_update, 'run_dependency_command', dependency)
    monkeypatch.setattr(git_update, 'install_locked_dependencies',
        lambda python, lock, wheels: calls.append(('install', python, lock, wheels)))
    kwargs = dict(remote=str(remote), ref='main', interpreter=Path(sys.executable).resolve())
    return bootstrap, tmp_path / 'managed', tmp_path / 'data', kwargs, git, revision, calls


def test_git_preparation_uses_final_paths_without_activation_and_verifies_retry(git_preparation):
    bootstrap, directory, data, kwargs, git, revision, calls = git_preparation
    result = bootstrap.prepare_git_installation(directory, data, **kwargs)
    assert result['source_revision'] == revision and result['activated'] is False
    assert calls[0][-1] == str(directory / 'env')
    assert '--require-hashes' in calls[1] and '--no-deps' in calls[1]
    assert calls[2][1] == directory / 'env/bin/python'
    assert (directory / 'bin/claudlobby').read_bytes() == (
        f"#!{kwargs['interpreter']}\n".encode() + (directory / 'src/claudlobby/installation.py').read_bytes())
    assert not (data / 'state/config.json').exists()
    assert not (data / 'state/update.json').exists()
    before = len(calls)
    assert bootstrap.prepare_git_installation(directory, data, **kwargs) == result
    assert len(calls) == before + 3
    assert '-I' in calls[-3] and '-c' in calls[-3]
    assert '--dry-run' in calls[-2] and calls[-1][-1] == 'check'
    assert all('download' not in call and 'venv' not in call and call[0] != 'install'
               for call in calls[before:])
    (directory / 'bin/claudlobby').write_text('foreign entry')
    with pytest.raises(bootstrap.BundleError, match='foreign bytes'):
        bootstrap.prepare_git_installation(directory, data, **kwargs)


def test_git_preparation_retry_keeps_resolved_commit_after_dependency_failure(git_preparation, monkeypatch):
    from claudlobby import git_update
    bootstrap, directory, data, kwargs, git, revision, calls = git_preparation
    original = git_update.run_dependency_command
    monkeypatch.setattr(git_update, 'run_dependency_command',
        lambda _: (_ for _ in ()).throw(RuntimeError('interrupted dependency preparation')))
    with pytest.raises(RuntimeError, match='interrupted'):
        bootstrap.prepare_git_installation(directory, data, **kwargs)
    remote = Path(kwargs['remote'])
    (remote / 'new-file').write_text('new revision')
    git('add', '.')
    git('commit', '--quiet', '-m', 'move ref')
    assert git('rev-parse', 'HEAD') != revision
    monkeypatch.setattr(git_update, 'run_dependency_command', original)
    assert bootstrap.prepare_git_installation(directory, data, **kwargs)['source_revision'] == revision
    (directory / 'src/foreign').write_text('retained')
    with pytest.raises(ValueError):
        bootstrap.prepare_git_installation(directory, data, **kwargs)
    assert (directory / 'src/foreign').read_text() == 'retained'


def test_git_preparation_refuses_foreign_directory_and_overlap(git_preparation):
    bootstrap, directory, data, kwargs, *_ = git_preparation
    directory.mkdir()
    (directory / 'foreign').write_text('retained')
    with pytest.raises(bootstrap.BundleError, match='unowned installation'):
        bootstrap.prepare_git_installation(directory, data, **kwargs)
    assert (directory / 'foreign').read_text() == 'retained'
    with pytest.raises(ValueError):
        bootstrap.prepare_git_installation(directory, directory / 'data', **kwargs)


@pytest.mark.parametrize('drift', ['missing', 'mode', 'symlink', 'interpreter'])
def test_completed_git_preparation_refuses_entry_or_environment_drift(git_preparation, monkeypatch, drift):
    from claudlobby import git_update
    bootstrap, directory, data, kwargs, *_ = git_preparation
    bootstrap.prepare_git_installation(directory, data, **kwargs)
    entry = directory / 'bin/claudlobby'
    if drift == 'missing':
        entry.unlink()
    elif drift == 'mode':
        entry.chmod(0o644)
    elif drift == 'symlink':
        original = entry.read_bytes()
        entry.unlink()
        foreign = directory.parent / 'foreign-entry'
        foreign.write_bytes(original)
        entry.symlink_to(foreign)
    else:
        monkeypatch.setattr(git_update, 'run_dependency_command', lambda _: b'{"prefix":"/wrong"}')
    with pytest.raises(bootstrap.BundleError):
        bootstrap.prepare_git_installation(directory, data, **kwargs)
    if drift == 'missing':
        assert not entry.exists()
    elif drift == 'mode':
        assert entry.stat().st_mode & 0o777 == 0o644
    elif drift == 'symlink':
        assert entry.is_symlink()


def test_first_git_preparation_requires_supported_final_environment(git_preparation, monkeypatch):
    import json
    from claudlobby import git_update
    bootstrap, directory, data, kwargs, *_ = git_preparation
    dependency = git_update.run_dependency_command
    def unsupported(arguments):
        if '-c' in arguments:
            python = Path(arguments[0])
            return json.dumps({'prefix': str(python.parent.parent), 'base_prefix': '/host-python',
                               'executable': str(python), 'version': [3, 9]}).encode()
        return dependency(arguments)
    monkeypatch.setattr(git_update, 'run_dependency_command', unsupported)
    with pytest.raises(bootstrap.BundleError, match='exact managed Python environment'):
        bootstrap.prepare_git_installation(directory, data, **kwargs)
    assert json.loads((directory / 'preparation.json').read_bytes())['complete'] is False
    assert not (directory / 'bin/claudlobby').exists()
    monkeypatch.setattr(git_update, 'run_dependency_command', dependency)
    assert bootstrap.prepare_git_installation(directory, data, **kwargs)['prepared'] is True


@pytest.mark.parametrize('completed', [False, True], ids=['fresh', 'completed-retry'])
@pytest.mark.parametrize('drift', ['missing-package', 'unsupported-report', 'inconsistent'])
def test_git_preparation_verifies_locked_dependencies_without_retry_install(
        git_preparation, monkeypatch, completed, drift):
    import json
    from claudlobby import git_update
    bootstrap, directory, data, kwargs, *_rest, calls = git_preparation
    if completed:
        bootstrap.prepare_git_installation(directory, data, **kwargs)
    before = len(calls)
    dependency = git_update.run_dependency_command
    observed = []
    def verify(arguments):
        argv = [str(arg) for arg in arguments]
        if '--report' in argv:
            observed.append(argv)
            if drift == 'missing-package':
                return b'{"version":"1","install":[{"metadata":{"name":"example","version":"1.0"}}]}'
            if drift == 'unsupported-report':
                return b'{"version":"2","install":[]}'
        if argv[-1] == 'check' and drift == 'inconsistent':
            observed.append(argv)
            raise git_update.UpdateError('installed dependency consistency refused')
        return dependency(arguments)
    monkeypatch.setattr(git_update, 'run_dependency_command', verify)
    expected = {'missing-package': 'explicit repair', 'unsupported-report': 'supported pip installation report',
                'inconsistent': 'consistency refused'}[drift]
    with pytest.raises(git_update.UpdateError, match=expected):
        bootstrap.prepare_git_installation(directory, data, **kwargs)
    report_command = next(argv for argv in observed if '--report' in argv)
    assert {'--dry-run', '--no-index', '--require-hashes', '--no-deps', '--only-binary=:all:',
            '--no-cache-dir', '--quiet', '-B'} <= set(report_command)
    assert report_command[report_command.index('--report') + 1] == '-'
    assert report_command[report_command.index('-r') + 1] == str(directory / 'src/requirements-runtime.lock')
    assert report_command[report_command.index('--find-links') + 1] == str(directory / 'cache/bootstrap-wheels')
    assert not (data / 'state/config.json').exists()
    assert not (data / 'state/update.json').exists()
    if completed:
        assert json.loads((directory / 'preparation.json').read_bytes())['complete'] is True
        assert all('download' not in call and 'venv' not in call and call[0] != 'install'
                   for call in calls[before:])
    else:
        assert json.loads((directory / 'preparation.json').read_bytes())['complete'] is False
        assert not (directory / 'bin/claudlobby').exists()


@pytest.mark.parametrize('report', [b'not json', b'{}', b'{"version":"1","install":null}'])
def test_locked_dependency_verification_refuses_missing_report_contract(tmp_path, report):
    from claudlobby.git_update import UpdateError, verify_locked_dependencies
    calls = []
    def runner(argv):
        calls.append(argv)
        return report
    with pytest.raises(UpdateError, match='supported pip installation report'):
        verify_locked_dependencies(tmp_path / 'python', tmp_path / 'requirements.lock', runner=runner)
    assert len(calls) == 1 and '--find-links' not in calls[0]
