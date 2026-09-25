"""Wiring tests for keepalive.sh and fleet-pulse.sh around a crash loop (#1769), first drafted by vera in her review of #1774.

The PR's unit tests call the library functions with a one-shot systemctl stub. This file
drives the REAL keepalive.sh and fleet-pulse.sh against a systemctl stub that answers `show`
from a scene's unit state and logs every `restart` with the counter it would zero.

Every scenario is hermetic: temp HOME/root, PLANE_EMIT_DISABLED=1, a stub curl, no network.
The emit=True scenes are the one exception to the middle of that: they arm the plane shim and
point its cold rung at a recorder, so the events the scripts raise can be read back.
"""

import json
import platform
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"

# The scenes drive the systemd branch of the real scripts and read /proc/uptime; on
# macOS service_is_crash_looping answers "unknown" by design, so they cannot hold.
pytestmark = pytest.mark.skipif(platform.system() != "Linux", reason="drives the systemd branch")

SYSTEMCTL = r"""#!/bin/bash
# systemctl stub. State: $UNIT_STATE (KEY=VAL), call log: $UNIT_CALLS
st="${UNIT_STATE:?}"; calls="${UNIT_CALLS:?}"
get() { sed -n "s/^$1=//p" "$st" | tail -n1; }
a=("$@"); [ "${a[0]:-}" = "--user" ] && a=("${a[@]:1}")
case "${a[0]:-}" in
  show)
    up=$(cut -d' ' -f1 /proc/uptime); age=$(get AGE_S)
    enter=$(awk -v u="$up" -v g="${age:-0}" 'BEGIN{printf "%.0f", (u-g)*1000000}')
    # Only what -p asked for, in systemd's own order, as real systemd answers: a
    # stub that printed every property could not see a call that stopped asking
    # for NRestarts (#1780, follow-up 3 of this file's review). In every spelling
    # getopt takes: -p X, -pX, --property=X and --property X (test_C3).
    want=" "; prev=""
    for x in "${a[@]:1}"; do
      case "$prev" in -p|--property) want="$want${x//,/ } " ;; esac
      case "$x" in
        --property=*) x="${x#--property=}"; want="$want${x//,/ } " ;;
        -p?*) x="${x#-p}"; want="$want${x//,/ } " ;;
      esac
      prev="$x"
    done
    for kv in "NRestarts=$(get NR)" "SubState=$(get SUB)" "ActiveState=$(get ACTIVE)" \
      "ExecMainStartTimestampMonotonic=$enter" "InactiveExitTimestampMonotonic=$enter"; do
      # no -p at all: systemd prints every property
      case "$want" in " "|*" ${kv%%=*} "*) printf '%s\n' "$kv" ;; esac
    done
    ;;
  restart)
    echo "restart nr_before=$(get NR)" >> "$calls" ;;
  is-active)
    [ "$(get ACTIVE)" = active ] && { echo active; exit 0; }; echo "$(get ACTIVE)"; exit 3 ;;
  *) echo "${a[*]}" >> "$calls" ;;
esac
exit 0
"""


