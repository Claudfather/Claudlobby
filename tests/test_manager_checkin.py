"""Python-wrapped bash test for lib/manager-checkin.sh (manager check-in PR 2,
chunk 2: the trigger). Spec: documentation/plans/2026-09-13-manager-checkin-design.md
section 5.

The composed `<prefix>.manager-checkin` FLEET timer runs
``manager-checkin.sh <fleet> [--min-gap-s N]``. For each bot the roster
declares in the named fleet, it fires `/checkin` into the bot's own session
through the slash-aware dispatch.sh when the bot is a MANAGER, is EQUIPPED
(the composed `checkin` skill symlink resolves), its session is ALIVE, it is
IDLE, and the plane shows no recent `checkin_triggered` row for it inside the
min-gap window -- recording `checkin_triggered` on a real send and
`checkin_skipped` (with a reason) on every gated skip except the two silent
ones (not a manager, not equipped) and the two the plane cannot see (rate
limited, unreachable). It fails CLOSED: an unreachable plane (rc 3) skips
without firing and without a row.

Like test_briefing_trigger.py, this copies the real script next to stub
helpers so it exercises the real control flow without a live tmux server or a
real plane db. `lib-common.sh` is stubbed down to the 13 helpers the script
calls; `bot_is_manager` / `bot_conf_get` read the REAL bot.conf files
`_run` writes (so the BOT_ID-vs-directory-name distinction is genuine, not
env-steered), and `dispatch.sh` / `plane-lookup.py` are stand-ins that capture
their inputs to files the assertions read.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
TRIGGER = REPO / "lib" / "manager-checkin.sh"

DEFAULT_MIN_GAP_S = 2700

# Stub lib-common: every helper manager-checkin.sh sources. install_error_trap
# and the tmux/time/log primitives are no-ops or fixed values, exactly the
# test_briefing_trigger.py shape. bot_is_manager and bot_conf_get are REAL
# (minimal) reimplementations reading the bot.conf _run() writes, so the
# manager/worker and BOT_ID/directory-name distinctions are genuine rather
# than env-steered. declared_bots_strict, check_tmux_session, bot_is_busy and
# emit_fleet_event are steered by env / capture files, per test.
STUB_LIB_COMMON = """\
#!/bin/bash
install_error_trap() { :; }

declared_bots_strict() {
    local bad_out="${1:-}"
    if [ -s "$STUB_ROSTER_BAD_FILE" ]; then
        if [ -n "$bad_out" ]; then
            cat "$STUB_ROSTER_BAD_FILE" > "$bad_out"
        else
            cat "$STUB_ROSTER_BAD_FILE" >&2
        fi
    elif [ -n "$bad_out" ]; then
        : > "$bad_out"
    fi
    [ -s "$STUB_ROSTER_FILE" ] && cat "$STUB_ROSTER_FILE"
    if [ -s "$STUB_ROSTER_BAD_FILE" ]; then
        return 1
    fi
    return 0
}

bot_is_manager() {
    local bot_dir="$1" mgr bid
    mgr=$(grep '^MANAGER_TMUX=' "$bot_dir/bot.conf" 2>/dev/null | tail -1 | sed 's/^MANAGER_TMUX=//')
    bid=$(grep '^BOT_ID=' "$bot_dir/bot.conf" 2>/dev/null | tail -1 | sed 's/^BOT_ID=//')
    [ -n "$bid" ] && [ "$mgr" = "$bid" ]
}

bot_conf_get() {
    local bot_dir="$1" key="$2" default="$3" val
    val=$(grep "^$key=" "$bot_dir/bot.conf" 2>/dev/null | tail -1 | sed "s/^$key=//")
    if [ -n "$val" ]; then
        printf '%s' "$val"
    else
        printf '%s' "$default"
    fi
    return 0
}

tmux_socket_for_bot() { printf 'fakesock'; return 0; }
check_tmux_session() { return "${STUB_SESSION_RC:-0}"; }

