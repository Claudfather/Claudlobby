"""#1517 fleet-signal recipient — pytest wrapper for tests/test_alert_recipient.sh.

CI runs pytest only, so a standalone bash test is not executed by CI at all.
This wrapper is what puts the recipient contract in front of the gate.

Asserts the PASS/FAIL tally rather than rc alone. The harness exits 0 whenever
nothing failed, which includes the case where it failed to run any assertions --
so rc on its own would let a silently empty run masquerade as a pass.

Needs no tmux, no systemd and no daemon: the property under test is path
resolution plus one disclosure, and the plane's cold rung is stood in for by
tests/plane_capture_cli.sh. So unlike its boot-capture sibling there is no skip
path here, and none should be added -- a skip is where a regression hides.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "tests" / "test_alert_recipient.sh"

# Raise this when cases are added; never lower it to make a red wrapper green.
MIN_ASSERTIONS = 17


def test_alert_recipient_contract() -> None:
    proc = subprocess.run(
        ["bash", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
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
