"""Cold setup commands delegate effects to the sealed release and activation owners."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby.command_result import CommandFailure, CommandOutput
from claudlobby.commands import setup


def test_authorized_fleet_file_copy_is_atomic_and_requires_explicit_replacement(tmp_path):
    source = tmp_path / "operator" / "fleet.yaml"
    source.parent.mkdir()
    source.write_bytes(b"fleet: one\n")
    target = tmp_path / "data" / "local" / "one" / "fleet.yaml"
    (tmp_path / "data").mkdir()

    assert setup._copy_config(source, target, replace=False)
    assert target.read_bytes() == source.read_bytes()
    assert not setup._copy_config(source, target, replace=False)
    source.write_bytes(b"fleet: changed\n")
    with pytest.raises(CommandFailure, match="--replace-config"):
        setup._copy_config(source, target, replace=False)
    assert target.read_bytes() == b"fleet: one\n"
    assert setup._copy_config(source, target, replace=True)
    assert target.read_bytes() == b"fleet: changed\n"

    target.unlink()
    target.symlink_to(source)
    with pytest.raises(CommandFailure, match="not a regular file"):
        setup._copy_config(source, target, replace=True)
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
    monkeypatch.setattr(releases, "_config_plan", lambda *_: CommandOutput(
        {"plan_id": "p-" + "b" * 64, "fleets": ["another"]}))
    monkeypatch.setattr(activation_state, "read_selection", lambda _: None)
    monkeypatch.setattr(host, "_operator_shell", lambda: None)
    monkeypatch.setattr(host, "_activate", lambda *_: pytest.fail("activation reached"))
    with pytest.raises(CommandFailure, match="authored fleet name differs"):
        setup.dispatch(SimpleNamespace(public_command="fleet.setup", root=str(root), fleet="example",
                                       seed=False, config=str(source), replace_config=False,
                                       install_directory=str(tmp_path / "native"), activation_id="a1"))


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
