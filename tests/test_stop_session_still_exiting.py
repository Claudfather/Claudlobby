"""A stopped bot's session that outlasts the stop's wait (#2981).

A bot unit runs KillMode=process, so systemd reads it stopped while the session it
started (tmux, claude, MCP servers) is still exiting, and `bot stop` waits up to 30 s
for that session to leave the unit's cgroup (#2227). On a loaded host it can take
longer. The stop then answered "bot lifecycle effect is unverified; inspect native
state" and wrote no bot_stopped row, and until the session had gone every lifecycle
door refused the bot at its inventory read with "loaded consumer lacks installed
source bytes", which reads as a broken enrollment.

A stop whose quiet check refuses while processes are still in the unit's cgroup now
says so, counts them, writes its row and answers retryable with a wait, and the
inventory reads a recorded stop's session still exiting as temporary. Everything here
runs over fakes: the worker CLI over the fake native adapter of
tests/test_bot_operations.py, and the inventory over the fake observations of
tests/test_supervision_inventory.py. No bot, unit or session is started or stopped.
"""

import json
from pathlib import Path
import shlex
import subprocess
from uuid import uuid4

import pytest

from claudlobby import bot_operations
from claudlobby.__main__ import main
from claudlobby.config_units import current_git_declarations
from claudlobby.stop_record import write_stop
from claudlobby.supervision_inventory import (InventoryError, SessionExiting, collect_enrollment,
                                              remaining_processes)
from claudlobby.supervision_runtime import RuntimeEvidenceError
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401
from tests.test_bot_operations import _plane_rows, _worker_cli
from tests.test_supervision_inventory import Observations

# Bound at import, before _worker_cli replaces it with the harness's own observation.
REAL_OBSERVED = bot_operations._observed
SETTLE = bot_operations._STOP_SETTLE_S
EXITING = ("its bot's stop is recorded and its session is still exiting ({count} left in its "
           "cgroup); this is temporary: retry once the session has exited")
OLD = "loaded consumer lacks installed source bytes"


def _processes(count):
    return f"{count} {'process' if count == 1 else 'processes'}"


def _quiet_refuses(*_args, **kwargs):
    """The stop's quiet check, still refusing when its wait ends; its reason is #2259's."""
    raise RuntimeEvidenceError("svc_activation_quiet", kwargs["target"], "native refusal (3)")


# ---------------------------------------------------------------- the stop's own answer


