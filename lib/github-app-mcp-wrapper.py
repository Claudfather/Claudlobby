#!/usr/bin/env python3
"""github-app-mcp-wrapper.py — long-lived wrapper around the GitHub MCP server.

App-auth P2 (#1272; plan:
documentation/plans/2026-08-19-github-app-installation-token-auth.md).
Spawns the GitHub MCP server with a freshly minted App installation token and
re-mints + respawns the child every ~50 minutes (just under the 1-hour ghs_
lifetime), so token expiry never requires a bot restart. Stdio passthrough:
the child inherits Claude Code's pipes directly — steady-state latency equals
running the server bare; in-flight MCP requests fail for ~2s per respawn and
the model is expected to retry (inherent to stdio respawn; the P6 canary
re-validates post-respawn tool calls against the pinned package).

Tokens come from lib/mint-github-token.sh — the one consumer door the P1
helper names for non-git callers. The mint CLI is itself helper-direct
(D1/D10: never `git credential fill`, whose pathless context silently serves
whatever identity ambient git config answers with).

IDENTITY MODEL (#1283 review consensus): the wrapper serves one of two typed
identities and says so on every spawn —
  app           a freshly minted ~1h ghs_ installation token
  operator-pat  the GITHUB_PAT env var, an explicit BRIDGE only
Rotation only ever moves TOWARD app: the refresh path mints App-only, so a
failed App mint can never replace a serving child with the PAT (measured
pre-fix: with GITHUB_PAT set, a healthy App child was killed every cycle and
silently downgraded to operator identity — the attribution class this whole
program exists to end). A PAT bridge engages only when there is NO live child
to keep (first boot, or a dead child with an aged token), is logged loudly,
and is upgraded to App at the short retry cadence as soon as minting heals.

FAILURE CONTRACT (D12 — the RTC-less Pi boot-clock window makes first-mint
failure an every-boot class; a dead server is the outcome this file bans):
  - first mint fails, no PAT -> stay alive, retry with backoff BEFORE the
    first spawn (late GitHub MCP, never a dead one)
  - first mint fails, PAT present -> spawn the PAT bridge loudly, keep
    attempting App at the short cadence, rotate up when minting heals
  - a REFRESH mint fails -> keep serving the live child (whatever its
    identity) and retry sooner; brief 401s beat a dead server
  - the child exits NONZERO -> respawn after a pause (no hot crash-loop),
    REUSING a token younger than the refresh window (no mint-per-crash)
  - the child exits ZERO -> the MCP client closed the stream: EXIT. An
    orphaned wrapper respawn-looping against a dead client measured 23
    spawns in 4s, forever, invisible to the orphan reaper
  - spawn itself fails (npx missing, fork ENOMEM) -> same retry-with-backoff
    posture as minting, never an uncaught crash
  - every mint failure was already evented (auth_mint_failed) by the helper
On Linux the child gets PR_SET_PDEATHSIG=SIGTERM, so a hard-killed wrapper
cannot leave an orphan MCP server holding the session pipes (macOS has no
equivalent; the rc==0 exit above is the cross-platform mitigation).

Knobs (env; malformed values log and fall back rather than dying):
  GITHUB_MCP_REFRESH_SECONDS   refresh cadence (default 3000; keep under the
                               3600s ghs_ lifetime)
  GITHUB_MCP_ONE_SHOT=1        exec the server once, no refresh loop
  GITHUB_MCP_POLL_SECONDS      liveness poll cadence (default 2; floored at
                               0.01 — Popen.wait(timeout) busy-polls at 50ms
                               in CPython, ~20 wakeups/s on a Pi)
  GITHUB_MCP_RETRY_BASE_SECONDS / GITHUB_MCP_RETRY_MAX_SECONDS /
  GITHUB_MCP_RESPAWN_PAUSE_SECONDS / GITHUB_MCP_REFRESH_RETRY_SECONDS
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Same pin as library/mcp/github.json — the two fragments move together
# (pinned by tests/test_github_app_fragment.py).
MCP_PACKAGE = "@modelcontextprotocol/server-github@2025.4.8"

MINT_CLI = Path(
    os.environ.get("GITHUB_APP_MINT_CLI")
    or Path(__file__).resolve().parent / "mint-github-token.sh"
)

# Vars the third-party server process has no business inheriting: it needs
# exactly GITHUB_PERSONAL_ACCESS_TOKEN. The key path and PAT stay out of the
# npm package's /proc-visible environment.
_SCRUB_FROM_CHILD = (
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_PAT",
)


def _log(msg: str) -> None:
    print(f"github-app-mcp-wrapper: {msg}", file=sys.stderr, flush=True)


def _envf(name: str, default: float, floor: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(float(raw), floor)
    except ValueError:
        _log(f"ignoring malformed {name}={raw!r}; using default {default}")
        return default


REFRESH_SECONDS = _envf("GITHUB_MCP_REFRESH_SECONDS", 50 * 60, floor=1.0)
POLL_SECONDS = _envf("GITHUB_MCP_POLL_SECONDS", 2.0, floor=0.01)
RETRY_BASE_SECONDS = _envf("GITHUB_MCP_RETRY_BASE_SECONDS", 5.0, floor=0.01)
RETRY_MAX_SECONDS = _envf("GITHUB_MCP_RETRY_MAX_SECONDS", 60.0, floor=0.01)
RESPAWN_PAUSE_SECONDS = _envf("GITHUB_MCP_RESPAWN_PAUSE_SECONDS", 1.0, floor=0.01)
REFRESH_RETRY_SECONDS = _envf("GITHUB_MCP_REFRESH_RETRY_SECONDS", 60.0, floor=0.01)
TERMINATE_GRACE_SEC = 5
ONE_SHOT = os.environ.get("GITHUB_MCP_ONE_SHOT", "") == "1"


def _child_argv() -> list:
    return ["npx", "-y", MCP_PACKAGE] + sys.argv[1:]


def mint_app_once() -> str:
    """One App mint through the CLI door. Returns the token, or '' — NEVER
    the PAT: callers decide whether a bridge identity is permitted, because
    the refresh path must not be able to rotate a child onto the PAT.

    stdin is closed off — the helper family ships stdin-drain code, and this
    process's fd 0 is the live MCP protocol stream. stderr is inherited, so
    helper diagnostics stream; the helper has already emitted its
    auth_mint_failed event by the time this returns.
    """
    try:
        proc = subprocess.run(
            [str(MINT_CLI)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _log(f"mint invocation failed: {e}")
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def acquire_token() -> tuple:
    """(token, identity) for a spawn with NO live child to keep.

    With a PAT available: one App attempt, then the loud bridge — a session
    with a working PAT should not wait out a clock-sync window token-dead.
    Without one: D12 backoff until the App mint heals.
    """
    token = mint_app_once()
    if token:
        return token, "app"
    pat = os.environ.get("GITHUB_PAT", "")
    if pat:
        _log(
            "App mint failing — serving OPERATOR-PAT identity as a bridge; "
            "writes attribute to the PAT owner until the next successful App "
            "mint upgrades this child (see auth_mint_failed events)"
        )
        return pat, "operator-pat"
    delay = RETRY_BASE_SECONDS
    while True:
        _log(f"no token from mint CLI and no GITHUB_PAT — retrying in {delay:.0f}s")
        time.sleep(delay)
        delay = min(delay * 2, RETRY_MAX_SECONDS)
        token = mint_app_once()
        if token:
            return token, "app"


def _preexec():  # pragma: no cover — one line of platform glue
    # Linux: die with the wrapper, so a hard-killed wrapper cannot orphan an
    # MCP server that still holds the session's actual pipe fds.
    try:
        import ctypes

        ctypes.CDLL(None).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG == 1
    except Exception:
        pass


def spawn_child(token: str, identity: str) -> subprocess.Popen:
    """Spawn with the same never-give-up posture as minting (fork ENOMEM on a
    loaded Pi at a refresh boundary must not crash the wrapper)."""
    env = os.environ.copy()
    for k in _SCRUB_FROM_CHILD:
        env.pop(k, None)
    env["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
    delay = RETRY_BASE_SECONDS
    while True:
        try:
            child = subprocess.Popen(
                _child_argv(),
                env=env,
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=sys.stderr,
                preexec_fn=_preexec if sys.platform.startswith("linux") else None,
            )
            _log(f"spawned MCP server pid={child.pid} identity={identity}")
            return child
        except OSError as e:
            _log(f"spawn failed ({e}); retrying in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)


def _wait_child(child: subprocess.Popen, seconds: float):
    """Chunked liveness poll: child rc, or None when `seconds` elapse."""
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
        try:
            # Bounded: on an SD/MMC D-state stall a kill-immune child must
            # not hang the shutdown path forever.
            child.wait(timeout=TERMINATE_GRACE_SEC)
        except subprocess.TimeoutExpired:
            _log(f"child pid={child.pid} ignoring SIGKILL (D-state?); abandoning")


def main() -> int:
    if ONE_SHOT:
        token, identity = acquire_token()
        os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
        for k in _SCRUB_FROM_CHILD:
            os.environ.pop(k, None)
        _log(f"one-shot exec identity={identity}")
        try:
            os.execvp("npx", _child_argv())
        except OSError as e:
            _log(f"one-shot exec failed: {e}")
            return 1
        return 1  # unreachable

    child = None
    shutting_down = {"flag": False}

    def on_signal(signum: int, _frame) -> None:
        shutting_down["flag"] = True
        if child is not None:
            _terminate(child)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    token, identity = acquire_token()
    minted_at = time.monotonic()
    child = spawn_child(token, identity)
    if shutting_down["flag"]:
        _terminate(child)
        return 0
    # A PAT bridge retries App at the short cadence; a real App token waits
    # out the full refresh window.
    wait_for = REFRESH_RETRY_SECONDS if identity == "operator-pat" else REFRESH_SECONDS

    while True:
        rc = _wait_child(child, wait_for)
        if rc == 0:
            # The MCP client closed the stream (claude exited, stdin EOF):
            # respawning against a dead client is an unbounded npx loop the
            # orphan reaper cannot see. Exit with it.
            _log("child exited rc=0 — client closed the stream; exiting")
            return 0
        if rc is not None:
            # Child died on its own: pause (no hot crash-loop), REUSE a
            # young token (no mint-per-crash), respawn.
            _log(f"child exited rc={rc}; respawning in {RESPAWN_PAUSE_SECONDS:.0f}s")
            time.sleep(RESPAWN_PAUSE_SECONDS)
            age = time.monotonic() - minted_at
            if identity != "app" or age >= REFRESH_SECONDS:
                token, identity = acquire_token()
                minted_at = time.monotonic()
                age = 0.0
            child = spawn_child(token, identity)
            if shutting_down["flag"]:
                _terminate(child)
                return 0
            wait_for = (
                REFRESH_RETRY_SECONDS
                if identity == "operator-pat"
                else max(5.0, REFRESH_SECONDS - age)
            )
        else:
            # Refresh due. App-only mint: rotation may only move TOWARD the
            # App identity, so a failed mint (or a PAT-only fleet) keeps the
            # live child serving — never a pointless or downgrading respawn.
            fresh = mint_app_once()
            if fresh:
                if identity == "operator-pat":
                    _log("App mint healed; upgrading the PAT bridge to App identity")
                else:
                    _log("refresh interval reached; rotating token + respawning child")
                _terminate(child)
                token, identity = fresh, "app"
                minted_at = time.monotonic()
                child = spawn_child(token, identity)
                if shutting_down["flag"]:
                    _terminate(child)
                    return 0
                wait_for = REFRESH_SECONDS
            else:
                if identity == "app":
                    _log(
                        "refresh mint failed; keeping live child, retrying in "
                        f"{REFRESH_RETRY_SECONDS:.0f}s"
                    )
                wait_for = REFRESH_RETRY_SECONDS


if __name__ == "__main__":
    sys.exit(main())
