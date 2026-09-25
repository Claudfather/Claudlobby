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
                   "supervisor.sh", "plane-emit.sh", "plane-socket-client.py")


def _real_rig(tmp_path: Path, *, scratch_plane_env) -> tuple[Path, dict]:
    root = plane_root(tmp_path)
    lib = root / "lib"
    lib.mkdir()
    for name in REAL_DOOR_FILES:
        (lib / name).symlink_to(LIB / name)
    env = {**scratch_plane_env(root), "FLEET_NAME": "f", "BOT_ID": "mgr",

           "HOME": str(root), "PATH": os.environ["PATH"]}
    return lib, env


def _rows(root: Path) -> list[tuple]:
    if not (root / "state" / "plane" / "plane.db").exists():
        return []
    with _ro(root) as conn:
        return [tuple(r) for r in conn.execute("SELECT kind, event, severity FROM events")]


def test_the_decision_lands_on_a_real_plane(tmp_path, *, scratch_plane_env):
    lib, env = _real_rig(tmp_path, scratch_plane_env=scratch_plane_env)
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


def test_a_refused_decision_leaves_no_row_at_all_on_a_real_plane(tmp_path, *, scratch_plane_env):
    # cycle-3 B2: under the REAL lib-common the ERR trap fires inside a command
    # substitution and lands a critical script_error while the door says "nothing
    # recorded"; the door runs the contract as a form-D pipeline so nothing fires
    lib, env = _real_rig(tmp_path, scratch_plane_env=scratch_plane_env)
    bad = _decision(); bad["action"] = "coffee"
    r = _run(lib, "checkin-record.sh", env, json.dumps(bad))
    assert r.returncode == 2 and "nothing recorded" in r.stderr and r.stdout == ""
    assert _rows(Path(env["CLAUDLOBBY_ROOT"])) == []


# --- dispatch-task.sh --project / --checkin; tg-post.sh alias (Task 2) ----------

from tests.test_task_id_dispatch import _bash, _fake_lib

DISPATCH_STUB = "#!/bin/bash\nprintf '%s\\n' \"$2\" > \"$DISPATCH_CAPTURE\"\nexit 0\n"
CK = "ck_" + "a" * 32


def test_dispatch_project_alone_opens_the_envelope_and_stamps_the_work_item(tmp_path, *, scratch_plane_env):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB, scratch_plane_env=scratch_plane_env)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    r = _bash(f'"{libdir}/dispatch-task.sh" --project shop w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr
    sent = (tmp_path / "sent.txt").read_text()
    assert sent.startswith("[BOTCOMMAND]") and "| project:shop" in sent     # --project ALONE opens the gate
    with _ro(tmp_path) as conn:
        row = conn.execute("SELECT project_key FROM work_items").fetchone()
    assert row["project_key"] == "shop"


def test_dispatch_refuses_a_non_slug_project(tmp_path, *, scratch_plane_env):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB, scratch_plane_env=scratch_plane_env)
    r = _bash(f'"{libdir}/dispatch-task.sh" --project "Not Slug" w1 "x"', env=env)
    # the literal validation text (dispatch-task.sh:130), not the unknown-flag
    # catch-all -- "unknown flag '--project'" also contains the substring "project"
    assert r.returncode == 1 and "must be a projects.yaml slug" in r.stderr


