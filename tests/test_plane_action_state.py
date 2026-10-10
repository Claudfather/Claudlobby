"""Exercise browser-independent request/receipt rules with the platform JS runtime."""
from pathlib import Path
import shutil
import subprocess

import pytest
from tests.conftest import constructed_env


def test_action_state_contract():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the browser action-state contract")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("plane_action_state.test.mjs"))],
        capture_output=True, text=True, timeout=30, env=constructed_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
