"""Focused contracts for selected-release bot relocation."""

from __future__ import annotations

from argparse import Namespace
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from claudlobby.command_result import CommandFailure
from claudlobby.commands import move_bot
from claudlobby.config import load_fleet_snapshot


def _move(tmp_path: Path, *, channel: bool = False) -> move_bot.Move:
    source_dir = tmp_path / "source" / "runtime" / "bots" / "worker"
    target_dir = tmp_path / "target" / "runtime" / "bots" / "worker"
    source_dir.mkdir(parents=True)
    bot = SimpleNamespace(bot_id="worker", telegram=SimpleNamespace(handle="worker" if channel else None),
                          channels=[])
    source = SimpleNamespace(fleet=SimpleNamespace(name="source", bots={"worker": bot},
                                           telegram_group_chat_id="-1001"),
                             paths=SimpleNamespace(package=object()))
    target = SimpleNamespace(fleet=SimpleNamespace(name="target", bots={"worker": bot},
                                           telegram_group_chat_id="-1002"),
                             paths=SimpleNamespace(package=source.paths.package))
    return move_bot.Move(tmp_path, source, target, source_dir, target_dir,
                         "r-selected", tmp_path / "units")


def test_retained_copy_preserves_durable_paths_and_keeps_source(tmp_path):
    move = _move(tmp_path)
    (move.source_dir / ".env").write_text("TOKEN=private\n")
    (move.source_dir / "memory").mkdir()
    (move.source_dir / "memory" / "fact.md").write_text("remember")
    (move.source_dir / "data").mkdir()
    (move.source_dir / "data" / "record.json").write_text('{"kept":true}')
    (move.source_dir / "projects" / "repo").mkdir(parents=True)
    (move.source_dir / "projects" / "repo" / "README").write_text("project")
    (move.source_dir / "projects" / "repo" / "linked").symlink_to("README")
    (move.source_dir / ".claude").mkdir()
    (move.source_dir / ".claude" / "session.md").write_text("handoff")
    (move.target_dir / "memory").mkdir(parents=True)
    (move.target_dir / "data" / "events").mkdir(parents=True)

    move_bot.check_copy_destinations(move.source_dir, move.target_dir)
    copied = move_bot.copy_retained(move)

    assert copied == [str(move.target_dir / name) for name in move_bot._RETAINED]
    assert (move.target_dir / ".env").read_text() == "TOKEN=private\n"
    assert (move.target_dir / ".env").stat().st_mode & 0o777 == 0o600
    assert (move.target_dir / "memory" / "fact.md").read_text() == "remember"
    assert (move.target_dir / "data" / "record.json").read_text() == '{"kept":true}'
    assert (move.target_dir / "data" / "events").is_dir()
    assert (move.target_dir / "projects" / "repo" / "README").read_text() == "project"
    assert (move.target_dir / "projects" / "repo" / "linked").is_symlink()
    assert (move.target_dir / "projects" / "repo" / "linked").readlink() == Path("README")
    assert (move.target_dir / ".claude" / "session.md").read_text() == "handoff"
    assert (move.source_dir / "memory" / "fact.md").read_text() == "remember"


def test_retained_copy_refuses_redirect_before_mutation(tmp_path):
    move = _move(tmp_path)
    move.target_dir.mkdir(parents=True)
    (move.target_dir / ".env").symlink_to(tmp_path / "outside")
    with pytest.raises(CommandFailure, match="redirected"):
        move_bot.check_copy_destinations(move.source_dir, move.target_dir)
    assert not (tmp_path / "outside").exists()


