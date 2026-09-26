"""Clearable fields and public task visibility; no live fleet or service calls."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from claudlobby import status
from claudlobby.paths import Paths
from claudlobby.utilization import compute_bot_utilization, write_utilization_json

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def state_writer(tmp_path):
    root, home = tmp_path / "root", tmp_path / "home"
    home.mkdir()
    lib = root / "lib"
    lib.mkdir(parents=True)
    for name in ("fleet-state-update.sh", "lib-common.sh", "supervisor.sh"):
        shutil.copyfile(ROOT / "lib" / name, lib / name)
    state = root / "state/fleet-state.json"
    env = {
        "PATH": os.environ["PATH"], "HOME": str(home), "CLAUDLOBBY_ROOT": str(root),
        "FLEET_STATE_PATH": str(state), "FLEET_NAME": "fixture", "TMPDIR": str(tmp_path),
        "PLANE_EMIT_DISABLED": "1", "PLANE_EMIT_CLI": "/usr/bin/false",
        "PLANE_SOCKET": str(tmp_path / "absent.sock"),
        "TELEGRAM_STATE_DIR": str(home / "channel"),
    }
    def run(*args):
        proc = subprocess.run(["/bin/bash", str(lib / "fleet-state-update.sh"), *args],
                              env=env, cwd=root, capture_output=True, text=True, timeout=15)
        assert proc.returncode == 0, proc.stderr
        assert not (root / "state/plane").exists()
        assert not (home / "channel").exists()
        return json.loads(state.read_text())
    return run, state, root


@pytest.mark.parametrize("index,field", [(0, "current_task"), (1, "current_repo"), (2, "last_completed")])
def test_dash_clears_only_selected_field_and_empty_keeps_others(state_writer, index, field):
    run, _, _ = state_writer
    before = run("probe", "working", "task", "repo", "last")["bots"]["probe"]
    args = ["", "", ""]
    args[index] = "-"
    after = run("probe", "idle", *args)["bots"]["probe"]
    assert after[field] is None
    for other in {"current_task", "current_repo", "last_completed"} - {field}:
        assert after[other] == before[other]


def test_nonempty_sets_empty_preserves_and_each_row_stamps_utc(state_writer):
    run, state, _ = state_writer
    first = run("probe", "working", "task", "repo", "last")
    probe = first["bots"]["probe"]
    assert probe["current_task"] == "task" and probe["current_repo"] == "repo"
    assert probe["last_completed"] == "last" and probe["fleet"] == "fixture"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", probe["updated_ts"])
    assert probe["updated_ts"] == first["updated"]
    # A deterministic stale stamp proves an update occurred without clock sleeps.
    first["bots"]["probe"]["updated_ts"] = "2000-01-01T00:00:00Z"
    first["bots"]["sibling"] = {"status": "working", "current_task": "untouched", "updated_ts": "1999-01-01T00:00:00Z"}
    state.write_text(json.dumps(first))
    after = run("probe", "blocked", "", "", "")
    assert after["bots"]["probe"]["updated_ts"] != "2000-01-01T00:00:00Z"
    assert after["bots"]["probe"]["current_task"] == "task"
    assert after["bots"]["sibling"] == first["bots"]["sibling"]


def test_existing_prune_stamp_and_dry_run_behavior_remain(state_writer):
    run, state, root = state_writer
    fleet = root / "local/fixture"
    fleet.mkdir(parents=True)
    manifest = fleet / "fleet.yaml"
    manifest.write_text("fleet:\n  name: fixture\n  bots:\n    probe:\n      expertise: [x]\n")
    data = run("departed", "idle")
    data["updated"] = "2000-01-01T00:00:00Z"
    state.write_text(json.dumps(data))
    before = state.read_bytes()
    run("prune", str(manifest), "--dry-run")
    assert state.read_bytes() == before
    after = run("prune", str(manifest))
    assert after["updated"] != "2000-01-01T00:00:00Z"
    assert "departed" not in after["bots"]


@pytest.mark.parametrize("state", ["idle", "offline", "unknown"])
def test_inactive_stale_task_is_not_current_in_any_status_renderer(monkeypatch, state):
    monkeypatch.setattr(status, "_COLOR", True)
    row = status.BotStatus(name="probe", state=state, current_task="STALE_TASK", last_completed="finished")
    table = status.format_table([row], "fixture")
    assert "STALE_TASK" not in table
    assert "\x1b[2mfinished\x1b[0m" in table
    assert "STALE_TASK" not in status.format_bot_detail(row)
    assert json.loads(status.format_json([row], "fixture"))["bots"][0]["current_task"] is None
    assert row.current_task == "STALE_TASK", "rendering must not rewrite the recorded object"


@pytest.mark.parametrize("state", ["working", "blocked"])
def test_active_and_blocked_tasks_stay_visible(monkeypatch, state):
    monkeypatch.setattr(status, "_COLOR", True)
    row = status.BotStatus(name="probe", state=state, current_task="LIVE_TASK", last_completed="finished")
    table = status.format_table([row], "fixture")
    assert "LIVE_TASK" in table and "\x1b[2mLIVE_TASK" not in table
    assert "LIVE_TASK" in status.format_bot_detail(row)
    assert json.loads(status.format_json([row], "fixture"))["bots"][0]["current_task"] == "LIVE_TASK"


@pytest.mark.parametrize("state", ["idle", "offline", "unknown", "working", "blocked"])
def test_utilization_projection_preserves_only_live_tasks(tmp_path, state):
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    recorded = {"status": state, "current_task": "recorded task"}
    result = compute_bot_utilization("probe", [], recorded, now)
    expected = "recorded task" if state in ("working", "blocked") else None
    assert result.current_task == expected
    output = write_utilization_json([result], Paths(root=tmp_path), now)
    assert json.loads(output.read_text())["bots"]["probe"]["current_task"] == expected
    assert recorded["current_task"] == "recorded task"
