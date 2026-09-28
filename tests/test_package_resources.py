"""Installed resource, composition and authoring smoke without service operation."""

import hashlib
from email.parser import BytesParser
from importlib import metadata as importlib_metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from tests.conftest import constructed_env


REPO = Path(__file__).resolve().parents[1]


def _run(args, cwd):
    result = subprocess.run(
        [str(arg) for arg in args], cwd=cwd, env=constructed_env(),
        text=True, capture_output=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _wheel_payload(path):
    with zipfile.ZipFile(path) as wheel:
        return {
            item.filename: (wheel.read(item), (item.external_attr >> 16) & 0o111)
            for item in wheel.infolist() if item.filename.startswith("claudlobby/")
        }


def _asset_hashes(package):
    paths = [package / "system.yaml", package / "_artifact.json"]
    for directory in ("_resources", "_native"):
        paths.extend(path for path in (package / directory).rglob("*") if path.is_file())
    return {
        path.relative_to(package).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o111,
        ) for path in paths
    }


def _copy_installed_dependencies(wheel, installed):
    """Copy this wheel's core dependency closure from the test interpreter.

    A nested --system-site-packages venv sees the base interpreter's packages,
    not its parent venv's. Use the wheel's requirements and installed RECORDs
    instead, without downloading or adding the source checkout to sys.path.
    """
    with zipfile.ZipFile(wheel) as archive:
        metadata_file, = [name for name in archive.namelist()
                          if name.endswith(".dist-info/METADATA")]
        metadata = BytesParser().parsebytes(archive.read(metadata_file))
    pending = [Requirement(value) for value in metadata.get_all("Requires-Dist", [])
               if Requirement(value).marker is None
               or Requirement(value).marker.evaluate({"extra": ""})]
    copied, expanded = set(), set()
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        assert name != "claudlobby", "dependency closure must not copy the source package"
        distribution = importlib_metadata.distribution(requirement.name)
        assert requirement.specifier.contains(distribution.version, prereleases=True), (
            f"Installed {name}=={distribution.version} does not satisfy {requirement}")
        if name not in copied:
            source_root = Path(distribution.locate_file("")).resolve()
            files = distribution.files
            assert files is not None, f"{name} has no installed file inventory"
            assert any(str(path).endswith(".dist-info/WHEEL") for path in files), (
                f"{name} must be an installed wheel, not an editable source dependency")
            for relative in files:
                relative = Path(relative)
                if (relative.is_absolute() or ".." in relative.parts
                        or "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}):
                    continue
                source = Path(distribution.locate_file(relative)).resolve()
                if not source.is_relative_to(source_root):
                    continue
                assert source.is_file(), f"{name} installed file is missing: {relative}"
                destination = installed / relative
                assert destination.resolve().is_relative_to(installed.resolve())
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    assert destination.read_bytes() == source.read_bytes(), (
                        f"Dependency files conflict at {relative}")
                else:
                    shutil.copy2(source, destination)
            copied.add(name)
        for extra in {"", *requirement.extras}:
            if (name, extra) in expanded:
                continue
            expanded.add((name, extra))
            for value in distribution.requires or []:
                dependency = Requirement(value)
                if dependency.marker is None or dependency.marker.evaluate({"extra": extra}):
                    pending.append(dependency)
    return sorted(copied)


def test_installed_resources_match_direct_and_sdist_wheels(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for directory in ("claudlobby", "lib", "library", "voices", "templates", "missions"):
        shutil.copytree(REPO / directory, source / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_artifact.json", "_resources", "_native"))
    for name in ("pyproject.toml", "setup.py", "README.md", ".gitignore", "fleet.yaml.seed", "fleet.yaml.example",
                 "projects.yaml.seed", ".env.seed.example"):
        shutil.copy2(REPO / name, source / name)
    removed_module = source / "claudlobby/_removed_for_build_smoke.py"
    removed_module.write_text("REMOVED_BUILD_SMOKE = True\n")
    # A private index proves authored inputs without a commit, remote or identity.
    _run(["git", "init", "--quiet"], source)
    _run(["git", "add", "."], source)
    ignored = ("voices/local/operator.md", "library/skills/printify/config.json",
               "lib/fleet-state.json")
    for name in ignored:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE-OPERATOR-FIXTURE-DO-NOT-PACKAGE\n")

    before_removal = tmp_path / "before-removal"
    _run([sys.executable, "-m", "build", "--no-isolation", "--wheel",
          "--outdir", before_removal, source], tmp_path)
    assert "claudlobby/_removed_for_build_smoke.py" in _wheel_payload(
        next(before_removal.glob("*.whl")))
    removed_module.unlink()
    direct, rebuilt = tmp_path / "direct", tmp_path / "rebuilt"
    _run([sys.executable, "-m", "build", "--no-isolation", "--wheel",
          "--outdir", direct, source], tmp_path)
    # The frontend's default builds an sdist and then builds the wheel FROM it.
    _run([sys.executable, "-m", "build", "--no-isolation",
          "--outdir", rebuilt, source], tmp_path)
    direct_payload = _wheel_payload(next(direct.glob("*.whl")))
    wheel = next(rebuilt.glob("*.whl"))
    payload = _wheel_payload(wheel)
    assert payload == direct_payload
    assert "claudlobby/_removed_for_build_smoke.py" not in payload
    assert all(b"PRIVATE-OPERATOR-FIXTURE-DO-NOT-PACKAGE" not in contents
               for contents, _ in payload.values())

    source_inputs = {source / "setup.py", source / "pyproject.toml"}
    # Assert complete base assets, not just a sentinel file in each directory.
    for directory in ("library", "voices", "templates"):
        expected = {path.relative_to(source).as_posix(): path for path in
                    (source / directory).rglob("*") if path.is_file()
                    and path.relative_to(source).as_posix() not in ignored}
        prefix = "claudlobby/_resources/"
        actual = {name.removeprefix(prefix) for name in payload
                  if name.startswith(prefix + directory + "/")}
        assert actual == set(expected)
        for name, path in expected.items():
            assert payload[prefix + name] == (path.read_bytes(), path.stat().st_mode & 0o111)
        source_inputs.update(expected.values())
    for name in ("fleet.yaml.seed", "fleet.yaml.example", "projects.yaml.seed", ".env.seed.example",
                 "missions/fleet.md.seed"):
        assert payload["claudlobby/_resources/seeds/" + name][0] == (source / name).read_bytes()
        source_inputs.add(source / name)
    assert payload["claudlobby/system.yaml"][0] == (source / "claudlobby/system.yaml").read_bytes()

    native = {name.removeprefix("claudlobby/_native/") for name in payload
              if name.startswith("claudlobby/_native/")}
    assert {"keepalive.sh", "lib-common.sh", "supervisor.sh", "git-credential-github-app",
            "env-tiers.sh", "setup-fleet", "plane-socket-client.py"} <= native
    assert not any(name.startswith(("rehearse-", "ab-", "boot-strand-", "plane-canary-"))
                   for name in native)
    assert not native.intersection({
        "CLAUDE.md", "coldstart-harness.sh", "freshbox-boot-gate.sh",
        "naked-bot-observe.py", "plane-durability-driver.py", "send-size-probe.sh",
        "validate-bot-change.sh", "vault-git-base-rate.py", "personal/finance-presync.sh",
    })
    for name in native:
        path = source / "lib" / name
        assert payload["claudlobby/_native/" + name] == (path.read_bytes(), path.stat().st_mode & 0o111)
        source_inputs.add(path)
    for name in payload:
        if not name.startswith(("claudlobby/_resources/", "claudlobby/_native/")):
            if name != "claudlobby/_artifact.json":
                source_inputs.add(source / name)

    metadata = json.loads(payload["claudlobby/_artifact.json"][0])
    digest = hashlib.sha256()
    for path in sorted(source_inputs):
        record = [path.relative_to(source).as_posix(), path.stat().st_mode & 0o111,
                  hashlib.sha256(path.read_bytes()).hexdigest()]
        digest.update((json.dumps(record, separators=(",", ":")) + "\n").encode())
    assert metadata["content_sha256"] == digest.hexdigest()
    assert metadata["artifact_id"] == "source-" + digest.hexdigest()
    assert "release_id" not in metadata
    assert not set(metadata["resource_sources"]).intersection(ignored)
    with tarfile.open(next(rebuilt.glob("*.tar.gz"))) as archive:
        prefix = archive.getnames()[0].split("/")[0] + "/"
        assert json.load(archive.extractfile(prefix + "claudlobby/_artifact.json")) == metadata
        assert all(prefix + path.relative_to(source).as_posix() in archive.getnames()
                   for path in source_inputs)
        assert prefix + "bin/claudlobby" not in archive.getnames()
        assert not any(prefix + name in archive.getnames() for name in ignored)

    release = tmp_path / "release"
    _run([sys.executable, "-m", "venv", release], tmp_path)
    python, cli = release / "bin/python", release / "bin/claudlobby"
    # Install only into the private environment. Populate its dependency closure
    # from the current test interpreter's installed distributions below.
    _run([python, "-m", "pip", "install", "--ignore-installed", "--no-deps",
          "--no-index", "--no-compile", wheel], tmp_path)
    installed = Path(_run([python, "-I", "-c",
                          "import sysconfig; print(sysconfig.get_path('purelib'))"], tmp_path).strip())
    dependencies = _copy_installed_dependencies(wheel, installed)
    _run([sys.executable, "-I", "-S", "-B", "-c", """
import sys
sys.path.insert(0, sys.argv[1])
from claudlobby.resources import get_resources
try:
    get_resources()
except RuntimeError as exc:
    assert 'not built' in str(exc)
else:
    raise AssertionError('unbuilt checkout was accepted as an installed release')
""", source], tmp_path)
    source.rename(tmp_path / "source-unavailable")
    # -S removes third-party packages, -I removes ambient Python configuration.
    # Reading paths/identity must work using only the installed artifact + stdlib.
    script = """
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
from claudlobby.resources import get_resources
r = get_resources()
assert all(path.is_absolute() and path.exists() for path in
           (r.library, r.voices, r.templates, r.seeds, r.native, r.system_yaml))
assert r.native.is_relative_to(pathlib.Path(sys.argv[1]).resolve())
assert (r.native / 'git-credential-github-app').stat().st_mode & 0o111
assert (r.seeds / '.env.seed.example').is_file()
assert (r.seeds / 'missions/fleet.md.seed').is_file()
assert not {'yaml', 'jinja2', 'pydantic'} & sys.modules.keys()
assert not hasattr(r, 'release_id')
print(json.dumps({'artifact_id': r.artifact_id, 'content_sha256': r.content_sha256}))
"""
    installed_identity = json.loads(_run(
        [python, "-I", "-S", "-B", "-c", script, installed], tmp_path))
    assert installed_identity == {key: metadata[key] for key in installed_identity}

    data, outside = tmp_path / "data", tmp_path / "outside"
    data.mkdir()
    outside.mkdir()
    assert not data.resolve().is_relative_to(REPO.resolve())
    (data / "fleet.yaml").write_text("""fleet:
  name: installed-smoke
  manager: manager
  service_prefix: com.installed-smoke
  system_defaults: false
  defaults:
    channels: []
  bots:
    manager:
      expertise: [orchestration]
    worker:
      expertise: [software-engineering]
      skills: [installed-smoke]
""")
    package = installed / "claudlobby"
    immutable_before = _asset_hashes(package)
    _run([cli, "--root", data, "new-skill", "--name", "installed-smoke",
          "--description", "Authored outside the installed package"], outside)
    authored = data / "library/skills/installed-smoke/SKILL.md"
    assert "Authored outside the installed package" in authored.read_text()
    assert not authored.resolve().is_relative_to(package.resolve())

    # No sys.path or resource injection: these imports must come from the
    # installed wheel while the working directory and data root are unrelated.
    _run([python, "-I", "-B", "-c", """
import importlib.metadata, pathlib, plistlib, shlex, sys
import claudlobby
from claudlobby.context import resolve_context
from claudlobby.composer import compose_fleet
from claudlobby.resources import get_resources, selected_cli
root, package, cli = map(pathlib.Path, sys.argv[1:4])
assert pathlib.Path(claudlobby.__file__).resolve().parent == package.resolve()
for name in sys.argv[4:]:
    distribution = importlib.metadata.distribution(name)
    assert pathlib.Path(distribution.locate_file('')).resolve() == package.parent.resolve()
resources = get_resources()
assert resources.native == package / '_native'
assert selected_cli() == cli
context = resolve_context(root=root)
assert context.paths.package == resources
assert context.fleet.manager == 'manager'
assert all(bot.channels == [] for bot in context.fleet.bots.values())
assert context.paths.find_library_file('expertise', 'orchestration') == resources.library / 'expertise/orchestration.md'
outputs = compose_fleet(context.fleet, context.paths)
assert set(outputs) == {'manager', 'worker'}
for bot_id, directory in outputs.items():
    assert directory.is_relative_to(root)
    assert (directory / 'CLAUDE.md').stat().st_size > 0
    conf = (directory / 'bot.conf').read_text()
    for key, value in {'CLAUDLOBBY_ROOT': root, 'CLAUDLOBBY_NATIVE_DIR': resources.native,
                       'CLAUDLOBBY_CLI': cli}.items():
        assert f'export {key}={shlex.quote(str(value))}' in conf
    unit = plistlib.loads((directory / f'com.installed-smoke.{bot_id}.plist').read_bytes())
    assert unit['ProgramArguments'] == [str(resources.native / 'start-bot.sh'), str(directory)]
    assert unit['EnvironmentVariables']['CLAUDLOBBY_CLI'] == str(cli)
    assert unit['EnvironmentVariables']['CLAUDLOBBY_ROOT'] == str(root)
assert (outputs['worker'] / '.claude/skills/installed-smoke').resolve() == root / 'library/skills/installed-smoke'
""", data, package, cli, *dependencies], outside)
    assert _asset_hashes(package) == immutable_before
    assert not any((data / name).exists() for name in (".git", "pyproject.toml", "claudlobby", "lib"))
