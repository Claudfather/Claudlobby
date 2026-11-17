"""A lifecycle refusal names its cause (#2487).

`bot` and `fleet` lifecycle commands refused a failed native read with a fixed
sentence and exit 6, and dropped the failure's own message, so nothing said which
unit or file had stopped them: "bot native state cannot be established", or, from a
fleet sweep, "fleet lifecycle stopped at unavailable bot evidence". The refusal now
ends with that message, and the JSON result also carries it as `data.cause`.
A generated unit that differs from its committed configuration is named the same
way at the bot, fleet and bot session doors, with every unit in `data.drift` (#2954).

The first test runs each public command through `main` with the real operation code
and the real inventory read, over the repo's activation fixture and the fake native
adapter of `tests/test_bot_operations.py`. The failure is injected at that native
layer, where a host's unit files and reads fail.
"""

import json
import shlex
import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby import bot_operations, context, fleet_operations
from claudlobby.__main__ import main
from claudlobby.command_result import execute
from claudlobby.commands import bot_runtime, fleet_runtime
from claudlobby.config_plan import PlanError
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
    """Only an inventory or OS failure, or a drifted unit (#2954), is named; another
    exception's text can carry a configuration value or a native call's arguments, so it
    stays private."""
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


# ---------------------------------------------------------------- a drifted unit is named (#2954)

DRIFT_COMMANDS = {
    **COMMANDS,
    "bot handoff": ["bot", "handoff", "worker"],
    "bot interrupt": ["bot", "interrupt", "worker"],
    "bot compact": ["bot", "compact", "worker"],
    "bot session": ["bot", "session", "worker"],
}


def _drift_refusal(command):
    """Each door's exit, error code and line before the cause (#2954)."""
    if command == "bot session":
        return 6, "unavailable", "private session or native enrollment could not be established"
    if command == "fleet reconcile":
        return 6, "unavailable", "fleet reconciliation has unavailable committed bot evidence"
    if command.startswith("fleet "):
        return 6, "unavailable", "fleet lifecycle stopped at unavailable bot evidence"
    return 4, "conflict", "selected bot configuration or activation is incomplete"


@pytest.mark.parametrize("command", sorted(DRIFT_COMMANDS))
def test_each_door_names_a_generated_unit_that_differs_from_its_committed_configuration(
        command, cold, monkeypatch, capsys):
    """Item 1 of #2954: a unit file changed by hand stops every bot binding, and the bot,
    fleet and bot session doors answered it with their fixed line alone. Each now ends that
    line with the drift refusal, and its JSON result carries it as `data.cause` and every
    unit as `data.drift`, as `host update` does (#2945). The exit and the code are as before."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    unit = next(Path(row["source"]) for row in worker.host.snapshot["units"]
                if row["platform"] == "Linux" and row["source"].endswith("/com.example.worker.service"))
    unit.chmod(0o600)  # the committed mode is another
    rc = main(["--root", str(worker.root), "--json", *DRIFT_COMMANDS[command]])
    body = json.loads(capsys.readouterr().out)
    expected_rc, code, refusal = _drift_refusal(command)
    cause = f"current generated unit differs from its committed configuration: {unit} (changed: other mode)"
    assert (rc, body["ok"], body["error"]["code"]) == (expected_rc, False, code), body
    assert body["error"]["message"] == f"{refusal}: {cause}"
    assert body["data"]["cause"] == cause
    assert body["data"]["drift"] == [{"path": str(unit), "state": "changed: other mode"}]
    assert worker.native.actions == []
    # Without --json the same message is the first line on stderr.
    assert main(["--root", str(worker.root), *DRIFT_COMMANDS[command]]) == expected_rc
    assert capsys.readouterr().err.splitlines()[0] == body["error"]["message"]


@pytest.mark.parametrize("command", ["bot start", "fleet stop", "bot session"])
def test_a_configuration_failure_that_is_not_drift_keeps_its_line_and_its_text_private(
        command, cold, monkeypatch, capsys):
    """Only a drift refusal is named (#2954). Another configuration failure's text can
    carry a configuration value, so it stays out, as #2487 keeps it."""
    worker = _worker_cli(cold, monkeypatch, capsys)

    def declarations(*_args, **_kwargs):
        raise PlanError("effective configuration holds a private value")

    monkeypatch.setattr(bot_operations, "current_git_declarations", declarations)
    rc = main(["--root", str(worker.root), "--json", *DRIFT_COMMANDS[command]])
    out = capsys.readouterr().out
    body = json.loads(out)
    expected_rc, code, refusal = _drift_refusal(command)
    assert (rc, body["error"]["code"], body["error"]["message"]) == (expected_rc, code, refusal), body
    assert "cause" not in (body.get("data") or {}) and "private value" not in out
