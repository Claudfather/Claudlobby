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
        "claudlobby/_resources/library/voices/fixture.md": "fixture",
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


@pytest.fixture
def bootstrap_bundle(tmp_path, monkeypatch):
    from claudlobby import bootstrap

    directory = (tmp_path / 'bundle').resolve()
    directory.mkdir()
    script = directory / 'bootstrap.py'
    script.write_bytes(Path(bootstrap.__file__).read_bytes())
    wheel = directory / 'claudlobby-0.1.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('claudlobby/bootstrap.py', script.read_bytes())
        archive.writestr('claudlobby/_artifact.json', json.dumps({'source_revision': 'a' * 40}))
    (directory / 'wheelhouse').mkdir()
    (directory / 'wheelhouse/dependency-1.0-py3-none-any.whl').write_bytes(b'dependency fixture')
    (directory / 'dependency.lock').write_text('dependency==1.0 --hash=sha256:' + 'c' * 64 + '\n')
    (directory / 'rehearsal.json').write_text(json.dumps({'passed': True, 'source_revision': 'a' * 40,
        'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}))
    (directory / 'built-on.txt').write_text('python 3.11.9\nplatform linux-aarch64\nlibc glibc 2.39\n')

    def sums():
        rows = [(p.relative_to(directory).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
                for p in directory.rglob('*') if p.is_file() and p.name != 'SHA256SUMS']
        (directory / 'SHA256SUMS').write_text(''.join(f'{digest}  ./{name}\n' for name, digest in sorted(rows)))
    sums()
    monkeypatch.setattr(bootstrap, '_platform', lambda _: None)
    return bootstrap, script, wheel, sums


def _installer(script, target):
    with zipfile.ZipFile(target, 'w') as archive:
        for path in script.parent.rglob('*'):
            if path.is_file():
                name = path.relative_to(script.parent).as_posix()
                archive.write(path, '__main__.py' if name == 'bootstrap.py' else name)


@pytest.mark.parametrize('packaged', [False, True])
@pytest.mark.parametrize('interrupted', [False, True])
def test_bootstrap_validates_finite_identity_and_uses_original_interpreter(bootstrap_bundle, tmp_path, monkeypatch, interrupted, packaged):
    bootstrap, script, wheel, _ = bootstrap_bundle
    calls = []
    monkeypatch.setattr(bootstrap, '_run', lambda argv, env, **_: calls.append((argv, env)))
    observed = {}
    original_bundle = bootstrap._bundle
    def inspect_bundle(script):
        observed['input'] = script.parent
        return original_bundle(script)
    monkeypatch.setattr(bootstrap, '_bundle', inspect_bundle)
    if packaged:
        installer = tmp_path / 'install.pyz'
        _installer(script, installer)
        monkeypatch.setattr(bootstrap, '__loader__', bootstrap.zipimport.zipimporter(str(installer)))
        monkeypatch.setattr(bootstrap, '__file__', str(installer / '__main__.py'))
    else:
        monkeypatch.setattr(bootstrap, '__file__', str(script))
    class Setup:
        def __init__(self, argv, env, start_new_session):
            observed.update(argv=argv, env=env, owned=start_new_session, work=Path(argv[0]).parents[2])
            assert Path(argv[argv.index('--wheel') + 1]).read_bytes() == wheel.read_bytes()
            assert argv[argv.index('--interpreter') + 1] == str(Path(sys.executable).resolve())
            assert observed['work'] not in Path(argv[-2]).parents
        waits = 0
        def wait(self):
            self.waits += 1
            assert observed['work'].is_dir(), 'setup inputs were deleted before child exit'
            assert observed['input'].is_dir(), 'installer inputs were deleted before child exit'
            if interrupted and self.waits <= 2:
                raise KeyboardInterrupt
            return 3
        def poll(self): return 3
    monkeypatch.setattr(bootstrap.subprocess, 'Popen', Setup)
    monkeypatch.setenv('SECRET_TOKEN', 'not-forwarded')
    monkeypatch.setenv('PYTHONPATH', '/not-forwarded')
    root = (tmp_path / 'new-data').resolve()
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            bootstrap.main(['--root', str(root), '--json'])
    else:
        assert bootstrap.main(['--root', str(root), '--json']) == 3
    assert not root.exists() and not observed['work'].exists()
    assert observed['input'].exists() is (not packaged)
    assert 'SECRET_TOKEN' not in observed['env'] and 'PYTHONPATH' not in observed['env']
    assert observed['owned'] and observed['argv'][-1] == '--json'
    assert len(calls) == 4
    assert '--no-index' in calls[1][0] and '--require-hashes' in calls[1][0] and '--no-deps' in calls[1][0]
    assert '--no-index' in calls[2][0] and '--no-deps' in calls[2][0]
    assert all(env['PIP_CONFIG_FILE'] == os.devnull for _, env in calls)


@pytest.mark.parametrize('fault', ['extra', 'symlink', 'missing-checksum', 'duplicate-checksum', 'traversal',
                                 'changed-bytes', 'wrong-revision', 'failed-rehearsal', 'wrong-launcher', 'url-lock'])
def test_bootstrap_carrier_refusals_precede_installation(bootstrap_bundle, tmp_path, monkeypatch, fault):
    bootstrap, script, wheel, sums = bootstrap_bundle
    directory = script.parent
    if fault == 'extra': (directory / 'extra').write_text('unexpected')
    elif fault == 'symlink':
        p = directory / 'wheelhouse/dependency-1.0-py3-none-any.whl'
        p.unlink(); p.symlink_to(wheel)
    elif fault in {'wrong-revision', 'failed-rehearsal'}:
        p = directory / 'rehearsal.json'; body = json.loads(p.read_text())
        body.update({'source_revision': 'b' * 40} if fault == 'wrong-revision' else {'passed': False})
        p.write_text(json.dumps(body)); sums()
    elif fault == 'wrong-launcher': script.write_bytes(script.read_bytes() + b'\n'); sums()
    elif fault == 'url-lock':
        (directory / 'dependency.lock').write_text('dependency @ https://example.invalid/pkg.whl --hash=sha256:' + 'c' * 64)
        sums()
    elif fault == 'changed-bytes': (directory / 'built-on.txt').write_text('changed')
    else:
        p = directory / 'SHA256SUMS'; text = p.read_text()
        if fault == 'missing-checksum': text = '\n'.join(text.splitlines()[1:]) + '\n'
        elif fault == 'duplicate-checksum': text += text.splitlines()[0] + '\n'
        else: text += 'a' * 64 + '  ../outside\n'
        p.write_text(text)
    monkeypatch.setattr(bootstrap, '_run', lambda *_: pytest.fail('installation started'))
    root = (tmp_path / 'new-data').resolve()
    with pytest.raises(bootstrap.BundleError): bootstrap.bootstrap(script, root)
    assert not root.exists()


@pytest.mark.parametrize('fault', ['traversal', 'symlink', 'duplicate', 'case-collision', 'extra',
                                 'oversized', 'wrong-launcher'])
def test_installer_rejects_bad_archive_before_installation(bootstrap_bundle, tmp_path, monkeypatch, fault):
    bootstrap, script, wheel, sums = bootstrap_bundle
    if fault == 'wrong-launcher':
        script.write_bytes(script.read_bytes() + b'\n')
        sums()  # Still must match the candidate module, even with updated checksums.
    installer = tmp_path / 'install.pyz'
    _installer(script, installer)
    if fault == 'oversized':
        monkeypatch.setattr(bootstrap, '_MAX_FILE', script.stat().st_size - 1)
    elif fault != 'wrong-launcher':
        name = {'traversal': '../escaped', 'symlink': 'linked', 'duplicate': 'built-on.txt',
                'case-collision': 'BUILT-ON.TXT', 'extra': 'extra'}[fault]
        info = zipfile.ZipInfo(name)
        if fault == 'symlink': info.external_attr = 0o120777 << 16
        with zipfile.ZipFile(installer, 'a') as archive:
            if fault == 'duplicate':
                with pytest.warns(UserWarning, match='Duplicate name'):
                    archive.writestr(info, b'changed')
            else: archive.writestr(info, b'changed')
    monkeypatch.setattr(bootstrap, '_run', lambda *_: pytest.fail('installation started'))
    directory = (tmp_path / 'extracted').resolve()
    directory.mkdir()
    root = (tmp_path / 'data').resolve()
    with pytest.raises(bootstrap.BundleError):
        bootstrap.bootstrap(bootstrap._extract_installer(installer, directory), root)
    assert not root.exists() and not (tmp_path / 'escaped').exists()


@pytest.mark.parametrize('target,machine,platform_tag,accepted', [
    ('linux', 'aarch64', 'linux-aarch64', True), ('darwin', 'arm64', 'macosx-15.0-arm64', True),
    ('darwin', 'arm64', 'macosx-10.9-universal2', True), ('darwin', 'x86_64', 'macosx-10.9-universal2', False),
    ('darwin', 'x86_64', 'macosx-15.0-x86_64', False), ('linux', 'aarch64', 'macosx-15.0-arm64', False),
    ('linux', 'armv7l', 'linux-armv7l', False)])
def test_bootstrap_platform_contract(target, machine, platform_tag, accepted, monkeypatch):
    from claudlobby import bootstrap
    monkeypatch.setattr(bootstrap.sys, 'platform', target)
    monkeypatch.setattr(bootstrap.sys, 'version_info', (3, 11, 9))
    monkeypatch.setattr(bootstrap.platform, 'machine', lambda: machine)
    text = f'python 3.11.9\nplatform {platform_tag}\nlibc unknown\n'
    if accepted: bootstrap._platform(text)
    else:
        with pytest.raises(bootstrap.BundleError): bootstrap._platform(text)


def test_bootstrap_install_failure_identifies_step_without_echoing_output(monkeypatch):
    from claudlobby import bootstrap

    class FailedInstall:
        returncode = 1
        def communicate(self, timeout):
            return b'private stdout', b'private stderr'

    monkeypatch.setattr(bootstrap.subprocess, 'Popen', lambda *args, **kwargs: FailedInstall())
    with pytest.raises(bootstrap.BundleError, match='failed at dependency installation') as exc:
        bootstrap._run(['unused'], {}, step='dependency installation')
    assert 'private' not in str(exc.value)
