"""#2243: a bot stopped on purpose is not an outage, and one fault is one alert.

`bot stop` removes the bot's unit file, which activation, keepalive and
reconcile already read as "stopped on purpose"; fleet-pulse did not, so a
stopped bot paged as `service_down` plus `session_missing`, every sweep, and
two stopped bots reached the fleet escalation twice over. The decision on the
issue (the pre-scope comment, final forks):

- only a RECORDED stop is silent: no unit file AND the stop door's local record
  (`data/.stopped`);
- no unit file and no record is its own cause, `unit_missing`: a critical row
  every sweep, one push per episode, counted by the escalation;
- a unit that is down raises `service_down` alone, the session's state in its
  payload; `session_missing` is for an active unit, or a bot with no service;
- the escalation sends every cause over its threshold in a sweep as ONE message.

Each test drives the REAL sweep against a throwaway plane through the real
doors, with two stubs: `tg-post.sh` captures the fleet page, and `systemctl`
answers from per-unit state files (a unit with no state file reads as systemd
reads a unit it cannot find). A unit counts as installed when its file is in
the scene's HOME, the fact `svc_is_registered` reads.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import time

import pytest

from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.fleet_events import fleet_event_request
from tests.plane_fixtures import F, plane_root, ro
from tests.test_fleet_pulse_events_plane import _pulse, _pulse_lib, _summary

pytestmark = [
    pytest.mark.skipif(shutil.which("tmux") is None, reason="fleet-pulse needs tmux"),
    pytest.mark.skipif(
        platform.system() != "Linux", reason="drives the systemd branch"
    ),
]

SYSTEMCTL = r"""#!/bin/bash
# systemctl stub: each unit's state is $UNIT_DIR/<label> (ACTIVE=, SUB=, NR=); a
# unit with no state file reads as one systemd cannot find.
dir="${UNIT_DIR:?}"
a=("$@"); [ "${a[0]:-}" = "--user" ] && a=("${a[@]:1}")
verb="${a[0]:-}"; a=("${a[@]:1}")
props=","; value=0; unit=""
while [ "${#a[@]}" -gt 0 ]; do
  case "${a[0]}" in
    -p) props="$props${a[1]},"; a=("${a[@]:2}") ;;
    --property=*) props="$props${a[0]#--property=},"; a=("${a[@]:1}") ;;
    --value) value=1; a=("${a[@]:1}") ;;
    -*) a=("${a[@]:1}") ;;
    *) unit="${a[0]%.service}"; a=("${a[@]:1}") ;;
  esac
done
st="$dir/$unit"
get() { sed -n "s/^$1=//p" "$st" | tail -n1; }
if [ -n "$unit" ] && [ -f "$st" ]; then
  load=loaded; active=$(get ACTIVE); sub=$(get SUB); nr=$(get NR)
else
  load=not-found; active=inactive; sub=dead; nr=0
fi
case "$verb" in
  is-active) echo "$active"; [ "$active" = active ]; exit $? ;;
  show)
    up=$(cut -d' ' -f1 /proc/uptime)
    enter=$(awk -v u="$up" 'BEGIN{printf "%.0f", (u-5000)*1000000}')
    for kv in "Id=$unit.service" "LoadState=$load" "ActiveState=$active" "SubState=$sub" \
              "NRestarts=${nr:-0}" "ExecMainStartTimestampMonotonic=$enter" \
              "InactiveExitTimestampMonotonic=$enter"; do
      k="${kv%%=*}"
      if [ "$props" = "," ] || [[ "$props" == *",$k,"* ]]; then
        if [ "$value" = 1 ]; then echo "${kv#*=}"; else echo "$kv"; fi
      fi
    done ;;
