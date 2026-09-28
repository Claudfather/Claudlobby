"""Release seal boundary checks; no package imports or service operations."""

from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import runpy
import shutil
import sys

import pytest


# Load only this stdlib file, including when claudlobby or its dependencies
# cannot import. This also supports the limited exported --noconftest run.
_source = Path(__file__).resolve().parents[1] / "claudlobby" / "releases.py"
_spec = importlib.util.spec_from_file_location("release_primitives_under_test", _source)
r = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = r
_spec.loader.exec_module(r)
_versions = runpy.run_path(str(_source.with_name("runtime_versions.py")))


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / "data"
    python = b"#!/bin/sh\nexit 0\n"
    wheel = b"release fixture wheel bytes"
    lock = b"example==1.0 --hash=sha256:fixture\n"
    inputs = r.ReleaseInputs.from_lock(
        source_revision="a" * 40, artifact_id="artifact-source-a",
        artifact_sha256="b" * 64, wheel_sha256=hashlib.sha256(wheel).hexdigest(),
        dependency_lock=lock,
        interpreter=r.InterpreterIdentity(
            "cpython", "3.11.9", "fixture-platform", hashlib.sha256(python).hexdigest()
        ),
    )
    directory = r.release_path(root, inputs.release_id)
    paths = r.ReleasePaths("venv/bin/python", "venv/bin/claudlobby", "package/_native",
                           "package/_artifact.json", "dependency.lock", "artifact.whl")
    for selected in (paths.interpreter, paths.cli, paths.artifact):
        (directory / selected).parent.mkdir(parents=True, exist_ok=True)
    (directory / paths.interpreter).write_bytes(python)
    (directory / paths.interpreter).chmod(0o755)
    (directory / paths.cli).write_text(f"#!{directory / paths.interpreter}\n")
    (directory / paths.cli).chmod(0o755)
    (directory / paths.dependency_lock).write_bytes(lock)
    (directory / paths.wheel).write_bytes(wheel)
    (directory / paths.native).mkdir()
    (directory / paths.native / "keepalive.sh").write_text("#!/bin/sh\nexit 0\n")
    (directory / paths.artifact).write_text(json.dumps({
        "schema": 1, "source_revision": inputs.source_revision,
        "artifact_id": inputs.artifact_id, "content_sha256": inputs.artifact_sha256,
        "compatibility": _versions["runtime_declaration"](),
    }))
    (directory / "package" / "__init__.py").write_text(
        "raise RuntimeError('broken package import must not affect metadata reads')\n"
    )
    (directory / "package" / "runtime_versions.py").write_text(
        "raise RuntimeError('candidate version code must not execute during recovery')\n"
    )
    compatibility = r.Compatibility.from_dict(_versions["runtime_declaration"]())
    return root, inputs, paths, compatibility, directory


def test_identity_is_stable_and_binds_assembly_inputs(installed):
    root, inputs, paths, compatibility, directory = installed
    assert r.release_path(root, inputs.release_id) == directory
    assert inputs.release_id != inputs.artifact_id
    assert inputs == r.ReleaseInputs.from_lock(
        source_revision=inputs.source_revision, artifact_id=inputs.artifact_id,
        artifact_sha256=inputs.artifact_sha256,
        wheel_sha256=inputs.wheel_sha256,
        dependency_lock=(directory / paths.dependency_lock).read_bytes(),
        interpreter=inputs.interpreter,
    )
    variants = [replace(inputs, source_revision="c" * 40),
                replace(inputs, artifact_sha256="c" * 64),
                replace(inputs, wheel_sha256="c" * 64),
                replace(inputs, dependency_lock_sha256="c" * 64),
                replace(inputs, interpreter=replace(inputs.interpreter, platform="other"))]
    assert all(value.release_id != inputs.release_id for value in variants)
    first = r.inventory_release(directory)
    (directory / paths.cli).touch()
    assert r.inventory_release(directory) == first  # mtimes are not identity
    sealed = r.seal_release(root, inputs, paths)
    assert r.read_release(root, inputs.release_id) == sealed
    assert sealed.cli_path == directory / paths.cli
    assert sealed.native_path == directory / paths.native
    assert sealed.inventory == first
    assert sealed.compatibility == compatibility
    assert compatibility.write_versions == {
        "schema": 12, "envelope": "1.0.0", "protocol": 0, "pending_format": 0,
        "receipt_format": 0, "task_model": 0, "config_plan": 1,
    }
    assert compatibility.blockers(compatibility.write_versions) == ()
    assert compatibility.blockers({**compatibility.write_versions, "schema": 13}) == (
        "unsupported schema: 13",)
    assert sealed.seal_sha256 == json.loads(
        (directory / r.MANIFEST).read_text()
    )["seal_sha256"]
    with pytest.raises(r.ReleaseError, match="cannot be resealed"):
        r.seal_release(root, inputs, paths)


