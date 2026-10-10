"""Core connected entry tests, with no real host/browser/account access."""
from pathlib import Path
import shutil
import subprocess
import pytest
from tests.conftest import constructed_env


def test_connected_workspace_node_contract():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is unavailable')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('plane_connected_workspace.test.mjs'))],
        env=constructed_env(), capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
