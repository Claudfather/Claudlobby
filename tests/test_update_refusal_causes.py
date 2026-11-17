"""The update path's refusals say what stopped them, and carry no native tool's output (#2945).

- `host update runtime` and `siblings` name the cause, as the lifecycle doors do (#2487);
- the updater's entry and its stages print a refusal's first line, cut to 1,000 characters;
- the drift check names every generated unit that differs, and whether the candidate keeps it;
- the foreign launchd refusal names the job, the anchor, where it was found, and a next step;
- the operator check that `host update apply` and `continue` run first names the drifted units
  or the foreign job that stopped it;
- operator hints name `host update apply`, not the retired activation.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from claudlobby import git_update, host_update_operations
from claudlobby.command_result import CommandFailure
from claudlobby.commands import host_update
from claudlobby.config_plan import PlanError
from claudlobby.config_units import UnitDriftError, current_git_declarations
from claudlobby.env_tiers import ResolverUnavailable
from claudlobby.supervision_inventory import InventoryError, _foreign_anchor_refusal, collect_enrollment
from tests.fixtures.current_runtime import current_runtime, tmp_path  # noqa: F401
from tests.test_manifest_provenance import FLEET_YAML, _fleet, _paths
from tests.test_supervision_inventory import Observations
from tests.test_update_operation import initial_store  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
APPLY = "host update apply FULL_COMMIT_SHA"


# ---------------------------------------------------------------- host update names the cause

@pytest.fixture
def operator(monkeypatch):
    """An operator shell: no bot identity, no scheduled-run flag."""
    for key in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE", "CLAUDLOBBY_UPDATE_SCHEDULED"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _refusal(root, monkeypatch, failure) -> CommandFailure:
    def run(*_args, **_kwargs):
        raise failure
    monkeypatch.setattr(host_update_operations, "run_host_update", run)
    args = SimpleNamespace(root=str(root), fleet=None, seed=False, public_command="host.update.runtime",
                           dry_run=False)
    with pytest.raises(CommandFailure) as refused:
        host_update.dispatch(args)
    return refused.value


def test_a_refused_host_update_names_the_first_line_of_its_cause(tmp_path, operator):
    refused = _refusal(tmp_path, operator, PlanError("current configuration is unreadable: state/config.json\n"
                                                     "a second line"))
    cause = "current configuration is unreadable: state/config.json"
    assert refused.error.code == "conflict"
    assert refused.error.message == f"selected host update scope cannot be verified: {cause}"
    assert refused.data["cause"] == cause and refused.data["native_outcome"] == "unattempted"


def test_a_host_update_cause_is_cut_to_500_characters(tmp_path, operator):
    refused = _refusal(tmp_path, operator, RuntimeError("x" * 600))
    assert refused.data["cause"] == "x" * 497 + "..."
    assert refused.error.message.endswith(": " + "x" * 497 + "...")


def test_a_wrapped_failure_is_named_after_the_host_update_refusal(tmp_path, operator):
    failure = host_update_operations.HostUpdateError("scheduled host update has no verified committed job")
    failure.__cause__ = InventoryError("svc_inventory_properties failed (5): first native line\n"
                                       "second native line")
    refused = _refusal(tmp_path, operator, failure)
    cause = "svc_inventory_properties failed (5): first native line"
    assert refused.error.code == "conflict"
    assert refused.error.message == f"scheduled host update has no verified committed job: {cause}"
    assert refused.data["cause"] == cause


def test_a_host_update_refusal_with_nothing_wrapped_is_left_as_it_was(tmp_path, operator):
    failure = host_update_operations.HostUpdateError("host update did not complete; inspect its log before "
                                                     "retrying", effect_attempted=True)
    refused = _refusal(tmp_path, operator, failure)
    assert refused.error.code == "unavailable" and refused.data["native_outcome"] == "unknown"
    assert refused.error.message == "host update did not complete; inspect its log before retrying"
    assert "cause" not in refused.data


def test_a_resolver_failure_is_named_by_its_kind_and_keeps_the_resolver_text_out(tmp_path, operator):
    refused = _refusal(tmp_path, operator, ResolverUnavailable("env-tiers.sh exited 1: resolver text that "
                                                               "must stay out"))
    assert refused.data["cause"] == "the .env tier resolver is unavailable"
    assert refused.error.message.endswith(": the .env tier resolver is unavailable")
    assert refused.error.hint == "claudlobby host env tiers shows why"
    assert "must stay out" not in json.dumps([refused.error.message, refused.error.hint, refused.data])


def test_a_drift_refusal_lists_every_unit_in_the_json_result(tmp_path, operator):
    rows = [{"path": f"/data/runtime/bots/b{n}/b{n}.service", "state": "missing"} for n in range(12)]
    refused = _refusal(tmp_path, operator, UnitDriftError(rows))
    assert refused.data["drift"] == rows
    assert refused.data["cause"].startswith("12 current generated units differ from their committed configuration")
    assert len(refused.data["cause"]) == 500


# ---------------------------------------------------------------- the updater's entries print one line

def _stage(directory, body):
    """Run the updater's stage import against a source whose entry's main runs `body`."""
    source = (directory / "source").resolve()
    (source / "claudlobby").mkdir(parents=True)
    (source / "claudlobby/__init__.py").write_text("")
    (source / "claudlobby/update_entry.py").write_text(
        "def main(argv):\n" + "".join(f"    {line}\n" for line in body.splitlines()))
    return subprocess.run([sys.executable, "-I", "-B", "-c", git_update._STAGE_IMPORT, str(source), "stage"],
                          capture_output=True, text=True, timeout=60, check=False)


def test_a_stage_refusal_prints_only_its_first_line_and_exits_6(tmp_path):
    result = _stage(tmp_path, 'raise ValueError("unit.service: refused\\nnative stderr 1\\nnative stderr 2")')
    assert (result.returncode, result.stdout, result.stderr) == (6, "", "unit.service: refused\n")


def test_a_stage_refusal_is_cut_to_1000_characters_or_named_by_its_kind(tmp_path):
    long = _stage(tmp_path / "long", 'raise RuntimeError("x" * 1500)')
    assert (long.returncode, long.stderr) == (6, "x" * 997 + "...\n")
    bare = _stage(tmp_path / "bare", "raise OSError()")
    assert (bare.returncode, bare.stderr) == (6, "OSError\n")


def test_a_stage_that_fails_another_way_keeps_its_traceback(tmp_path):
    result = _stage(tmp_path, 'raise TypeError("a defect")')
    assert result.returncode == 1
    assert "Traceback" in result.stderr and "TypeError: a defect" in result.stderr


def test_a_stage_that_finishes_exits_with_what_its_main_returned(tmp_path):
    result = _stage(tmp_path, "return 0")
    assert (result.returncode, result.stderr) == (0, "")


def test_the_updater_entry_prints_a_refusals_first_line_and_exits_6(monkeypatch, capsys):
    def main(argv=None):
        raise git_update.UpdateError("refused: first line\nnative stderr line")
    monkeypatch.setattr(git_update, "main", main)
    assert git_update._entry([]) == 6
    assert capsys.readouterr().err == "claudlobby update: refused: first line\n"
    assert git_update.error_line(ValueError("y" * 1200)) == "y" * 997 + "..."
    assert git_update.error_line(OSError()) == "OSError"


# ---------------------------------------------------------------- the drift check names every unit

def _linux_units(host):
    return [Path(row["source"]) for row in host.snapshot["units"] if row["platform"] == "Linux"]


def test_the_drift_check_names_every_unit_that_differs_missing_or_changed(current_runtime):
    _root, _installation, _rendered, host = current_runtime
    units = _linux_units(host)
    assert len(units) == 2, units  # precondition: the fixture's worker and manager
    missing, changed = units
    missing.unlink()
    changed.write_bytes(changed.read_bytes() + b"# edited by hand\n")
    changed.chmod(0o600)
    with pytest.raises(UnitDriftError) as refused:
        current_git_declarations(host.snapshot, "Linux")
    assert list(refused.value.rows) == [{"path": str(missing), "state": "missing"},
                                        {"path": str(changed), "state": "changed: other bytes and mode"}]
    assert str(refused.value) == ("2 current generated units differ from their committed configuration: "
                                  f"{missing} (missing), {changed} (changed: other bytes and mode)")
    assert isinstance(refused.value, PlanError)


def test_one_unit_that_differs_is_named_as_before(current_runtime):
    _root, _installation, _rendered, host = current_runtime
    unit = _linux_units(host)[0]
    unit.chmod(0o600)
    with pytest.raises(UnitDriftError) as refused:
        current_git_declarations(host.snapshot, "Linux")
    assert str(refused.value) == ("current generated unit differs from its committed configuration: "
                                  f"{unit} (changed: other mode)")


def test_a_unit_replaced_by_a_link_or_a_directory_is_named_changed(current_runtime):
    _root, _installation, _rendered, host = current_runtime
    linked, replaced = _linux_units(host)
    target = linked.with_name("elsewhere.service")
    target.write_bytes(linked.read_bytes())
    linked.unlink()
    linked.symlink_to(target)
    replaced.unlink()
    replaced.mkdir()
    with pytest.raises(UnitDriftError) as refused:
        current_git_declarations(host.snapshot, "Linux")
    assert [row["state"] for row in refused.value.rows] == ["changed: a symbolic link",
                                                            "changed: not a regular file"]


def test_a_unit_reached_through_a_redirected_directory_is_named_changed(current_runtime):
    """Item 2 of #2954: the unit is a regular file and no link, but a directory on its
    path is a link to one elsewhere, so the path is not its own resolution."""
    _root, _installation, _rendered, host = current_runtime
    unit = _linux_units(host)[0]
    directory = unit.parent
    elsewhere = directory.with_name(directory.name + "-elsewhere")
    directory.rename(elsewhere)
    directory.symlink_to(elsewhere, target_is_directory=True)
    assert unit.is_file() and not unit.is_symlink()  # precondition
    with pytest.raises(UnitDriftError) as refused:
        current_git_declarations(host.snapshot, "Linux")
    states = {row["path"]: row["state"] for row in refused.value.rows}
    assert states[str(unit)] == "changed: its directory is redirected", states
    assert set(states.values()) == {"changed: its directory is redirected"}, states
    assert f"{unit} (changed: its directory is redirected)" in str(refused.value)


def test_a_drift_refusal_read_against_a_candidate_says_whether_it_keeps_each_unit():
    rows = [{"path": "/data/kept.service", "state": "missing"},
            {"path": "/data/gone.service", "state": "changed: other bytes"}]
    candidate = {"units": [{"source": "/data/kept.service", "platform": "Linux"},
                           {"source": "/data/gone.service", "platform": "Darwin"}]}
    named = UnitDriftError(rows).against(candidate, "Linux")
    assert [row["candidate"] for row in named.rows] == ["keeps", "drops"]
    assert str(named) == ("2 current generated units differ from their committed configuration: "
                          "/data/kept.service (missing; the candidate keeps it), "
                          "/data/gone.service (changed: other bytes; the candidate drops it)")


def test_an_update_names_whether_its_candidate_keeps_each_drifted_unit(initial_store, monkeypatch):
    from claudlobby import update_operation as operation
    store = initial_store
    store.record = None
    placement = {"manager": "Linux", "domain": "", "directory": str(store.root / "units")}
    previous = {"fingerprint": "previous", "identities": {}, "units": [], "supervision": placement}
    candidate = {**previous, "fingerprint": "candidate",
                 "units": [{"source": "/data/kept.service", "platform": "Linux"}]}
    rendered = SimpleNamespace(revision="target", fingerprint="render", check_inputs=lambda: None)
    adapter = SimpleNamespace(package=None, read=lambda *_: "catalog")
    monkeypatch.setattr(operation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(operation, "_catalog", lambda *_: ("Linux", "", (), set(), {}))
    monkeypatch.setattr(operation, "read_current_config", lambda *_a, **_k: previous)
    monkeypatch.setattr(operation, "read_registry_identity_bindings", lambda *_a, **_k: {})
    monkeypatch.setattr(operation, "snapshot_from_plan", lambda *_a, **_k: candidate)
    monkeypatch.setattr(operation, "validate_candidate_consumers", lambda *_a, **_k: None)
    monkeypatch.setattr(operation, "_schema", lambda *_: operation.SQL_SCHEMA_VERSION)

    def freeze(*_args, **_kwargs):
        raise UnitDriftError([{"path": "/data/kept.service", "state": "missing"},
                              {"path": "/data/other.service", "state": "changed: other mode"}])
    monkeypatch.setattr(operation, "freeze_service_intent", freeze)
    with pytest.raises(UnitDriftError) as refused:
        operation.prepare_operation(store, rendered, adapter, Path(placement["directory"]),
                                    Path("/python"), "same-request")
    assert [row["candidate"] for row in refused.value.rows] == ["keeps", "drops"]
    assert "/data/kept.service (missing; the candidate keeps it)" in str(refused.value)


# ---------------------------------------------------------------- the foreign launchd refusal

def _app_print(program, environment=(), extra=()):
    return ("gui/501/application.fixture = {\n\tpath = (submitted by runningboardd.1)\n"
            f"\ttype = Submitted\n\tstate = running\n\tprogram = {program}\n"
            "\tinherited environment = {\n" + "".join(f"\t\t{line}\n" for line in environment) + "\t}\n"
            + "".join(f"\t{line}\n" for line in extra) + "\tdomain = gui/501 [1]\n\tpid = 123\n}\n")


def _darwin(tmp_path):
    obs = Observations(tmp_path)
    obs.manager = "Darwin"
    obs.add("com.fixture.owned.plist", scope="bot")
    return obs


def _foreign_refusal(obs) -> str:
    with pytest.raises(InventoryError) as refused:
        collect_enrollment(obs.root, tuple(obs.declarations), package=obs.package,
                           runner=obs.runner).require_complete()
    return str(refused.value)


def test_a_desktop_app_that_inherited_a_bot_environment_is_named_with_its_anchor_and_next_step(tmp_path):
    """No narrowing: a mention in the environment alone still refuses, and says why."""
    obs = _darwin(tmp_path)
    bot = (obs.root / "runtime/alpha/bots/worker").resolve()
    obs.launchd["application.fixture.plist"] = _app_print("/Applications/Foreign", (
        f"BOT_DIR => {bot}", f"CLAUDLOBBY_ROOT => {obs.root}", "PRIVATE_SETTING => print text that must stay out"))
    message = _foreign_refusal(obs)
    assert ("application.fixture.plist: cannot observe effective definition: foreign launchd print mentions a "
            "reviewed root or release: job application.fixture names the working directory of "
            "com.fixture.owned.plist in its inherited environment; it most likely inherited a bot's environment "
            "(an app opened from a bot's shell): quit it, then start it again from a shell that did not come "
            "from a bot pane") in message
    for text in ("must stay out", "PRIVATE_SETTING", "BOT_DIR", "=>"):
        assert text not in message


def test_a_foreign_job_running_from_the_data_root_is_named_with_the_unload_step(tmp_path):
    obs = _darwin(tmp_path)
    obs.launchd["application.fixture.plist"] = _app_print(str(obs.root / "private-program"))
    message = _foreign_refusal(obs)
    assert ("foreign launchd print mentions a reviewed root or release: job application.fixture names the data "
            "root in its program; if it is a leftover of this installation, unload it (launchctl bootout "
            "gui/501/application.fixture); otherwise start it from outside this installation") in message


def test_a_foreign_job_that_names_no_anchor_is_still_accepted(tmp_path):
    obs = _darwin(tmp_path)
    obs.launchd["application.fixture.plist"] = _app_print("/Applications/Foreign", ("HOME => /Users/example",))
    result = collect_enrollment(obs.root, tuple(obs.declarations), package=obs.package,
                                runner=obs.runner).require_complete()
    assert result.issues == ()


def test_a_field_off_the_list_is_named_another_field_after_the_listed_ones(tmp_path):
    """Item 3 of #2954: the sentence read "in its another field". A field the refusal
    may not name is "another field" after the listed ones, and neither its name nor its
    text is printed."""
    obs = _darwin(tmp_path)
    program = str(obs.root / "private-program")
    obs.launchd["application.fixture.plist"] = _app_print(program, extra=(f"bundle path = {program}.bundle",))
    message = _foreign_refusal(obs)
    assert ("job application.fixture names the data root in its program and in another field; if it is a "
            "leftover of this installation, unload it") in message, message
    obs.launchd["application.fixture.plist"] = _app_print(
        "/Applications/Foreign", extra=(f"bundle path = {obs.root}/private.bundle",))
    message = _foreign_refusal(obs)
    assert ("job application.fixture names the data root in another field; if it is a leftover of this "
            "installation, unload it") in message, message
    for text in ("its another field", "bundle path", "private.bundle"):
        assert text not in message


def test_of_two_anchors_of_one_length_the_same_one_is_named_whichever_comes_first():
    """Item 3 of #2954: the anchors found come from a set, and of two of one length the
    refusal named whichever the set gave first. It names the first by its text now, and
    a longer anchor still comes before a shorter one."""
    target = "gui/501/application.fixture"
    lines = [f"{target} = {{", "\tprogram = /data/aa/x /data/bb/x /data/longer/x", "}"]
    kinds = {"/data/aa": "the first anchor", "/data/bb": "the second anchor",
             "/data/longer": "the longest anchor"}
    named = {_foreign_anchor_refusal(lines, target, found, kinds)
             for found in (["/data/aa", "/data/bb"], ["/data/bb", "/data/aa"])}
    assert len(named) == 1, named
    assert "names the first anchor in its program" in named.pop()
    longest = _foreign_anchor_refusal(lines, target, ["/data/aa", "/data/longer", "/data/bb"], kinds)
    assert "names the longest anchor in its program" in longest


# ---------------------------------------------------------------- operator hints name the Git update door

HINT_SOURCES = ("claudlobby/ignition.py", "claudlobby/doctor.py", "claudlobby/validator.py",
                "claudlobby/commands/scaffolding.py", "claudlobby/_runtime_scripts/install-bot.sh",
                "claudlobby/_runtime_scripts/install-bot-systemd.sh",
                "claudlobby/_runtime_scripts/manager-checkin.sh", "claudlobby/_runtime_scripts/vault-sync.sh")


@pytest.mark.parametrize("source", HINT_SOURCES)
def test_an_operator_hint_names_the_git_update_not_the_retired_activation(source):
    text = (REPO / source).read_text()
    assert "host activate" not in text and APPLY in text


def _provenance(tmp_path, *, record=None, changed=False):
    from claudlobby.doctor import DoctorReport, check_manifest_provenance
    from claudlobby.composer import write_manifest_provenance

    paths = _paths(tmp_path, in_git=True)
    if record is not None:
        (paths.runtime / "composed.json").write_text(json.dumps(record))
    elif changed:
        write_manifest_provenance(_fleet(), paths)
        paths.fleet_yaml.write_text(FLEET_YAML + "# changed under the fleet\n")
    report = DoctorReport()
    check_manifest_provenance(_fleet(), paths, report)
    return report.checks[-1].detail


@pytest.mark.parametrize("case", ["no record", "unknown schema", "changed manifest"])
def test_each_manifest_provenance_warning_names_the_update_that_records_it(tmp_path, case):
    detail = _provenance(tmp_path, record={"schema": "unknown"} if case == "unknown schema" else None,
                         changed=case == "changed manifest")
    assert f"`claudlobby --root <data-root> {APPLY}`" in detail, detail
    assert "config plan" not in detail and "host activate" not in detail, detail


# ---------------------------------------------------------------- item 4 of #2954: the lines that said activate

def test_config_diff_names_the_git_update_where_a_runtime_directory_is_missing(fleet_dir):
    """`config diff` said "activate a configuration plan first" for a bot or the fleet's
    timers that were never composed."""
    from claudlobby.config import load_fleet
    from claudlobby.diff import diff_bot, diff_fleet_timers
    from claudlobby.paths import Paths
    from tests.package_fixtures import git_package

    paths = Paths(root=fleet_dir, package=git_package(fleet_dir))
    fleet, merged = load_fleet(paths.fleet_yaml)
    for text in (diff_bot("lead", fleet, paths), diff_fleet_timers(fleet, paths, merged)):
        assert f"does not exist — apply the configuration with `claudlobby --root <data-root> {APPLY}` first" in text
        assert "activate" not in text and "plan" not in text, text


@pytest.mark.parametrize("case", ["no record", "unknown schema"])
def test_config_diff_names_the_update_that_records_the_manifest_provenance(tmp_path, case):
    """Its manifest line said "stage and activate a release" to record the provenance."""
    from claudlobby.diff import manifest_header

    paths = _paths(tmp_path, in_git=True)
    if case == "unknown schema":
        (paths.runtime / "composed.json").write_text(json.dumps({"schema": "unknown"}))
    header = manifest_header(_fleet(), paths)
    # The unknown-schema line begins a sentence with it: "Apply the configuration with".
    named = f"apply the configuration with `claudlobby --root <data-root> {APPLY}` to record"
    assert named.lower() in header.lower(), header
    assert "activate" not in header.lower() and "release" not in header, header


def test_a_backup_of_an_input_changed_since_it_was_applied_names_the_git_update(tmp_path):
    """The refusal said "review and activate it first"."""
    from claudlobby.activation_state import ActivationRefusal
    from claudlobby.config_plan import path_state
    from claudlobby.host_backup import _capture_source

    source = tmp_path / "fleet.yaml"
    source.write_text("fleet: as applied\n")
    applied = path_state(source, source=True)
    source.write_text("fleet: edited since\n")
    with pytest.raises(ActivationRefusal) as refused:
        _capture_source(source, Path("fleet.yaml"), {}, expected=applied)
    assert str(refused.value) == ("backup configuration changed since it was applied; review it, then apply it "
                                  f"with `claudlobby --root <data-root> {APPLY}` first")


@pytest.mark.parametrize("schema", [1, 2])
def test_the_restore_guidance_names_no_retired_step(schema, tmp_path, monkeypatch):
    """A restore of an older bundle said "plan and activate through the normal host
    commands", and both said to select host-system.yaml "when planning". No command
    starts a restored root yet (documentation/backup-and-restore.md)."""
    from argparse import Namespace
    from claudlobby import host_backup as backup
    from claudlobby.commands import host_backup as command

    for name in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(backup, "restore_backup", lambda *_args, **_kwargs: {"schema": schema})
    args = Namespace(fleet=None, seed=False, backup_command="restore", root=str(tmp_path / "replacement"),
                     bundle=str(tmp_path / "bundle"), replacing_retired_host=True)
    text = " ".join(command.dispatch(args).lines)
    assert "activate" not in text and "plan" not in text, text
    assert "select it with CLAUDLOBBY_HOST_SYSTEM_YAML while reviewing" in text, text
    if schema == 1:
        assert "No command starts a restored root yet: keep it inactive until a reviewed adoption step" in text


# ---------------------------------------------------------------- the operator check names them too

def _operator_reads(monkeypatch, snapshot):
    """`require_operator_context`'s first reads, stubbed: the selected package and its committed
    configuration. `host update apply` and `continue` run this check first, in the authorize stage.
    What follows those reads is the shipped code, up to the stub each test names."""
    from claudlobby import active_config, context
    for key in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE"):
        monkeypatch.delenv(key, raising=False)
    package = SimpleNamespace(installation=object(), source_revision="a" * 40)
    monkeypatch.setattr(context, "resolve_paths", lambda **_: SimpleNamespace(package=package))
    monkeypatch.setattr(active_config, "read_current_config", lambda *_a, **_k: snapshot)


def test_the_operator_check_names_each_drifted_unit_that_stopped_it(current_runtime, monkeypatch):
    from claudlobby import supervision_inventory
    from claudlobby.commands.operator_context import require_operator_context
    root, _installation, _rendered, host = current_runtime
    _operator_reads(monkeypatch, host.snapshot)
    monkeypatch.setattr(supervision_inventory, "Adapter",
                        lambda _package: SimpleNamespace(read=lambda _name: "catalog"))
    monkeypatch.setattr(supervision_inventory, "_catalog", lambda _text: ("Linux", "", [], set(), {}))
    monkeypatch.setattr(supervision_inventory, "collect_enrollment",
                        lambda *_a, **_k: pytest.fail("the drift check refuses before the inventory is read"))
    missing, changed = _linux_units(host)
    missing.unlink()
    changed.write_bytes(changed.read_bytes() + b"# edited by hand\n")
    with pytest.raises(CommandFailure) as refused:
        require_operator_context(root)
    cause = ("2 current generated units differ from their committed configuration: "
             f"{missing} (missing), {changed} (changed: other bytes)")
    assert (refused.value.error.code, refused.value.error.message, refused.value.data) == (
        "unavailable", f"operator ancestry could not be verified: {cause}", {"cause": cause})
    assert isinstance(refused.value.__cause__, UnitDriftError)


def test_the_operator_check_names_the_foreign_job_that_stopped_it(tmp_path, monkeypatch):
    from claudlobby import config_units, supervision_inventory
    from claudlobby.commands.operator_context import require_operator_context
    obs = _darwin(tmp_path)
    bot = (obs.root / "runtime/alpha/bots/worker").resolve()
    obs.launchd["application.fixture.plist"] = _app_print("/Applications/Foreign", (
        f"BOT_DIR => {bot}", f"CLAUDLOBBY_ROOT => {obs.root}", "PRIVATE_SETTING => print text that must stay out"))
    _operator_reads(monkeypatch, {})
    shipped = supervision_inventory.Adapter
    monkeypatch.setattr(supervision_inventory, "Adapter", lambda _package: shipped(obs.package, runner=obs.runner))
    monkeypatch.setattr(config_units, "current_git_declarations", lambda *_: tuple(obs.declarations))
    with pytest.raises(CommandFailure) as refused:
        require_operator_context(obs.root)
    cause = ("incomplete enrollment inventory: application.fixture.plist: cannot observe effective definition: "
             "foreign launchd print mentions a reviewed root or release: job application.fixture names the "
             "working directory of com.fixture.owned.plist in its inherited environment; it most likely inherited "
             "a bot's environment (an app opened from a bot's shell): quit it, then start it again from a shell "
             "that did not come from a bot pane; application.fixture.plist: loaded job has no observable "
             "installed binding")
    assert len(cause) <= 500  # within failure_cause's cut: both of the job's issues are named whole
    assert (refused.value.error.code, refused.value.error.message, refused.value.data) == (
        "unavailable", f"operator ancestry could not be verified: {cause}", {"cause": cause})
    assert isinstance(refused.value.__cause__, InventoryError)
    for text in ("must stay out", "PRIVATE_SETTING", "BOT_DIR", "=>"):
        assert text not in refused.value.error.message
