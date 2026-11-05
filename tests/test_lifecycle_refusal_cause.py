"""A lifecycle refusal names its cause (#2487).

`bot` and `fleet` lifecycle commands refused a failed native read with a fixed
sentence and exit 6, and dropped the failure's own message, so nothing said which
unit or file had stopped them: "bot native state cannot be established", or, from a
fleet sweep, "fleet lifecycle stopped at unavailable bot evidence". The refusal now
ends with that message, and the JSON result also carries it as `data.cause`.

The first test runs each public command through `main` with the real operation code
and the real inventory read, over the repo's activation fixture and the fake native
adapter of `tests/test_bot_operations.py`. The failure is injected at that native
layer, where a host's unit files and reads fail.
"""

import json
import shlex
import subprocess
from argparse import Namespace
from types import SimpleNamespace

import pytest

from claudlobby import bot_operations, context, fleet_operations
from claudlobby.__main__ import main
from claudlobby.command_result import execute
from claudlobby.commands import bot_runtime, fleet_runtime
from claudlobby.config_units import current_git_declarations
from claudlobby.supervision_inventory import InventoryError
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401
from tests.test_bot_operations import _worker_cli

# Bound at import, before _worker_cli replaces it with the harness's own observation.
REAL_OBSERVED = bot_operations._observed
DENIED = PermissionError(13, "Permission denied", "/example/user-units")
COMMANDS = {
    "bot start": ["bot", "start", "worker"],
    "bot stop": ["bot", "stop", "worker"],
    "bot restart": ["bot", "restart", "worker"],
    "fleet start": ["fleet", "start", "--workers"],
    "fleet stop": ["fleet", "stop", "--workers"],
    "fleet restart": ["fleet", "restart", "--workers"],
    "fleet reconcile": ["fleet", "reconcile"],
}


def _refusal(command, failure):
    """What the command printed before the cause was added to it."""
    if command.startswith("bot "):
        return "bot native state cannot be established"
    if command != "fleet reconcile":
        return "fleet lifecycle stopped at unavailable bot evidence"
    # Reconciliation reads each bot's binding, the catalogue first, before it reads
    # the whole inventory.
    return ("fleet reconciliation has unavailable committed bot evidence" if failure == "os"
            else "fleet native state cannot be established")


def _fail(worker, monkeypatch, failure):
    """Make the native layer fail the way a host does, before any native effect."""
    native, host = worker.native, worker.host
    monkeypatch.setattr(bot_operations, "_observed", REAL_OBSERVED)
    units = {row.source.name: row for row in current_git_declarations(host.snapshot, "Linux")
             if row.scope == "bot"}
    serve = native.call

    def call(function, *args, timeout=30):
        if function == "svc_inventory_catalog" and failure == "os":
            native.calls.append(function)
            raise DENIED
        if function == "svc_inventory_catalog" and failure == "timeout":
            native.calls.append(function)
            raise subprocess.TimeoutExpired([function, "private-argument"], timeout)
        if function == "svc_bot_unit_owned_by":
            native.calls.append(function)  # the generated units are the bots' own
            return subprocess.CompletedProcess([function], 0, "", "")
        if function == "svc_inventory_properties":
            native.calls.append(function)
            (name,) = args
            unit, removed = units[name], failure == "inventory"
            # Loaded and running. After an inventory failure the unit file is gone and the
            # user manager not yet reloaded, so it runs from the bytes it last read.
            props = {"Id": name, "LoadState": "loaded", "ActiveState": "active",
                     "UnitFileState": "" if removed else "enabled",
                     "FragmentPath": str(host.directory / name), "DropInPaths": "",
                     "NeedDaemonReload": "yes" if removed else "no", "Triggers": "", "TriggeredBy": "",
                     "WorkingDirectory": str(unit.working_directory),
                     "Environment": " ".join(shlex.quote(f"{key}={value}")
                                             for key, value in unit.environment),
                     "ExecStart": "{ path=/bin/true ; argv[]=/bin/true ; }"}
            return subprocess.CompletedProcess(
                [function], 0, "".join(f"{key}={value}\n" for key, value in props.items()), "")
        if function == "svc_bot_session_observe" and failure == "session":
            native.calls.append(function)
            return subprocess.CompletedProcess([function], 1, "", "")
        return serve(function, *args, timeout=timeout)

    if failure == "inventory":
        for name in units:
            (host.directory / name).unlink()
    native.call = call


