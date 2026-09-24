"""Wiring tests for keepalive.sh and fleet-pulse.sh around a crash loop (#1769) -- reviewer-supplied.

The PR's unit tests call the library functions with a one-shot systemctl stub, and its
real-unit harness never drives a keepalive restart in the middle of a loop. This file
drives the REAL keepalive.sh and fleet-pulse.sh against a STATEFUL systemctl stub whose
`restart` behaves like systemd's manual restart: it zeroes NRestarts and starts a fresh
phase, and it records what data/.restart-carry held at that instant (so ORDER is visible).

Every scenario is hermetic: temp HOME/root, PLANE_EMIT_DISABLED=1, a stub curl, no network.
"""

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"

SYSTEMCTL = r"""#!/bin/bash
# stateful systemctl stub. State: $UNIT_STATE (KEY=VAL), call log: $UNIT_CALLS
st="${UNIT_STATE:?}"; calls="${UNIT_CALLS:?}"
get() { sed -n "s/^$1=//p" "$st" | tail -n1; }
put() { { grep -v "^$1=" "$st" 2>/dev/null; printf '%s=%s\n' "$1" "$2"; } > "$st.new"; mv "$st.new" "$st"; }
a=("$@"); [ "${a[0]:-}" = "--user" ] && a=("${a[@]:1}")
case "${a[0]:-}" in
  show)
    up=$(cut -d' ' -f1 /proc/uptime); age=$(get AGE_S)
    enter=$(awk -v u="$up" -v g="${age:-0}" 'BEGIN{printf "%.0f", (u-g)*1000000}')
    # Only what -p asked for, in systemd's own order, as real systemd answers: a
    # stub that printed every property could not see a call that stopped asking
    # for NRestarts (#1780, follow-up 3 of this file's review).
    want=" "; prev=""
    for x in "${a[@]:1}"; do
      case "$prev" in -p|--property) want="$want${x//,/ } " ;; esac
      case "$x" in --property=*) x="${x#--property=}"; want="$want${x//,/ } " ;; esac
      prev="$x"
    done
    for kv in "NRestarts=$(get NR)" "SubState=$(get SUB)" "ActiveState=$(get ACTIVE)" \
      "ExecMainStartTimestampMonotonic=$enter" "InactiveExitTimestampMonotonic=$enter"; do
      # no -p at all: systemd prints every property
      case "$want" in " "|*" ${kv%%=*} "*) printf '%s\n' "$kv" ;; esac
    done
    ;;
  restart)
    echo "restart carry_before=$(head -1 "${CARRY_FILE:-/dev/null}" 2>/dev/null || true) nr_before=$(get NR)" >> "$calls"
    put NR 0; put AGE_S 0; put ACTIVE activating; put SUB start-pre
    ;;
  is-active)
    [ "$(get ACTIVE)" = active ] && { echo active; exit 0; }; echo "$(get ACTIVE)"; exit 3 ;;
  *) echo "${a[*]}" >> "$calls" ;;
esac
exit 0
"""


