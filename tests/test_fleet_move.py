"""Cold fleet layout move through the existing native bootstrap inventory."""

from __future__ import annotations

import json

import pytest

from claudlobby.__main__ import main
from claudlobby.supervision_inventory import Adapter
from tests.test_supervision_inventory import Observations


def _cold(tmp_path, monkeypatch):
    from claudlobby import resources, supervision_inventory

    observations = Observations(tmp_path)
    source = observations.root / "local" / "alpha"
    source.mkdir(parents=True)
    (source / "fleet.yaml").write_text(
        "fleet:\n  name: alpha\n  manager: worker\n  bots:\n"
        "    worker:\n      expertise: [software-engineering]\n")
    monkeypatch.setattr(resources, "get_resources", lambda: observations.package)
    monkeypatch.setattr(supervision_inventory, "Adapter",
                        lambda package: Adapter(package, runner=observations.runner))
    return observations, source


def _move(root):
    return main(["--root", str(root), "--fleet", "alpha", "fleet", "move",
                 "--system", "sys1", "--json"])


def test_cold_move_preserves_entire_source_and_rerun_is_noop(tmp_path, monkeypatch, capsys):
    obs, source = _cold(tmp_path, monkeypatch)
    (source / "shared").mkdir()
    (source / "shared" / "note.txt").write_text("durable authoring content\n")

    assert _move(obs.root) == 0
    first = json.loads(capsys.readouterr().out)
    target = obs.root / "local" / "sys1" / "alpha"
    assert first["data"]["status"] == "moved"
    assert not source.exists()
    assert (target / "shared" / "note.txt").read_text() == "durable authoring content\n"
    assert (target.parent / ".claudron-system").is_file()
    assert _move(obs.root) == 0
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "already_nested"


def test_cold_move_refuses_owned_native_consumer(tmp_path, monkeypatch, capsys):
    obs, source = _cold(tmp_path, monkeypatch)
    obs.add("claudlobby-alpha.service", working=source, declared=False)

    assert _move(obs.root) == 4
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "conflict"
    assert source.is_dir()
    assert not (obs.root / "local" / "sys1").exists()


def test_cold_move_refuses_retained_activation_history(tmp_path, monkeypatch, capsys):
    obs, source = _cold(tmp_path, monkeypatch)
    history = obs.root / "state" / "activations" / "old"
    history.mkdir(parents=True)

    assert _move(obs.root) == 4
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "conflict"
    assert source.is_dir()


@pytest.mark.parametrize("hazard", ["runtime", "selection", "redirected", "collision", "container-fleet", "root-fleet"])
def test_cold_move_refuses_unsafe_layout_without_rename(tmp_path, monkeypatch, capsys, hazard):
    obs, source = _cold(tmp_path, monkeypatch)
    destination = obs.root / "local/sys1/alpha"
    if hazard == "runtime":
        (source / "runtime").mkdir()
    elif hazard == "selection":
        from claudlobby import activation_state
        monkeypatch.setattr(activation_state, "read_selection", lambda root: {"release_id": "selected"})
        # This case isolates the cold-host refusal; ancestry has its own owner tests.
        monkeypatch.setattr("claudlobby.commands.operator_context.require_operator_context", lambda root: None)
    elif hazard == "redirected":
        real = tmp_path / "elsewhere"
        source.rename(real)
        source.symlink_to(real, target_is_directory=True)
    elif hazard == "collision":
        destination.mkdir(parents=True)
        (destination / "fleet.yaml").write_text((source / "fleet.yaml").read_text())
        (destination.parent / ".claudron-system").write_text("container\n")
    elif hazard == "container-fleet":
        destination.parent.mkdir()
        (destination.parent / "fleet.yaml").write_text("fleet:\n  name: sys1\n")
    else:
        (obs.root / "fleet.yaml").write_text((source / "fleet.yaml").read_text())
    from claudlobby.commands import fleet_move
    monkeypatch.setattr(fleet_move.os, "rename", lambda *args: pytest.fail("unsafe move attempted a rename"))
    assert _move(obs.root) == 4
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "conflict"
    assert (source / "fleet.yaml").is_file()
