"""Run every hermetic bash suite in tests/ under the pytest lane so CI enforces them.

The ``tests/*.sh`` suites are standalone bash (not pytest-collected on their own),
so without this wrapper an edit to a helper they cover breaks them with zero CI
signal — a green badge that does not actually cover the shell suites (#723; the
earlier #696 finding-3 that ``10/10 hermetic`` was never gated). This wrapper
discovers *every* ``tests/test_*.sh`` by glob and runs each via subprocess,
failing if it exits non-zero.

Glob, not a hand-maintained list: a new suite is gated the moment it lands, with
no allowlist to forget — that drift is exactly what let ``test_tg_post.sh`` ship
ungated. Discovery is guarded below so it can never silently collapse to zero.

Contract: every ``tests/*.sh`` suite must be hermetic — it stubs tmux, network
(curl/gh), sudo, and any real service onto a private PATH so it runs clean on the
Linux CI runner. A suite that reaches a real service is a bug in the suite; fix
the suite, do not drop it from the glob.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

# Every bash suite in tests/, discovered by glob so the collected set never drifts
# from what is on disk. Sorted for a stable parametrize order and test-id sequence.
SH_SUITES = sorted(p.name for p in TESTS_DIR.glob("test_*.sh"))


def test_sh_suites_discovered():
    """Discovery must never silently collapse to zero suites — an empty glob would
    reproduce the #723 gap (shell coverage present in the tree, absent from CI)."""
    assert SH_SUITES, f"no tests/test_*.sh suites discovered under {TESTS_DIR}"


@pytest.mark.parametrize("suite", SH_SUITES)
def test_hermetic_bash_suite(suite):
    path = TESTS_DIR / suite
    assert path.is_file(), f"missing bash suite: {suite}"
    bash = shutil.which("bash") or "/bin/bash"
    proc = subprocess.run([bash, str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"{suite} failed (exit {proc.returncode}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
