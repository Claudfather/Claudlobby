"""App-auth P2 (#1272): lib/github-app-mcp-wrapper.py behavioral battery.

Real wrapper under subprocess; the helper and the npx child are stubs on a
private PATH (Lane-A). The child stub appends one line per spawn — the token
it received — so respawn behavior is observable as a log, and a sleep keeps
it alive so wait(timeout=...) exercises the refresh path for real.

Pins the plan's contracts:
- D10 helper-direct: a booby-trapped `git` on PATH is never invoked.
- D12 first-mint resilience: mint-fails-then-succeeds boots the child LATE,
  never dead; a failed REFRESH keeps the live child serving.
- Respawn rotates the token (the second child sees a NEW ghs_).
- GITHUB_PAT fallback when the helper cannot mint.
"""

import os
import signal
import subprocess
import time
from pathlib import Path

from tests.conftest import _write_exec, constructed_env

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "lib" / "github-app-mcp-wrapper.py"


def _stub_helper(bindir: Path) -> Path:
    """Counting helper stub: token ghs_MINT<n>; fails while <n> <= FAIL_FIRST."""
    helper = bindir / "helper-stub"
    _write_exec(
        helper,
        """#!/bin/bash
n=0
[ -f "$STUB_DIR/mint-count" ] && n=$(cat "$STUB_DIR/mint-count")
n=$((n + 1))
printf '%s' "$n" > "$STUB_DIR/mint-count"
if [ "$n" -le "${FAIL_FIRST:-0}" ]; then
  printf 'helper-stub: simulated mint failure %s\\n' "$n" >&2
  printf 'quit=1\\n'
  exit 1
fi
printf 'username=x-access-token\\npassword=ghs_MINT%s\\n' "$n"
""",
    )
    return helper


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


def _env(bindir: Path, helper: Path, **overrides):
    env = constructed_env(
        PATH=f"{bindir}:{os.environ['PATH']}",
        STUB_DIR=str(bindir),
        GITHUB_APP_HELPER=str(helper),
        GITHUB_MCP_RETRY_BASE_SECONDS="0.1",
        GITHUB_MCP_RETRY_MAX_SECONDS="0.2",
        GITHUB_MCP_RESPAWN_PAUSE_SECONDS="0.1",
    )
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def _spawns(bindir: Path):
    log = bindir / "spawns.log"
    return log.read_text().splitlines() if log.exists() else []


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
        helper = _stub_helper(tmp_path)
        _stub_npx(tmp_path, behavior="exit3")
        env = _env(tmp_path, helper, GITHUB_MCP_REFRESH="false")
        r = subprocess.run(
            ["python3", str(WRAPPER)], env=env, capture_output=True, text=True, timeout=30
        )
        # execvp replaces the wrapper: the child's exit code IS the result.
        assert r.returncode == 3
        assert _spawns(tmp_path) == ["ghs_MINT1"]


class TestRefreshLoop:
    def test_refresh_rotates_the_token_and_respawns(self, tmp_path):
        helper = _stub_helper(tmp_path)
        _stub_npx(tmp_path)
        env = _env(tmp_path, helper, GITHUB_MCP_REFRESH_SECONDS="1")
        proc = _start(env)
        try:
            assert _wait_for(lambda: len(_spawns(tmp_path)) >= 2, timeout=15)
            spawns = _spawns(tmp_path)
            assert spawns[0] == "ghs_MINT1"
            assert spawns[1] != spawns[0], "respawn must carry a FRESH token"
        finally:
            _stop(proc)

    def test_first_mint_failure_boots_the_child_late_never_dead(self, tmp_path):
        helper = _stub_helper(tmp_path)
        _stub_npx(tmp_path)
        env = _env(
            tmp_path, helper, FAIL_FIRST="2", GITHUB_MCP_REFRESH_SECONDS="600"
        )
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
        helper = _stub_helper(tmp_path)
        _stub_npx(tmp_path)
        # First mint succeeds; every later mint fails (FAIL_FIRST is huge but
        # the counter is pre-seeded so mint 1 passes).
        (tmp_path / "mint-count").write_text("0")
        env = _env(
            tmp_path,
            helper,
            GITHUB_MCP_REFRESH_SECONDS="1",
            GITHUB_MCP_REFRESH_RETRY_SECONDS="1",
            FAIL_AFTER_FIRST="1",
        )
        # Repurpose the stub: fail every call except the first.
        _write_exec(
            tmp_path / "helper-stub",
            """#!/bin/bash
n=0
[ -f "$STUB_DIR/mint-count" ] && n=$(cat "$STUB_DIR/mint-count")
n=$((n + 1))
printf '%s' "$n" > "$STUB_DIR/mint-count"
if [ "$n" -gt 1 ]; then
  printf 'helper-stub: refresh mint down\\n' >&2
  printf 'quit=1\\n'
  exit 1
fi
printf 'username=x-access-token\\npassword=ghs_MINT%s\\n' "$n"
""",
        )
        proc = _start(env)
        try:
            assert _wait_for(lambda: _spawns(tmp_path) != [], timeout=15)
            # Give it several refresh cycles whose mints all fail.
            assert _wait_for(
                lambda: int((tmp_path / "mint-count").read_text()) >= 3, timeout=15
            )
            assert _spawns(tmp_path) == ["ghs_MINT1"], (
                "a failed refresh must keep the LIVE child, not kill or respawn it"
            )
            assert proc.poll() is None
        finally:
            _stop(proc)

    def test_dead_child_respawns_with_a_pause(self, tmp_path):
        helper = _stub_helper(tmp_path)
        _stub_npx(tmp_path, behavior="exit3")
        env = _env(tmp_path, helper, GITHUB_MCP_REFRESH_SECONDS="600")
        proc = _start(env)
        try:
            assert _wait_for(lambda: len(_spawns(tmp_path)) >= 2, timeout=15)
        finally:
            _stop(proc)

    def test_pat_fallback_when_helper_cannot_mint(self, tmp_path):
        helper = _stub_helper(tmp_path)
        _stub_npx(tmp_path)
        env = _env(
            tmp_path,
            helper,
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
        # D10 pin: the wrapper mints helper-direct — a booby-trapped `git` on
        # PATH proves git is never consulted.
        helper = _stub_helper(tmp_path)
        _stub_npx(tmp_path)
        _write_exec(
            tmp_path / "git",
            '#!/bin/bash\ntouch "$STUB_DIR/git-was-called"\nexit 1\n',
        )
        env = _env(tmp_path, helper, GITHUB_MCP_REFRESH_SECONDS="600")
        proc = _start(env)
        try:
            assert _wait_for(lambda: _spawns(tmp_path) != [], timeout=15)
            assert not (tmp_path / "git-was-called").exists()
        finally:
            _stop(proc)
