"""App-auth P2 (#1272): lib/github-app-mcp-wrapper.py behavioral battery.

Real wrapper under subprocess; the mint CLI and the npx child are stubs on a
private PATH (Lane-A). The child stub appends the token it received per
spawn, so respawn behavior is observable as a log; a sleep keeps it alive so
the refresh path runs for real. The mint stub counts calls in a file, which
doubles as the mint-rate observable for the crash-loop pin.

Pins the plan's contracts:
- D10 helper-direct: a booby-trapped `git` on PATH is never invoked (the
  wrapper consumes the mint CLI, which is helper-direct by construction).
- D12 first-mint resilience: mint-fails-then-succeeds boots the child LATE,
  never dead; a failed REFRESH keeps the live child serving.
- Refresh rotates the token (the second child sees a NEW ghs_).
- E2: a crash-looping child reuses the young token — no mint-per-crash.
- The respawn pause throttles a crash loop (observed via elapsed time).
"""

import os
import signal
import subprocess
import time
from pathlib import Path

from tests.conftest import _write_exec, booby_trap_git, constructed_env

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "lib" / "github-app-mcp-wrapper.py"


def _stub_mint(bindir: Path) -> Path:
    """Counting mint-CLI stub: prints ghs_MINT<n> bare on stdout.

    FAIL_FIRST=<k>: calls 1..k fail (rc 1, empty stdout — the D9 shape).
    FAIL_AFTER=<k>: every call AFTER the k-th fails.
    """
    mint = bindir / "mint-stub"
    _write_exec(
        mint,
        """#!/bin/bash
n=0
[ -f "$STUB_DIR/mint-count" ] && n=$(cat "$STUB_DIR/mint-count")
n=$((n + 1))
printf '%s' "$n" > "$STUB_DIR/mint-count"
if [ "$n" -le "${FAIL_FIRST:-0}" ]; then
  printf 'mint-stub: simulated failure %s\\n' "$n" >&2
  exit 1
fi
if [ "${FAIL_AFTER:-0}" -gt 0 ] && [ "$n" -gt "${FAIL_AFTER}" ]; then
  printf 'mint-stub: simulated failure %s\\n' "$n" >&2
  exit 1
fi
printf 'ghs_MINT%s' "$n"
""",
    )
    return mint


def _stub_npx(bindir: Path, behavior: str = "sleep") -> None:
    """Child stub: logs the token it received, then sleeps or exits."""
    _write_exec(
        bindir / "npx",
        f"""#!/bin/bash
printf '%s\\n' "$GITHUB_PERSONAL_ACCESS_TOKEN" >> "$STUB_DIR/spawns.log"
case "{behavior}" in
  sleep) exec sleep 300 ;;
  exit3) exit 3 ;;
esac
""",
    )


def _env(bindir: Path, mint: Path, **overrides):
    return constructed_env(
        **{
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "STUB_DIR": str(bindir),
            "GITHUB_APP_MINT_CLI": str(mint),
            "GITHUB_MCP_POLL_SECONDS": "0.05",
            "GITHUB_MCP_RETRY_BASE_SECONDS": "0.1",
            "GITHUB_MCP_RETRY_MAX_SECONDS": "0.2",
            "GITHUB_MCP_RESPAWN_PAUSE_SECONDS": "0.1",
            **overrides,
        }
    )


def _spawns(bindir: Path):
    log = bindir / "spawns.log"
    return log.read_text().splitlines() if log.exists() else []


