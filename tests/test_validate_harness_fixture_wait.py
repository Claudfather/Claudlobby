"""The #1530 scenario in harness/validate-bot-change.sh boots a bot only once
its foreign-session poller holds the slot, and says why when it never does
(#2716).

The scenario starts a production-shaped poller tree (claude -> bun ... start ->
bun ... server.ts), whose last process writes bot.pid, then boots the bot and
checks that another session's live poller is not read as ready. Its
precondition was a fixed 100 polls of 0.05 s, and the boot ran whatever that
wait found. On CI (run 42101336828, attempt 1) the poller's pid came after the
boot's own subreaper's, so the boot met an empty slot: three checks failed and
the flip check passed with no poller to flip.

These tests run the scenario's setup lifted verbatim from the harness, from the
line after its val_scenario through its precondition check, with `node`
replaced by a stand-in whose tree holds the slot late, exits first, or never
holds it.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "harness" / "validate-bot-change.sh"
LIB_COMMON = REPO / "claudlobby/_runtime_scripts" / "lib-common.sh"
SCENARIO = 'val_scenario "validate-bot-change: session-scoped readiness (#1530)"'
PRECONDITION = "foreign-session poller holds the slot before the boot (fixture precondition)"

# What the stand-in does as the tree's root (`claude tree.js`). Eight seconds is
# past the old fixed wait (5.2 to 5.3 s measured on a Raspberry Pi 5 at load 6
# to 7) and well inside the 30 s bound.
LATE = 'sleep 8; printf %s "$$" > "$TELEGRAM_STATE_DIR/bot.pid"; exec sleep 300'
EXITS = "exit 3"


def _setup_block() -> str:
    """The scenario's lines after its val_scenario, through its precondition check."""
    lines = HARNESS.read_text().split("\n")
    start = lines.index(SCENARIO) + 1
    end = next(i for i in range(start, len(lines)) if PRECONDITION in lines[i] and "harness_check" in lines[i])
    return "\n".join(lines[start : end + 1])


def _wait_function() -> str:
    lines = HARNESS.read_text().split("\n")
    start = lines.index("_sc_await_slot() {")
    return "\n".join(lines[start : lines.index("}", start) + 1])


def _node(bindir: Path, root_does: str) -> None:
    """A stand-in for node: as the tree's root it does <root_does>; as anything else it exits."""
    node = bindir / "node"
    node.write_text(f'#!/bin/bash\ncase "${{0##*/}}" in\n    claude) {root_does} ;;\n    *) exit 0 ;;\nesac\n')
    node.chmod(0o755)


def _bash(script: str, *args: str, env: dict | None = None) -> str:
    """Run <script>; return its output. The tree it starts runs in a session of its
    own, so the run's EXIT trap kills it, and so does this, should the run be
    killed before its trap could."""
    out = ""
    try:
        result = subprocess.run(
            ["bash", "-c", script, "_", *args], capture_output=True, text=True, env=env, timeout=120
        )
        out = result.stdout + result.stderr
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        raise
    finally:
        for root in re.findall(r"^ROOT (\d+)$", out, re.M):
            try:
                os.killpg(int(root), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return out


def _run_setup(tmp_path: Path, root_does: str, after: str = "") -> tuple[str, float]:
    """Run the lifted setup with the stand-in tree; return its output and seconds taken."""
    bindir, rb_root = tmp_path / "bin", tmp_path / "rb"
    bindir.mkdir()
    (rb_root / "bot" / "state").mkdir(parents=True)
    _node(bindir, root_does)
    script = "\n".join(
        [
            '. "$1"',
            "set -euo pipefail",
            'RB_ROOT="$2"; RB_DIR="$2/bot"; VAL_PY="$3"; VAL_REPO="$4"',
            'HARNESS_REFUSALS="$RB_ROOT/.refusals"; : > "$HARNESS_REFUSALS"',
            "pass=0; fail=0; refused=0",
            # The tree runs in a session of its own; never signal a group with an empty id.
            "trap '[ -z \"${_SC_ROOT_PID:-}\" ] || kill -9 -- \"-$_SC_ROOT_PID\" 2>/dev/null || true' EXIT",
            _setup_block(),
            'printf "ROOT %s\\n" "$_SC_ROOT_PID"',
            after,
            'printf "COUNTS pass=%s fail=%s refused=%s\\n" "$pass" "$fail" "$refused"',
        ]
    )
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    began = time.monotonic()
    out = _bash(script, str(LIB_COMMON), str(rb_root), sys.executable, str(REPO), env=env)
    return out, time.monotonic() - began


def _counts(out: str) -> tuple[int, int, int]:
    line = [ln for ln in out.splitlines() if ln.startswith("COUNTS ")][-1]
    return tuple(int(part.split("=")[1]) for part in line.split()[1:])


def test_the_scenario_waits_for_a_poller_that_holds_the_slot_late(tmp_path):
    out, _ = _run_setup(tmp_path, LATE)
    assert f"  PASS  {PRECONDITION}\n" in out, out
    assert _counts(out) == (1, 0, 0), out


def test_a_tree_that_exits_first_refuses_the_scenario_at_once_naming_why(tmp_path):
    later = 'harness_check "  ...a later check in the scenario" yes'
    out, took = _run_setup(tmp_path, EXITS, after=later)
    # The precondition and every check after it in the scenario fail, naming the
    # tree's exit, so none of them is scored against an empty slot.
    assert re.search(rf"  FAIL  {re.escape(PRECONDITION)} — REFUSED, .*: its tree \(pid \d+\) exited after", out), out
    assert "  FAIL    ...a later check in the scenario — REFUSED, " in out, out
    assert _counts(out) == (0, 2, 2), out
    # At once, not at the 30 s bound.
    assert took < 15, took


def test_a_tree_that_never_holds_the_slot_is_refused_at_the_bound_naming_its_processes(tmp_path):
    pidfile = tmp_path / "bot.pid"
    script = "\n".join(
        [
            "set -euo pipefail",
            _wait_function(),
            # A pid file left by a process that has exited is not a held slot.
            'true & stale=$!; wait "$stale"; printf %s "$stale" > "$1"',
            '"$2" -I -B -c "import os, sys; os.setsid(); os.execv(sys.argv[1], sys.argv[1:])" /bin/sleep 300 &',
            "root=$!",
            "trap 'kill -9 -- \"-$root\" 2>/dev/null || true' EXIT",
            'printf "ROOT %s\\n" "$root"',
            'rc=0; out="$(_sc_await_slot "$root" "$1" 2)" || rc=$?',
            'printf "RC %s\\nOUT %s\\n" "$rc" "$out"',
        ]
    )
    out = _bash(script, str(pidfile), sys.executable)
    root = re.search(r"^ROOT (\d+)$", out, re.M).group(1)
    assert "RC 1\n" in out, out
    assert f"OUT no live process held {pidfile} within 2 s; its tree still runs: {root} sleep 300\n" in out, out
