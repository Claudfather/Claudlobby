"""#1965 run-bounded.sh -- pytest wrapper for tests/test_run_bounded.sh.

tests/test_sh_suites.py already runs the harness, as it runs every
tests/test_*.sh, and fails it on a nonzero exit. This wrapper adds the one check
an exit status cannot show: that every assertion ran. The harness exits 0
whenever nothing failed, which includes a run that never reached its cases. The
macOS half is the `timer-bound` job in .github/workflows/macos-shell.yml, which
runs the same harness under /bin/bash 3.2.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tests.conftest import constructed_env

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "tests" / "test_run_bounded.sh"

# Raise this when cases are added; never lower it to make a red wrapper green.
MIN_ASSERTIONS = 17


def test_run_bounded_contract() -> None:
    proc = subprocess.run(
        ["bash", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=REPO_ROOT,
        env=constructed_env(),
    )
    out = proc.stdout + proc.stderr
    m = re.search(r"=== (\d+)/(\d+) passed ===", out)
    assert m, f"harness printed no tally -- it did not complete:\n{out}"
    passed, total = int(m.group(1)), int(m.group(2))
    assert total >= MIN_ASSERTIONS, (
        f"only {total} assertions ran, floor is {MIN_ASSERTIONS} -- "
        f"a truncated run must not read as a pass:\n{out}"
    )
    assert passed == total, f"{total - passed} assertion(s) failed:\n{out}"
    assert proc.returncode == 0, f"harness rc={proc.returncode}:\n{out}"
