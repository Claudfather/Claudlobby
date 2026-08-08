"""#831: a debounced FLEET-PULSE alert must survive a manager restart.

`debounce_notify` keyed its marker on `<bot_id>.<suffix>` alone, so it recorded
*that* a notification fired but never *to whom*. The push therefore fired once
per episode into whatever manager session existed at that instant; if that
session was restarted, every later session was structurally incapable of
receiving it for the rest of the episode. That cost a 1.25-day dark-bot outage
in which ~3504 events accumulated while the push path stayed silent.

The marker now carries the recipient identity as its *content*, so a changed
recipient re-fires. Content rather than a per-recipient filename deliberately:
one marker per (bot, suffix) means markers cannot accumulate one-per-restart,
and `debounce_clear` needs no glob.

Only the first and fourth tests are red against the pre-fix code. The rest are
guards: the fire-once contract that `reload-fleet.sh` depends on, and the
no-accumulation property. They are what would catch a fix that re-notifies
correctly but leaks state or breaks the unrelated caller.
"""

import os
import subprocess

from tests.conftest import constructed_env

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO_ROOT, "lib")

# A notify_fn that records delivery instead of sending: the assertion surface is
# "did a notification fire", which is the property the outage turned on.
PRELUDE = """
state="$1"; mkdir -p "$state"
_notify() { printf '%s\\n' "$1" >> "$state/sent.log"; }
"""


def _run(tmp_path, body):
    """Source lib-common and run `body` with $1 = a throwaway state dir."""
    state = tmp_path / "state"
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB}/lib-common.sh"\n{PRELUDE}\n{body}', "_", str(state)],
        env=constructed_env(CLAUDLOBBY_ROOT=tmp_path),
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"harness failed: {r.stderr}"
    log = state / "sent.log"
    return log.read_text().splitlines() if log.exists() else []


def _marker(tmp_path):
    return tmp_path / "state" / "bot1.session_alerted"


class TestRecipientIdentity:
    def test_renotifies_when_the_recipient_changes(self, tmp_path):
        """The outage itself: the manager session is replaced mid-episode, and
        the new one must still be told. Red against the pre-fix marker."""
        sent = _run(
            tmp_path,
            """
            debounce_notify "$state" bot1 session_alerted _notify "bot1 is down" "mgr-A"
            debounce_notify "$state" bot1 session_alerted _notify "bot1 is down" "mgr-A"
            debounce_notify "$state" bot1 session_alerted _notify "bot1 is down" "mgr-B"
            """,
        )
        assert sent == ["bot1 is down", "bot1 is down"], (
            "expected one delivery to mgr-A and a re-delivery to mgr-B"
        )

    def test_does_not_renotify_for_the_same_recipient(self, tmp_path):
        """Debounce still debounces — the fix must not become a per-tick alarm."""
        sent = _run(
            tmp_path,
            """
            for _ in 1 2 3 4 5; do
                debounce_notify "$state" bot1 session_alerted _notify "down" "mgr-A"
            done
            """,
        )
        assert sent == ["down"]

    def test_marker_does_not_accumulate_one_per_recipient(self, tmp_path):
        """Recipient identity lives in the marker's content, not its name, so a
        long episode across many restarts leaves exactly one marker per
        (bot, suffix) — not an unbounded pile in the state dir."""
        _run(
            tmp_path,
            """
            for r in A B C D E; do
                debounce_notify "$state" bot1 session_alerted _notify "down" "mgr-$r"
            done
            """,
        )
        markers = sorted(p.name for p in (tmp_path / "state").glob("bot1.*"))
        assert markers == ["bot1.session_alerted"]

    def test_clear_then_reoccur_fires_again(self, tmp_path):
        """The original contract: resolving the condition re-arms the alert."""
        sent = _run(
            tmp_path,
            """
            debounce_notify "$state" bot1 session_alerted _notify "down" "mgr-A"
            debounce_clear  "$state" bot1 session_alerted
            debounce_notify "$state" bot1 session_alerted _notify "down" "mgr-A"
            """,
        )
        assert sent == ["down", "down"]


class TestUnaddressedCaller:
    """`reload-fleet.sh:98` debounces an *action* (an npx warm attempt), not a
    notification — it has no recipient at all. Passing none must behave exactly
    as before, or that caller's once-per-episode contract silently changes."""

    def test_fire_once_semantics_preserved_when_no_recipient_given(self, tmp_path):
        sent = _run(
            tmp_path,
            """
            for _ in 1 2 3; do
                debounce_notify "$state" npx warm-attempted _notify "warm"
            done
            """,
        )
        assert sent == ["warm"]


