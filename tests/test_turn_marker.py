"""turn-marker.sh (#1053): the bot's own record of when input last reached its
session (data/.turn-input) and when its last turn ended (data/.turn-ended).

Payloads are built as Claude Code sends them, compact JSON, identifiers faked.
Nothing reads the markers yet; these tests pin what writes them.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from textwrap import dedent

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "claudlobby/_runtime_scripts/turn-marker.sh"
COMMAND = "$CLAUDLOBBY_NATIVE_DIR/turn-marker.sh"
MARKERS = {"UserPromptSubmit": ".turn-input", "Stop": ".turn-ended", "StopFailure": ".turn-ended"}
# The event key as a prompt might quote it; JSON escapes its quotes in transit.
QUOTED = '"hook_event_name":"Stop"'
# Exit 0 with empty stdout and stderr, on every path: a UserPromptSubmit hook's
# stdout joins the prompt on every bot and its exit 2 blocks the prompt; a Stop
# hook's exit 2 keeps the turn going.
CLEAN = (0, "", "")


def _payload(event: str, *, compact: bool = True, **extra) -> str:
    p = {
        "session_id": "00000000-0000-4000-8000-000000000000",
        "transcript_path": "/tmp/harness/transcript.jsonl",
        "cwd": "/tmp/harness",
        "permission_mode": "default",
        "hook_event_name": event,
        **extra,
    }
    return json.dumps(p, separators=(",", ":")) if compact else json.dumps(p)


def _extra(event: str) -> dict:
    if event == "UserPromptSubmit":
        return {"prompt": "hello"}
    if event == "StopFailure":
        return {"error": "server_error", "last_assistant_message": "API Error: 529"}
    return {"stop_hook_active": False, "last_assistant_message": "Done."}


def _run(env_bot_dir: str | None, payload: str, cwd: Path | None = None):
    env = {"PATH": "/usr/bin:/bin"}
    if env_bot_dir is not None:
        env["BOT_DIR"] = env_bot_dir
    r = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=30,
    )
    return r.returncode, r.stdout, r.stderr


def _names(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir())


@pytest.fixture
def bot(tmp_path):
    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.mark.parametrize("event", sorted(MARKERS))
def test_each_event_stamps_its_own_marker(bot, event):
    assert _run(str(bot), _payload(event, **_extra(event))) == CLEAN
    assert _names(bot / "data") == [MARKERS[event]]
    assert (bot / "data" / MARKERS[event]).read_text() == event + "\n"


def test_an_event_moves_only_its_own_marker(bot):
    data = bot / "data"
    for name in (".turn-input", ".turn-ended"):
        (data / name).write_text("old\n")
        os.utime(data / name, (1_000_000_000, 1_000_000_000))
    assert _run(str(bot), _payload("Stop", **_extra("Stop"))) == CLEAN
    assert (data / ".turn-ended").stat().st_mtime > time.time() - 60
    assert (data / ".turn-ended").read_text() == "Stop\n"
    assert (data / ".turn-input").stat().st_mtime == 1_000_000_000
    assert (data / ".turn-input").read_text() == "old\n"


def test_a_payload_with_spaces_after_its_colons_stamps_too(bot):
    assert _run(str(bot), _payload("UserPromptSubmit", compact=False, prompt="hi")) == CLEAN
    assert _names(bot / "data") == [".turn-input"]


def test_the_payload_is_read_whole(bot):
    """The key after a 300 KB prompt still counts: nothing assumes it comes first."""
    p = json.dumps({"prompt": "x" * 300_000, "hook_event_name": "UserPromptSubmit"},
                   separators=(",", ":"))
    assert _run(str(bot), p) == CLEAN
    assert (bot / "data/.turn-input").read_text() == "UserPromptSubmit\n"


def test_a_prompt_quoting_another_event_is_read_by_its_own_key(bot):
    p = json.dumps({"prompt": "see " + QUOTED, "hook_event_name": "UserPromptSubmit"},
                   separators=(",", ":"))
    assert _run(str(bot), p) == CLEAN
    assert _names(bot / "data") == [".turn-input"]


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not json",
        "[]",
        "{}",
        _payload("SessionStart", source="startup"),
        _payload("PreToolUse", tool_name="Bash"),
        _payload("PostToolUse", tool_name="Bash"),
        _payload("SubagentStop", stop_hook_active=False),
        _payload("PreCompact", trigger="manual"),
        _payload("Notification", message="see " + QUOTED),
        json.dumps({"message": QUOTED, "hook_event_name": "Notification"}, separators=(",", ":")),
    ],
)
def test_any_other_event_stamps_nothing(bot, payload):
    assert _run(str(bot), payload) == CLEAN
    assert _names(bot / "data") == []


def test_a_closed_stdin_exits_0(bot):
    """No payload at all: cat fails, and the hook still prints nothing and exits 0."""
    r = subprocess.run(
        ["bash", "-c", 'exec bash "$0" <&-', str(HOOK)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "BOT_DIR": str(bot)},
        timeout=30,
    )
    assert (r.returncode, r.stdout, r.stderr) == CLEAN
    assert _names(bot / "data") == []


def test_without_data_it_writes_nothing(tmp_path):
    assert _run(str(tmp_path), _payload("Stop", **_extra("Stop"))) == CLEAN
    assert _names(tmp_path) == []


@pytest.mark.parametrize("bot_dir", [None, "", "relative/bot"])
def test_without_an_absolute_bot_dir_it_writes_nowhere(tmp_path, bot_dir):
    # Writable data/ dirs where a cwd-relative write would land.
    (tmp_path / "data").mkdir()
    (tmp_path / "relative/bot/data").mkdir(parents=True)
    for event in sorted(MARKERS):
        assert _run(bot_dir, _payload(event, **_extra(event)), cwd=tmp_path) == CLEAN
    assert _names(tmp_path / "data") == []
    assert _names(tmp_path / "relative/bot/data") == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only directory")
def test_a_read_only_data_dir_exits_0(bot):
    data = bot / "data"
    data.chmod(0o500)
    try:
        assert _run(str(bot), _payload("UserPromptSubmit", prompt="hi")) == CLEAN
    finally:
        data.chmod(0o700)
    assert _names(data) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only file")
def test_a_read_only_marker_exits_0(bot):
    marker = bot / "data/.turn-ended"
    marker.write_text("old\n")
    marker.chmod(0o400)
    assert _run(str(bot), _payload("Stop", **_extra("Stop"))) == CLEAN
    assert marker.read_text() == "old\n"


def test_every_bot_is_composed_with_it_on_the_three_turn_events(tmp_path):
    from claudlobby.composer import compose_settings_local
    from claudlobby.config import load_fleet
    from claudlobby.paths import Paths
    from tests.package_fixtures import git_package

    root = tmp_path / "claudlobby"
    (root / "library" / "expertise").mkdir(parents=True)
    (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
    for bot_id in ("lead", "worker"):
        (root / "runtime" / "bots" / bot_id).mkdir(parents=True)
    (root / "fleet.yaml").write_text(
        dedent(
            """\
            fleet:
              name: test-fleet
              manager: lead
              service_prefix: com.test
              bots:
                lead:
                  expertise: [eng]
                worker:
                  expertise: [eng]
            """
        )
    )
    fleet, _ = load_fleet(root / "fleet.yaml")
    paths = Paths(root=root, fleet_dir=root, package=git_package(root))
    for bot_id in ("lead", "worker"):  # the manager and a worker alike
        hooks = compose_settings_local(fleet.bots[bot_id], fleet, paths)["hooks"]
        found = {
            event: sum(h["command"] == COMMAND for group in groups for h in group["hooks"])
            for event, groups in hooks.items()
        }
        assert {e: n for e, n in found.items() if n} == {
            "UserPromptSubmit": 1,
            "Stop": 1,
            "StopFailure": 1,
        }, (bot_id, found)