class Scene:
    def __init__(
        self, tmp, *, active, sub, nr, age_s=2, grace=300, marker=False, emit=False,
    ):
        self.tmp = Path(tmp)
        self.home = self.tmp / "home"
        self.root = self.tmp / "root"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.tmp / "tmux").mkdir()
        unit = self.home / ".config/systemd/user/probe1774svc.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("")
        self.bot = self.root / "local/F/runtime/bots/b"
        (self.bot / "data").mkdir(parents=True)
        (self.bot / "bot.conf").write_text(
            "BOT_NAME=b\nBOT_ID=b\nBOT_SERVICE=probe1774svc\nTMUX_SESSION=b1774\n"
        )
        self.state, self.calls = self.tmp / "unit.state", self.tmp / "unit.calls"
        self.state.write_text(f"ACTIVE={active}\nSUB={sub}\nNR={nr}\nAGE_S={age_s}\n")
        self.calls.write_text("")
        self.emit_dir = None
        if emit:
            # Arms the plane shim and points its cold rung at a recorder that keeps each batch
            # it is handed (test_fleet_pulse_no_events.py's idiom): no plane, no daemon, and
            # the events the scripts raise can be read back with emitted().
            self.emit_dir = self.tmp / "emitted"
            self.emit_dir.mkdir()
            rec = self.bin / "plane-cli"
            rec.write_text('#!/bin/bash\ncp "${@: -1}" "$EMIT_DIR/$(date +%s%N).json"\n')
            rec.chmod(0o755)
        for name, body in (
            ("systemctl", SYSTEMCTL),
            ("curl", '#!/bin/bash\nprintf "%s" \'{"ok":false}\'\n'),
        ):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.grace = grace
        self.pulse_state = self.root / "state" / "pulse"
        self.pulse_state.mkdir(parents=True)
        if marker:
            (self.pulse_state / "b.crashloop_alerted").write_text("|0")

    def env(self):
        env = {
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "CLAUDLOBBY_ROOT": str(self.root),
            "CLAUDLOBBY_FLEET": "F",
            "PLANE_EMIT_DISABLED": "1",
            "UNIT_STATE": str(self.state),
            "UNIT_CALLS": str(self.calls),
            "KEEPALIVE_BOOT_GRACE_S": str(self.grace),
            "TMUX_TMPDIR": str(self.tmp / "tmux"),
            "FLEET_PULSE_ESCALATION_CHAT_ID": "-100999",
            "FLEET_PULSE_ESCALATE_CHAT_ID": "",
            "TELEGRAM_STATE_DIR": str(self.tmp / "tg"),
        }
        if self.emit_dir:
            del env["PLANE_EMIT_DISABLED"]
            env["PLANE_EMIT_CLI"] = str(self.bin / "plane-cli")
            env["PLANE_SOCKET"] = str(self.tmp / "no-daemon.sock")
            env["EMIT_DIR"] = str(self.emit_dir)
        return env

    def keepalive(self):
        r = subprocess.run(
            ["bash", str(LIB / "keepalive.sh"), str(self.bot)],
            env=self.env(),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=90,
        )
        log = (
            (self.bot / "keepalive.log").read_text(errors="replace")
            if (self.bot / "keepalive.log").exists()
            else ""
        )
        return r, log

    def pulse(self):
        r = subprocess.run(
            ["bash", str(LIB / "fleet-pulse.sh"), "F"],
            env=self.env(),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )
        summary = self.pulse_state / "pulse-summary.txt"
        return r, (summary.read_text(errors="replace") if summary.exists() else "")

    def restarts(self):
        return [
            l for l in self.calls.read_text(errors="replace").splitlines() if l.startswith("restart")
        ]

    def markers(self):
        return sorted(p.name for p in self.pulse_state.glob("b.*"))

    def emitted(self, name):
        """The plane events called `name` that the scripts handed the shim (emit=True scenes)."""
        rows = []
        for f in sorted(self.emit_dir.glob("*.json")):
            rows += [e["payload"] for e in json.loads(f.read_text())["events"]]
        return [p for p in rows if p.get("event") == name]


# --- keepalive -----------------------------------------------------------------------------


def test_K1_a_loop_is_named_and_never_restarted(tmp_path):
    s = Scene(tmp_path, active="activating", sub="auto-restart", nr=3, age_s=2)
    r, log = s.keepalive()
    assert "SKIP — crash loop (3 automatic restarts" in log, (log, r.stderr[-400:])
    assert s.restarts() == [], "keepalive stacked a restart on a crash loop"
    # a skip is not a failure of the watchdog: it exits 0
    assert r.returncode == 0, (r.returncode, r.stderr[-400:])


def test_K4_a_settled_unit_with_a_stale_counter_still_restarts(tmp_path):
    s = Scene(tmp_path, active="active", sub="exited", nr=1971, age_s=5000)
    r, log = s.keepalive()
    assert "RESTART" in log, (log, r.stderr[-400:])
    assert s.restarts() == ["restart nr_before=1971"], s.restarts()


# --- fleet-pulse ---------------------------------------------------------------------------


def test_P1_a_loop_raises_its_own_page_and_replaces_session_missing(tmp_path):
    s = Scene(tmp_path, active="activating", sub="auto-restart", nr=3, age_s=2)
    r, summary = s.pulse()
    assert "b.crashloop_alerted" in s.markers(), (s.markers(), r.stderr[-500:])
    assert not [
        m for m in s.markers() if m in ("b.session_alerted", "b.service_alerted")
    ], s.markers()
    assert "crash-loop" in summary, summary


def test_P2_a_settled_unit_clears_the_page(tmp_path):
    s = Scene(tmp_path, active="active", sub="exited", nr=1971, age_s=5000, marker=True)
    s.pulse()
    assert "b.crashloop_alerted" not in s.markers(), s.markers()


def test_P3_an_ambiguous_tick_does_not_clear_the_page(tmp_path):
    s = Scene(
        tmp_path, active="deactivating", sub="stop-post", nr=3, age_s=1, marker=True
    )
    s.pulse()
    assert "b.crashloop_alerted" in s.markers(), s.markers()


def test_P4_a_healthy_boot_raises_nothing(tmp_path):
    s = Scene(tmp_path, active="activating", sub="start-pre", nr=0, age_s=20)
    r, summary = s.pulse()
    assert "b.crashloop_alerted" not in s.markers(), s.markers()
    assert "crash-loop" not in summary, summary