esac
exit 0
"""

ALERT = "FLEET ALERT:"


class Fleet:
    """One fleet `f` on a throwaway plane: bots, their units and their stop records."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = plane_root(tmp_path, initialize=True)
        # The overdue reader refuses a plane that holds no bot of the fleet, and
        # pages that it cannot see (#1014's class); its scan runs before the
        # sweep's first row. A live fleet's plane holds its bots, so seed the
        # roster through the real write door, with a bot no test looks at.
        emit_batch(self.root, [fleet_event_request(
            "pane_stuck", fleet=F, subject_kind="actor", subject=f"bot:{F}/roster-seed",
            source="pulse", data={})], require_commit=True)
        self.home = self.root / "home"
        self.units = tmp_path / "units"
        self.units.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        stub = self.bin / "systemctl"
        stub.write_text(SYSTEMCTL)
        stub.chmod(0o755)
        (self.root / "tmux").mkdir()
        self.bots_dir = self.root / "local" / F / "runtime" / "bots"
        self.bots_dir.mkdir(parents=True)
        self.capture = tmp_path / "tg.log"
        self.libdir = _pulse_lib(tmp_path, self.capture)
        self.names: list[str] = []

    def bot(self, name, *, unit=None, record=None, service=True):
        """unit: None (no unit file) or the unit's ActiveState ("active", "failed",
        ...); record: None, or how long ago (seconds) the stop door recorded a stop."""
        d = self.bots_dir / name
        (d / "data").mkdir(parents=True)
        conf = f"BOT_NAME={name}\nTMUX_SOCKET=t2243-{name}\n"
        label = f"com.t.{name}"
        if service:
            conf += f"BOT_SERVICE={label}\n"
        (d / "bot.conf").write_text(conf)
        if unit is not None:
            path = self.home / ".config/systemd/user" / f"{label}.service"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[Service]\n")
            sub = {"active": "exited", "failed": "failed"}.get(unit, "dead")
            (self.units / label).write_text(f"ACTIVE={unit}\nSUB={sub}\nNR=0\n")
        if record is not None:
            at = int(time.time()) - record
            (d / "data" / ".stopped").write_text(
                json.dumps(
                    {
                        "by": "bot:f/m",
                        "reason": "parked for the test",
                        "request_id": "r-2243",
                        "stopped_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)
                        ),
                        "stopped_epoch": at,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        self.names.append(name)
        (self.root / "local" / F / "fleet.yaml").write_text(
            "fleet:\n  name: f\n  bots:\n"
            + "".join(
                f"    {n}:\n      expertise: [software-engineering]\n"
                for n in self.names
            )
        )
        return d

    def sweep(self, scratch_plane_env, **extra):
        r = _pulse(
            self.root,
            self.libdir,
            scratch_plane_env=scratch_plane_env,
            PATH=f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            UNIT_DIR=str(self.units),
            **extra,
        )
        assert r.returncode == 0, r.stderr[-2000:]
        return r

    def rows(self, bot, event, *, at_least=0, timeout=15):
        """The pulse rows for one bot; waits (up to *timeout*) for *at_least* of
        them, since an emit that staged instead of committing lands on replay."""
        deadline = time.monotonic() + timeout
        while True:
            got = self._rows(bot, event)
            if len(got) >= at_least or time.monotonic() > deadline:
                return got
            time.sleep(0.25)

    def _rows(self, bot, event):
        with ro(self.root) as conn:
            return [
                json.loads(d or "{}")
                for (d,) in conn.execute(
                    "SELECT detail FROM events WHERE event = ? AND subject_alias = ?",
                    (event, f"bot:{F}/{bot}"),
                )
            ]

    def severity(self, event):
        with ro(self.root) as conn:
            return {
                s
                for (s,) in conn.execute(
                    "SELECT DISTINCT severity FROM events WHERE event = ?", (event,)
                )
            }

    def markers(self, bot):
        return sorted(
            p.name.split(".", 1)[1]
            for p in (self.root / "state/pulse").glob(f"{bot}.*")
            if not p.name.endswith(("pane_hash", "pane_ts"))
        )

    def pages(self):
        text = self.capture.read_text() if self.capture.exists() else ""
        return [line for line in text.splitlines() if line.startswith(ALERT)]

    def summary_line(self, bot):
        return next(
            l for l in _summary(self.root).splitlines() if l.split()[:1] == [bot]
        )


def _detail_data(row):
    """A pulse row's own keys: the fleet-event envelope nests them under data.data."""
    data = row.get("data", row)
    return data.get("data", data)


# --- 1, 2: a recorded stop is silent ---------------------------------------------------------


def test_a_recorded_stop_is_silent_and_the_summary_says_stopped_since_and_by(
    tmp_path, *, scratch_plane_env
):
    f = Fleet(tmp_path)
    f.bot("s", record=3600)
    f.bot("c", service=False)  # the control: no service, no session
    f.sweep(scratch_plane_env)
    assert f.rows("c", "session_missing", at_least=1), (
        "control: the sweep's rows did not reach the plane"
    )
    for event in ("service_down", "session_missing", "unit_missing", "crash_loop"):
        assert not f.rows("s", event), (event, f.rows("s", event))
    assert f.markers("s") == [], f.markers("s")
    line = f.summary_line("s")
    assert line.split()[1:3] == ["stopped", "stopped"], line
    assert "stopped since " in line and "by bot:f/m" in line, line


def test_two_recorded_stops_raise_no_fleet_alert(tmp_path, *, scratch_plane_env):
    f = Fleet(tmp_path)
    f.bot("s1", record=600)
    f.bot("s2", record=600)
    # The control writes the fleet's one row (session_missing, below the threshold),
    # so the plane holds a bot of this fleet and its readers answer: a fleet with no
    # row at all pages that its overdue reader is unreachable, which is not this.
    f.bot("c", service=False)
    f.sweep(scratch_plane_env)
    assert f.rows("c", "session_missing", at_least=1), "control: no row reached the plane"
    assert f.pages() == [], f.pages()
    assert [f.summary_line(b).split()[1] for b in ("s1", "s2")] == [
        "stopped",
        "stopped",
    ]


# --- 3, 4: a unit that dies on its own still pages, exactly once ---------------------------


def test_a_unit_that_dies_on_its_own_is_one_service_down_carrying_the_session(
    tmp_path, *, scratch_plane_env
):
    f = Fleet(tmp_path)
    f.bot("d", unit="failed")
    f.sweep(scratch_plane_env)
    f.sweep(scratch_plane_env)
    rows = f.rows("d", "service_down", at_least=2)
    assert len(rows) == 2, rows  # one row every sweep
    assert {
        (_detail_data(r).get("state"), _detail_data(r).get("session")) for r in rows
    } == {("failed", "missing")}, rows
    assert f.rows("d", "session_missing") == []  # the same fault, said once
    assert f.markers("d") == ["service_alerted"], f.markers(
        "d"
    )  # one push for the episode


def test_an_active_unit_whose_session_is_gone_is_still_session_missing(
    tmp_path, *, scratch_plane_env
):
    """The case keepalive heals: unchanged by #2243 (a control for the one above)."""
    f = Fleet(tmp_path)
    f.bot("e", unit="active")
    f.sweep(scratch_plane_env)
    assert len(f.rows("e", "session_missing", at_least=1)) == 1
    assert f.rows("e", "service_down") == []
    assert f.markers("e") == ["session_alerted"], f.markers("e")


def test_two_dead_units_are_one_fleet_alert_naming_both(tmp_path, *, scratch_plane_env):
    f = Fleet(tmp_path)
    f.bot("d1", unit="failed")
    f.bot("d2", unit="failed")
    f.sweep(scratch_plane_env)
    assert f.pages() == [
        f"{ALERT} service_down on 2 bots (d1 d2). Check f fleet health immediately."
    ], f.pages()


# --- 5: a stop does not count toward a real outage's escalation -----------------------------


def test_a_stopped_bot_beside_a_dead_one_does_not_escalate(
    tmp_path, *, scratch_plane_env
):
    f = Fleet(tmp_path)
    f.bot("d", unit="failed")
    f.bot("s", record=600)
    f.sweep(scratch_plane_env)
    assert f.pages() == [], f.pages()  # threshold 2: one real fault
    assert f.markers("d") == ["service_alerted"], f.markers(
        "d"
    )  # its own push still goes out
    assert f.markers("s") == [], f.markers("s")


# --- 6, 9: no unit and no stop record is its own cause -----------------------------------------


def test_no_unit_and_no_record_is_unit_missing_every_sweep_with_one_push(
    tmp_path, *, scratch_plane_env
):
    f = Fleet(tmp_path)
    f.bot("u")
    f.sweep(scratch_plane_env)
    f.sweep(scratch_plane_env)
    rows = f.rows("u", "unit_missing", at_least=2)
    assert len(rows) == 2, rows
    assert {
        (_detail_data(r).get("unit"), _detail_data(r).get("session")) for r in rows
    } == {("com.t.u", "missing")}, rows
    assert f.severity("unit_missing") == {"critical"}
    assert f.rows("u", "service_down") == [] and f.rows("u", "session_missing") == []
    assert f.markers("u") == ["unit_alerted"], f.markers("u")
    assert f.summary_line("u").split()[2] == "unit-missing", f.summary_line("u")


def test_three_bots_with_no_unit_and_no_record_are_one_fleet_alert(
    tmp_path, *, scratch_plane_env
):
    f = Fleet(tmp_path)
    for name in ("u1", "u2", "u3"):
        f.bot(name)
    f.sweep(scratch_plane_env)
    assert f.pages() == [
        f"{ALERT} unit_missing on 3 bots (u1 u2 u3). Check f fleet health immediately."
    ], f.pages()


# --- 7: a forgotten stop is a reminder, never an outage ---------------------------------------


def test_a_stop_older_than_the_reminder_gets_one_notice_and_nothing_critical(
    tmp_path, *, scratch_plane_env
):
    f = Fleet(tmp_path)
    f.bot("old", record=4 * 86400)
    f.bot("new", record=3600)
    f.bot("c", service=False)  # the control: its row shows the plane was read
    f.sweep(scratch_plane_env)
    assert f.rows("c", "session_missing", at_least=1), "control: no row reached the plane"
    assert f.markers("old") == ["stopped_reminded"], f.markers("old")
    assert f.markers("new") == [], f.markers("new")  # inside the 3 days
    with ro(f.root) as conn:
        critical = conn.execute(
            "SELECT event FROM events WHERE severity = 'critical' AND subject_alias "
            "IN ('bot:f/old', 'bot:f/new')"
        ).fetchall()
    assert critical == [], critical


def test_the_reminder_is_off_at_zero_days(tmp_path, *, scratch_plane_env):
    f = Fleet(tmp_path)
    f.bot("old", record=40 * 86400)
    f.sweep(scratch_plane_env, FLEET_PULSE_STOPPED_REMIND_DAYS="0")
    assert f.markers("old") == [], f.markers("old")


# --- F7 (b): every cause over its threshold in one sweep is one message ----------------------


def test_two_causes_over_threshold_in_one_sweep_are_one_message(
    tmp_path, *, scratch_plane_env
):
    f = Fleet(tmp_path)
    for name, unit in (("d1", "failed"), ("d2", "failed"), ("u1", None), ("u2", None)):
        f.bot(name, unit=unit)
    f.sweep(scratch_plane_env)
    assert f.pages() == [
        f"{ALERT} service_down on 2 bots (d1 d2); unit_missing on 2 bots (u1 u2). "
        "Check f fleet health immediately."
    ], f.pages()
