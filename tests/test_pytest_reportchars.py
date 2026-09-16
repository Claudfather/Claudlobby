"""The suite's own summary block must NAME failures, not just count them.

`-r` is STORE, not append. So `addopts = "-rs"` did not *add* skip reasons to
pytest's defaults — it REPLACED the built-in `fE`, and every failure name
vanished from the "short test summary info" block (#1509). The count line still
read "N failed", so a reader of a red CI log knew how many tests failed and not
which: enough signal to know something is wrong, none to act on it.

It survived because the two consumers fail in opposite directions. CLAUDE.md's
regression-diff recipe passes its own `-ra`, which overrides addopts, so the
deliberate local procedure was never affected and could not surface this. CI
passes no `-r` at all (`.github/workflows/test.yml` runs `pytest -v`), so the
ad-hoc read — the one nobody rehearses — is exactly the blind one.

These tests pin BOTH halves, because the obvious repair in either direction
breaks the other: dropping `s` re-buries the skip reasons `-rs` was added for
(#1012), and dropping `f`/`E` re-buries the failure names. They invoke pytest
WITHOUT any `-r` flag on purpose — passing one would test the flag rather than
the configuration, which is precisely the substitution that hid the defect.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "pyproject.toml"

PROBE = '''
import pytest


def test_probe_fails_on_purpose():
    assert 1 == 2


@pytest.mark.skip(reason="probe-skip-reason-marker")
def test_probe_skips_on_purpose():
    pass
'''


def _summary_block(tmp_path):
    """Run a known-failing, known-skipping probe under the REPO's own config.

    Returns the "short test summary info" block only. Everything else -- captured
    logs, tracebacks, progress -- prints ABOVE that banner, so scoping to it is
    what makes the assertion about pytest's verdict rather than about noise.
    """
    probe = tmp_path / "test_reportchars_probe.py"
    probe.write_text(PROBE)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(CONFIG),
            "-p",
            "no:cacheprovider",
            str(probe),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    # rc 1 == tests failed, which is the expected outcome for the probe. Anything
    # else means the probe did not run, and an empty summary would then be an
    # artifact of the harness rather than a fact about reportchars.
    assert proc.returncode == 1, (
        f"probe did not run as expected (rc={proc.returncode}); "
        f"summary assertions below would be meaningless.\n{proc.stdout[-2000:]}"
    )
    marker = "short test summary info"
    idx = proc.stdout.find(marker)
    assert idx != -1, f"no summary block emitted at all:\n{proc.stdout[-2000:]}"
    return proc.stdout[idx:]


def test_a_failing_test_is_named_in_the_short_summary(tmp_path):
    block = _summary_block(tmp_path)
    named = [ln for ln in block.splitlines() if ln.startswith("FAILED")]
    assert named, (
        "the summary block named no failures. `-r` is store-not-append, so an "
        "addopts lacking `f` replaces pytest's default `fE` and blanks every "
        "failure name -- a red run then reads as a wall of SKIPPED (#1509). "
        f"Block was:\n{block}"
    )
    assert any("test_probe_fails_on_purpose" in ln for ln in named), (
        f"summary named a failure, but not the probe's:\n{block}"
    )


def test_skip_reasons_still_print(tmp_path):
    """The #1012 property the original `-rs` was added for, kept as a pin.

    Without this, the obvious repair for #1509 -- swapping `-rs` for `-rfE` --
    would pass the failure-name test above while silently re-burying every skip
    reason, which is the defect #1012 fixed.
    """
    block = _summary_block(tmp_path)
    skipped = [ln for ln in block.splitlines() if ln.startswith("SKIPPED")]
    assert skipped, f"the summary block printed no skip reasons (#1012):\n{block}"
    assert any("probe-skip-reason-marker" in ln for ln in skipped), (
        f"skip line present but without its reason:\n{block}"
    )