def test_dispatch_checkin_appends_the_join_row_to_the_same_batch(tmp_path, *, scratch_plane_env):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB, scratch_plane_env=scratch_plane_env)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    r = _bash(f'"{libdir}/dispatch-task.sh" --project shop --checkin {CK} w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "sent.txt").read_text().startswith("[BOTCOMMAND]")   # the send happened
    with _ro(tmp_path) as conn:
        asg = conn.execute("SELECT assignment_id, work_item_id, source_ref, assigned_by_uid FROM assignments").fetchone()
        link = conn.execute("SELECT detail, subject_uid, source_ref, severity FROM events"
                            " WHERE kind='system' AND event='checkin_dispatch'").fetchone()
    assert asg is not None and link is not None
    d = json.loads(link["detail"])
    assert d["checkin_id"] == CK and d["assignment_id"] == asg["assignment_id"]
    assert d["work_item_id"] == asg["work_item_id"] and d["task_id"].startswith("t-")
    assert link["source_ref"] == asg["source_ref"] and link["severity"] == "notice"
    assert link["subject_uid"] == asg["assigned_by_uid"]      # the dispatcher, by the plane's own alias rule


def test_dispatch_checkin_on_an_id_less_task_dispatch_records_task_id_null(tmp_path, *, scratch_plane_env):
    # a flagless task send is TRACKED (#1491: the gate is the type) but mints no
    # legacy id, so the join carries task_id null -- never "" (cycle-3 gap)
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB, scratch_plane_env=scratch_plane_env)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    r = _bash(f'"{libdir}/dispatch-task.sh" --checkin {CK} w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr
    with _ro(tmp_path) as conn:
        asg = conn.execute("SELECT source_ref FROM assignments").fetchone()
        link = conn.execute("SELECT detail FROM events WHERE kind='system' AND event='checkin_dispatch'").fetchone()
    assert asg is not None and asg["source_ref"].startswith("dispatch-log:sha:")
    assert link is not None and json.loads(link["detail"])["task_id"] is None


def test_dispatch_checkin_on_an_untracked_dispatch_is_disclosed_not_dropped(tmp_path, *, scratch_plane_env):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB, scratch_plane_env=scratch_plane_env)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    r = _bash(f'"{libdir}/dispatch-task.sh" --type query --checkin {CK} w1 "where are you?"', env=env)
    assert r.returncode == 0, r.stderr
    assert "--checkin ignored" in r.stderr and "query" in r.stderr
    with _ro(tmp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event='checkin_dispatch'").fetchone()[0] == 0


def test_dispatch_refuses_a_malformed_checkin_id(tmp_path, *, scratch_plane_env):
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB, scratch_plane_env=scratch_plane_env)
    r = _bash(f'"{libdir}/dispatch-task.sh" --checkin nope w1 "x"', env=env)
    # the literal validation text (dispatch-task.sh:133), not the unknown-flag
    # catch-all -- "unknown flag '--checkin'" also contains the substring "--checkin"
    assert r.returncode == 1 and "must be a check-in id" in r.stderr


def test_dispatch_checkin_without_a_value_is_rc_1_never_0(tmp_path, *, scratch_plane_env):
    # flag FIRST and alone: the parse loop stops at the first positional
    # (dispatch-task.sh:115), so a trailing flag would be task text. Through
    # _fake_lib the REAL lib-common (and its EXIT trap) is in play.
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB, scratch_plane_env=scratch_plane_env)
    r = _bash(f'"{libdir}/dispatch-task.sh" --checkin', env=env)
    assert r.returncode == 1 and "--checkin needs a value" in r.stderr and r.stdout == ""


def test_plane_lookup_answers_a_checkin_id(tmp_path, *, scratch_plane_env):
    from claudlobby.plane.emit_api import emit_batch
    from tests.test_task_id_dispatch import plane_env
    plane_env(tmp_path, scratch_plane_env=scratch_plane_env)
    emit_batch(tmp_path, [{"event_type": "system", "emitter": "t", "fleet": "f", "source_ref": f"checkin:{CK}",
                           "payload": {"event": "checkin_decision", "subject_kind": "actor", "subject": "bot:f/mgr", "data": {"schema": 1}}}])
    look = [sys.executable, "-S", "-E", str(LIB / "plane-lookup.py"), "--root", str(tmp_path), "--checkin-id"]
    hit = subprocess.run([*look, CK], capture_output=True, text=True)
    assert hit.returncode == 0 and hit.stdout.strip() == CK, hit.stderr
    miss = subprocess.run([*look, "ck_" + "f" * 32], capture_output=True, text=True)
    assert miss.returncode == 0 and miss.stdout == "" and "no checkin_decision" in miss.stderr