@pytest.mark.parametrize("count, reason", [(3, None), (1, "parked until the review lands")])
def test_a_stop_whose_session_outlasts_its_wait_says_so_and_writes_its_row(cold, monkeypatch, capsys,  # noqa: F811
                                                                           count, reason):
    """Every native step ran and the unit is gone, but processes the session started are
    still in its cgroup when the wait ends. The stop says so and counts them, keeps its
    record, writes its bot_stopped row (point 4), and answers retryable with the wait."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    rows = _plane_rows(monkeypatch)
    waits = []

    def quiet(*args, **kwargs):
        waits.append(kwargs.get("settle_s"))
        _quiet_refuses(*args, **kwargs)
    monkeypatch.setattr(bot_operations, "assert_quiescent", quiet)
    worker.native.cgroup_members = count
    body = worker.call("bot", "stop", "worker", *(("--reason", reason) if reason else ()), expected=6)
    error, data = body["error"], body["data"]
    assert (error["code"], error["retryable"]) == ("unavailable", True)
    assert error["message"] == (f"bot stop de-enrolled worker, and its session was still exiting when the "
                                f"stop's {SETTLE} s wait ended: {_processes(count)} left in its unit's cgroup")
    assert f"wait {SETTLE} s and run bot stop worker again" in error["hint"]
    assert ("--reason" in error["hint"]) is (reason is not None)
    assert (data["session"], data["remaining_processes"]) == ("exiting", count)
    assert (data["native_outcome"], data["recording"]) == ("unknown", "committed")
    assert data["target"] == worker.native.target
    assert worker.native.actions == ["stop"] and waits == [SETTLE]
    assert worker.native.calls.count("svc_activation_cgroup_members") == 1
    record = json.loads((worker.root / "runtime/bots/worker/data/.stopped").read_text())
    assert record["reason"] == reason  # the stop is made, so its record stays
    (row,) = rows
    assert row["payload"]["event"] == "bot_stopped"
    written = row["payload"]["data"]["data"]
    assert (written["changed"], written["remaining_processes"], written["reason"]) == (True, count, reason)
    assert written["request_id"] == record["request_id"]


def test_a_stop_whose_row_the_plane_refuses_says_its_recording_is_degraded(cold, monkeypatch, capsys):  # noqa: F811
    worker = _worker_cli(cold, monkeypatch, capsys)
    _plane_rows(monkeypatch, refuse=True)
    monkeypatch.setattr(bot_operations, "assert_quiescent", _quiet_refuses)
    worker.native.cgroup_members = 2
    body = worker.call("bot", "stop", "worker", expected=6)
    assert (body["data"]["session"], body["data"]["recording"]) == ("exiting", "degraded")
    assert (worker.root / "runtime/bots/worker/data/.stopped").exists()


@pytest.mark.parametrize("counted", [(0, "0\n"), (3, ""), (0, "many\n")],
                         ids=["none-left", "unreadable", "not-a-count"])
def test_a_quiet_refusal_with_no_process_counted_keeps_the_unverified_answer(cold, monkeypatch, capsys,  # noqa: F811
                                                                             counted):
    """Only processes counted in the unit's cgroup make a stop say its session is still
    exiting. With none left, or a count that cannot be read, the refusal's cause is
    unknown and the stop reads unverified as before, with no row: that is #2259's."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    rows = _plane_rows(monkeypatch)
    monkeypatch.setattr(bot_operations, "assert_quiescent", _quiet_refuses)
    serve = worker.native.call

    def call(function, *args, timeout=30):
        if function == "svc_activation_cgroup_members":
            worker.native.calls.append(function)
            return subprocess.CompletedProcess([function], *counted, "")
        return serve(function, *args, timeout=timeout)
    monkeypatch.setattr(worker.native, "call", call)
    body = worker.call("bot", "stop", "worker", expected=6)
    assert body["error"]["message"] == "bot lifecycle effect is unverified; inspect native state"
    assert body["error"]["retryable"] is False and "session" not in body["data"]
    assert "svc_activation_cgroup_members" in worker.native.calls
    assert worker.native.actions == ["stop"] and rows == []


@pytest.mark.parametrize("action", ["stop", "start"])
def test_a_door_that_meets_a_stopped_session_still_exiting_says_it_is_temporary(cold, monkeypatch, capsys,  # noqa: F811
                                                                                action):
    """The bot is stopped already: its unit file is gone. A stop again and a start each
    prove its old session quiet before anything else. Processes still in the cgroup are
    counted, the answer is retryable with the wait, and nothing is enrolled or stopped.
    The stop writes its row, as a stop of a stopped bot does; a start writes none."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    rows = _plane_rows(monkeypatch)
    (worker.host.directory / worker.native.target).unlink()
    worker.host.states[worker.native.target] = "not-found not-found inactive"
    monkeypatch.setattr(bot_operations, "assert_quiescent", _quiet_refuses)
    worker.native.cgroup_members = 2
    body = worker.call("bot", action, "worker", expected=6)
    error, data = body["error"], body["data"]
    assert (error["code"], error["retryable"]) == ("unavailable", True)
    assert error["message"] == ("worker's stopped session is still exiting: 2 processes left in its "
                                "unit's cgroup; this is temporary")
    assert error["hint"].startswith(f"a stopped bot's session is still exiting: wait {SETTLE} s, then run "
                                    f"bot {action} worker again")
    assert (data["session"], data["remaining_processes"], data["native_outcome"]) == ("exiting", 2, "unattempted")
    assert worker.native.actions == []
    if action == "stop":
        (row,) = rows
        written = row["payload"]["data"]["data"]
        assert (written["changed"], written["remaining_processes"]) == (False, 2)
        assert data["recording"] == "committed"
    else:
        assert rows == [] and "recording" not in data


def test_a_stop_sweep_names_a_session_that_outlasted_its_wait(cold, monkeypatch, capsys):  # noqa: F811
    """`fleet stop --workers` runs the stop for each worker. A worker whose session outlasts
    the stop's wait stops the sweep, as before, and the answer now says why and is retryable."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    _plane_rows(monkeypatch)
    monkeypatch.setattr(bot_operations, "assert_quiescent", _quiet_refuses)
    worker.native.cgroup_members = 3
    rc = main(["--root", str(worker.root), "--json", "fleet", "stop", "--workers"])
    body = json.loads(capsys.readouterr().out)
    error, data = body["error"], body["data"]
    assert (rc, error["code"], error["retryable"]) == (6, "unavailable", True)
    cause = "the stopped session is still exiting: 3 processes left in its unit's cgroup"
    assert (data["cause"], data["failed_bot"]) == (cause, "worker")
    assert error["message"] == f"fleet lifecycle stopped at an unverified bot: {cause}"
    assert error["hint"].startswith(f"a stopped bot's session is still exiting: wait {SETTLE} s, "
                                    "then run the sweep again")
    assert worker.native.actions == ["stop"]