def test_P5_a_start_that_is_not_yet_a_loop_does_not_clear_the_page(tmp_path):
    # `starting` (a start state below the threshold) is no more "over" than the `none` of P3 is:
    # the page is cleared only when the loop is positively over, or it re-pages on every retry.
    s = Scene(tmp_path, active="activating", sub="start-pre", nr=1, age_s=20, marker=True)
    s.pulse()
    assert "b.crashloop_alerted" in s.markers(), s.markers()


def test_P6_a_loop_records_one_crash_loop_event_with_its_keys(tmp_path):
    s = Scene(tmp_path, active="activating", sub="auto-restart", nr=3, age_s=2, emit=True)
    s.pulse()
    events = s.emitted("crash_loop")
    assert len(events) == 1, events
    # the fleet-event envelope: payload.data is {source, legacy_ts, data}, the last being the keys
    data = events[0]["data"]
    assert data["source"] == "pulse", data
    assert data["data"] == {"unit": "probe1774svc", "restarts": 3, "state": "activating/auto-restart"}, data


# --- controls: the AGE plumbing itself (a young phase is a boot in flight) ---

def test_C1_a_young_start_after_one_retry_is_a_boot_in_flight_not_a_restart(tmp_path):
    s = Scene(tmp_path, active="activating", sub="start-pre", nr=1, age_s=20, grace=300)
    r, log = s.keepalive()
    assert "SKIP — boot in flight" in log, (log, r.stderr[-300:])
    assert s.restarts() == []


def test_C2_stub_timestamps_are_sane(tmp_path):
    s = Scene(tmp_path, active="activating", sub="start-pre", nr=0, age_s=20)
    out = subprocess.run(["systemctl", "--user", "show", "x"], env=s.env(), capture_output=True, text=True).stdout
    ts = int(dict(l.split("=", 1) for l in out.splitlines())["InactiveExitTimestampMonotonic"])
    up = float(open("/proc/uptime").read().split()[0]) * 1e6
    assert 15e6 < up - ts < 30e6, (up, ts)


# What real systemd 252 answers for each way of writing -p (measured against a running unit): only
# what was asked, whichever spelling asked it. A stub that reads one spelling as "no -p" answers
# all five, and one that drops it cannot witness that a call still asks for NRestarts.
ALL_FIVE = {
    "NRestarts", "SubState", "ActiveState", "ExecMainStartTimestampMonotonic", "InactiveExitTimestampMonotonic",
}
SPELLINGS = [
    pytest.param(["-p", "NRestarts"], {"NRestarts"}, id="p-separate"),
    pytest.param(["-pNRestarts"], {"NRestarts"}, id="p-attached"),
    pytest.param(["-p", "SubState", "-pNRestarts"], {"SubState", "NRestarts"}, id="p-mixed"),
    pytest.param(["-pNRestarts,SubState"], {"NRestarts", "SubState"}, id="p-attached-comma"),
    pytest.param(["--property=NRestarts"], {"NRestarts"}, id="property-equals"),
    pytest.param(["--property", "NRestarts"], {"NRestarts"}, id="property-separate"),
    pytest.param(["-p", "Bogus"], set(), id="p-unknown"),
    pytest.param([], ALL_FIVE, id="no-p"),
]


@pytest.mark.parametrize("argv, keys", SPELLINGS)
def test_C3_the_stub_answers_only_what_each_spelling_of_p_asks(tmp_path, argv, keys):
    s = Scene(tmp_path, active="activating", sub="start-pre", nr=0, age_s=20)
    out = subprocess.run(["systemctl", "--user", "show", *argv, "x"], env=s.env(), capture_output=True, text=True).stdout
    assert {l.split("=", 1)[0] for l in out.splitlines()} == keys, out


def test_K6_a_loop_whose_current_attempt_outlived_the_boot_grace_is_still_not_restarted(tmp_path):
    # keepalive's crash SKIP has to hold ON ITS OWN: the boot gate below it ages only the CURRENT phase, so
    # an attempt older than the grace (a start that hangs, then fails) would otherwise fall through to a
    # restart stacked on systemd's loop.
    s = Scene(tmp_path, active="activating", sub="start-pre", nr=3, age_s=10, grace=5)
    r, log = s.keepalive()
    assert "SKIP — crash loop (3 automatic restarts" in log, (log, r.stderr[-400:])
    assert s.restarts() == [], "keepalive stacked a restart on a loop whose current attempt was old"


def test_K8_the_crash_skip_records_a_keepalive_skip_event(tmp_path):
    s = Scene(tmp_path, active="activating", sub="auto-restart", nr=3, age_s=2, emit=True)
    s.keepalive()
    events = s.emitted("keepalive_skip")
    assert len(events) == 1, events
    assert "crash loop (3 automatic restarts)" in events[0]["data"]["data"]["detail"], events