# bot_is_busy: a plain fixed rc for every existing caller (STUB_BUSY_RC,
# default 1 = idle). STUB_BUSY_RC_2 additionally steers the SECOND and every
# later call in the process (via a call-count file), for the gate-vs-send
# re-check (A4) -- idle at the gate, busy at the send.
bot_is_busy() {
    local n
    n=$(( $(cat "$STUB_BUSY_CALLS" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$STUB_BUSY_CALLS"
    if [ "$n" -ge 2 ] && [ -n "${STUB_BUSY_RC_2:-}" ]; then
        return "$STUB_BUSY_RC_2"
    fi
    return "${STUB_BUSY_RC:-1}"
}

emit_fleet_event() {
    printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$1" "$2" "${3:-}" "${4:-}" "${5:-}" "${FLEET_NAME:-}" >> "$EVENTS_CAPTURE"
}

epoch_to_iso_utc() {
    local epoch="$1"
    if date -u -r "$epoch" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null; then
        return 0
    fi
    date -u -d "@$epoch" +%Y-%m-%dT%H:%M:%SZ
}

safe_mktemp() { mktemp; }
ts_iso() { printf '%s' "2026-07-16T00:00:00Z"; }
setup_log_dir() { mkdir -p "$(dirname "$1")" 2>/dev/null || true; }
json_escape() { printf '%s' "$1" | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g'; }
"""

# Stub dispatch.sh: capture <session>\\t<message>, exit with the steered code
# (test_briefing_trigger.py's STUB_DISPATCH, verbatim).
STUB_DISPATCH = """\
#!/bin/bash
printf '%s\\t%s' "$1" "$2" > "$DISPATCH_CAPTURE"
exit "${STUB_DISPATCH_RC:-0}"
"""

# Stub plane-lookup.py: record the argv the trigger called it with (the
# rate-limit read's own assertions), print a hit when the test wants the
# min-gap window to suppress the beat, and exit with the steered code (3 =
# unreachable, per plane-lookup.py's real contract).
STUB_PLANE_LOOKUP = """\
#!/usr/bin/env python3
import os
import sys

argv_capture = os.environ.get("STUB_PLANE_ARGV_CAPTURE")
if argv_capture:
    with open(argv_capture, "w") as f:
        f.write("\\t".join(sys.argv[1:]))

hits = os.environ.get("STUB_PLANE_HITS", "")
if hits:
    sys.stdout.write(hits)

sys.exit(int(os.environ.get("STUB_PLANE_RC", "0")))
"""


def _run(
    tmp_path: Path,
    *,
    bots: list[dict],
    fleet: str | None = "f",
    extra_args: list[str] | None = None,
    env_extra: dict | None = None,
    roster_bad: list[str] | None = None,
) -> tuple[int, str, str]:
    libdir = tmp_path / "lib"
    libdir.mkdir(exist_ok=True)
    (libdir / "lib-common.sh").write_text(STUB_LIB_COMMON)
    dispatch = libdir / "dispatch.sh"
    dispatch.write_text(STUB_DISPATCH)
    dispatch.chmod(0o755)
    plane_lookup = libdir / "plane-lookup.py"
    plane_lookup.write_text(STUB_PLANE_LOOKUP)
    plane_lookup.chmod(0o755)
    (libdir / "manager-checkin.sh").write_text(TRIGGER.read_text())

    bots_dir = tmp_path / "bots"
    roster_lines = []
    for spec in bots:
        dir_name = spec["dir"]
        bfleet = spec.get("fleet", "f")
        bot_id = spec.get("bot_id", dir_name)
        manager = spec.get("manager", True)
        equip = spec.get("equip", "equipped")

        bdir = bots_dir / dir_name
        bdir.mkdir(parents=True, exist_ok=True)
        mgr_value = bot_id if manager else "someone-else-entirely"
        (bdir / "bot.conf").write_text(f"BOT_ID={bot_id}\nMANAGER_TMUX={mgr_value}\n")

        if equip in ("equipped", "dangling"):
            skills = bdir / ".claude" / "skills"
            skills.mkdir(parents=True, exist_ok=True)
            link = skills / "checkin"
            # _run() may be called more than once against the same tmp_path
            # (the flag-vs-env min-gap test): drop any symlink a prior call
            # in this same test left behind before re-creating it.
            if link.is_symlink() or link.exists():
                link.unlink()
            if equip == "equipped":
                target = tmp_path / "skill-targets" / dir_name
                target.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target)
            else:
                link.symlink_to(tmp_path / "no-such-target" / dir_name)
        elif equip != "none":
            raise ValueError(f"unknown equip mode: {equip!r}")

        roster_lines.append(f"{dir_name}\t{bfleet}\t{bdir}")

    roster_file = tmp_path / "roster_stdout"
    roster_file.write_text("\n".join(roster_lines) + ("\n" if roster_lines else ""))
    roster_bad_file = tmp_path / "roster_bad"
    roster_bad_file.write_text(
        "\n".join(roster_bad) + "\n" if roster_bad else ""
    )

    env = {
        **os.environ,
        "CLAUDLOBBY_ROOT": str(tmp_path),
        "MANAGER_CHECKIN_LOG": str(tmp_path / "checkin.log"),
        "DISPATCH_CAPTURE": str(tmp_path / "dispatch_capture"),
        "EVENTS_CAPTURE": str(tmp_path / "events_capture"),
        "STUB_ROSTER_FILE": str(roster_file),
        "STUB_ROSTER_BAD_FILE": str(roster_bad_file),
        "STUB_PLANE_ARGV_CAPTURE": str(tmp_path / "plane_argv_capture"),
        "STUB_BUSY_CALLS": str(tmp_path / "busy_calls"),
    }
    env.pop("CLAUDLOBBY_FLEET", None)
    env.pop("CHECKIN_MIN_GAP_S", None)
    env.pop("FLEET_NAME", None)
    env.pop("PLANE_EMIT_DISABLED", None)
    if env_extra:
        env.update(env_extra)

    argv = ["bash", str(libdir / "manager-checkin.sh")]
    if fleet is not None:
        argv.append(fleet)
    if extra_args:
        argv.extend(extra_args)

    proc = subprocess.run(argv, env=env, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _dispatched(tmp_path: Path) -> str:
    f = tmp_path / "dispatch_capture"
    return f.read_text() if f.exists() else ""


def _events(tmp_path: Path) -> list[dict]:
    f = tmp_path / "events_capture"
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        if not line:
            continue
        parts = line.split("\t")
        parts += [""] * (6 - len(parts))
        event_type, source, data_json, bot_dir, bot_id, fleet_name_env = parts[:6]
        data = json.loads(data_json) if data_json else {}
        out.append(
            {
                "type": event_type,
                "source": source,
                "data": data,
                "bot_dir": bot_dir,
                "bot_id": bot_id,
                "fleet_name_env": fleet_name_env,
            }
        )
    return out


def _log(tmp_path: Path) -> str:
    f = tmp_path / "checkin.log"
    return f.read_text() if f.exists() else ""


def _plane_argv(tmp_path: Path) -> list[str]:
    f = tmp_path / "plane_argv_capture"
    return f.read_text().split("\t") if f.exists() else []


def _flag(argv: list[str], name: str) -> str | None:
    if name in argv:
        idx = argv.index(name)
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return None


def _since_epoch(argv: list[str]) -> float:
    since = _flag(argv, "--since")
    assert since is not None, argv
    return (
        datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def test_an_equipped_idle_manager_gets_slash_checkin_in_its_own_session(tmp_path):
    rc, _out, err = _run(tmp_path, bots=[{"dir": "mgr", "fleet": "f"}])
    assert rc == 0, err
    assert _dispatched(tmp_path) == "mgr\t/checkin"
    events = _events(tmp_path)
    assert [e["type"] for e in events] == ["checkin_triggered"]


def test_the_plane_row_is_anchored_on_BOT_ID_not_the_directory_name(tmp_path):
    rc, _out, err = _run(
        tmp_path, bots=[{"dir": "mgrdir", "fleet": "f", "bot_id": "lead"}]
    )
    assert rc == 0, err
    session, message = _dispatched(tmp_path).split("\t")
    assert session == "mgrdir"
    assert message == "/checkin"
    events = _events(tmp_path)
    assert len(events) == 1
    assert events[0]["type"] == "checkin_triggered"
    assert events[0]["bot_id"] == "lead"


def test_the_triggered_row_anchors_on_the_argv_fleet_not_an_ambient_one(tmp_path):
    rc, _out, err = _run(
        tmp_path,
        bots=[{"dir": "mgr", "fleet": "f"}],
        env_extra={"FLEET_NAME": "otherfleet"},
    )
    assert rc == 0, err
    events = _events(tmp_path)
    assert [e["type"] for e in events] == ["checkin_triggered"]
    assert events[0]["fleet_name_env"] == "f"


def test_a_worker_is_never_injected_into(tmp_path):
    rc, _out, err = _run(
        tmp_path, bots=[{"dir": "w1", "fleet": "f", "manager": False}]
    )
    assert rc == 0, err
    assert _dispatched(tmp_path) == ""
    assert _events(tmp_path) == []


def test_a_manager_without_the_composed_skill_symlink_is_skipped_silently(tmp_path):
    rc, _out, err = _run(
        tmp_path, bots=[{"dir": "mgr", "fleet": "f", "equip": "none"}]
    )
    assert rc == 0, err
    assert _dispatched(tmp_path) == ""
    assert _events(tmp_path) == []


def test_a_dangling_skill_symlink_counts_as_unequipped(tmp_path):
    rc, _out, err = _run(
        tmp_path, bots=[{"dir": "mgr", "fleet": "f", "equip": "dangling"}]
    )
    assert rc == 0, err
    assert _dispatched(tmp_path) == ""
    assert _events(tmp_path) == []


def test_a_manager_whose_session_is_down_is_recorded_not_injected(tmp_path):
    rc, _out, err = _run(
        tmp_path,
        bots=[{"dir": "mgr", "fleet": "f"}],
        env_extra={"STUB_SESSION_RC": "1"},
    )
    assert rc == 0, err
    assert _dispatched(tmp_path) == ""
    events = _events(tmp_path)
    assert len(events) == 1
    assert events[0]["type"] == "checkin_skipped"
    assert events[0]["data"]["reason"] == "session_down"


def test_a_busy_manager_is_never_injected_into_mid_turn(tmp_path):
    rc, _out, err = _run(
        tmp_path,
        bots=[{"dir": "mgr", "fleet": "f"}],
        env_extra={"STUB_BUSY_RC": "0"},
    )
    assert rc == 0, err
    assert _dispatched(tmp_path) == ""
    events = _events(tmp_path)
    assert len(events) == 1
    assert events[0]["type"] == "checkin_skipped"
    assert events[0]["data"]["reason"] == "busy"


def test_a_manager_idle_at_the_gate_but_busy_at_the_send_gets_no_dispatch(tmp_path):
    # A4: the plane rate-limit read sits between the gate check and the send,
    # so a re-check immediately before dispatch.sh catches a manager that went
    # busy in that window -- idle on the first bot_is_busy call, busy on the
    # second.
    rc, _out, err = _run(
        tmp_path,
        bots=[{"dir": "mgr", "fleet": "f"}],
        env_extra={"STUB_BUSY_RC": "1", "STUB_BUSY_RC_2": "0"},
    )
    assert rc == 0, err
    assert _dispatched(tmp_path) == ""
    events = _events(tmp_path)
    assert [e["type"] for e in events] == ["checkin_skipped"]
    assert events[0]["data"]["reason"] == "busy"


def test_a_recent_trigger_inside_the_min_gap_suppresses_the_beat(tmp_path):
    rc, _out, err = _run(
        tmp_path,
        bots=[{"dir": "mgr", "fleet": "f"}],
        env_extra={"STUB_PLANE_HITS": '{"ts":"2026-09-18T00:00:00Z"}\n'},
    )
    assert rc == 0, err
    assert _dispatched(tmp_path) == ""
    assert _events(tmp_path) == []


def test_the_rate_limit_read_names_the_manager_the_type_and_the_window(tmp_path):
    before = time.time()
    rc, _out, err = _run(tmp_path, bots=[{"dir": "lead", "fleet": "f"}])
    assert rc == 0, err
    argv = _plane_argv(tmp_path)
    assert "--events" in argv
    assert _flag(argv, "--fleet") == "f"
    assert _flag(argv, "--bot") == "lead"
    assert _flag(argv, "--type") == "checkin_triggered"
    since_epoch = _since_epoch(argv)
    assert abs(since_epoch - (before - DEFAULT_MIN_GAP_S)) <= 2


def test_the_min_gap_is_overridable_by_flag_and_by_env(tmp_path):
    before_env = time.time()
    rc, _out, err = _run(
        tmp_path,
        bots=[{"dir": "lead", "fleet": "f"}],
        env_extra={"CHECKIN_MIN_GAP_S": "60"},
    )
    assert rc == 0, err
    since_env = _since_epoch(_plane_argv(tmp_path))
    assert abs(since_env - (before_env - 60)) <= 2

    before_flag = time.time()
    rc, _out, err = _run(
        tmp_path,
        bots=[{"dir": "lead", "fleet": "f"}],
        extra_args=["--min-gap-s", "60"],
        env_extra={"CHECKIN_MIN_GAP_S": "9999"},
    )
    assert rc == 0, err
    since_flag = _since_epoch(_plane_argv(tmp_path))
    assert abs(since_flag - (before_flag - 60)) <= 2


def test_a_min_gap_that_is_not_a_number_is_a_usage_refusal_at_rc_2(tmp_path):
    rc, _out, err = _run(tmp_path, bots=[], extra_args=["--min-gap-s", "later"])
    assert rc == 2, err
    assert _dispatched(tmp_path) == ""


def test_an_unreachable_plane_does_not_fire(tmp_path):
    rc, _out, err = _run(
        tmp_path,
        bots=[{"dir": "mgr", "fleet": "f"}],
        env_extra={"STUB_PLANE_RC": "3"},
    )
    assert rc == 0, err
    assert _dispatched(tmp_path) == ""
    assert _events(tmp_path) == []
    assert "unreachable" in _log(tmp_path)


def test_a_disabled_plane_emit_warns_the_trigger_was_not_recorded(tmp_path):
    # A2: PLANE_EMIT_DISABLED=1 is the one signal a real emit_fleet_event call
    # returns 0 without ever recording anything (plane_armed's early return) --
    # the only failure this door can actually see from outside emit_fleet_event,
    # since its own PLANE_EMIT_LAST_RC is restored to its pre-call value on
    # return and so never reflects this call's own outcome.
    rc, _out, err = _run(
        tmp_path,
        bots=[{"dir": "mgr", "fleet": "f"}],
        env_extra={"PLANE_EMIT_DISABLED": "1"},
    )
    assert rc == 0, err
    log = _log(tmp_path)
    assert "WARN" in log
    assert "not recorded" in log


def test_a_clean_emit_writes_no_warn(tmp_path):
    rc, _out, err = _run(tmp_path, bots=[{"dir": "mgr", "fleet": "f"}])
    assert rc == 0, err
    assert "WARN" not in _log(tmp_path)


def test_a_failed_send_records_no_trigger_so_the_next_beat_retries(tmp_path):
    rc, _out, err = _run(
        tmp_path,
        bots=[{"dir": "mgr", "fleet": "f"}],
        env_extra={"STUB_DISPATCH_RC": "1"},
    )
    assert rc == 0, err
    events = _events(tmp_path)
    assert [e["type"] for e in events] == ["checkin_skipped"]
    assert events[0]["data"]["reason"] == "send_failed"


def test_a_bot_of_another_fleet_is_not_touched(tmp_path):
    rc, _out, err = _run(
        tmp_path,
        bots=[
            {"dir": "w1", "fleet": "g"},
            {"dir": "lead", "fleet": "f"},
        ],
    )
    assert rc == 0, err
    assert _dispatched(tmp_path) == "lead\t/checkin"
    events = _events(tmp_path)
    assert len(events) == 1
    assert events[0]["type"] == "checkin_triggered"
    assert events[0]["bot_id"] == "lead"


def test_an_empty_roster_injects_nothing(tmp_path):
    rc, _out, err = _run(tmp_path, bots=[])
    assert rc == 0, err
    assert _dispatched(tmp_path) == ""
    assert _events(tmp_path) == []


def test_a_bad_sibling_manifest_is_disclosed_and_the_good_fleet_still_fires(tmp_path):
    rc, _out, err = _run(
        tmp_path,
        bots=[{"dir": "mgr", "fleet": "f"}],
        roster_bad=["/nonexistent/other-fleet/fleet.yaml\tunreadable"],
    )
    assert rc == 0, err
    assert "manager-checkin: roster:" in err
    assert "unreadable" in err
    assert _dispatched(tmp_path) == "mgr\t/checkin"


def test_no_fleet_named_is_a_usage_refusal_at_rc_2(tmp_path):
    rc, _out, err = _run(tmp_path, bots=[], fleet=None)
    assert rc == 2, err
    assert _dispatched(tmp_path) == ""


def test_the_trigger_reads_the_plane_through_the_shipped_door_not_sqlite():
    text = TRIGGER.read_text()
    assert "plane-lookup.py" in text
    assert "sqlite3" not in text


def test_neither_nonzero_expecting_call_runs_in_a_command_substitution():
    text = TRIGGER.read_text()
    assert "$(declared_bots_strict" not in text
    assert "$(python3" not in text
    assert re.search(r"(?m)^\s*if declared_bots_strict\b", text)
    assert re.search(r"(?m)^\s*if python3 .*plane-lookup\.py", text)


# ---------------------------------------------------------------------------
# PR 2 chunk 2 — the composed fleet job (system.yaml defaults.jobs) and its
# switch row (switches.py). These exercise the PYTHON composer, not the bash
# trigger above: real load_fleet + compose_fleet_timers against a throwaway
# fleet.yaml with the real lib/ symlinked in — never a hand-built FleetConfig
# or a stubbed env_tiers resolver — the test_switches.py `_root` shape, so
# what these prove is the shipped composer against the shipped system.yaml,
# not a copy of either.
# ---------------------------------------------------------------------------

_CHECKIN_FLEET = """\
fleet:
  name: checkin-fleet
  service_prefix: com.checkin
  bots:
    lead:
      expertise: [orchestration]
"""


def _checkin_timers(tmp_path: Path, extra: str = "") -> Path:
    """Compose a throwaway fleet's timers: real lib/, real load_fleet, real
    compose_fleet_timers. `extra` is raw YAML appended under `fleet:` at its
    own 2-space indent, for a `defaults.jobs.manager-checkin.enroll`
    override (the TestTaskRecheckTimer shape in tests/test_composer.py)."""
    from claudlobby.composer import compose_fleet_timers
    from claudlobby.config import load_fleet
    from claudlobby.paths import Paths

    root = tmp_path / "r"
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "lib").exists():
        (root / "lib").symlink_to(REPO / "lib")
    (root / "fleet.yaml").write_text(_CHECKIN_FLEET + extra)
    fleet, merged = load_fleet(root / "fleet.yaml")
    paths = Paths(root=root, fleet_dir=root)
    return compose_fleet_timers(fleet, paths, merged)


def _dormant_entries(timers_dir: Path) -> list[str]:
    manifest = timers_dir / "DORMANT"
    if not manifest.exists():
        return []
    return [
        ln for ln in manifest.read_text().splitlines()
        if ln and not ln.startswith("#")
    ]


def test_the_job_is_composed_but_dormant_on_a_fleet_that_did_not_ask(tmp_path):
    timers = _checkin_timers(tmp_path)
    assert (timers / "com.checkin.manager-checkin.service").is_file()
    assert (timers / "com.checkin.manager-checkin.timer").is_file()
    assert "com.checkin.manager-checkin" in _dormant_entries(timers)


def test_arming_the_job_takes_it_out_of_the_dormant_manifest(tmp_path):
    extra = (
        "  defaults:\n"
        "    jobs:\n"
        "      manager-checkin:\n"
        "        enroll: true\n"
    )
    timers = _checkin_timers(tmp_path, extra)
    service = timers / "com.checkin.manager-checkin.service"
    assert service.is_file()
    # Not just present: composed from the REAL system.yaml job (script +
    # fleet argument), not a fleet-only stub the override happened to create
    # with no script of its own.
    assert "lib/manager-checkin.sh checkin-fleet" in service.read_text()
    assert "com.checkin.manager-checkin" not in _dormant_entries(timers)


def test_the_unit_execs_the_trigger_with_the_fleet_as_its_argument(tmp_path):
    timers = _checkin_timers(tmp_path)
    service = (timers / "com.checkin.manager-checkin.service").read_text()
    assert "lib/manager-checkin.sh checkin-fleet" in service


def test_the_beat_is_fifteen_minutes(tmp_path):
    timers = _checkin_timers(tmp_path)
    timer = (timers / "com.checkin.manager-checkin.timer").read_text()
    assert "OnBootSec=900" in timer
    assert "OnUnitActiveSec=900" in timer


def test_the_switch_row_is_opt_in_and_states_why():
    from claudlobby import switches as sw

    s = sw.by_key("manager-checkin")
    assert s.polarity == sw.OPT_IN
    assert s.why_opt_in
    assert "model spend" in s.why_opt_in


def test_the_arm_line_is_the_enroll_carrier():
    from claudlobby import switches as sw

    s = sw.by_key("manager-checkin")
    assert "defaults.jobs.manager-checkin.enroll: true" in s.arm
    assert "lib/setup-fleet" in s.arm