# ---------------------------------------------------------------- the lifecycle doors' inventory read

COMMANDS = {
    "bot start": ["bot", "start", "worker"],
    "bot stop": ["bot", "stop", "worker"],
    "bot restart": ["bot", "restart", "worker"],
    "fleet start": ["fleet", "start", "--workers"],
    "fleet stop": ["fleet", "stop", "--workers"],
    "fleet restart": ["fleet", "restart", "--workers"],
    "fleet reconcile": ["fleet", "reconcile"],
}


def _refusal(command):
    """Each door's line before the cause, as tests/test_lifecycle_refusal_cause.py reads it."""
    if command.startswith("bot "):
        return "bot native state cannot be established"
    if command == "fleet reconcile":
        return "fleet native state cannot be established"
    return "fleet lifecycle stopped at unavailable bot evidence"


def _stopped_while_exiting(worker, monkeypatch, *, recorded=True, counted=(0, "3\n")):
    """The worker as bot stop leaves it on a loaded host: its unit file gone and its stop
    recorded, while systemd still lists the unit (not-found, inactive) because processes
    are left in its cgroup, so the quiet proof refuses. The manager's unit runs as before."""
    native, host = worker.native, worker.host
    name = native.target
    units = {row.source.name: row for row in current_git_declarations(host.snapshot, "Linux")
             if row.scope == "bot"}
    (host.directory / name).unlink()
    host.states[name] = "not-found not-found inactive"  # still listed: the catalogue's loaded row
    if recorded:
        write_stop(units[name].working_directory, by="operator", reason=None, request_id=str(uuid4()))
    serve = native.call

    def call(function, *args, timeout=30):
        if function in {"svc_bot_unit_owned_by", "svc_inventory_properties", "svc_activation_cgroup_members"}:
            native.calls.append(function)
        if function == "svc_bot_unit_owned_by":
            return subprocess.CompletedProcess([function], 0, "", "")  # the generated units are the bots' own
        if function == "svc_inventory_properties":
            (unit,) = args
            if unit == name:  # listed with no file: systemctl show omits ExecStart for such a unit
                props = {"LoadState": "not-found", "ActiveState": "inactive", "UnitFileState": "",
                         "FragmentPath": "", "WorkingDirectory": "", "Environment": ""}
            else:
                props = {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled",
                         "FragmentPath": str(host.directory / unit),
                         "WorkingDirectory": str(units[unit].working_directory),
                         "Environment": " ".join(shlex.quote(f"{key}={value}")
                                                 for key, value in units[unit].environment),
                         "ExecStart": "{ path=/bin/true ; argv[]=/bin/true ; }"}
            props.update(Id=unit, DropInPaths="", NeedDaemonReload="no", Triggers="", TriggeredBy="")
            return subprocess.CompletedProcess([function], 0, "".join(f"{key}={value}\n"
                                                                      for key, value in props.items()), "")
        if function == "svc_activation_quiet" and args[1] == name:
            native.calls.append(function)
            return subprocess.CompletedProcess([function], 3, "", "")  # processes left in its cgroup
        if function == "svc_activation_cgroup_members":
            assert args[1] == name and Path(args[0]).name == name and not Path(args[0]).exists()
            return subprocess.CompletedProcess([function], *counted, "")
        return serve(function, *args, timeout=timeout)

    native.call = call
    monkeypatch.setattr(bot_operations, "_observed", REAL_OBSERVED)
    return name, units[name].working_directory / "data/.stopped"


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_each_lifecycle_door_reads_a_recorded_stops_session_still_exiting_as_temporary(
        command, cold, monkeypatch, capsys):  # noqa: F811
    """Points 2 and 3 of #2981: until the stopped session had gone, every door refused the
    bot at its inventory read as a consumer without its bytes, and said nothing of a wait.
    With the stop recorded and processes counted in the cgroup, the refusal says the
    session is still exiting and that this is temporary; it is retryable, its hint names
    the wait, and nothing changes."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    name, record = _stopped_while_exiting(worker, monkeypatch)
    before = record.read_text()
    rc = main(["--root", str(worker.root), "--json", *COMMANDS[command]])
    body = json.loads(capsys.readouterr().out)
    error, data = body["error"], body["data"]
    assert (rc, body["ok"], error["code"], error["retryable"]) == (6, False, "unavailable", True), body
    cause = f"incomplete enrollment inventory: {name}: " + EXITING.format(count=_processes(3))
    assert (data["cause"], error["message"]) == (cause, f"{_refusal(command)}: {cause}")
    assert error["hint"].startswith(f"a stopped bot's session is still exiting: wait {SETTLE} s, then run ")
    if command.startswith("bot ") or command == "fleet reconcile":
        assert data["exiting"] == {name: 3}
    assert worker.native.actions == [] and record.read_text() == before
    # Without --json the same message is the first line on stderr.
    assert main(["--root", str(worker.root), *COMMANDS[command]]) == 6
    assert capsys.readouterr().err.splitlines()[0] == error["message"]


@pytest.mark.parametrize("recorded, counted", [(False, (0, "3\n")), (True, (0, "0\n")), (True, (3, ""))],
                         ids=["no-stop-recorded", "none-left", "unreadable"])
def test_a_door_keeps_its_refusal_for_a_listed_unit_that_is_not_a_recorded_stop_still_exiting(
        recorded, counted, cold, monkeypatch, capsys):  # noqa: F811
    """A unit with no file whose quiet proof fails is a consumer without its bytes, as
    before, unless its stop is recorded and processes are counted in its cgroup."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    name, _record = _stopped_while_exiting(worker, monkeypatch, recorded=recorded, counted=counted)
    rc = main(["--root", str(worker.root), "--json", "bot", "start", "worker"])
    body = json.loads(capsys.readouterr().out)
    assert (rc, body["error"]["retryable"]) == (6, False)
    assert body["data"]["cause"] == f"incomplete enrollment inventory: {name}: {OLD}"
    assert "exiting" not in body["data"]
    assert ("svc_activation_cgroup_members" in worker.native.calls) is recorded
    assert worker.native.actions == []


