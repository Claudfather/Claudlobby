"""#644 P4 real-boot gate — pytest wrapper for harness/freshbox-boot-gate.sh.

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
import subprocess
from pathlib import Path

import pytest
from tests.conftest import REALBOOT_HOST_CREDS, realboot_skip_reason

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "harness" / "freshbox-boot-gate.sh"

# Shared real-boot dep contract (claude, jq, claudron, host auth) lives in
# conftest.realboot_skip_reason so it cannot drift between harness wrappers.
_skip_reason = realboot_skip_reason("FRESHBOX_REALBOOT")


def test_realboot_wrapper_keeps_checked_auth_path_with_private_home(monkeypatch, tmp_path):
    class Captured(Exception):
        pass

    def capture(_argv, **kwargs):
        assert kwargs["env"]["HOME"] == str(tmp_path)
        assert kwargs["env"]["CLAUDLOBBY_REALBOOT_HOST_CREDS"] == str(REALBOOT_HOST_CREDS)
        raise Captured

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", capture)
    with pytest.raises(Captured):
        test_freshbox_boot_gate()


@pytest.mark.skipif(bool(_skip_reason), reason=_skip_reason)
def test_freshbox_boot_gate():
    env = {**os.environ, "CLAUDLOBBY_SRC": str(REPO_ROOT),
           "CLAUDLOBBY_REALBOOT_HOST_CREDS": str(REALBOOT_HOST_CREDS)}
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
        # L2 additions
        "composed settings carry the three claudron session-loop hooks",
        "composed settings carry the four narrow claudron verb grants",
        "no Bash(claudron *) wildcard in composed settings",
        "vault-wired bot cannot run claudron promote",
        "SessionStart injected the recall brief",
    ):
        assert marker in out, f"missing scenario marker {marker!r}:\n{out}"