def test_nonempty_target_retained_path_refuses_before_source_stop(tmp_path, monkeypatch):
    from claudlobby import bot_operations

    move = _move(tmp_path)
    (move.source_dir / "data").mkdir()
    (move.source_dir / "data" / "fact.md").write_text("source")
    (move.target_dir / "data").mkdir(parents=True)
    (move.target_dir / "data" / "fact.md").write_text("target")
    monkeypatch.setattr(move_bot, "no_active_assignment", lambda *_: None)
    monkeypatch.setattr(move_bot, "source_session", lambda *_, **__: None)
    monkeypatch.setattr(bot_operations, "set_bot_running",
                        lambda **_: pytest.fail("source stopped before retained conflict refusal"))

    with pytest.raises(CommandFailure, match="target retained path is not empty"):
        move_bot.apply_move(move, "worker", force=False, cleanup=True)
    assert (move.target_dir / "data" / "fact.md").read_text() == "target"
    assert (move.source_dir / "data" / "fact.md").read_text() == "source"


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "-c", "commit.gpgsign=false", *args],
                   cwd=repo, check=True, capture_output=True, text=True)


def test_source_wip_refuses_unpublished_work_before_purge(tmp_path):
    move = _move(tmp_path)
    repo = move.source_dir / "projects" / "repo"
    upstream = tmp_path / "upstream.git"
    repo.mkdir(parents=True)
    _git(tmp_path, "init", "--bare", "-q", str(upstream))
    _git(repo, "init", "-q")
    (repo / "README").write_text("one\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "remote", "add", "origin", str(upstream))
    with pytest.raises(CommandFailure, match="no upstream"):
        move_bot.source_wip(move.source_dir)
    _git(repo, "push", "-q", "-u", "origin", "HEAD")
    move_bot.source_wip(move.source_dir)

    (repo / "README").write_text("two\n")
    _git(repo, "commit", "-q", "-am", "two")
    with pytest.raises(CommandFailure, match="unpushed"):
        move_bot.source_wip(move.source_dir)
    _git(repo, "push", "-q")
    _git(repo, "branch", "side")
    _git(repo, "checkout", "-q", "side")
    (repo / "README").write_text("side\n")
    _git(repo, "commit", "-q", "-am", "side")
    _git(repo, "checkout", "-q", "-")
    with pytest.raises(CommandFailure, match="unpushed"):
        move_bot.source_wip(move.source_dir)
    _git(repo, "branch", "-q", "-D", "side")
    move_bot.source_wip(move.source_dir)

    (repo / "README").write_text("stashed\n")
    _git(repo, "stash", "-q")
    with pytest.raises(CommandFailure, match="stashed"):
        move_bot.source_wip(move.source_dir)
    _git(repo, "stash", "drop", "-q")
    _git(repo, "checkout", "-q", "--detach")
    with pytest.raises(CommandFailure, match="detached"):
        move_bot.source_wip(move.source_dir)
    assert (repo / "README").read_text() == "two\n"


@pytest.mark.parametrize(("observed", "quiet", "force", "refusal"), [
    ("absent", None, False, None),
    ("ready", None, False, "live session"),
    ("ready", None, True, None),
    # A clean stop's leftover socket: only the kernel quiescence proof reads absent.
    ("unknown", True, False, None),
    ("unknown", False, True, "unknown"),
])
def test_source_session_uses_frozen_placement_for_stopped_source(tmp_path, monkeypatch,
                                                                 observed, quiet, force, refusal):
    from claudlobby import activation_runtime, bot_operations, supervision, supervision_inventory

    proofs = []

    def assert_quiescent(adapter, *, installed_file, target, socket_path):
        proofs.append((installed_file, target, socket_path))
        if not quiet:
            raise activation_runtime.RuntimeEvidenceError("quiescence", target,
                                                          "known socket still accepts connections")

    monkeypatch.setattr(activation_runtime, "assert_quiescent", assert_quiescent)

    move = replace(_move(tmp_path), installed=str(tmp_path / "units/worker.plist"),
                   native_target="gui/501/worker")
    calls = []

    class Native:
        def __init__(self, package):
            assert package is move.source.paths.package

        def read(self, function, *args):
            calls.append((function, *args))
            return observed + "\n"

    monkeypatch.setattr(supervision, "build_supervision_spec", lambda *_: SimpleNamespace(
        bot_dir=move.source_dir, label="worker", environment={"TMUX_TMPDIR": str(tmp_path)}))
    monkeypatch.setattr(supervision_inventory, "Adapter", Native)
    monkeypatch.setattr(bot_operations, "_selected_adapter", lambda _r, _f, _b, adapter: adapter)
    if refusal:
        with pytest.raises(CommandFailure, match=refusal):
            move_bot.source_session(move, "worker", force=force)
    else:
        move_bot.source_session(move, "worker", force=force)
    assert calls == [("svc_bot_session_observe", move.source_dir, "worker", str(tmp_path),
                      move.installed, "gui/501/worker")]
    assert proofs == ([] if quiet is None else [
        (Path(move.installed), "gui/501/worker", tmp_path / f"tmux-{os.getuid()}" / "worker")])


def test_source_session_refuses_without_frozen_placement(tmp_path):
    with pytest.raises(CommandFailure, match="frozen native placement"):
        move_bot.source_session(_move(tmp_path), "worker", force=True)


def test_access_replaces_only_owned_source_group(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    move = _move(tmp_path, channel=True)
    path = tmp_path / ".claude/channels/telegram-worker/access.json"
    path.parent.mkdir(parents=True)
    original = {"groups": {"-1001": {"allowFrom": ["42"], "requireMention": False}},
                "other": "retained"}
    path.write_text(json.dumps(original))
    assert move_bot.update_access(move, "worker", dry_run=True) == str(path)
    assert json.loads(path.read_text()) == original
    move_bot.update_access(move, "worker")
    assert json.loads(path.read_text()) == {
        "groups": {"-1002": {"allowFrom": ["42"], "requireMention": False}},
        "other": "retained",
    }
    path.write_text(json.dumps({"groups": {"-1003": {"allowFrom": []}}}))
    with pytest.raises(CommandFailure, match="another group"):
        move_bot.update_access(move, "worker", dry_run=True)


def test_apply_stages_after_copy_and_reports_failed_activation(tmp_path, monkeypatch):
    from claudlobby import activation, activation_state, bot_operations, config_plan, config_staging, releases

    manifest, target_manifest = str(tmp_path / "source/fleet.yaml"), str(tmp_path / "target/fleet.yaml")
    move = replace(_move(tmp_path), selected_plan_id="p-selected",
                   move_inputs=((manifest, "reviewed"), (target_manifest, "reviewed")))
    monkeypatch.setattr(config_plan, "read_plan", lambda _root, plan_id: (
        SimpleNamespace(fleets=("source", "target"), inputs={manifest: {"state": "before"},
                                                             target_manifest: {"state": "before"}})
        if plan_id == "p-selected" else pytest.fail("unexpected plan read")))
    (move.source_dir / ".env").write_text("TOKEN=private\n")
    events = []
    monkeypatch.setattr(move_bot, "no_active_assignment", lambda *_: events.append("assignment"))
    monkeypatch.setattr(move_bot, "source_session", lambda *_, **__: events.append("session"))
    monkeypatch.setattr(bot_operations, "set_bot_running", lambda **_: (events.append("stop") or SimpleNamespace(state="stopped")))
    monkeypatch.setattr(releases, "read_release", lambda *_: SimpleNamespace(release_id="r-selected"))

    def stage(*_):
        assert (move.target_dir / ".env").read_text() == "TOKEN=private\n"
        events.append("stage")
        return SimpleNamespace(plan_id="p-staged", fleets=("source", "target"),
                               inputs={manifest: {"state": "reviewed"},
                                       target_manifest: {"state": "reviewed"}})

    monkeypatch.setattr(config_staging, "stage_configuration", stage)
    monkeypatch.setattr(move_bot, "declared_paths", lambda *_, **__: [object()])
    monkeypatch.setattr(activation, "upgrade_activation", lambda *_: (events.append("activate") or (_ for _ in ()).throw(RuntimeError("failed"))))
    monkeypatch.setattr(activation_state, "read_selection", lambda *_: None)
    with pytest.raises(CommandFailure, match="incomplete") as caught:
        move_bot.apply_move(move, "worker", force=False, cleanup=True)
    assert events == ["assignment", "session", "stop", "stage", "activate"]
    assert caught.value.data["plan_id"] == "p-staged"
    assert caught.value.data["source_stopped"] is True
    assert caught.value.data["source_retained"] is True
    assert (move.source_dir / ".env").exists()


def test_staged_move_plan_refuses_unrelated_authoring_before_activation(tmp_path, monkeypatch):
    from claudlobby import config_plan

    source_manifest, target_manifest = str(tmp_path / "source/fleet.yaml"), str(tmp_path / "target/fleet.yaml")
    other, access = str(tmp_path / "other/fleet.yaml"), str(tmp_path / "home/access.json")
    source_env, target_env = str(move_dirs(tmp_path)[0] / ".env"), str(move_dirs(tmp_path)[1] / ".env")
    move = replace(_move(tmp_path), selected_plan_id="p-selected", access_input=access,
                   move_inputs=((source_manifest, 2), (target_manifest, 2)))
    selected = SimpleNamespace(fleets=("other", "source", "target"), inputs={
        source_manifest: {"state": 1}, target_manifest: {"state": 1}, other: {"state": 1},
        access: {"state": 1}, source_env: {"state": 1}})
    monkeypatch.setattr(config_plan, "read_plan", lambda *_: selected)
    # Exactly the move: reviewed manifests, its access rewrite, the source bot's
    # tier gone and the retained target tier new.
    own = SimpleNamespace(fleets=selected.fleets, inputs={
        source_manifest: {"state": 2}, target_manifest: {"state": 2}, other: {"state": 1},
        access: {"state": 2}, target_env: {"state": 3}})
    move_bot.staged_scope(move, own)
    for changed, reason in (({other: {"state": 2}}, "unrelated authoring"),
                            ({str(tmp_path / "overlay/new-skill"): {"state": 1}}, "unrelated authoring"),
                            ({source_manifest: {"state": 3}}, "after the move was reviewed")):
        with pytest.raises(RuntimeError, match=reason):
            move_bot.staged_scope(move, replace_inputs(own, changed))
    with pytest.raises(RuntimeError, match="unrelated authoring"):
        move_bot.staged_scope(move, SimpleNamespace(fleets=own.fleets, inputs={
            key: value for key, value in own.inputs.items() if key != other}))
    with pytest.raises(RuntimeError, match="fleet set"):
        move_bot.staged_scope(move, SimpleNamespace(fleets=("source", "target"), inputs={}))
    with pytest.raises(RuntimeError, match="fleet set"):
        move_bot.staged_scope(replace(move, move_inputs=()), own)


def move_dirs(tmp_path):
    return (tmp_path / "source/runtime/bots/worker", tmp_path / "target/runtime/bots/worker")


def replace_inputs(plan, changed):
    return SimpleNamespace(fleets=plan.fleets, inputs={**plan.inputs, **changed})


def test_preview_discloses_host_scope_without_effect(tmp_path, monkeypatch):
    move = _move(tmp_path)
    monkeypatch.setattr(move_bot, "preflight", lambda *_: move)
    monkeypatch.setattr(move_bot, "apply_move", lambda *_args, **_kwargs: pytest.fail("preview mutated"))
    args = Namespace(root=str(tmp_path), bot="worker", to="target", from_fleet="source",
                     apply=False, cleanup_source=False, force=False)
    result = move_bot.dispatch(args)
    assert result.data["state"] == "preview"
    assert result.data["host_restart_scope"] == "all_declared_fleets"
    assert result.data["changed"] is False


def _preflight_fixture(tmp_path, monkeypatch):
    from claudlobby import activation_enrollment, activation_state, active_config
    from claudlobby import config_plan, context, releases, supervision_inventory
    from claudlobby.commands import _helpers
    from claudlobby.commands import host
    from claudlobby.paths import Paths

    root = tmp_path / "host"
    source_dir = root / "local/source"
    target_dir = tmp_path / "external/target"
    source_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    # Authored manifests on disk: exactly this move (worker leaves source and
    # joins target). The frozen side parses the pre-move bytes with the same
    # load_fleet_snapshot owner that context_from_plan uses.
    (source_dir / "fleet.yaml").write_text(_fleet_yaml("source", "lead", LEAD, OTHER))
    (target_dir / "fleet.yaml").write_text(_fleet_yaml("target", "boss", BOSS, WORKER))
    package = SimpleNamespace(native=tmp_path / "sealed-native", artifact_id="artifact")
    source_paths = Paths(root, package=package, fleet_dir=source_dir)
    target_paths = Paths(root, package=package, fleet_dir=target_dir)
    bot_dir = source_paths.bot_runtime("worker")
    bot_dir.mkdir(parents=True)
    (bot_dir / "bot.conf").write_text("BOT_ID=worker\n")
    bot = SimpleNamespace(bot_id="worker")
    source = SimpleNamespace(paths=source_paths, fleet=load_fleet_snapshot(
        source_paths.fleet_yaml, _fleet_yaml("source", "lead", LEAD, WORKER, OTHER).encode(), None)[0])
    frozen_target = SimpleNamespace(paths=target_paths, fleet=load_fleet_snapshot(
        target_paths.fleet_yaml, _fleet_yaml("target", "boss", BOSS).encode(), None)[0])
    effects = {"fleet_manifests": {"source": str(source_paths.fleet_yaml), "target": str(target_paths.fleet_yaml)},
               "fleet_sources": {"source": {"fleet": {"path": str(source_paths.fleet_yaml), "sha256": "source-hash"}},
                                 "target": {"fleet": {"path": str(target_paths.fleet_yaml), "sha256": "target-hash"}}}}
    # Selected inputs: the two manifests as they were before this move's roster
    # edits, and one unrelated host input that must still match.
    host_input = root / "host-override.yaml"
    host_input.write_text("reviewed: true\n")
    inputs = {str(host_input): {"follow_links": True,
                                "state": config_plan.path_state(host_input, source=True)}}
    for manifest in (source_paths.fleet_yaml, target_paths.fleet_yaml):
        inputs[str(manifest)] = {"follow_links": True, "state": {"node": {"kind": "file", "sha256": "old"}}}
    plan = SimpleNamespace(fleets=("source", "target"), release_id="selected", release_seal="seal",
                           effects=effects, inputs=inputs)
    release = SimpleNamespace(release_id="selected", seal_sha256="seal", native_path=package.native,
                              inputs=SimpleNamespace(artifact_id="artifact"))
    monkeypatch.setattr(context, "resolve_paths", lambda **_: SimpleNamespace(root=root, package=package))
    monkeypatch.setattr(activation_state, "read_selection", lambda *_: {"plan_id": "plan", "release_id": "selected"})
    monkeypatch.setattr(config_plan, "read_plan", lambda *_: plan)
    monkeypatch.setattr(releases, "read_release", lambda *_: release)
    monkeypatch.setattr(active_config, "context_from_plan", lambda _plan, name, **_kwargs: (
        source if name == "source" else frozen_target))
    def authored_context(paths):
        fleet, defaults = load_fleet_snapshot(paths.fleet_yaml, paths.fleet_yaml.read_bytes(), None)
        return SimpleNamespace(paths=paths, fleet=fleet, defaults=defaults)

    monkeypatch.setattr(context, "load_context", authored_context)
    monkeypatch.setattr(_helpers, "_validation_gate", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(host, "_operator_shell", lambda *_: None)
    monkeypatch.setattr(supervision_inventory, "Adapter", lambda *_: SimpleNamespace(read=lambda *_: ""))
    monkeypatch.setattr(supervision_inventory, "_catalog", lambda *_: ("Linux", "", (), set(), {}))
    monkeypatch.setattr(activation_enrollment, "selected_bot_entry", lambda *_: {"installed": str(root / "units/worker.service"),
                                    "target": "worker.service"})
    args = Namespace(root=str(root), bot="worker", to="target", from_fleet="source",
                     apply=False, cleanup_source=False, force=False)
    return args, host_input, (source_dir / "fleet.yaml", target_dir / "fleet.yaml")


LEAD, BOSS = "    lead: {expertise: [lead]}\n", "    boss: {expertise: [lead]}\n"
WORKER, OTHER = "    worker: {expertise: [build]}\n", "    other: {expertise: [review]}\n"


def _fleet_yaml(name, manager, *bots, extra=""):
    return (f"fleet:\n  name: {name}\n  manager: {manager}\n  system_defaults: false\n{extra}"
            "  bots:\n" + "".join(bots))


def test_preview_preflight_reads_frozen_manifest_paths_not_source_records(tmp_path, monkeypatch):
    from claudlobby.config_plan import path_state

    args, _, manifests = _preflight_fixture(tmp_path, monkeypatch)
    preview = move_bot.dispatch(args)
    assert preview.data["state"] == "preview"
    assert preview.data["target_fleet"] == "target"
    assert preview.data["changed"] is False
    move = move_bot.preflight(args)
    assert dict(move.move_inputs) == {str(path): path_state(path, source=True) for path in manifests}
    assert move.selected_plan_id == "plan"
    assert move.access_input == str(Path.home() / ".claude/channels/telegram-worker/access.json")


def test_move_refuses_pending_unrelated_authoring_before_any_effect(tmp_path, monkeypatch):
    args, host_input, _ = _preflight_fixture(tmp_path, monkeypatch)
    host_input.write_text("reviewed: false\n")  # validated but never activated
    with pytest.raises(CommandFailure, match="authored input changed") as refused:
        move_bot.dispatch(args)
    assert "host activate" in refused.value.error.hint


@pytest.mark.parametrize(("side", "text", "changed"), [
    # Another bot's declaration edited in the source manifest.
    ("source", _fleet_yaml("source", "lead", LEAD, "    other: {expertise: [ship]}\n"), "source fleet.yaml has pending bots"),
    # Another bot added to the target manifest alongside the move.
    ("target", _fleet_yaml("target", "boss", BOSS, WORKER, OTHER), "target fleet.yaml has pending bots"),
    # A fleet-level field edited in the target manifest.
    ("target", _fleet_yaml("target", "boss", BOSS, WORKER, extra="  mission: pending edit\n"),
     "target fleet.yaml has pending mission"),
])
def test_move_refuses_unrelated_semantic_edits_in_its_own_manifests(tmp_path, monkeypatch,
                                                                    side, text, changed):
    args, _, manifests = _preflight_fixture(tmp_path, monkeypatch)
    manifests[0 if side == "source" else 1].write_text(text)
    with pytest.raises(CommandFailure, match=changed) as refused:
        move_bot.dispatch(args)
    assert "host activate" in refused.value.error.hint


def test_retained_copy_leaves_the_stop_record_behind(tmp_path):
    """#2243: the move stops the source through the stop door, which records that stop in data/.
    The moved bot is started by the move's activation, so a copied record would only be stale,
    and one left beside a unit that is later lost would keep that loss silent."""
    move = _move(tmp_path)
    (move.source_dir / "data").mkdir()
    (move.source_dir / "data" / "record.json").write_text('{"kept":true}')
    (move.source_dir / "data" / ".stopped").write_text(
        '{"by": "operator", "reason": null, "request_id": "r", '
        '"stopped_at": "2026-10-10T07:00:00Z", "stopped_epoch": 1791615600}\n')

    move_bot.check_copy_destinations(move.source_dir, move.target_dir)
    move_bot.copy_retained(move)

    assert (move.target_dir / "data" / "record.json").read_text() == '{"kept":true}'  # data/ was copied
    assert not (move.target_dir / "data" / ".stopped").exists()
    assert (move.source_dir / "data" / ".stopped").exists()  # the source keeps its own