# ---------------------------------------------------------------- the inventory itself


class _Observations(Observations):
    """The inventory's fake observations, with the count of a stopped unit's cgroup."""

    def __init__(self, tmp_path):  # noqa: F811
        super().__init__(tmp_path)
        self.members = {}  # svc_activation_cgroup_members' (rc, stdout) per unit

    def runner(self, command, **kwargs):
        if command[5] == "svc_activation_cgroup_members":
            self.calls.append((command[5], command[6:]))
            return subprocess.CompletedProcess(command, *self.members[command[7]], "")
        return super().runner(command, **kwargs)


def _stopped(obs, name, scope="bot", working=None):
    """A declared unit as bot stop leaves it: no file, still listed, its quiet proof failing."""
    obs.add(name, scope, working=working).unlink()
    del obs.properties[name]
    obs.extra[name] = {"Environment": "", "WorkingDirectory": "", "Id": name, "Triggers": "",
                       "TriggeredBy": "", "LoadState": "not-found", "ActiveState": "inactive",
                       "FragmentPath": "", "DropInPaths": "", "UnitFileState": "", "NeedDaemonReload": "no"}
    obs.rows.append(f"loaded\t{name}")
    obs.quiet[name] = (3, "")
    return obs.declarations[-1]


def test_the_inventory_reads_a_recorded_stops_session_still_exiting_as_temporary(tmp_path):  # noqa: F811
    obs = _Observations(tmp_path)
    name = "com.example.worker.service"
    declaration = _stopped(obs, name)
    write_stop(declaration.working_directory, by="operator", reason=None, request_id=str(uuid4()))
    obs.members[name] = (0, "3\n")
    for only_names in (None, frozenset({name})):  # every reader, and a lifecycle door's own read
        inventory = collect_enrollment(obs.root, tuple(obs.declarations), package=obs.package,
                                       runner=obs.runner, only_names=only_names)
        with pytest.raises(SessionExiting) as raised:
            inventory.require_complete()
        assert isinstance(raised.value, InventoryError) and raised.value.processes == {name: 3}
        issue = f"{name}: " + EXITING.format(count=_processes(3))
        assert str(raised.value) == f"incomplete enrollment inventory: {issue}"
        # The issue is a str: the inventory's payload and digest keep their shape.
        assert inventory.payload()["issues"] == [issue]
        assert json.loads(json.dumps(inventory.payload()))["issues"] == [issue]
        assert len(inventory.digest) == 64
    (asked,) = {tuple(args) for function, args in obs.calls if function == "svc_activation_cgroup_members"}
    assert asked[1] == name and Path(asked[0]).name == name and not Path(asked[0]).exists()


