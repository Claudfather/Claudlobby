"""Fixtures for tests of historical releases and their plane databases.

``releases`` asks for the ``installed`` fixture, so a test file that uses it
imports ``installed`` from ``tests.test_releases`` as well.
"""

from dataclasses import replace
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from claudlobby.plane.db import db_file
from claudlobby.plane.migrations import SCHEMA_USER_VERSION, _migration_files
from tests.test_releases import historical_release, r


@pytest.fixture
def releases(installed, request):
    root, inputs, paths, _, directory = installed
    target_inputs = replace(inputs, source_revision="c" * 40, artifact_id="candidate")
    target_dir = r.release_path(root, target_inputs.release_id)
    shutil.copytree(directory, target_dir)
    sql_dir = Path(__file__).resolve().parents[1] / "claudlobby/plane/migrations"
    for dest, assembly, version in ((directory, inputs, 1), (target_dir, target_inputs, SCHEMA_USER_VERSION)):
        metadata = json.loads((dest / paths.artifact).read_text())
        metadata.update(source_revision=assembly.source_revision, artifact_id=assembly.artifact_id)
        metadata["compatibility"]["schema"] = {"read": [version], "write": version}
        if assembly == inputs:
            for name in ("receipt_format", "task_model"):
                metadata["compatibility"][name] = {"read": [0], "write": 0}
        else:
            metadata["compatibility"].update(getattr(request, "param", {}))
        (dest / paths.artifact).write_text(json.dumps(metadata))
        (dest / paths.cli).write_text(f"#!{dest / paths.interpreter}\n")
        selected = dest / "package/plane/migrations"
        selected.mkdir(parents=True)
        for source in sql_dir.glob("*.sql"):
            if int(source.name[:4]) <= version:
                shutil.copy2(source, selected / source.name)
    return root, historical_release(root, inputs, paths), historical_release(root, target_inputs, paths)


def _database(root, version=1):
    path = db_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    for number, sql in _migration_files():
        if number <= version:
            conn.executescript(sql)
    return conn


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
