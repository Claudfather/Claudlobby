"""Tests for `harness_check` (lib-common.sh) pass/fail DISCRIMINATION — #781.

Since #780 consolidated the PASS/FAIL assertion into the shared `harness_check`
helper, it is the single point of failure behind ~112 assertion sites across the
lib/ rehearsal and gate harnesses (validate-bot-change, freshbox-boot-gate,
ab-comms-eval). Nothing in the suite exercised its OWN discrimination, so a
regression that broke it — e.g. always incrementing `pass` — would leave every
harness reporting all-green regardless of the real behavior, and the whole suite
would stay green (rajan). These pin the discrimination directly: a passing check
lands in `pass`, a FAILING check lands in `fail` and prints FAIL. The
`test_failing_check_*` case is the one an always-pass mutation reds.
"""

import os
import subprocess
from pathlib import Path

LIB_COMMON = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"


def _run_harness_check(*calls: tuple[str, str]) -> tuple[str, int, int]:
    """Source lib-common, run `harness_check <desc> <cond>` for each call against
    the ambient `pass`/`fail` counters, and return (stdout, final_pass, final_fail)."""
    lines = ['. "$1"', "pass=0", "fail=0"]
    lines += [f'harness_check "{desc}" "{cond}"' for desc, cond in calls]
    lines.append('printf "COUNTS pass=%s fail=%s\\n" "$pass" "$fail"')
    proc = subprocess.run(
        ["bash", "-c", "\n".join(lines) + "\n", "_", str(LIB_COMMON)],
        capture_output=True,
        text=True,
        env={**os.environ},
        timeout=20,
    )
    assert proc.returncode == 0, f"harness_check run failed:\n{proc.stderr}"
    counts = [ln for ln in proc.stdout.splitlines() if ln.startswith("COUNTS ")][-1]
    p = int(counts.split("pass=")[1].split()[0])
    f = int(counts.split("fail=")[1].split()[0])
    return proc.stdout, p, f


def test_passing_check_increments_pass_only():
    out, passed, failed = _run_harness_check(("ok", "yes"))
    assert (passed, failed) == (1, 0)
    assert "  PASS  ok" in out
    assert "  FAIL  ok" not in out


def test_failing_check_increments_fail_only():
    # The discrimination #781 pins: a "no" MUST land in `fail`, not `pass`, and
    # print FAIL. An always-pass mutation of harness_check reds exactly this —
    # which is what nothing in the suite caught before (the SPOF blind spot).
    out, passed, failed = _run_harness_check(("bad", "no"))
    assert (passed, failed) == (0, 1)
    assert "  FAIL  bad" in out
    assert "  PASS  bad" not in out


def test_mixed_checks_discriminate():
    # A realistic mix must tally correctly, not collapse to all-pass/all-fail.
    out, passed, failed = _run_harness_check(("a", "yes"), ("b", "no"), ("c", "yes"))
    assert (passed, failed) == (2, 1)
    assert "  PASS  a" in out and "  FAIL  b" in out and "  PASS  c" in out
