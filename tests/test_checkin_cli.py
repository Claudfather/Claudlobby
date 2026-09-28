"""One selected-root check-in flow through the public CLI and real Plane."""

from dataclasses import replace
from datetime import datetime, timezone
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from claudlobby import activation, context
from claudlobby.__main__ import main
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.plane.db import db_file
from claudlobby.isolation import transcript_slug
from tests.package_fixtures import source_package
from tests.test_activation import cold, tmp_path  # noqa: F401 — short private socket root
from tests.test_checkin_contract import _decision
from tests.test_releases import installed  # noqa: F401 — cold fixture dependency
from tests.test_sprint_selection_record import _record as selection_record


@pytest.fixture
def active(cold, monkeypatch):  # noqa: F811 — fixture dependency
    root, release, plan, host = cold
    # Freeze real runner configuration into the selected plan used by this
    # public CLI fixture; the cold fixture's original plan has no runner.
    manifest = root / "fleet.yaml"
    manifest.write_text(manifest.read_text().replace(
        "worker: {expertise: [testing]}\n    manager: {expertise: [orchestration]}",
        "worker:\n      expertise: [testing]\n"
        "      autonomous_runner: {skill: /claudna:tech-debt, cadence: 1h, target_repo: org/repo}\n"
        "    manager:\n      expertise: [orchestration]\n"
        "      autonomous_runner: {skill: /claudna:tech-debt, cadence: 1h, target_repo: org/repo}"))
    effects = deepcopy(plan.effects)
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256,
                                plan.fleets, effects=effects)
    for path in plan.inputs:
        digest = builder.input_content(Path(path))
        if Path(path) == manifest:
            effects["fleet_sources"]["example"]["fleet"]["sha256"] = digest
    for change in plan.changes:
        target = Path(change.target)
        if change.after["kind"] == "directory":
            builder.directory(target)
        else:
            assert change.after["kind"] == "file"
            builder.file(target, plan.blob(change.after["sha256"]), mode=change.after["mode"])
    plan = builder.seal()
    for key in ("FLEET_NAME", "CLAUDLOBBY_FLEET", "BOT_ID", "BOT_NAME", "BOT_DIR",
                "FLEET_ROOT", "CLAUDLOBBY_RELEASE_ID"):
        monkeypatch.delenv(key, raising=False)
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    package = replace(source_package(), native=release.native_path,
                      artifact_id=release.inputs.artifact_id)
    monkeypatch.setattr(context, "get_resources", lambda: package)
    # This fixture seals minimal native assets. Keep real reader/decoder logic
    # while selecting the private test tree's assets at the import boundary.
    from claudlobby import brief, env_tiers, paths
    load_module = paths.load_lib_module
    native = source_package().native
    monkeypatch.setattr(paths, "load_lib_module", lambda directory, name:
                        load_module(native if directory == release.native_path else directory, name))
    monkeypatch.setattr(brief, "load_dispatch_doors", lambda selected:
                        load_module(native, "dispatch-overdue.py"))
    monkeypatch.setattr(env_tiers, "resolve", lambda paths, bot_name=None, fleet_name=None: {})
    return root, release


