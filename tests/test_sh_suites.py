"""Run the hermetic bash test suites under the pytest lane so CI enforces them.

The ``.sh`` tests are standalone (not pytest-collected on their own), so a future
edit to a helper they cover could break them with zero CI signal — the #696
review's finding 3 (``10/10 hermetic`` wasn't actually gated). This wrapper runs
each listed suite via subprocess and fails if it exits non-zero.

Only genuinely hermetic suites belong here — no tmux, network, sudo, or real
services — so they run clean on the Linux CI runner. Add more as each is
verified hermetic; the tmux/service-dependent suites stay out.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

HERMETIC_SH_SUITES = [
    "test_rolling_restart.sh",
]


@pytest.mark.parametrize("suite", HERMETIC_SH_SUITES)
def test_hermetic_bash_suite(suite):
    path = TESTS_DIR / suite
    assert path.is_file(), f"missing bash suite: {suite}"
    bash = shutil.which("bash") or "/bin/bash"
    proc = subprocess.run(
        [bash, str(path)], capture_output=True, text=True
    )
    assert proc.returncode == 0, (
        f"{suite} failed (exit {proc.returncode}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
