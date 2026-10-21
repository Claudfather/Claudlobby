"""Claude Code's Telegram channel plugin MCP logs, laid out where it writes them (#2423).

The captures in channel-mcp-log/ are live (Linux, Claude Code 2.1.306) with the
session id, cwd, marketplace and bot handle replaced; each ends in Claude Code's
decision line. The layout is computed here independently of the shell reader
under test: Claude Code's cache dir, then the session's cwd as
claudlobby.isolation.transcript_slug names it.
"""

from __future__ import annotations

import datetime
import os
import sys
import time
from pathlib import Path

from claudlobby.isolation import transcript_slug

CAPTURES = Path(__file__).resolve().parent / "channel-mcp-log"
REASON = (
    "plugin telegram@example-plugins is not on the approved channels allowlist "
    "(use --dangerously-load-development-channels for local dev)"
)


def capture(name: str) -> str:
    return (CAPTURES / f"{name}.jsonl").read_text()


def decision_line(name: str) -> str:
    """The capture's last line: Claude Code's decision."""
    return capture(name).splitlines(keepends=True)[-1]


#: A session whose server has connected and whose log holds no decision yet.
CONNECTING = "".join(capture("refused").splitlines(keepends=True)[:-1])


def log_dir(home: Path, bot: Path) -> Path:
    if sys.platform == "darwin":
        cache = home / "Library" / "Caches"
    else:
        cache = home / ".cache"
    return cache / "claude-cli-nodejs" / transcript_slug(os.path.realpath(bot)) / "mcp-logs-plugin-telegram-telegram"


def stamp(epoch: float) -> str:
    """A log's name: its session's UTC start, as Claude Code writes it."""
    t = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H-%M-%S-") + f"{t.microsecond // 1000:03d}Z"


def write_log(home: Path, bot: Path, text: str, *, epoch: float | None = None) -> Path:
    """One session's log, named for a session started at epoch (default now)."""
    d = log_dir(home, bot)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{stamp(time.time() if epoch is None else epoch)}.jsonl"
    p.write_text(text)
    return p
