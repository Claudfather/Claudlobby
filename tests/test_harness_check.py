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


# --- #1777: a check fed by a read that could not run is REFUSED, not scored --


def _run_script(body: str, **env: str) -> subprocess.CompletedProcess:
    """Source lib-common, zero the counters, run <body>, print the counters."""
    script = (
        '. "$1"\npass=0; fail=0; refused=0\n' + body
        + '\nprintf "COUNTS pass=%s fail=%s refused=%s\\n" "$pass" "$fail" "$refused"\n'
    )
    return subprocess.run(
        ["bash", "-c", script, "_", str(LIB_COMMON)],
        capture_output=True, text=True, env={**os.environ, **env}, timeout=20,
    )


def _counts(stdout: str) -> tuple[int, int, int]:
    line = [ln for ln in stdout.splitlines() if ln.startswith("COUNTS ")][-1]
    return tuple(int(part.split("=")[1]) for part in line.split()[1:])


def _run_armed(tmp_path, body: str) -> tuple[subprocess.CompletedProcess, Path]:
    """_run_script with a refusal ledger armed; the script must not abort."""
    ledger = tmp_path / "ledger"
    ledger.write_text("")
    r = _run_script(body, HARNESS_REFUSALS=str(ledger))
    assert r.returncode == 0, r.stderr
    return r, ledger


def test_a_refusal_fails_every_later_check_in_its_scenario(tmp_path):
    # The fail-open direction #1777 found: a check expecting ABSENCE reads an
    # unreadable source as empty and says "yes". Refused, it must FAIL, with
    # the reason on its own line, and the FAIL prefix every counter greps.
    # EVERY later check, not the next one: one read often feeds two absence
    # checks. The second check here may not even read it; it is refused too.
    r, ledger = _run_armed(
        tmp_path,
        'harness_refuse "no plane db at /x/plane.db"\n'
        'harness_check "nothing fired" "yes"\n'
        'harness_check "and nothing else fired either" "yes"',
    )
    assert _counts(r.stdout) == (0, 2, 2)
    for desc in ("nothing fired", "and nothing else fired either"):
        assert (f"  FAIL  {desc} — REFUSED, a read before it in this scenario "
                "could not run: no plane db at /x/plane.db") in r.stdout
        assert f"  PASS  {desc}" not in r.stdout
    assert ledger.read_text() == ""   # reported, so no longer pending


def test_a_scenario_boundary_ends_a_refusal_a_check_reported(tmp_path):
    r, _ = _run_armed(
        tmp_path,
        'harness_refuse "cannot read A"\n'
        'harness_check "scenario A" "yes"\n'
        "harness_scenario\n"
        'harness_check "scenario B scores again" "yes"',
    )
    assert _counts(r.stdout) == (1, 1, 1)
    assert "  PASS  scenario B scores again" in r.stdout


def test_a_refusal_no_check_reported_carries_into_the_next_scenario(tmp_path):
    # A read in a scenario's setup can run before its header prints. Dropping
    # it at the boundary would let that scenario's own checks score it, and a
    # check expecting absence would pass.
    r, _ = _run_armed(
        tmp_path,
        'harness_check "scenario A" "yes"\n'
        'harness_refuse "setup read of B"\n'
        "harness_scenario\n"
        'harness_check "scenario B: nothing fired" "yes"',
    )
    assert _counts(r.stdout) == (1, 1, 1)
    assert ("  FAIL  scenario B: nothing fired — REFUSED, a read before it in "
            "this scenario could not run: setup read of B") in r.stdout


def test_a_boundary_keeps_only_what_no_check_reported(tmp_path):
    # Reported and unreported refusals in one scenario: the boundary drops the
    # first and carries the second, and the next scenario names only that one.
    r, _ = _run_armed(
        tmp_path,
        'harness_refuse "read one"\n'
        'harness_check "A" "yes"\n'
        'harness_refuse "read two"\n'
        "harness_scenario\n"
        'harness_check "B" "yes"',
    )
    assert _counts(r.stdout) == (0, 2, 2)
    b_line = [ln for ln in r.stdout.splitlines() if ln.startswith("  FAIL  B ")][0]
    assert b_line.endswith("could not run: read two"), b_line


def test_finish_fails_the_run_on_a_refusal_no_check_reported(tmp_path):
    r, _ = _run_armed(
        tmp_path,
        'harness_check "last check" "yes"\n'
        'harness_refuse "a read after it"\n'
        "harness_finish",
    )
    assert _counts(r.stdout) == (1, 1, 1)
    assert ("  FAIL  a read after the last check could not run — REFUSED: "
            "a read after it") in r.stdout


def test_finish_does_not_count_a_refusal_twice(tmp_path):
    r, _ = _run_armed(
        tmp_path, 'harness_refuse "read"\nharness_check "refused" "yes"\nharness_finish'
    )
    assert _counts(r.stdout) == (0, 1, 1)


def test_unarmed_boundary_and_finish_change_nothing():
    r = _run_script(
        'harness_check "a" "yes"\nharness_scenario\nharness_check "b" "no"\nharness_finish',
        HARNESS_REFUSALS="",
    )
    assert r.returncode == 0, r.stderr
    assert _counts(r.stdout) == (1, 1, 0)
    assert "REFUSED" not in r.stdout


def test_refusal_crosses_a_command_substitution(tmp_path):
    # Readers run inside $( ), where the counters cannot be reached: the ledger
    # is a file for exactly this. set -e is armed, as in the harnesses, and
    # the refusal must not abort the caller.
    r, _ = _run_armed(
        tmp_path,
        'set -euo pipefail\n'
        'n=$(harness_refuse "first"; harness_refuse "second"; printf "")\n'
        '[ "${n:-1}" -eq 0 ] && v=yes || v=no\n'
        'harness_check "count is zero" "$v"',
    )
    assert _counts(r.stdout) == (0, 1, 1)
    assert "could not run: first; second" in r.stdout


def test_a_multi_line_reason_stays_one_ledger_entry(tmp_path):
    _, ledger = _run_armed(tmp_path, 'harness_refuse "line one\nline two\n"')
    assert ledger.read_text() == "line one line two\n"


def test_unarmed_the_reason_goes_to_stderr_and_scoring_is_unchanged():
    # A harness that never arms a ledger keeps today's behaviour exactly.
    r = _run_script('harness_refuse "cannot read"\nharness_check "ok" "yes"',
                    HARNESS_REFUSALS="")
    assert r.returncode == 0, r.stderr
    assert _counts(r.stdout) == (1, 0, 0)
    assert "  PASS  ok" in r.stdout
    assert "harness: a read could not run: cannot read" in r.stderr
