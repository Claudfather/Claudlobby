"""Core-only injected two-reader contract; no real carrier or fleet is contacted."""
from pathlib import Path
import shutil
import subprocess
import pytest
from tests.conftest import constructed_env


def test_two_source_node_contract():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is unavailable')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('plane_two_source.test.mjs'))],
        env=constructed_env(), capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
