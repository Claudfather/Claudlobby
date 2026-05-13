#!/usr/bin/env python3
"""github-mcp-wrapper.py — long-lived wrapper around the GitHub MCP server.

Spawns @modelcontextprotocol/server-github with a freshly-minted GitHub App
installation token. Every ~50 minutes (just under the 1-hour token lifetime)
the child is terminated and respawned with a new token. The bot stays up;
only the MCP subprocess blips for ~2s during respawn.

Stdio passthrough: this script inherits Claude Code's stdin/stdout pipes and
hands them straight to the child. No proxying happens in this process — the
child reads/writes the MCP host's pipes directly, so steady-state latency is
identical to running the MCP server bare.

Falls back to the GITHUB_PAT env var when git's credential helper can't mint a
token. Refuses to start if neither path produces a token.

Caveats:
- During the ~2s restart window, in-flight MCP requests fail. The model is
  expected to retry on transient MCP errors.
- The credential helper cache (50-minute TTL by default) is cleared before
  each mint so we get a fresh `ghs_` rather than a near-expiry cached value.
"""

import os
import signal
import subprocess
import sys
import time

REFRESH_INTERVAL = 50 * 60  # seconds — well under the 1-hour ghs_ lifetime
MCP_PACKAGE = "@modelcontextprotocol/server-github@2025.4.8"
TERMINATE_GRACE_SEC = 5

# Allow opt-out — single-shot mode that exec's the MCP server with no refresh
# loop. Useful for fleets that already restart MCP servers on a schedule.
ONE_SHOT = os.environ.get("GITHUB_MCP_REFRESH", "true").lower() == "false"


def _git_credential(action: str) -> str:
    """Run `git credential <action>` against the github.com input block."""
    return subprocess.run(
        ["git", "credential", action],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        capture_output=True,
        check=False,
    ).stdout


def mint_token() -> str:
    """Mint a fresh ghs_ token. Returns empty string on failure."""
    # Invalidate any cached credential so the next fill goes through
    # git-credential-botfarm and produces a fresh ghs_.
    _git_credential("reject")
    out = _git_credential("fill")
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password=") :]
    return os.environ.get("GITHUB_PAT", "")


def spawn_child(token: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
    return subprocess.Popen(
        ["npx", "-y", MCP_PACKAGE] + sys.argv[1:],
        env=env,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def _log(msg: str) -> None:
    print(f"github-mcp-wrapper: {msg}", file=sys.stderr, flush=True)


def _terminate(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    try:
        child.terminate()
        child.wait(timeout=TERMINATE_GRACE_SEC)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def main() -> int:
    if ONE_SHOT:
        token = mint_token()
        if not token:
            _log("no token from helper or GITHUB_PAT — refusing to start")
            return 1
        os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
        os.execvp("npx", ["npx", "-y", MCP_PACKAGE] + sys.argv[1:])
        return 1  # unreachable

    child: subprocess.Popen | None = None

    def on_signal(signum: int, _frame) -> None:
        if child is not None:
            _terminate(child)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    while True:
        token = mint_token()
        if not token:
            _log("no token from helper or GITHUB_PAT — refusing to start")
            return 1

        child = spawn_child(token)
        _log(f"spawned MCP server pid={child.pid}, refresh in {REFRESH_INTERVAL}s")

        try:
            rc = child.wait(timeout=REFRESH_INTERVAL)
            _log(f"child exited rc={rc} before refresh interval; respawning")
        except subprocess.TimeoutExpired:
            _log("refresh interval reached; rotating token + respawning child")
            _terminate(child)


if __name__ == "__main__":
    sys.exit(main())