class TestAgeOut:
    """The composing second leg. Recipient identity cannot cover every case —
    a send can fail silently, and a pid can be reused — so a marker older than
    the re-notify window re-surfaces even when the recipient looks unchanged."""

    def test_renotifies_after_the_marker_ages_out(self, tmp_path):
        _run(
            tmp_path,
            """
            debounce_notify "$state" bot1 session_alerted _notify "down" "mgr-A" 3600
            """,
        )
        m = _marker(tmp_path)
        os.utime(m, (0, 0))  # age it well past the window
        sent = _run(
            tmp_path,
            """
            debounce_notify "$state" bot1 session_alerted _notify "down" "mgr-A" 3600
            """,
        )
        assert sent == ["down", "down"]

    def test_does_not_renotify_inside_the_window(self, tmp_path):
        sent = _run(
            tmp_path,
            """
            debounce_notify "$state" bot1 session_alerted _notify "down" "mgr-A" 3600
            debounce_notify "$state" bot1 session_alerted _notify "down" "mgr-A" 3600
            """,
        )
        assert sent == ["down"]


class TestRearmRateLimit:
    """#1088: a churning recipient must not page on every pulse tick.

    pane_pid is the claude process, so pid churn is a REAL signal — each new pid
    never saw the alert. The token stays; only the RE-ARM RATE is bounded.

    The load-bearing choice is that the FIRST recipient change is always free.
    An earlier attempt floored re-arming on marker AGE instead, which also
    suppressed the first change — and that is #831's property. It failed the
    real-tmux rehearsal ("restarted manager receives the alert": expected 1, got
    0). Every test in the classes above passes here UNMODIFIED, which is the
    evidence that this bounds the rate without weakening the guarantee.
    """

    def test_pid_churn_does_not_page_every_tick(self, tmp_path):
        """Red against pre-#1088: 8 pulses, a new pid each, 8 pages."""
        sent = _run(
            tmp_path,
            'for i in $(seq 8); do\n'
            '  debounce_notify "$state" bot1 session_alerted _notify "m" "tok-$i" 21600\n'
            'done',
        )
        # Two, not one: the opening alert plus one free change. The free change
        # IS #831 being honoured — a fix that returned 1 here would have
        # suppressed a genuine restart.
        assert len(sent) == 2, f"expected opening alert + one free change, got {len(sent)}"

    def test_the_first_recipient_change_is_always_free(self, tmp_path):
        """THE property that separates this from an age floor. No aging, no
        sleep — a manager that restarted once is told immediately."""
        sent = _run(
            tmp_path,
            'debounce_notify "$state" bot1 session_alerted _notify "m" "mgr-A" 21600\n'
            'debounce_notify "$state" bot1 session_alerted _notify "m" "mgr-A" 21600\n'
            'debounce_notify "$state" bot1 session_alerted _notify "m" "mgr-B" 21600',
        )
        assert len(sent) == 2

    def test_a_legacy_marker_does_not_swallow_the_next_change(self, tmp_path):
        """Markers written before #1088 hold a bare recipient with no separator.
        They must read as zero prior re-arms, or the upgrade itself eats one
        genuine restart per bot."""
        sent = _run(
            tmp_path,
            'printf "mgr-OLD" > "$state/bot1.session_alerted"\n'
            'debounce_notify "$state" bot1 session_alerted _notify "m" "mgr-NEW" 21600',
        )
        assert len(sent) == 1

    def test_window_of_zero_restores_pre_fix_behaviour(self, tmp_path):
        """The escape hatch has to actually escape."""
        sent = _run(
            tmp_path,
            'export FLEET_PULSE_REARM_WINDOW_S=0\n'
            'for i in $(seq 8); do\n'
            '  debounce_notify "$state" bot1 session_alerted _notify "m" "tok-$i" 21600\n'
            'done',
        )
        assert len(sent) == 8

    def test_the_age_out_leg_does_not_consume_the_rearm_budget(self, tmp_path):
        """A renotify fired by the 6h age-out is not a re-arm. If it stamped the
        re-arm clock, a long episode would silently spend the allowance a
        genuine restart is entitled to."""
        _run(
            tmp_path,
            'debounce_notify "$state" bot1 session_alerted _notify "m" "mgr-A" 3600',
        )
        os.utime(_marker(tmp_path), (0, 0))  # age past the renotify window
        sent = _run(
            tmp_path,
            'debounce_notify "$state" bot1 session_alerted _notify "m" "mgr-A" 3600\n'
            'debounce_notify "$state" bot1 session_alerted _notify "m" "mgr-B" 3600',
        )
        # age-out fires, then the restart still gets its free change
        assert len(sent) == 3
