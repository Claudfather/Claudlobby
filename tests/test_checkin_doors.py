# tests/test_checkin_doors.py
"""The check-in's write doors (spec §7): checkin-record.sh (Task 1), and
dispatch-task.sh --project / --checkin plus the tg-post.sh alias (Task 2).
Two rigs: a STUB lib-common that captures the batch (tests/test_briefing_trigger.py's
shape), and the REAL shim landing rows in a scratch plane through the cold CLI
rung (tests/test_task_id_dispatch.py's _fake_lib / plane_env). Only the real rig
carries lib-common's ERR trap, so the refusal case runs there too."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.conftest import constructed_env
from tests.plane_fixtures import plane_root
from tests.plane_fixtures import ro as _ro

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib"
CLI = Path(sys.executable).parent / "claudlobby"

STUB_LIB_COMMON = """\
#!/bin/bash
install_error_trap() { :; }
trap 'true' EXIT
plane_armed() { [ "${PLANE_EMIT_DISABLED:-0}" != "1" ]; }
plane_mint_id() { printf '%s_%s' "$1" "${STUB_MINT_HEX:-0123456789abcdef0123456789abcdef}"; }
safe_mktemp() { mktemp "${TMPDIR:-/tmp}/ck-stub.XXXXXXXX"; }
json_escape() { printf '%s' "$1" | python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read())[1:-1])'; }
show_help() { awk 'NR == 1 { next } /^[^#]/ { exit } { sub(/^# ?/, ""); print }' "$1"; }
plane_emit_events() { cat > "$EMIT_CAPTURE"; PLANE_EMIT_LAST_RC="${STUB_EMIT_RC:-0}"; }
PLANE_EMIT_LAST_RC=0
"""
STUB_CK = "ck_0123456789abcdef0123456789abcdef"


def _decision() -> dict:
    return {"prev_checkin_id": None,
            "inputs_seen": {"open_tasks": 1, "stalls": 0, "unacked": 0, "issues_seen": None,
                            "issues_considered": 0, "knowledge_hits": 0, "considered": [], "unavailable": ["gh"]},
            "delta": {"tasks_opened": 0, "tasks_completed": 0, "stalls_appeared": 0, "stalls_cleared": 0,
                      "issues_new": 0, "messages_new": None, "held_pending": 0},
            "action": "nothing", "project_key": None,
            "rationale": "nothing worth starting",
            "raise": {"decided": False, "reason": "no delta", "held": []}}


def _stub_rig(tmp_path: Path) -> tuple[Path, dict]:
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "lib-common.sh").write_text(STUB_LIB_COMMON)
    for name in ("checkin-record.sh", "checkin-contract.py"):
        shutil.copy(LIB / name, lib / name)
        (lib / name).chmod(0o755)
    env = {"EMIT_CAPTURE": str(tmp_path / "emit.json"), "FLEET_NAME": "f", "BOT_ID": "mgr",
           "TMPDIR": str(tmp_path), "PATH": os.environ["PATH"]}
    return lib, env


def _run(lib: Path, script: str, env: dict, stdin: str = "", *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(lib / script), *args], input=stdin, capture_output=True,
                          text=True, env=constructed_env(**env), timeout=60)


def _captured(env: dict) -> dict:
    return json.loads(Path(env["EMIT_CAPTURE"]).read_text())


# --- checkin-record.sh, stub transport ----------------------------------------

def test_record_lands_one_actor_anchored_decision_with_the_shared_mint(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == STUB_CK                   # minted by plane_mint_id ck, never a private mint
    (e,) = _captured(env)["events"]
    assert e["event_type"] == "system" and e["emitter"] == "checkin-record"
    assert e["source_ref"] == f"checkin:{STUB_CK}"
    assert e["payload"]["event"] == "checkin_decision"
    assert e["payload"]["subject_kind"] == "actor" and e["payload"]["subject"] == "bot:f/mgr"
    assert e["payload"]["data"]["checkin_id"] == STUB_CK and e["payload"]["data"]["action"] == "nothing"
    assert e["payload"]["data"]["inputs_seen"]["issues_seen"] is None      # could not measure, kept null


def test_record_prefers_bot_id_over_bot_name(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", {**env, "BOT_NAME": "Display Name"}, json.dumps(_decision()))
    assert r.returncode == 0, r.stderr
    assert _captured(env)["events"][0]["payload"]["subject"] == "bot:f/mgr"


def test_record_refuses_a_malformed_decision_at_rc_2_and_records_nothing(tmp_path):
    lib, env = _stub_rig(tmp_path)
    bad = _decision(); bad["action"] = "coffee"
    r = _run(lib, "checkin-record.sh", env, json.dumps(bad))
    assert r.returncode == 2
    assert "action must be one of" in r.stderr and "nothing recorded" in r.stderr
    assert not Path(env["EMIT_CAPTURE"]).exists()


def test_record_says_rc_3_when_the_plane_did_not_record(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", {**env, "STUB_EMIT_RC": "5"}, json.dumps(_decision()))
    assert r.returncode == 3 and "NOT recorded" in r.stderr and r.stdout == ""


def test_record_silenced_only_by_the_harness_exemption_is_rc_3(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", {**env, "PLANE_EMIT_DISABLED": "1"}, json.dumps(_decision()))
    assert r.returncode == 3 and "PLANE_EMIT_DISABLED" in r.stderr
    assert not Path(env["EMIT_CAPTURE"]).exists()


def test_record_needs_an_identity_rc_1(tmp_path):
    lib, env = _stub_rig(tmp_path)
    env = {k: v for k, v in env.items() if k not in ("FLEET_NAME", "BOT_ID")}
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()))
    assert r.returncode == 1 and "identity" in r.stderr


def test_record_unknown_flag_is_rc_1(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()), "--bot", "x")
    assert r.returncode == 1 and "unknown flag" in r.stderr and r.stdout == ""


def test_record_dry_run_validates_prefixes_the_id_and_writes_nothing(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()), "--dry-run")
    assert r.returncode == 0 and r.stdout.strip() == f"DRY-RUN {STUB_CK}"   # never mistakable for a recorded id
    assert not Path(env["EMIT_CAPTURE"]).exists()
    bad = _decision(); bad["action"] = "coffee"
    r = _run(lib, "checkin-record.sh", env, json.dumps(bad), "--dry-run")
    assert r.returncode == 2 and r.stdout == ""                             # validation IS the dry run's point


def test_record_help_prints_the_whole_header(tmp_path):
    lib, env = _stub_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, "", "--help")
    assert r.returncode == 0 and "exit:" in r.stdout and "3 the plane could not record" in r.stdout


# --- the REAL spine: the row lands in a scratch plane -----------------------------

REAL_DOOR_FILES = ("checkin-record.sh", "checkin-contract.py", "lib-common.sh",
                   "plane-emit.sh", "plane-socket-client.py")


def _real_rig(tmp_path: Path) -> tuple[Path, dict]:
    root = plane_root(tmp_path)
    lib = root / "lib"
    lib.mkdir()
    for name in REAL_DOOR_FILES:
        (lib / name).symlink_to(LIB / name)
    env = {"CLAUDLOBBY_ROOT": str(root), "FLEET_NAME": "f", "BOT_ID": "mgr",
           "PLANE_EMIT_CLI": str(CLI), "PLANE_SOCKET": str(root / "no-daemon.sock"),
           "HOME": str(root), "PATH": os.environ["PATH"]}
    return lib, env


def _rows(root: Path) -> list[tuple]:
    if not (root / "state" / "plane" / "plane.db").exists():
        return []
    with _ro(root) as conn:
        return [tuple(r) for r in conn.execute("SELECT kind, event, severity FROM events")]


def test_the_decision_lands_on_a_real_plane(tmp_path):
    lib, env = _real_rig(tmp_path)
    r = _run(lib, "checkin-record.sh", env, json.dumps(_decision()))
    assert r.returncode == 0, r.stderr
    ck = r.stdout.strip()
    assert ck.startswith("ck_") and len(ck) == 35
    with _ro(Path(env["CLAUDLOBBY_ROOT"])) as conn:
        row = conn.execute(
            "SELECT severity, source_ref, subject_alias, detail, detail_truncated FROM events"
            " WHERE kind='system' AND event='checkin_decision'").fetchone()
    assert row is not None
    assert row["severity"] == "notice" and row["source_ref"] == f"checkin:{ck}"
    assert row["subject_alias"] == "bot:f/mgr" and row["detail_truncated"] == 0
    assert json.loads(row["detail"])["action"] == "nothing"
    assert _rows(Path(env["CLAUDLOBBY_ROOT"])) == [("system", "checkin_decision", "notice")]   # and nothing else


def test_a_refused_decision_leaves_no_row_at_all_on_a_real_plane(tmp_path):
    # cycle-3 B2: under the REAL lib-common the ERR trap fires inside a command
    # substitution and lands a critical script_error while the door says "nothing
    # recorded"; the door runs the contract as a form-D pipeline so nothing fires
    lib, env = _real_rig(tmp_path)
    bad = _decision(); bad["action"] = "coffee"
    r = _run(lib, "checkin-record.sh", env, json.dumps(bad))
    assert r.returncode == 2 and "nothing recorded" in r.stderr and r.stdout == ""
    assert _rows(Path(env["CLAUDLOBBY_ROOT"])) == []