@pytest.mark.parametrize("failure", ["inventory", "os"])
@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_each_lifecycle_command_names_the_native_failure_that_stopped_it(command, failure, cold,
                                                                         monkeypatch, capsys):
    worker = _worker_cli(cold, monkeypatch, capsys)
    _fail(worker, monkeypatch, failure)
    rc = main(["--root", str(worker.root), "--json", *COMMANDS[command]])
    body = json.loads(capsys.readouterr().out)
    assert (rc, body["ok"], body["error"]["code"]) == (6, False, "unavailable")
    cause = body["data"]["cause"]
    assert body["error"]["message"] == f"{_refusal(command, failure)}: {cause}"
    if failure == "os":
        assert cause == str(DENIED)
    else:
        assert cause.startswith("incomplete enrollment inventory: ")
        assert "com.example.worker.service: loaded consumer lacks installed source bytes" in cause
    if command != "fleet reconcile":  # reconciliation only reads
        assert body["data"]["native_outcome"] == "unattempted"
    assert worker.native.actions == []
    # Without --json the same message is the first line on stderr.
    assert main(["--root", str(worker.root), *COMMANDS[command]]) == 6
    assert capsys.readouterr().err.splitlines()[0] == body["error"]["message"]


def test_fleet_reconcile_names_a_failure_inside_its_per_bot_reads(cold, monkeypatch, capsys):
    worker = _worker_cli(cold, monkeypatch, capsys)
    _fail(worker, monkeypatch, "session")  # the inventory is complete; a session read fails
    rc = main(["--root", str(worker.root), "--json", "fleet", "reconcile"])
    body = json.loads(capsys.readouterr().out)
    assert (rc, body["error"]["code"]) == (6, "unavailable")
    assert body["error"]["message"] == ("fleet reconciliation stopped at unavailable bot evidence: "
                                        "private bot session observation is unavailable")
    assert body["data"]["cause"] == "private bot session observation is unavailable"


def test_a_fleet_sweep_keeps_the_text_of_any_other_failure_private(cold, monkeypatch, capsys):
    """Only an inventory or OS failure is named; another exception's text can carry a
    configuration value or a native call's arguments, so it stays private."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    _fail(worker, monkeypatch, "timeout")
    rc = main(["--root", str(worker.root), "--json", "fleet", "stop", "--workers"])
    out = capsys.readouterr().out
    body = json.loads(out)
    assert (rc, body["error"]["code"]) == (6, "unavailable")
    assert body["error"]["message"] == "fleet lifecycle stopped at unavailable bot evidence"
    assert "cause" not in body["data"]
    assert "private-argument" not in out


@pytest.mark.parametrize("action", ["start", "reconcile"])
def test_a_fleet_command_names_a_failure_before_it_reaches_a_bot(action, tmp_path, monkeypatch, capsys):
    # The sweep and the reconciliation read the committed configuration first.
    denied = PermissionError(13, "Permission denied", "/example/state/config")

    def scope(*_args, **_kwargs):
        raise denied

    monkeypatch.setattr(context, "resolve_paths", lambda **_k: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(fleet_operations, "_scope", scope)
    args = Namespace(public_command=f"fleet.{action}", seed=False, fleet="example",
                     workers=False, root=str(tmp_path))
    rc = execute(f"fleet.{action}", lambda: fleet_runtime.dispatch(args), json_output=True)
    body = json.loads(capsys.readouterr().out)
    assert (rc, body["error"]["code"]) == (6, "unavailable")
    assert body["error"]["message"] == f"fleet native state cannot be established: {denied}"
    assert body["data"]["cause"] == str(denied)


def test_a_cause_is_the_failures_first_line_cut_short_or_else_its_kind():
    assert bot_runtime.failure_cause(InventoryError()) == "InventoryError"
    assert bot_runtime.failure_cause(InventoryError("unit: refused\nnative stderr")) == "unit: refused"
    assert bot_runtime.failure_cause(InventoryError("x" * 600)) == "x" * 497 + "..."
