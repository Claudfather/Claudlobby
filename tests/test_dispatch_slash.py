"""Python-wrapped bash test for dispatch.sh's slash-command classifier.

#627 P2 / plan #628 F6. A leading slash command must reach the worker's pane as
the FIRST characters, so Claude Code's slash-command recognition fires — the
`set +H;` history-expansion guard dispatch.sh prepends would otherwise swallow
it (the live briefing-cron bug). Everything else keeps the guard, and file-path
prose / leading-whitespace must NOT be misclassified as a slash command.

The test copies dispatch.sh next to a stub lib-common.sh (stubbing bot_tmux_send
to capture the payload) so it exercises the real conditional without a live tmux.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DISPATCH = REPO / "lib" / "dispatch.sh"

STUB_LIB_COMMON = """\
#!/bin/bash
# Minimal stubs so dispatch.sh runs without a real tmux server or socket.
install_error_trap() { :; }
tmux_socket_for_session() { printf '%s' "fakesock"; }
bot_tmux_send() { printf '%s' "$3" > "$CAPTURE_FILE"; return 0; }
"""


def _dispatched_payload(tmp_path: Path, message: str) -> str:
    """Return the exact text dispatch.sh hands to bot_tmux_send for *message*."""
    libdir = tmp_path / "lib"
    libdir.mkdir(exist_ok=True)
    (libdir / "lib-common.sh").write_text(STUB_LIB_COMMON)
    (libdir / "dispatch.sh").write_text(DISPATCH.read_text())
    capture = tmp_path / "capture"
    subprocess.run(
        ["bash", str(libdir / "dispatch.sh"), "worker", message],
        env={**os.environ, "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
    )
    return capture.read_text()


# Genuine leading slash commands → must land BARE (no set +H; swallow).
SLASH_BARE = ["/briefing morning", "/autonomous-sprint", "/claudna:ironclad x"]

# Prose, file-path text, and leading-whitespace → must KEEP the set +H; guard.
PROSE_PREFIXED = [
    "deploy failed !!",
    "/home/crog/x is broken",
    "/etc/hosts",
    " /leading-space",
]


@pytest.mark.parametrize("message", SLASH_BARE)
def test_slash_command_lands_bare(tmp_path: Path, message: str):
    payload = _dispatched_payload(tmp_path, message)
    assert not payload.startswith("set +H;"), (
        f"slash command swallowed by guard: {payload!r}"
    )
    assert payload == message, f"slash command must land verbatim, got: {payload!r}"


@pytest.mark.parametrize("message", PROSE_PREFIXED)
def test_prose_keeps_history_expansion_guard(tmp_path: Path, message: str):
    payload = _dispatched_payload(tmp_path, message)
    assert payload == f"set +H; {message}", (
        f"prose must keep set +H; guard, got: {payload!r}"
    )


# Amended F6 (rajan's edge case): a payload that STARTS with a slash-word but
# contains '!' matches the slash regex yet must KEEP the guard — bare-sending it
# would re-expose bash history-expansion on the '!'. Covers prose-with-slash-word
# (/reports ...) and the accepted tradeoff of a genuine slash command whose args
# carry '!' (it keeps the guard rather than risk a swallowed send).
BANG_KEEPS_GUARD = ["/reports is missing !!", "/deploy the fix now!"]


@pytest.mark.parametrize("message", BANG_KEEPS_GUARD)
def test_slashword_payload_with_bang_keeps_history_guard(tmp_path: Path, message: str):
    payload = _dispatched_payload(tmp_path, message)
    assert payload == f"set +H; {message}", (
        f"payload with '!' must keep set +H; guard, got: {payload!r}"
    )
