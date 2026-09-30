"""Assembly boundary tests with pip simulated; no application/native imports."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import ast
import shlex
import sys
import types
import zipfile

import pytest


_source = Path(__file__).resolve().parents[1] / "claudlobby"
_package = types.ModuleType("release_install_under_test")
_package.__path__ = [str(_source)]
sys.modules[_package.__name__] = _package
for _name in ("releases", "release_install"):
    _spec = importlib.util.spec_from_file_location(
        f"{_package.__name__}.{_name}", _source / f"{_name}.py")
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)
r = sys.modules[f"{_package.__name__}.releases"]
install = sys.modules[f"{_package.__name__}.release_install"]
_versions = runpy.run_path(str(_source / "runtime_versions.py"))


@pytest.fixture
def assembly(tmp_path, monkeypatch):
    inputs = tmp_path / "operator-inputs"
    inputs.mkdir()
    interpreter = inputs / "python"
    interpreter.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    interpreter.chmod(0o755)
    wheel = inputs / "claudlobby-0.1.0-py3-none-any.whl"
    artifact = {"schema": 1, "source_revision": "a" * 40,
                "content_sha256": "b" * 64, "artifact_id": "source-artifact",
                "compatibility": _versions["runtime_declaration"]()}
    payload = {
        "claudlobby/_artifact.json": json.dumps(artifact),
        "claudlobby/__init__.py": "raise RuntimeError('do not import candidate')\n",
        "claudlobby/__main__.py": "def main(): return 0\n",
        "claudlobby/system.yaml": "system: fixture\n",
        "claudlobby/_runtime_scripts/run.sh":"#!/bin/sh\nexit 0\n",
        "claudlobby/_resources/library/fixture.md": "fixture",
        "claudlobby/_resources/voices/fixture.md": "fixture",
        "claudlobby/_resources/templates/fixture.j2": "fixture",
        "claudlobby/_resources/seeds/fleet.yaml.seed": "fixture",
        "claudlobby-0.1.0.dist-info/METADATA": "Name: claudlobby\nVersion: 0.1.0\n",
        "claudlobby-0.1.0.dist-info/entry_points.txt":
            "[console_scripts]\nclaudlobby = claudlobby.__main__:main\n",
    }
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in payload.items():
            archive.writestr(name, content)
    lock = inputs / "requirements.lock"
    lock.write_text("dependency==1.0 \\" + "\n    --hash=sha256:" + "c" * 64 + "\n")
    wheelhouse = inputs / "wheelhouse"
    wheelhouse.mkdir()
    root = tmp_path / "fleet data"
    calls = []
    behavior = {"fail_pip": False, "alter_package": False, "shared_library": None}

    def runner(argv, env, cwd, timeout=300):
        calls.append(argv)
        assert Path(env["HOME"]).is_relative_to(cwd)
        assert Path(env["TMPDIR"]).is_relative_to(cwd)
        assert env["PIP_CONFIG_FILE"] == os.devnull
        assert "PYTHONPATH" not in env
        if install._INTERPRETER_QUERY in argv:
            return json.dumps({"implementation": "cpython", "version": "3.11",
                               "platform": "fixture",
                               "shared_library": behavior["shared_library"]})
        if "venv" in argv:
            venv = Path(argv[-1])
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").write_bytes(interpreter.read_bytes())
            (venv / "bin/python").chmod(0o755)
            cache = venv / "lib/site-packages/pip/__pycache__"
            cache.mkdir(parents=True)
            (cache / "fixture.pyc").write_bytes(b"ensurepip bytecode")
            return ""
        venv = Path(argv[0]).parent.parent
        site = venv / "lib/site-packages"
        if "ensurepip" in argv:
            library = Path(behavior["shared_library"])
            assert (venv / "lib" / library.name).read_bytes() == library.read_bytes()
            return ""
        if install._SITE_QUERY in argv:
            return str(site) + "\n"
        if "--require-hashes" in argv and behavior["fail_pip"]:
            raise r.ReleaseError("pip rejected dependency hash mismatch")
        if argv[-1].endswith(".whl"):
            with zipfile.ZipFile(argv[-1]) as archive:
                for entry in archive.infolist():
                    path = site / entry.filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(archive.read(entry))
                    path.chmod(0o644 | ((entry.external_attr >> 16) & 0o111))
            (venv / "bin/claudlobby").write_text("pip entrypoint\n")
            if behavior["alter_package"]:
                (site / "claudlobby/_artifact.json").write_text("{}")
        return ""

    monkeypatch.setattr(install, "_run", runner)
    return (root, wheel, lock, wheelhouse, interpreter), calls, behavior


def test_complete_assembly_seals_exact_inputs_and_reuses_without_install(assembly):
    args, calls, _ = assembly
    root, wheel, lock, _, _ = args
    manifest = install.assemble_release(*args)
    assert manifest.compatibility.to_dict() == _versions["runtime_declaration"]()
    assert manifest.inputs.wheel_sha256 == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert manifest.inputs.dependency_lock_sha256 == hashlib.sha256(lock.read_bytes()).hexdigest()
    assert (manifest.directory / manifest.paths.wheel).read_bytes() == wheel.read_bytes()
    assert not list(manifest.directory.rglob("__pycache__"))
    assert not (root / "state/current").exists()
    assert r.read_release(root, manifest.release_id) == manifest
    before = r.inventory_release(manifest.directory)
    # Static bootstrap proof only; direct execution with an actual installed
    # interpreter belongs to the installed-artifact CI probe.
    command = shlex.split(manifest.cli_path.read_text().splitlines()[1])
    assert command[:5] == ["exec", str(manifest.directory / manifest.paths.interpreter),
                           "-I", "-B", "-c"]
    ast.parse(command[5])
    assert command[-2:] == ["$0", "$@"]
    assert r.inventory_release(manifest.directory) == before
    first_count = len(calls)
    assert install.assemble_release(*args) == manifest
    assert len(calls) == first_count + 1  # only the bounded interpreter identity probe


def test_copied_interpreter_gets_its_shared_library_before_ensurepip(assembly):
    args, calls, behavior = assembly
    library = args[4].parent / "libpython3.11.dylib"
    library.write_bytes(b"fixture shared library")
    behavior["shared_library"] = str(library)
    manifest = install.assemble_release(*args)
    installed = manifest.directory / "venv/lib/libpython3.11.dylib"
    assert installed.read_bytes() == library.read_bytes()
    venv_call = next(call for call in calls if "venv" in call)
    assert "--copies" in venv_call and "--without-pip" in venv_call
    ensurepip_call = next(call for call in calls if "ensurepip" in call)
    assert ensurepip_call[0] == str(manifest.directory / "venv/bin/python")
    assert r.read_release(args[0], manifest.release_id) == manifest


def test_failed_pip_retains_unsealed_directory_and_refuses_retry(assembly):
    args, calls, behavior = assembly
    behavior["fail_pip"] = True
    with pytest.raises(r.ReleaseError, match="incomplete release retained"):
        install.assemble_release(*args)
    directory, = (args[0] / "state/releases").iterdir()
    assert (directory / "dependency.lock").read_bytes() == args[2].read_bytes()
    assert not (directory / "release.json").exists()
    assert not (args[0] / "state/current").exists()
    count = len(calls)
    behavior["fail_pip"] = False
    with pytest.raises(r.ReleaseError, match="refusing to overwrite"):
        install.assemble_release(*args)
    assert len(calls) == count + 1
    assert not (directory / "release.json").exists()


def test_network_lock_and_wrong_wheel_refuse_before_install(assembly):
    args, calls, _ = assembly
    root, wheel, lock, *_ = args
    valid_lock = lock.read_bytes()
    lock.write_text("dependency @ https://example.invalid/pkg.whl --hash=sha256:" + "c" * 64)
    with pytest.raises(r.ReleaseError, match="pinned names"):
        install.assemble_release(*args)
    assert not root.exists()
    assert not calls
    lock.write_bytes(valid_lock)
    with zipfile.ZipFile(wheel) as archive:
        payload = {entry.filename: archive.read(entry) for entry in archive.infolist()}
    artifact = json.loads(payload["claudlobby/_artifact.json"])
    artifact["compatibility"]["declaration_version"] = 2
    payload["claudlobby/_artifact.json"] = json.dumps(artifact)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in payload.items():
            archive.writestr(name, content)
    with pytest.raises(r.ReleaseError, match="unsupported runtime compatibility declaration"):
        install.assemble_release(*args)
    assert not root.exists()
    assert not calls
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("unrelated-1.dist-info/METADATA", "Name: unrelated\n")
    with pytest.raises(r.ReleaseError, match="only claudlobby"):
        install.assemble_release(*args)
    assert not root.exists()
    assert not calls


def test_installed_artifact_mismatch_cannot_seal(assembly):
    args, _, behavior = assembly
    behavior["alter_package"] = True
    with pytest.raises(r.ReleaseError, match="installed candidate differs from wheel"):
        install.assemble_release(*args)
    directory, = (args[0] / "state/releases").iterdir()
    assert not (directory / "release.json").exists()