def test_dispatch_checkin_to_a_decision_the_plane_cannot_see_is_disclosed_not_refused(tmp_path, *, scratch_plane_env):
    # the skill hands the id over in the same call (ck=$(record) && dispatch), a hand
    # caller pastes it; a well-formed id can still name nothing (mis-copied by hand, or
    # a record the shim spooled): say so, record the join as given.
    # The plane must EXIST for this to be "cannot see" rather than "cannot answer"
    # (a fresh rig has no db until the first emit), so one unrelated row seeds it.
    from claudlobby.plane.emit_api import emit_batch
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB, scratch_plane_env=scratch_plane_env)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    emit_batch(tmp_path, [{"event_type": "system", "emitter": "t", "fleet": "f",
                           "payload": {"event": "report_status", "subject_kind": "actor", "subject": "bot:f/w1", "data": {"status": "progress"}}}])
    r = _bash(f'"{libdir}/dispatch-task.sh" --project shop --checkin {CK} w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr
    assert "names no checkin_decision" in r.stderr
    with _ro(tmp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event='checkin_dispatch'").fetchone()[0] == 1


def test_dispatch_checkin_when_the_plane_cannot_answer_says_so_not_absent(tmp_path, *, scratch_plane_env):
    # unreachable is not empty (source_state): a root whose plane db cannot be
    # opened must not print the "names no checkin_decision" line. The lookup runs
    # in the join block, which sits INSIDE the door's first emit, so on a root with
    # no plane.db it answers "cannot answer" (rc 3), not "cannot see" -- which is
    # why the cannot-see test seeds a row first. The unreachable shape tested here
    # is an UNOPENABLE db (a directory where the db file belongs: sqlite refuses
    # it, rc 3). The lookup is one python3 spawn on the pre-send path (cycle 9);
    # chunk 3 may move it below the send if the measured cost warrants.
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB, scratch_plane_env=scratch_plane_env)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    broken = tmp_path / "broken"
    (broken / "state" / "plane" / "plane.db").mkdir(parents=True)
    env["CLAUDLOBBY_ROOT"] = str(broken)
    r = _bash(f'"{libdir}/dispatch-task.sh" --project shop --checkin {CK} w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr                    # the door never blocks the send on a plane fault
    assert "the plane could not answer" in r.stderr and "names no checkin_decision" not in r.stderr


def test_dispatch_checkin_to_a_recorded_decision_is_quiet(tmp_path, *, scratch_plane_env):
    from claudlobby.plane.emit_api import emit_batch
    libdir, env = _fake_lib(tmp_path, DISPATCH_STUB, scratch_plane_env=scratch_plane_env)
    env["DISPATCH_CAPTURE"] = str(tmp_path / "sent.txt")
    emit_batch(tmp_path, [{"event_type": "system", "emitter": "t", "fleet": "f", "source_ref": f"checkin:{CK}",
                           "payload": {"event": "checkin_decision", "subject_kind": "actor", "subject": "bot:f/lead", "data": {"schema": 1}}}])
    r = _bash(f'"{libdir}/dispatch-task.sh" --project shop --checkin {CK} w1 "fix the feed"', env=env)
    assert r.returncode == 0, r.stderr
    assert "names no checkin_decision" not in r.stderr


def test_tg_post_anchors_the_sender_on_bot_id_with_the_hand_caller_fallback():
    text = (LIB / "tg-post.sh").read_text()
    assert 'bot:$FLEET_NAME/${BOT_ID:-$BOT_NAME}' in text        # a session has BOT_ID; bot-sweep-cron.sh sets only BOT_NAME
    assert 'bot:$FLEET_NAME/$BOT_NAME"' not in text
