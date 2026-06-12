"""first_bot_with_conf — resolve the first fleet bot whose bot.conf declares a
non-empty value for a key.

Backs fleet-pulse.sh escalation chat-id resolution: the escalation Telegram
target must be drawn from a bot that actually declares TELEGRAM_GROUP_CHAT_ID,
not blindly from the alphabetically-first bot directory (which may lack the key
and silently mute the entire fleet-alert safety net).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_COMMON = REPO_ROOT / "lib" / "lib-common.sh"

_HAS = 'export TELEGRAM_GROUP_CHAT_ID="-100222"\n'


def _resolve(bots_dir: Path, key: str) -> tuple[str, int]:
    """Source lib-common.sh and call first_bot_with_conf; return (stdout, rc)."""
    proc = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; first_bot_with_conf "$2" "$3"',
            "_",
            str(LIB_COMMON),
            str(bots_dir),
            key,
        ],
        capture_output=True,
        text=True,
    )
    # Trailing slash is an artifact of the dir glob; normalize for comparison.
    return proc.stdout.strip().rstrip("/"), proc.returncode


def _bot(bots_dir: Path, name: str, conf: str) -> Path:
    d = bots_dir / name
    d.mkdir(parents=True)
    (d / "bot.conf").write_text(conf)
    return d


@pytest.mark.parametrize(
    "bots, expected_name, expected_rc",
    [
        # First bot already declares the key -> returned as-is.
        (
            [("aaa", 'export TELEGRAM_GROUP_CHAT_ID="-100111"\n'), ("bbb", _HAS)],
            "aaa",
            0,
        ),
        # First bot omits the key entirely -> skipped, later bot returned.
        ([("aaa", "BOT_NAME=aaa\n"), ("bbb", _HAS)], "bbb", 0),
        # First bot declares the key but empty -> treated as missing, skipped.
        ([("aaa", "export TELEGRAM_GROUP_CHAT_ID=\n"), ("bbb", _HAS)], "bbb", 0),
        # No bot declares a non-empty value -> no output, non-zero return.
        (
            [("aaa", "BOT_NAME=aaa\n"), ("bbb", "export TELEGRAM_GROUP_CHAT_ID=\n")],
            None,
            1,
        ),
    ],
    ids=["first-bot-has-it", "skips-missing-key", "skips-empty-value", "none-qualify"],
)
def test_resolves_first_bot_with_nonempty_key(
    tmp_path, bots, expected_name, expected_rc
):
    bots_dir = tmp_path / "bots"
    for name, conf in bots:
        _bot(bots_dir, name, conf)
    out, rc = _resolve(bots_dir, "TELEGRAM_GROUP_CHAT_ID")
    assert rc == expected_rc
    assert out == (str(bots_dir / expected_name) if expected_name else "")


def test_returns_nonzero_when_bots_dir_absent(tmp_path):
    out, rc = _resolve(tmp_path / "nope", "TELEGRAM_GROUP_CHAT_ID")
    assert rc == 1
    assert out == ""