def test_automation_public_commands_use_selected_runner_and_reject_worker_routing(active, capsys,
                                                                                     monkeypatch):
    root, release = active
    state_path = root / "state/fleet-state.json"
    state_path.write_text(json.dumps({"bots": {
        "manager": {"fleet": "example", "status": "idle"},
        "worker": {"fleet": "example", "status": "idle", "unrelated": "preserved"}},
        "other": {"preserved": True}}))
    initial = _call(capsys, root, "bot", "automation", "status", "worker")["data"]
    assert initial["eligible"] is True and initial["runs_recorded"] == 0

    pause_id = str(uuid4())
    paused = _call(capsys, root, "bot", "automation", "pause", "worker",
                   "--reason", "Human review", "--request-id", pause_id)["data"]
    assert paused["recording"] == "committed" and paused["state"]["ineligible_reason"] == "paused"
    replay = _call(capsys, root, "bot", "automation", "pause", "worker",
                   "--reason", "Human review", "--request-id", pause_id)["data"]
    assert replay["replayed"] is True and replay["recording"] == "unchanged"
    assert len(json.loads(state_path.read_text())["bots"]["worker"]["autonomous_runner_controls"]) == 1

    resumed = _call(capsys, root, "bot", "automation", "resume", "worker",
                    "--request-id", str(uuid4()))["data"]
    assert resumed["state"]["eligible"] is True
    recorded = _call(capsys, root, "bot", "automation", "record", "worker",
                     "--outcome", "completed", "--pr", "https://github.com/org/repo/pull/12",
                     "--request-id", str(uuid4()))["data"]
    assert recorded["state"]["runs_recorded"] == 1
    assert _call(capsys, root, "bot", "automation", "status", "worker")["data"]["last_run"][
        "pr_url"] == "https://github.com/org/repo/pull/12"
    stored = json.loads(state_path.read_text())
    assert stored["other"] == {"preserved": True} and stored["bots"]["worker"]["unrelated"] == "preserved"

    with monkeypatch.context() as generated:
        generated.setenv("CLAUDLOBBY_ROOT", str(root))
        generated.setenv("FLEET_NAME", "example")
        generated.setenv("BOT_ID", "worker")
        generated.setenv("CLAUDLOBBY_RELEASE_ID", release.release_id)
        refused = _call(capsys, root, "bot", "automation", "pause", "manager",
                        "--reason", "Wrong role", "--request-id", str(uuid4()), expected=4)
        assert refused["error"]["code"] == "conflict"
        assert "cannot update this bot" in refused["error"]["message"]
    assert json.loads(state_path.read_text()) == stored


def _call(capsys, root, *argv, expected=0):
    assert main(["--root", str(root), "--json", *argv]) == expected
    value = json.loads(capsys.readouterr().out)
    assert value["schema_version"] == 1 and value["ok"] is (expected == 0)
    return value


def _counts(root):
    with sqlite3.connect(db_file(root)) as conn:
        return (conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0])


