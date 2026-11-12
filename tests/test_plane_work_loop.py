"""Exercise the real work-loop controller with a minimal DOM and no fleet access."""
from pathlib import Path
import shutil
import subprocess

import pytest
from tests.conftest import constructed_env


def test_work_loop_controller():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the browser work-loop controller")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("plane_work_loop.test.mjs"))],
        capture_output=True, text=True, timeout=30, env=constructed_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
