"""#917 with_timeout with no timeout(1) and no gtimeout -- pytest wrapper for
tests/test_with_timeout.sh.

tests/test_sh_suites.py already runs the harness, as it runs every
tests/test_*.sh, and fails it on a nonzero exit. This wrapper checks the two
things an exit status cannot show. The macOS half is the `with-timeout` job in
.github/workflows/macos-shell.yml, which runs the same harness under /bin/bash
3.2. The harness masks timeout and gtimeout from PATH itself, so both platforms
mask them the same way.

First, the oracle arm ran. A Linux host has timeout(1), so the harness runs every
case through the real timeout(1) as well as through the fallback. That arm is
what proves the expected values are timeout(1)'s own; a run without it checks the
fallback against nothing but the harness's say-so, and still exits 0.

Second, every assertion ran. The harness exits 0 whenever nothing failed, which
includes a run that never reached its cases, so the tally is checked against a
floor.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from tests.conftest import constructed_env

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "tests" / "test_with_timeout.sh"

# Both arms. Raise this when cases are added; never lower it to make a red
# wrapper green.
MIN_ASSERTIONS = 39


def test_with_timeout_fallback_matches_timeout1() -> None:
    assert shutil.which("timeout") or shutil.which("gtimeout"), (
        "no timeout(1) on this Linux host: the oracle arm cannot run"
    )
    proc = subprocess.run(
        ["bash", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=REPO_ROOT,
        env=constructed_env(),
    )
    out = proc.stdout + proc.stderr
    assert "arms: oracle fallback" in out, f"the oracle arm did not run:\n{out}"
    m = re.search(r"=== (\d+)/(\d+) passed ===", out)
    assert m, f"harness printed no tally -- it did not complete:\n{out}"
    passed, total = int(m.group(1)), int(m.group(2))
    assert total >= MIN_ASSERTIONS, (
        f"only {total} assertions ran, floor is {MIN_ASSERTIONS} -- "
        f"a truncated run must not read as a pass:\n{out}"
    )
    assert passed == total, f"{total - passed} assertion(s) failed:\n{out}"
    assert proc.returncode == 0, f"harness rc={proc.returncode}:\n{out}"
