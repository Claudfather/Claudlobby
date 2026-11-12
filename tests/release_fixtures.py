"""Fixtures for tests of historical releases and their plane databases.

``releases`` asks for the ``installed`` fixture, so a test file that uses it
imports ``installed`` from ``tests.test_releases`` as well.
"""

from dataclasses import replace
import json
import shutil
import sqlite3

import pytest

from claudlobby.plane.db import db_file
from claudlobby.plane.migrations import SCHEMA_USER_VERSION, _migration_files
from tests.test_releases import historical_release, r


@pytest.fixture
def releases(installed):
    root, inputs, paths, _, directory = installed
    target_inputs = replace(inputs, source_revision="c" * 40, artifact_id="candidate")
    target_dir = r.release_path(root, target_inputs.release_id)
    shutil.copytree(directory, target_dir)
    for dest, assembly, version in ((directory, inputs, 1), (target_dir, target_inputs, SCHEMA_USER_VERSION)):
        metadata = json.loads((dest / paths.artifact).read_text())
        metadata.update(source_revision=assembly.source_revision, artifact_id=assembly.artifact_id)
        metadata["compatibility"]["schema"] = {"read": [version], "write": version}
        if assembly == inputs:
            for name in ("receipt_format", "task_model"):
                metadata["compatibility"][name] = {"read": [0], "write": 0}
        (dest / paths.artifact).write_text(json.dumps(metadata))
        (dest / paths.cli).write_text(f"#!{dest / paths.interpreter}\n")
    return root, historical_release(root, inputs, paths), historical_release(root, target_inputs, paths)


def _database(root):
    """A plane database at the original schema, user_version 1."""
    path = db_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(_migration_files()[0][1])
    return conn


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