def _mints(bindir: Path) -> int:
    f = bindir / "mint-count"
    return int(f.read_text()) if f.exists() else 0


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _start(env):
    return subprocess.Popen(
        ["python3", str(WRAPPER)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class TestOneShot:
    def test_execs_child_with_minted_token(self, tmp_path):
        mint = _stub_mint(tmp_path)
        _stub_npx(tmp_path, behavior="exit3")
        env = _env(tmp_path, mint, GITHUB_MCP_ONE_SHOT="1")
        r = subprocess.run(
            ["python3", str(WRAPPER)], env=env, capture_output=True, text=True, timeout=30
        )
        # execvp replaces the wrapper: the child's exit code IS the result.
        assert r.returncode == 3
        assert _spawns(tmp_path) == ["ghs_MINT1"]


class TestRefreshLoop:
    def test_refresh_rotates_the_token_and_respawns(self, tmp_path):
        mint = _stub_mint(tmp_path)
        _stub_npx(tmp_path)
        env = _env(tmp_path, mint, GITHUB_MCP_REFRESH_SECONDS="0.4")
        proc = _start(env)
        try:
            assert _wait_for(lambda: len(_spawns(tmp_path)) >= 2, timeout=15)
            spawns = _spawns(tmp_path)
            assert spawns[0] == "ghs_MINT1"
            assert spawns[1] != spawns[0], "respawn must carry a FRESH token"
        finally:
            _stop(proc)

    def test_first_mint_failure_boots_the_child_late_never_dead(self, tmp_path):
        mint = _stub_mint(tmp_path)
        _stub_npx(tmp_path)
        env = _env(tmp_path, mint, FAIL_FIRST="2", GITHUB_MCP_REFRESH_SECONDS="600")
        proc = _start(env)
        try:
            assert _wait_for(lambda: _spawns(tmp_path) != [], timeout=15), (
                "D12: the wrapper must stay alive and boot the child after "
                "the mint starts succeeding"
            )
            # Two failures were consumed first: the child got the THIRD mint.
            assert _spawns(tmp_path) == ["ghs_MINT3"]
            assert proc.poll() is None
        finally:
            _stop(proc)

    def test_failed_refresh_keeps_the_live_child(self, tmp_path):
        mint = _stub_mint(tmp_path)
        _stub_npx(tmp_path)
        # Mint 1 succeeds; every later mint fails — several refresh cycles
        # must all keep the ORIGINAL child serving (D12).
        env = _env(
            tmp_path,
            mint,
            FAIL_AFTER="1",
            GITHUB_MCP_REFRESH_SECONDS="0.3",
            GITHUB_MCP_REFRESH_RETRY_SECONDS="0.2",
        )
        proc = _start(env)
        try:
            assert _wait_for(lambda: _spawns(tmp_path) != [], timeout=15)
            assert _wait_for(lambda: _mints(tmp_path) >= 3, timeout=15)
            assert _spawns(tmp_path) == ["ghs_MINT1"], (
                "a failed refresh must keep the LIVE child, not kill or respawn it"
            )
            assert proc.poll() is None
        finally:
            _stop(proc)

    def test_crash_loop_is_paced_and_reuses_the_young_token(self, tmp_path):
        mint = _stub_mint(tmp_path)
        _stub_npx(tmp_path, behavior="exit3")
        env = _env(
            tmp_path,
            mint,
            GITHUB_MCP_REFRESH_SECONDS="600",
            GITHUB_MCP_RESPAWN_PAUSE_SECONDS="0.3",
        )
        t0 = time.monotonic()
        proc = _start(env)
        try:
            assert _wait_for(lambda: len(_spawns(tmp_path)) >= 3, timeout=15)
            elapsed = time.monotonic() - t0
            # The pause throttles the loop: spawns 2 and 3 each cost >= ~0.3s.
            assert elapsed >= 0.45, f"crash loop not paced (elapsed {elapsed:.2f}s)"
            # E2: the token is minutes young — respawns must REUSE it, never
            # mint per crash (~1800 redundant GitHub mints/hour otherwise).
            assert _mints(tmp_path) == 1
            assert set(_spawns(tmp_path)) == {"ghs_MINT1"}
        finally:
            _stop(proc)

    def test_pat_fallback_when_mint_cannot(self, tmp_path):
        mint = _stub_mint(tmp_path)
        _stub_npx(tmp_path)
        env = _env(
            tmp_path,
            mint,
            FAIL_FIRST="999999",
            GITHUB_PAT="ghp_FALLBACKPAT",
            GITHUB_MCP_REFRESH_SECONDS="600",
        )
        proc = _start(env)
        try:
            assert _wait_for(lambda: _spawns(tmp_path) != [], timeout=15)
            assert _spawns(tmp_path) == ["ghp_FALLBACKPAT"]
        finally:
            _stop(proc)

    def test_never_shells_to_git(self, tmp_path):
        mint = _stub_mint(tmp_path)
        _stub_npx(tmp_path)
        sentinel = booby_trap_git(tmp_path)
        env = _env(tmp_path, mint, GITHUB_MCP_REFRESH_SECONDS="600")
        proc = _start(env)
        try:
            assert _wait_for(lambda: _spawns(tmp_path) != [], timeout=15)
            assert not sentinel.exists()
        finally:
            _stop(proc)

    def test_malformed_knob_falls_back_instead_of_dying(self, tmp_path):
        # A3: knob parse errors must not produce the dead server D12 bans.
        mint = _stub_mint(tmp_path)
        _stub_npx(tmp_path)
        env = _env(tmp_path, mint, GITHUB_MCP_REFRESH_SECONDS="fifty-minutes")
        proc = _start(env)
        try:
            assert _wait_for(lambda: _spawns(tmp_path) != [], timeout=15)
            assert proc.poll() is None
        finally:
            _stop(proc)
