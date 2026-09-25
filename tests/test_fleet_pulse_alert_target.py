"""fleet-pulse and the alert PAIR (#1771, part B) -- reviewer-supplied for #1782.

The PR's tests pin the resolver (`test_alert_target_pair.py`) and `_emit_fleet_signal`; this drives the
REAL fleet-pulse sweep with a REFUSED escalation target, which is loud: a WARNING that names the variable,
and a plane event (`alert_target_refused`), while nothing is sent to the refused chat. The page's once-a-day
repeat is `debounce_notify`'s, tested on its own.

Same rig as `test_fleet_pulse_escalated.py`: the real sweep, a throwaway plane, one stub (`tg-post.sh`).
Fake ids only.
"""

from __future__ import annotations

import json

from tests.conftest import read_fleet_events
from tests.test_fleet_pulse_escalated import _pulse, _pulse_lib, _scene, needs_tmux

CHAT_ESC = "-1003333333333"
FAST = {"FLEET_EVENT_EMIT_TIMEOUT_S": "120"}

# the test runs the real sweep, which needs tmux
pytestmark = needs_tmux


def _events(root, kind):
    rows = [json.loads(line) for line in read_fleet_events(root).splitlines() if line]
    return [r for r in rows if r["type"] == kind]


def _marker(root):
    return root / "state" / "pulse" / "fleet.alert_target_refused"


# --- a refused escalation target is loud ------------------------------------------


def test_a_refused_escalation_chat_is_loud(tmp_path):
    root, paths, wi, asg = _scene(tmp_path)
    capture = tmp_path / "tg.log"
    libdir = _pulse_lib(tmp_path, capture)
    refused = {
        "FLEET_PULSE_ESCALATION_CHAT_ID": CHAT_ESC,
        "FLEET_PULSE_ESCALATION_STATE_DIR": "",
        **FAST,
    }

    r = _pulse(root, libdir, **refused)
    assert r.returncode == 0, r.stderr[-2000:]
    # loud, and it names the fix
    assert "escalation Telegram target REFUSED" in r.stderr, r.stderr[-1500:]
    assert "FLEET_PULSE_ESCALATION_STATE_DIR" in r.stderr
    # nothing is sent at all: the refused target is not attempted, and no other page rides in its place
    assert not capture.exists(), capture.read_text()
    # ...and the refusal reaches the plane, once
    assert len(_events(root, "alert_target_refused")) == 1
    assert _marker(root).exists()
