#!/usr/bin/env python3
"""github-app-mcp-wrapper.py — long-lived wrapper around the GitHub MCP server.

App-auth P2 (#1272; plan:
documentation/plans/2026-08-19-github-app-installation-token-auth.md).
Spawns the GitHub MCP server with a freshly minted App installation token and
re-mints + respawns the child every ~50 minutes (just under the 1-hour ghs_
lifetime), so token expiry never requires a bot restart. Stdio passthrough:
the child inherits Claude Code's pipes directly — steady-state latency equals
running the server bare; in-flight MCP requests fail for ~2s per respawn and
the model is expected to retry (validated against the pinned package below;
any server swap must re-validate post-respawn tool calls).

THE PROGRAM INVARIANT (D1/D10): tokens come from invoking
lib/git-credential-github-app DIRECTLY — never `git credential fill`, which a
pathless context makes silently serve whatever identity ambient git config
answers with (the fork original minted that way and was rewritten here).

FAILURE CONTRACT (D12, the RTC-less Pi boot-clock window makes first-mint
failure an every-boot class):
  - first mint fails -> stay alive and retry with backoff BEFORE the first
    child spawn (the stdio client waits; the session gets a late GitHub MCP,
    never a dead one)
  - a REFRESH mint fails -> keep serving the live child and retry sooner;
    its token may outlive one refresh window (brief 401s beat a dead server)
  - the child dies on its own -> respawn (with a fresh mint) after a short
    pause, so a crash-looping server cannot spin hot
  - every mint failure has already emitted an auth_mint_failed JSONL event
    from inside the helper, so degradation is visible same-day
Falls back to the GITHUB_PAT env var when the helper cannot mint. Refuses
nothing at startup: with neither path yielding a token it keeps retrying.

One-shot opt-out: GITHUB_MCP_REFRESH=false execs the server once with no
refresh loop, for fleets that already restart MCP servers on a schedule.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Same pin as library/mcp/github.json — the respawn-transparency evidence is
# package-specific, so the two fragments must move together.
MCP_PACKAGE = "@modelcontextprotocol/server-github@2025.4.8"

HELPER = Path(
    os.environ.get("GITHUB_APP_HELPER")
    or Path(__file__).resolve().parent / "git-credential-github-app"
)

REFRESH_SECONDS = int(os.environ.get("GITHUB_MCP_REFRESH_SECONDS", str(50 * 60)))
RETRY_BASE_SECONDS = float(os.environ.get("GITHUB_MCP_RETRY_BASE_SECONDS", "5"))
RETRY_MAX_SECONDS = float(os.environ.get("GITHUB_MCP_RETRY_MAX_SECONDS", "60"))
RESPAWN_PAUSE_SECONDS = float(os.environ.get("GITHUB_MCP_RESPAWN_PAUSE_SECONDS", "1"))
REFRESH_RETRY_SECONDS = float(os.environ.get("GITHUB_MCP_REFRESH_RETRY_SECONDS", "60"))
TERMINATE_GRACE_SEC = 5
ONE_SHOT = os.environ.get("GITHUB_MCP_REFRESH", "true").lower() == "false"

CTX = "protocol=https\nhost=github.com\n\n"


def _log(msg: str) -> None:
    print(f"github-app-mcp-wrapper: {msg}", file=sys.stderr, flush=True)


def mint_once() -> str:
    """One helper-direct mint. Returns the token, or '' on failure.

    The helper owns the failure ceremony (stderr reason, auth_mint_failed
    event); this passes its stderr through and parses only the protocol
    output. GITHUB_PAT is the last-resort fallback for mixed-mode fleets.
    """
    try:
        proc = subprocess.run(
            [str(HELPER), "get"],
            input=CTX,
            text=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _log(f"helper invocation failed: {e}")
        return os.environ.get("GITHUB_PAT", "")
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        sys.stderr.flush()
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password=") :]
    return os.environ.get("GITHUB_PAT", "")


def mint_with_retry() -> str:
    """Block until a token exists — backoff capped, never gives up (D12)."""
    delay = RETRY_BASE_SECONDS
    while True:
        token = mint_once()
        if token:
            return token
        _log(f"no token from helper or GITHUB_PAT — retrying in {delay:.0f}s")
        time.sleep(delay)
        delay = min(delay * 2, RETRY_MAX_SECONDS)


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
        token = mint_with_retry()
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

    token = mint_with_retry()
    child = spawn_child(token)
    _log(f"spawned MCP server pid={child.pid}, refresh in {REFRESH_SECONDS}s")
    wait_for = REFRESH_SECONDS

    while True:
        try:
            rc = child.wait(timeout=wait_for)
            _log(f"child exited rc={rc}; respawning in {RESPAWN_PAUSE_SECONDS:.0f}s")
            time.sleep(RESPAWN_PAUSE_SECONDS)
            token = mint_with_retry()
            child = spawn_child(token)
            _log(f"respawned MCP server pid={child.pid}")
            wait_for = REFRESH_SECONDS
        except subprocess.TimeoutExpired:
            fresh = mint_once()
            if fresh:
                _log("refresh interval reached; rotating token + respawning child")
                _terminate(child)
                token = fresh
                child = spawn_child(token)
                wait_for = REFRESH_SECONDS
            else:
                # D12: a failed refresh must not kill a working server — keep
                # serving on the old token and retry sooner.
                _log(
                    "refresh mint failed; keeping live child, retrying in "
                    f"{REFRESH_RETRY_SECONDS:.0f}s"
                )
                wait_for = REFRESH_RETRY_SECONDS


if __name__ == "__main__":
    sys.exit(main())
