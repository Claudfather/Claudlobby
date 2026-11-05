"""A lifecycle refusal names its cause (#2487).

`bot` and `fleet` lifecycle commands refused an inventory or OS failure as "native
state cannot be established" and dropped the failure's own message. A unit file
removed while its enablement link still pointed at it then refused every start with
exit 6 and nothing to inspect. The refusal now carries that message, in its text and
alone as `data.cause`.
"""

import json
from argparse import Namespace
from types import SimpleNamespace

import pytest

from claudlobby import bot_operations, context, fleet_operations
from claudlobby.command_result import execute
from claudlobby.commands import bot_runtime, fleet_runtime
from claudlobby.supervision_inventory import InventoryError

# The inventory's own text for a unit whose file was removed while an enablement link
# still pointed at it (supervision_inventory.py raises both halves of it).
INVENTORY = ("incomplete enrollment inventory: example-worker.service: "
             "loaded consumer lacks installed source bytes")
FAILURES = {
    "inventory": lambda: InventoryError(INVENTORY),
    "os": lambda: PermissionError(13, "Permission denied", "/example/user-units/example-worker.service"),
}


def _raises(exc):
    def operation(**_kwargs):
        raise exc
    return operation


def _bot_args(action, tmp_path):
    return SimpleNamespace(seed=False, bot_id="worker", public_command=f"bot.{action}",
                           ceiling=None, fleet="example", root=tmp_path)


@pytest.mark.parametrize("failure", sorted(FAILURES))
@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_a_bot_lifecycle_refusal_names_the_failure_that_stopped_it(action, failure, tmp_path,
                                                                    monkeypatch, capsys):
    exc = FAILURES[failure]()
    monkeypatch.setattr(context, "resolve_paths", lambda **_k: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(bot_operations, "set_bot_running", _raises(exc))
    args = _bot_args(action, tmp_path)
    rc = execute(f"bot.{action}", lambda: bot_runtime.dispatch(args), json_output=True)
    body = json.loads(capsys.readouterr().out)
    assert rc == 6
    assert body["error"]["code"] == "unavailable"
    assert body["error"]["message"] == f"bot native state cannot be established: {exc}"
    assert body["data"]["cause"] == str(exc)
    assert body["data"]["native_outcome"] == "unattempted"


@pytest.mark.parametrize("failure", sorted(FAILURES))
@pytest.mark.parametrize("action, operation", [("start", "set_fleet_running"),
                                               ("reconcile", "reconcile_fleet")])
def test_a_fleet_refusal_names_the_failure_that_stopped_it(action, operation, failure, tmp_path,
                                                           monkeypatch, capsys):
    exc = FAILURES[failure]()
    monkeypatch.setattr(context, "resolve_paths", lambda **_k: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(fleet_operations, operation, _raises(exc))
    args = Namespace(public_command=f"fleet.{action}", seed=False, fleet="example",
                     workers=False, root=str(tmp_path))
    rc = execute(f"fleet.{action}", lambda: fleet_runtime.dispatch(args), json_output=True)
    body = json.loads(capsys.readouterr().out)
    assert rc == 6
    assert body["error"]["code"] == "unavailable"
    assert body["error"]["message"] == f"fleet native state cannot be established: {exc}"
    assert body["data"]["cause"] == str(exc)


def test_the_cause_reaches_a_reader_without_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(context, "resolve_paths", lambda **_k: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(bot_operations, "set_bot_running", _raises(InventoryError(INVENTORY)))
    args = _bot_args("start", tmp_path)
    assert execute("bot.start", lambda: bot_runtime.dispatch(args), json_output=False) == 6
    assert capsys.readouterr().err == f"bot native state cannot be established: {INVENTORY}\n"


def test_a_failure_with_no_message_is_named_by_its_kind(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(context, "resolve_paths", lambda **_k: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(bot_operations, "set_bot_running", _raises(InventoryError()))
    args = _bot_args("start", tmp_path)
    assert execute("bot.start", lambda: bot_runtime.dispatch(args), json_output=True) == 6
    body = json.loads(capsys.readouterr().out)
    assert body["error"]["message"] == "bot native state cannot be established: InventoryError"
    assert body["data"]["cause"] == "InventoryError"
