"""Connected browser carrier regressions; no real host or browser is contacted."""
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.conftest import constructed_env


def test_connected_read_client_node_regressions():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is unavailable')
    result = subprocess.run(
        [node, '--test', str(Path(__file__).with_name('plane_connected_read.test.mjs'))],
        env=constructed_env(), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
