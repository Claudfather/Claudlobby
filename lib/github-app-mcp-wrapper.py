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

Tokens come from lib/mint-github-token.sh — the one consumer door the P1
helper names for non-git callers. The mint CLI is itself helper-direct
(D1/D10: never `git credential fill`, whose pathless context silently serves
whatever identity ambient git config answers with — the fork original minted
that way and was rewritten). The CLI seam keeps ONE copy of the credential
protocol parse estate-wide and streams helper stderr live for free.

FAILURE CONTRACT (D12, the RTC-less Pi boot-clock window makes first-mint
failure an every-boot class):
  - first mint fails -> stay alive and retry with backoff BEFORE the first
    child spawn (the stdio client waits; the session gets a late GitHub MCP,
    never a dead one)
  - a REFRESH mint fails -> keep serving the live child and retry sooner;
    its token may outlive one refresh window (brief 401s beat a dead server)
  - the child dies on its own -> respawn after a short pause (no hot
    crash-loop), REUSING the current token while it is younger than the
    refresh window — a crash-looping child must not become a mint-per-crash
    loop against GitHub
  - every mint failure has already emitted an auth_mint_failed JSONL event
    from inside the helper, so degradation is visible same-day
Falls back to the GITHUB_PAT env var when the mint CLI cannot mint. Refuses
nothing at startup: with neither path yielding a token it keeps retrying.

Knobs (env; a malformed value logs and falls back to the default rather than
dying — a parse error must not produce the dead server D12 exists to prevent):
  GITHUB_MCP_REFRESH_SECONDS   refresh cadence (default 3000 — the operator
                               knob; keep it under the 3600s ghs_ lifetime)
  GITHUB_MCP_ONE_SHOT=1        exec the server once, no refresh loop (for
                               fleets that already restart MCPs on a schedule)
  GITHUB_MCP_POLL_SECONDS      child liveness poll cadence (default 2 —
                               Popen.wait(timeout) busy-polls at 50ms in
                               CPython, ~20 wakeups/s for the daemon's life;
                               a chunked poll is the Pi-friendly shape)
  GITHUB_MCP_RETRY_BASE_SECONDS / GITHUB_MCP_RETRY_MAX_SECONDS /
  GITHUB_MCP_RESPAWN_PAUSE_SECONDS / GITHUB_MCP_REFRESH_RETRY_SECONDS
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

MINT_CLI = Path(
    os.environ.get("GITHUB_APP_MINT_CLI")
    or Path(__file__).resolve().parent / "mint-github-token.sh"
)


def _log(msg: str) -> None:
    print(f"github-app-mcp-wrapper: {msg}", file=sys.stderr, flush=True)


def _envf(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        _log(f"ignoring malformed {name}={raw!r}; using default {default}")
        return default


REFRESH_SECONDS = _envf("GITHUB_MCP_REFRESH_SECONDS", 50 * 60)
POLL_SECONDS = _envf("GITHUB_MCP_POLL_SECONDS", 2.0)
RETRY_BASE_SECONDS = _envf("GITHUB_MCP_RETRY_BASE_SECONDS", 5.0)
RETRY_MAX_SECONDS = _envf("GITHUB_MCP_RETRY_MAX_SECONDS", 60.0)
RESPAWN_PAUSE_SECONDS = _envf("GITHUB_MCP_RESPAWN_PAUSE_SECONDS", 1.0)
REFRESH_RETRY_SECONDS = _envf("GITHUB_MCP_REFRESH_RETRY_SECONDS", 60.0)
TERMINATE_GRACE_SEC = 5
ONE_SHOT = os.environ.get("GITHUB_MCP_ONE_SHOT", "") == "1"


def _child_argv() -> list:
    return ["npx", "-y", MCP_PACKAGE] + sys.argv[1:]


def mint_once() -> str:
    """One mint through the CLI door. Returns the token, or '' on failure.

    stderr is inherited, so the helper's diagnostics stream live; the helper
    has already emitted its auth_mint_failed event by the time this returns.
    GITHUB_PAT is the last-resort fallback for mixed-mode fleets.
    """
    try:
        proc = subprocess.run(
            [str(MINT_CLI)],
            stdout=subprocess.PIPE,
            text=True,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _log(f"mint invocation failed: {e}")
        return os.environ.get("GITHUB_PAT", "")
    token = proc.stdout.strip() if proc.returncode == 0 else ""
    return token or os.environ.get("GITHUB_PAT", "")


def mint_with_retry() -> str:
    """Block until a token exists — backoff capped, never gives up (D12)."""
    delay = RETRY_BASE_SECONDS
    while True:
        token = mint_once()
        if token:
            return token
        _log(f"no token from mint CLI or GITHUB_PAT — retrying in {delay:.0f}s")
        time.sleep(delay)
        delay = min(delay * 2, RETRY_MAX_SECONDS)


def spawn_child(token: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
    return subprocess.Popen(
        _child_argv(),
        env=env,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def _wait_child(child: subprocess.Popen, seconds: float):
    """Chunked liveness poll: child rc, or None when `seconds` elapse (E1)."""
    deadline = time.monotonic() + seconds
    while True:
        rc = child.poll()
        if rc is not None:
            return rc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(POLL_SECONDS, remaining))


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
        os.execvp("npx", _child_argv())
        return 1  # unreachable

    child: "subprocess.Popen | None" = None

    def on_signal(signum: int, _frame) -> None:
        if child is not None:
            _terminate(child)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    token = mint_with_retry()
    minted_at = time.monotonic()
    child = spawn_child(token)
    _log(f"spawned MCP server pid={child.pid}, refresh in {REFRESH_SECONDS:.0f}s")
    wait_for = REFRESH_SECONDS

    while True:
        rc = _wait_child(child, wait_for)
        if rc is not None:
            # Child died on its own: pause (no hot crash-loop), REUSE the
            # token while it is younger than the refresh window (E2 — a
            # crash-looping child must not mint per crash), respawn.
            _log(f"child exited rc={rc}; respawning in {RESPAWN_PAUSE_SECONDS:.0f}s")
            time.sleep(RESPAWN_PAUSE_SECONDS)
            age = time.monotonic() - minted_at
            if age >= REFRESH_SECONDS:
                token = mint_with_retry()
                minted_at = time.monotonic()
                age = 0.0
            child = spawn_child(token)
            _log(f"respawned MCP server pid={child.pid}")
            wait_for = max(5.0, REFRESH_SECONDS - age)
        else:
            # Refresh due: mint BEFORE terminating, keeping mint latency out
            # of the downtime window.
            fresh = mint_once()
            if fresh:
                _log("refresh interval reached; rotating token + respawning child")
                _terminate(child)
                token = fresh
                minted_at = time.monotonic()
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
