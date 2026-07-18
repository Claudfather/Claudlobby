"""#644 P4 real-boot gate — pytest wrapper for lib/freshbox-boot-gate.sh.

A gated job, not a per-PR blocker (Fork F4(c) / Risk R4). Gating follows the
repo's in-suite idiom (there is no workflow_dispatch/schedule precedent): the
test skips unless explicitly opted in via FRESHBOX_REALBOOT=1 AND the heavy deps
(claude binary, real auth, jq) are present. So a normal `pytest` run — including
per-PR CI, which has no provisioned credentials — skips it cleanly and visibly,
while the nightly/manual gated job runs `FRESHBOX_REALBOOT=1 pytest -k
freshbox_boot`. Mirrors tests/test_validate_harness.py: run the harness, assert
rc 0, and assert the scenario markers appear so a silent skip cannot masquerade
as a pass.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "lib" / "freshbox-boot-gate.sh"
HOST_CREDS = Path.home() / ".claude" / ".credentials.json"

_OPT_IN = os.environ.get("FRESHBOX_REALBOOT") == "1"
_missing = [
    name
    for name, present in (
        ("claude", shutil.which("claude") is not None),
        ("jq", shutil.which("jq") is not None),
        ("auth ~/.claude/.credentials.json", HOST_CREDS.is_file()),
    )
    if not present
]

if not _OPT_IN:
    _skip_reason = "gated job — set FRESHBOX_REALBOOT=1 to run the real-boot gate"
elif _missing:
    _skip_reason = f"real-boot gate needs: {', '.join(_missing)}"
else:
    _skip_reason = ""


@pytest.mark.skipif(bool(_skip_reason), reason=_skip_reason)
def test_freshbox_boot_gate():
    env = {**os.environ, "CLAUDLOBBY_SRC": str(REPO_ROOT)}
    result = subprocess.run(
        ["bash", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, f"real-boot gate failed:\n{out}"
    # Every scenario name must appear — a silent skip must not read as a pass.
    for marker in (
        "reaches a clean (non-error) result",
        "composed settings.local.json honored",
        "the bot ran a tool and returned the probe token",
        "no auth wall",
        "no onboarding/trust wizard",
        "zero permission prompts",
        "transcript tool-set ⊆ composed allow-list",
        "trust-seed teeth",
    ):
        assert marker in out, f"missing scenario marker {marker!r}:\n{out}"