def test_wrong_directory_and_manifest_tamper_refuse(installed):
    root, inputs, paths, compatibility, directory = installed
    r.seal_release(root, inputs, paths)
    other_id = replace(inputs, source_revision="d" * 40).release_id
    shutil.copytree(directory, r.release_path(root, other_id))
    with pytest.raises(r.ReleaseError, match="identity mismatch"):
        r.read_release(root, other_id)
    relocated_root = root.parent / "relocated"
    shutil.copytree(directory, r.release_path(relocated_root, inputs.release_id))
    with pytest.raises(r.ReleaseError, match="identity mismatch"):
        r.read_release(relocated_root, inputs.release_id)
    manifest_file = directory / r.MANIFEST
    raw = json.loads(manifest_file.read_text())
    raw["compatibility"]["schema"] = {"read": [13], "write": 13}
    manifest_file.write_text(json.dumps(raw))
    with pytest.raises(r.ReleaseError, match="manifest digest mismatch"):
        r.read_release(root, inputs.release_id)
    # Even a recomputed seal must not override the candidate's declaration.
    raw.pop("seal_sha256")
    raw["seal_sha256"] = r._digest(r._json(raw))
    manifest_file.write_text(json.dumps(raw))
    with pytest.raises(r.ReleaseError, match="artifact and release manifest compatibility mismatch"):
        r.read_release(root, inputs.release_id)


@pytest.mark.parametrize("change", ["bytes", "mode", "added", "missing"])
def test_runtime_tamper_refuses(installed, change):
    root, inputs, paths, compatibility, directory = installed
    r.seal_release(root, inputs, paths)
    script = directory / paths.native / "keepalive.sh"
    if change == "bytes":
        script.write_text("changed")
    elif change == "mode":
        script.chmod(0o700)
    elif change == "added":
        (directory / "unexpected").write_text("extra")
    else:
        script.unlink()
    with pytest.raises(r.ReleaseError, match="runtime inventory digest mismatch"):
        r.read_release(root, inputs.release_id)


@pytest.mark.parametrize("change", ["lock", "artifact", "interpreter", "native", "cli", "bytecode"])
def test_incomplete_or_mismatched_install_never_seals(installed, change):
    root, inputs, paths, compatibility, directory = installed
    if change == "lock":
        with (directory / paths.dependency_lock).open("ab") as output:
            output.write(b"\n")  # exact bytes matter, including blank lines
    elif change == "artifact":
        (directory / paths.artifact).write_text("{}")
    elif change == "interpreter":
        (directory / paths.interpreter).write_text("different interpreter")
    elif change == "native":
        (directory / paths.native / "keepalive.sh").unlink()
    elif change == "cli":
        (directory / paths.cli).chmod(0o644)
    else:
        cache = directory / "package" / "__pycache__"
        cache.mkdir()
        (cache / "module.pyc").write_bytes(b"bytecode")
    with pytest.raises(r.ReleaseError):
        r.seal_release(root, inputs, paths)
    assert not (directory / r.MANIFEST).exists()
    assert not list(directory.glob(".release-*"))


def test_escaped_selection_and_symlink_refuse(installed, tmp_path):
    root, inputs, paths, compatibility, directory = installed
    with pytest.raises(r.ReleaseError, match="relative path"):
        replace(paths, cli="../../outside")
    outside = tmp_path / "outside"
    outside.write_text("external")
    (directory / "escape").symlink_to(outside)
    with pytest.raises(r.ReleaseError, match="symlink escapes"):
        r.seal_release(root, inputs, paths)
    assert not (directory / r.MANIFEST).exists()
    (directory / "escape").unlink()
    (directory / "venv/bin/python3").symlink_to("python")
    r.seal_release(root, inputs, paths)
    assert r.read_release(root, inputs.release_id).release_id == inputs.release_id


def test_redirected_store_and_incomplete_manifest_refuse(installed, tmp_path):
    root, inputs, paths, compatibility, directory = installed
    alias = tmp_path / "alias-data"
    alias.mkdir()
    (alias / "state").symlink_to(root / "state", target_is_directory=True)
    with pytest.raises(r.ReleaseError, match="redirected"):
        r.release_path(alias, inputs.release_id)
    (directory / r.MANIFEST).write_text('{"schema": 1}')
    with pytest.raises(r.ReleaseError, match="incomplete"):
        r.read_release(root, inputs.release_id)


def test_failed_publication_leaves_no_seal(installed, monkeypatch):
    root, inputs, paths, compatibility, directory = installed

    def fail_publish(*args):
        raise OSError("publication failed")

    monkeypatch.setattr(r.os, "link", fail_publish)
    with pytest.raises(OSError, match="publication failed"):
        r.seal_release(root, inputs, paths)
    assert not (directory / r.MANIFEST).exists()
    assert not list(directory.glob(".release-*"))
