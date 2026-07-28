"""Pin the bash 3.2 premise that makes arming errtrace safe (#844).

``install_error_trap`` sets ``set -E`` so the ERR trap is inherited by shell
functions. That is only safe because bash suppresses the ERR trap in the same
contexts it suppresses errexit — ``f || true``, ``if f; then`` — and that
suppression is inherited by callees. If it did NOT hold, arming errtrace would
emit a ``script_error`` row for every one of ``lib/``'s ~700 deliberately
guarded call sites.

That premise was measured on bash 5.2, but ``/bin/bash`` on macOS is 3.2 and is
this repo's stated shebang target, so the fleet's real exposure is on a bash
nobody here runs day to day. Docker gives a real 3.2 cheaply, so the premise is
pinned by a test rather than carried as an assumption with a date on it — the
failure mode being that someone revisits this in a year and finds only a claim.

Not in the ``tests/*.sh`` lane: those suites are contractually hermetic (no
network), and pulling an image is not.

**Deliberately NOT opt-in gated**, which is a considered divergence from
``test_freshbox_boot_harness.py`` — do not "fix" it to match. That harness gates
behind ``FRESHBOX_REALBOOT=1`` because it needs provisioned credentials CI does
not have, so it *cannot* run there. This needs only docker, which the CI runner
has, so an env gate CI never sets would turn the pin into a test that never runs
— precisely the failure mode it exists to prevent. It runs by default and costs
one small image pull.

The probe is a module-scoped fixture rather than import-time work, so it runs
only when these tests are actually selected: collection, and any ``-k`` run that
does not name them, pays nothing. Absent or unusable docker skips visibly; a
docker that runs and reports different semantics FAILS, because that is the
premise breaking rather than the tooling being missing.
"""

import shutil
import subprocess

import pytest

IMAGE = "bash:3.2"

# Three sub-shells inside one container: a bare in-function failure (the trap
# MUST fire — this is what #844 fixed), then the two suppressed shapes (the trap
# must NOT fire). Each prints "T" per firing; stderr is dropped so only trap
# output is compared. Version is echoed so a moved tag cannot silently pin 5.x.
PROBE = r"""
r() { bash -c "set -Eeuo pipefail; trap 'printf T' ERR; f() { false; }; $1; :" 2>/dev/null; }
printf 'bare=[%s] or=[%s] if=[%s] ver=[%s]' \
    "$(r 'f')" "$(r 'f || true')" "$(r 'if f; then :; fi')" \
    "$(bash --version | head -1 | sed 's/.*version \([0-9][0-9.]*\).*/\1/')"
"""


@pytest.fixture(scope="module")
def probe_output():
    """Run the probe once per module, or skip if docker itself is unusable."""
    if not shutil.which("docker"):
        pytest.skip("docker not installed")
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm", IMAGE, "bash", "-c", PROBE],
            capture_output=True,
            text=True,
            timeout=300,  # generous: covers a cold image pull on a slow link
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"could not run {IMAGE}: {exc}")
    # A non-zero rc here means docker could not run the image at all (no daemon,
    # no network for the pull, no matching arch) — infrastructure, not premise.
    if proc.returncode != 0:
        pytest.skip(f"docker could not run {IMAGE}: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def test_bash32_is_actually_bash_32(probe_output):
    """Guard the guard: if the tag ever resolves to a different bash, the rest of
    this module would be pinning the premise on the wrong interpreter."""
    assert "ver=[3.2" in probe_output, f"expected bash 3.2, got: {probe_output}"


def test_errtrace_fires_inside_functions_on_bash32(probe_output):
    """The #844 fix itself: with errtrace, an in-function failure reaches the trap."""
    assert "bare=[T]" in probe_output, (
        f"ERR trap did not fire in a function: {probe_output}"
    )


def test_suppressed_contexts_stay_silent_on_bash32(probe_output):
    """The premise the blast-radius analysis rests on. If this fails, arming
    errtrace floods every guarded call site in lib/ and the fix must be revisited
    — do not relax this assertion to make it pass."""
    assert "or=[]" in probe_output, (
        f"'f || true' fired the ERR trap on 3.2: {probe_output}"
    )
    assert "if=[]" in probe_output, (
        f"'if f; then' fired the ERR trap on 3.2: {probe_output}"
    )
