"""Public reload passes only selected scope to the packaged native owner."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from claudlobby import fleet_reload


@contextmanager
def _admitted(root, **kwargs):
    yield SimpleNamespace(release_id="r-selected", native_path=Path("/selected/native"),
                          cli_path=Path("/selected/venv/bin/claudlobby"))


def _scope(**kwargs):
    paths = SimpleNamespace(lib=Path("/selected/native"), runtime_bots=Path("/data/bots"))
    config = SimpleNamespace(name="demo", manager="lead", bots={"lead": object(), "worker": object()},
                             plugins=SimpleNamespace(required=("one@vendor", "two@vendor")))
    return SimpleNamespace(paths=paths, fleet=config), None


def test_reload_uses_selected_native_roster_and_reports_observed_result(monkeypatch):
    seen = {}
    monkeypatch.setattr(fleet_reload, "mutation_admission", _admitted)
    monkeypatch.setattr(fleet_reload, "resolve_operation_scope", _scope)
    monkeypatch.setattr(fleet_reload, "native_environment", lambda paths: {"CLAUDLOBBY_CLI": "/selected/venv/bin/claudlobby"})

    def run(command, **kwargs):
        seen.update(command=command, env=kwargs["env"])
        return subprocess.CompletedProcess(command, 0, "marked\tlead\nrefreshed\tone@vendor\nrefreshed\ttwo@vendor\n", "")

    monkeypatch.setattr(fleet_reload.subprocess, "run", run)
    result = fleet_reload.reload_fleet(root=Path("/data"), fleet="demo")
    assert result.plugins_refreshed == ("one@vendor", "two@vendor")
    assert result.bots_marked == ("lead",)
    assert seen["command"] == ["/selected/native/reload-fleet.sh", "--selected-release", "r-selected",
                               "--fleet", "demo", "--bots-dir", "/data/bots", "--plugin", "one@vendor",
                               "--plugin", "two@vendor", "--bot", "lead", "--bot", "worker"]
    assert seen["env"]["CLAUDLOBBY_NATIVE_PYTHON"] == "/selected/venv/bin/python"


def test_reload_rejects_unselected_native_owner_before_subprocess(monkeypatch):
    monkeypatch.setattr(fleet_reload, "mutation_admission", _admitted)
    destination, origin = _scope()
    destination.paths.lib = Path("/other/native")
    monkeypatch.setattr(fleet_reload, "resolve_operation_scope", lambda **kwargs: (destination, origin))
    monkeypatch.setattr(fleet_reload.subprocess, "run", lambda *args, **kwargs: pytest.fail("native ran"))
    with pytest.raises(fleet_reload.FleetReloadError, match="differs"):
        fleet_reload.reload_fleet(root=Path("/data"), fleet="demo")