class Scene:
    def __init__(
        self, tmp, *, active, sub, nr, age_s=2, carry=None, grace=300, marker=False
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
        for name, body in (
            ("systemctl", SYSTEMCTL),
            ("curl", '#!/bin/bash\nprintf "%s" \'{"ok":false}\'\n'),
        ):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.carry_file = self.bot / "data" / ".restart-carry"
        if carry is not None:
            self.carry_file.write_text(carry + "\n")
        self.grace = grace
        self.pulse_state = self.root / "state" / "pulse"
        self.pulse_state.mkdir(parents=True)
        if marker:
            (self.pulse_state / "b.crashloop_alerted").write_text("|0")

    def set(self, **kv):
        cur = dict(
            l.split("=", 1) for l in self.state.read_text(errors="replace").splitlines() if "=" in l
        )
        cur.update({k.upper(): str(v) for k, v in kv.items()})
        self.state.write_text("".join(f"{k}={v}\n" for k, v in cur.items()))

    def env(self):
        return {
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "CLAUDLOBBY_ROOT": str(self.root),
            "CLAUDLOBBY_FLEET": "F",
            "PLANE_EMIT_DISABLED": "1",
            "UNIT_STATE": str(self.state),
            "UNIT_CALLS": str(self.calls),
            "CARRY_FILE": str(self.carry_file),
            "KEEPALIVE_BOOT_GRACE_S": str(self.grace),
            "TMUX_TMPDIR": str(self.tmp / "tmux"),
            "FLEET_PULSE_ESCALATION_CHAT_ID": "-100999",
            "FLEET_PULSE_ESCALATE_CHAT_ID": "",
            "TELEGRAM_STATE_DIR": str(self.tmp / "tg"),
        }

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

    def carry(self):
        return self.carry_file.read_text(errors="replace").strip() if self.carry_file.exists() else None


# --- keepalive -----------------------------------------------------------------------------


def test_K1_a_loop_is_named_and_never_restarted(tmp_path):
    s = Scene(tmp_path, active="activating", sub="auto-restart", nr=3, age_s=2)
    r, log = s.keepalive()
    assert "SKIP — crash loop (3 automatic restarts" in log, (log, r.stderr[-400:])
    assert s.restarts() == [], "keepalive stacked a restart on a crash loop"
    assert s.carry() is None


def test_K2_a_restart_that_interrupts_a_streak_records_the_count_BEFORE_it_is_zeroed(
    tmp_path,
):
    # A wedged start (phase older than the boot grace) after ONE automatic restart: keepalive
    # restarts it. The count it is about to wipe must be on disk at the instant of the restart.
    s = Scene(
        tmp_path, active="activating", sub="start-pre", nr=1, age_s=400, grace=300
    )
    r, log = s.keepalive()
    assert "RESTART" in log, (log, r.stderr[-400:])
    assert s.restarts() == ["restart carry_before=1 nr_before=1"], s.restarts()
    assert s.carry() == "1"


def test_K3_the_streak_survives_keepalives_own_restart(tmp_path):
    s = Scene(
        tmp_path, active="activating", sub="start-pre", nr=1, age_s=400, grace=300
    )
    s.keepalive()  # the interrupting restart (NR 1 -> 0, carry=1)
    assert s.restarts() == ["restart carry_before=1 nr_before=1"]
    s.set(
        active="activating", sub="auto-restart", nr=1, age_s=2
    )  # ONE more failure after it
    r, log = s.keepalive()
    assert "SKIP — crash loop (2 automatic restarts" in log, (log, r.stderr[-400:])
    assert len(s.restarts()) == 1, (
        "keepalive stacked a second restart on the resumed loop"
    )


def test_K4_a_settled_unit_restarts_clean_and_carries_nothing(tmp_path):
    s = Scene(tmp_path, active="active", sub="exited", nr=1971, age_s=5000, carry="7")
    r, log = s.keepalive()
    assert "RESTART" in log, (log, r.stderr[-400:])
    assert s.restarts() == ["restart carry_before= nr_before=1971"], s.restarts()
    assert s.carry() is None, "a stale count seeded the next boot"


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


# --- controls: the AGE plumbing itself (an aged-out phase restarts; a young one is a boot in flight) ---

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


def test_K6_a_loop_whose_current_attempt_outlived_the_boot_grace_is_still_not_restarted(tmp_path):
    # keepalive's crash SKIP has to hold ON ITS OWN: the boot gate below it ages only the CURRENT phase, so
    # an attempt older than the grace (a start that hangs, then fails) would otherwise fall through to a
    # restart stacked on systemd's loop.
    s = Scene(tmp_path, active="activating", sub="start-pre", nr=3, age_s=400, grace=300)
    r, log = s.keepalive()
    assert "SKIP — crash loop (3 automatic restarts" in log, (log, r.stderr[-400:])
    assert s.restarts() == [], "keepalive stacked a restart on a loop whose current attempt was old"