@pytest.mark.parametrize("case", ["no-stop-recorded", "none-left", "unreadable", "host-unit"])
def test_the_inventory_keeps_its_refusal_unless_a_recorded_stops_processes_are_counted(tmp_path, case):  # noqa: F811
    obs = _Observations(tmp_path)
    name = "claudlobby-example.service" if case == "host-unit" else "com.example.worker.service"
    declaration = _stopped(obs, name, "host" if case == "host-unit" else "bot")
    if case != "no-stop-recorded":
        write_stop(declaration.working_directory, by="operator", reason=None, request_id=str(uuid4()))
    obs.members[name] = {"none-left": (0, "0\n"), "unreadable": (3, "")}.get(case, (0, "3\n"))
    with pytest.raises(InventoryError, match=f"{name}: {OLD}") as raised:
        obs.collect().require_complete()
    assert not isinstance(raised.value, SessionExiting)
    counted = [function for function, _ in obs.calls if function == "svc_activation_cgroup_members"]
    assert len(counted) == (1 if case in {"none-left", "unreadable"} else 0)


def test_a_refusal_with_any_other_issue_is_not_temporary(tmp_path):  # noqa: F811
    """Only stopped sessions still exiting make the refusal temporary. With another unit's
    issue beside one, it stays an InventoryError that names both."""
    obs = _Observations(tmp_path)
    name = "com.example.worker.service"
    declaration = _stopped(obs, name)
    write_stop(declaration.working_directory, by="operator", reason=None, request_id=str(uuid4()))
    obs.members[name] = (0, "2\n")
    other = obs.add("com.example.other.service", "bot", working=tmp_path / "other")
    other.write_bytes(other.read_bytes() + b"# changed by hand\n")
    with pytest.raises(InventoryError) as raised:
        obs.collect().require_complete()
    assert not isinstance(raised.value, SessionExiting)
    assert f"{name}: " + EXITING.format(count=_processes(2)) in str(raised.value)
    assert "com.example.other.service: installed bytes differ from reviewed generated source" in str(raised.value)


@pytest.mark.parametrize("answer, expected", [
    ((0, "3\n"), 3), ((0, "0\n"), 0), ((3, ""), None), ((0, ""), None), ((0, "3 4\n"), None),
    ((0, "-1\n"), None), ((0, "03\n"), None), ((0, "3.5\n"), None)])
def test_the_count_is_one_plain_number_or_nothing(answer, expected):
    class Native:
        def call(self, function, *args, timeout=30):
            assert (function, args) == ("svc_activation_cgroup_members",
                                        (Path("/units/x.service"), "x.service"))
            return subprocess.CompletedProcess([function], *answer, "")
    assert remaining_processes(Native(), Path("/units/x.service"), "x.service") == expected


def test_a_count_that_cannot_be_asked_is_nothing():
    class Native:
        def call(self, function, *args, timeout=30):
            raise subprocess.TimeoutExpired([function], timeout)
    assert remaining_processes(Native(), Path("/units/x.service"), "x.service") is None
