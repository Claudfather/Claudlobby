"""#2036: every keystroke into a bot's pane goes through the pane's send lock.

A bare ``send-keys`` outside the locked helpers types into a pane without its
lock, which is the #2036 interleave. Two slipped in between the lock's design and
its landing, the held-delivery repair's Enter (#2105) and bot interrupt's Escape,
and nothing failed (vera's #2040 review: no test scans for a bare send-keys).

So every non-comment line that runs ``send-keys`` in the runtime scripts, and in
the shell the package embeds, must sit in a function named below, each of which
runs with the recipient's lock held. A new one anywhere else fails here until it
goes through ``pane_send_key`` or ``pane_send_verified``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCES = sorted([*(REPO / "claudlobby/_runtime_scripts").glob("*.sh"),
                  *(REPO / "claudlobby/_runtime_scripts").glob("*.py"),
                  *(REPO / "claudlobby").rglob("*.py")])

# (file, shell function) -> why its keystrokes are under the lock.
LOCKED = {
    ("lib-common.sh", "_pane_send_payload"): "typed only from the locked send body",
    ("lib-common.sh", "_pane_send_verified_locked"): "the send body, run under pane_send_verified's lock",
    ("lib-common.sh", "_pane_receipt_enter"): "run under pane_await_receipt's lock",
    ("lib-common.sh", "pane_send_key"): "takes the lock itself",
    ("message_transport.py", "_repair_look_and_press"): "run under the repair script's lock",
}

_FUNC = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*[{(]?\s*$")
_END = re.compile(r"^[})]\s*$")


def _sends(text: str):
    """(shell function or None, line number) for each line that runs send-keys.

    A shell function opens on a column-0 ``name() {`` or ``name() (`` line and
    ends at a column-0 ``}`` or ``)``, as every function in these files does."""
    func = None
    for number, line in enumerate(text.splitlines(), 1):
        opened = _FUNC.match(line)
        if opened:
            func = opened.group(1)
        elif "send-keys" in line and not line.lstrip().startswith("#"):
            yield func, number
        if _END.match(line):
            func = None


def test_every_send_keys_runs_under_the_send_lock():
    bare = [f"{path.relative_to(REPO)}:{number} in {func or 'top level'}"
            for path in SOURCES
            for func, number in _sends(path.read_text(encoding="utf-8"))
            if (path.name, func) not in LOCKED]
    assert bare == [], "send-keys outside the pane's send lock (route it through pane_send_key):\n" + "\n".join(bare)


def test_the_scan_finds_a_planted_bare_send():
    """The positive control: a scan that never fires passes the check above."""
    planted = ("helper() {\n    bot_tmux s send-keys -t x Enter\n}\n"
               "bot_tmux s send-keys -t x Escape\n"
               "# bot_tmux s send-keys in a comment\n"
               "boxed() (\n    tmux send-keys -t x C-c\n)\n")
    assert list(_sends(planted)) == [("helper", 2), (None, 4), ("boxed", 7)]


def test_every_listed_function_still_sends_keys():
    """An entry whose function was renamed or emptied allows nothing; keep the list true."""
    found = {(path.name, func) for path in SOURCES for func, _ in _sends(path.read_text(encoding="utf-8"))}
    assert sorted(set(LOCKED) - found) == []