def test_record_list_show_and_assignment_link_keep_exact_selected_scope(active, capsys, tmp_path):  # noqa: F811
    root, release = active
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps(selection_record()), encoding="utf-8")
    assert _call(capsys, root, "checkin", "selection", "verify", str(selection))["data"]["verdict"] == "OK"
    mission = tmp_path / "mission.md"
    mission.write_text("**Current sprint focus:**\n\n1. Ship #4242\n", encoding="utf-8")
    assert _call(capsys, root, "checkin", "selection", "focus-refs", str(mission))["data"]["refs"] == ["4242"]
    decision = _decision(action="dispatch", project_key="example")
    file = tmp_path / "decision.json"
    file.write_text(json.dumps(decision), encoding="utf-8")
    request_id = str(uuid4())
    before = _counts(root)
    impossible = selection_record()
    impossible["cut"]["selected_ids"] = [999]
    selection.write_text(json.dumps(impossible), encoding="utf-8")
    assert _call(capsys, root, "checkin", "record", "--file", str(file),
                 "--selection-file", str(selection), "--request-id", request_id,
                 expected=2)["error"]["code"] == "invalid_argument"
    assert _counts(root) == before
    dry = _call(capsys, root, "checkin", "record", "--file", str(file),
                "--request-id", request_id, "--dry-run")
    assert dry["data"]["checkin_id"] is None and _counts(root) == before
    file.write_text('{"action":"coffee"}', encoding="utf-8")
    assert _call(capsys, root, "checkin", "record", "--file", str(file),
                 "--request-id", request_id, expected=2)["error"]["code"] == "invalid_argument"
    assert _counts(root) == before
    file.write_text(json.dumps(decision), encoding="utf-8")

    recorded = _call(capsys, root, "checkin", "record", "--file", str(file),
                     "--request-id", request_id)
    checkin_id = recorded["data"]["checkin_id"]
    assert recorded["request_id"] == request_id and recorded["release_id"] == release.release_id
    assert recorded["data"]["actor"].startswith("human:")
    assert recorded["data"]["recording"] == "committed"
    assert recorded["data"]["request_persisted"] is True
    assert recorded["data"]["replayed"] is False
    after = _counts(root)
    replay = _call(capsys, root, "checkin", "record", "--file", str(file),
                   "--request-id", request_id)
    assert replay["data"]["replayed"] is True and replay["data"]["checkin_id"] == checkin_id
    assert _counts(root) == after
    listed = _call(capsys, root, "checkin", "list", "--last")["data"]["items"]
    assert len(listed) == 1 and listed[0]["checkin_id"] == checkin_id
    assert listed[0]["actor"] == recorded["data"]["actor"] and listed[0]["bot"] == ""
    assert _call(capsys, root, "checkin", "list", "--bot", "worker")["data"]["items"] == []
    summary = _call(capsys, root, "checkin", "list", "--summary")["data"]["summary"]
    assert summary["totals"]["checkins"] == 1
    assert _call(capsys, root, "checkin", "list", "--summary", "--limit", "1",
                 expected=2)["error"]["code"] == "invalid_argument"
    shown = _call(capsys, root, "checkin", "show", checkin_id)["data"]["decision"]
    assert shown["checkin_id"] == checkin_id and shown["record"]["action"] == "dispatch"

    admitted = _call(capsys, root, "task", "admit", "--title", "Selected work",
                     "--request-id", str(uuid4()))["data"]
    assigned = _call(capsys, root, "task", "assign", admitted["task_id"],
                     "--bot", "worker", "--checkin", checkin_id,
                     "--request-id", str(uuid4()))["data"]
    linked = _call(capsys, root, "checkin", "show", checkin_id)["data"]["decision"]
    assert [row["assignment_id"] for row in linked["dispatches"]] == [assigned["assignment_id"]]

    open_id = str(uuid4())
    stream = _call(capsys, root, "workstream", "open", "Human signoff",
                   "--request-id", open_id)["data"]["workstream_ids"][0]
    assert _call(capsys, root, "workstream", "open", "Human signoff",
                 "--request-id", open_id)["data"]["replayed"] is True
    assert _call(capsys, root, "workstream", "show", stream)["data"]["workstream"]["title"] == "Human signoff"
    _call(capsys, root, "workstream", "block", stream, "--on", "human:reviewer",
          "--note", "Awaiting signoff", "--request-id", str(uuid4()))
    listed_stream = _call(capsys, root, "workstream", "list")["data"]["workstreams"][stream]
    assert listed_stream["status"] == "blocked" and listed_stream["waiting_on"] == "human:reviewer"

    transcript_dir = Path.home() / ".claude/projects" / transcript_slug(root / "runtime/bots/worker")
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "session.jsonl").write_text(json.dumps({
        "type": "assistant", "sessionId": "s-one", "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": {"id": "m-one", "model": "claude-test", "usage": {
            "input_tokens": 11, "output_tokens": 7,
            "cache_creation_input_tokens": 3, "cache_read_input_tokens": 5}}}) + "\n")
    bot_usage = _call(capsys, root, "bot", "usage", "worker", "--since", "24h")
    assert [bot_usage["data"]["usage"][field] for field in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens")] == [11, 7, 3, 5]
    assert bot_usage["data"]["coverage"]["status"] == "observed"
    assert bot_usage["data"]["quota"]["status"] == "unavailable"
    assert bot_usage["data"]["shared_account_with_selected_fleet_bots"] == ["manager"]
    fleet_usage = _call(capsys, root, "fleet", "usage", "--since", "24h")
    assert fleet_usage["data"]["usage"]["input_tokens"] == 11
    assert fleet_usage["data"]["coverage"]["status"] == "partial"
    assert fleet_usage["data"]["coverage"]["bots_observed"] == 1
    assert _call(capsys, root, "fleet", "usage", "--since", "8d", expected=2)["error"]["code"] == "invalid_argument"
