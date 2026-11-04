"""Installed resource, composition and authoring smoke without service operation."""

import hashlib
from email.parser import BytesParser
from importlib import metadata as importlib_metadata
import json
import platform
import sysconfig
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest
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


def _source_installation(tmp_path):
    """A real tiny Git checkout, with no built resource/artifact copies."""
    from claudlobby.installation import Installation, write_installation
    installation = Installation(tmp_path / "installation", tmp_path / "persistent-data")
    source = installation.source
    for name in ("library/voices", "templates", "claudlobby/_runtime_scripts"):
        (source / name).mkdir(parents=True, exist_ok=True)
    for name in ("fleet.yaml.seed", "projects.yaml.seed", ".env.seed.example", "claudlobby/system.yaml"):
        (source / name).write_text("fixture\n")
    for name in ("__init__.py", "resources.py", "installation.py"):
        shutil.copy2(REPO / "claudlobby" / name, source / "claudlobby" / name)
    (source / "templates/candidate.txt").write_text("original template\n")
    (source / "claudlobby/_runtime_scripts/fixture.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "library/voices/example.md").write_text("example voice\n")
    _run(["git", "init", "--quiet", source], tmp_path)
    _run(["git", "-C", source, "add", "."], tmp_path)
    _run(["git", "-C", source, "-c", "user.name=Example", "-c", "user.email=example@example.invalid",
          "commit", "--quiet", "-m", "Fixture source"], tmp_path)
    installation.bin.mkdir()
    installation.cli.write_text("# fixture launcher\n")
    installation.data_root.mkdir()
    write_installation(installation)
    return installation


def test_source_resources_execute_from_explicit_installation_outside_checkout(tmp_path):
    installation = _source_installation(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    output = _run([sys.executable, "-I", "-B", "-c", """
import pathlib, sys
sys.path.insert(0, sys.argv[1])
from claudlobby.installation import read_installation
from claudlobby.resources import get_resources, selected_cli
installation = read_installation(pathlib.Path(sys.argv[2]))
resources = get_resources(installation=installation)
assert resources.library == installation.source / 'library'
assert resources.voices == resources.library / 'voices'
assert resources.templates == installation.source / 'templates'
assert resources.seeds == installation.source
assert resources.native == resources.runtime_native == installation.native
assert resources.runtime_library == installation.source / 'library'
assert resources.source_revision == installation.preflight()
assert resources.artifact_id == resources.content_sha256 == ''
assert selected_cli(installation=installation) == installation.cli
assert not (installation.source / 'claudlobby/_artifact.json').exists()
assert not (installation.source / 'claudlobby/_resources').exists()
print(resources.source_revision)
""", installation.source, installation.directory], outside)
    assert output.strip() == installation.preflight()


def test_source_installation_preflight_refuses_dirty_and_wrong_revision(tmp_path):
    from claudlobby.installation import InstallationError
    installation = _source_installation(tmp_path)
    revision = installation.preflight()
    with pytest.raises(InstallationError, match="revision"):
        installation.preflight(expected_revision="0" * len(revision))
    template = installation.source / "templates/candidate.txt"
    template.write_text("operator edit must survive\n")
    with pytest.raises(InstallationError, match="dirty"):
        installation.preflight()
    assert template.read_text() == "operator edit must survive\n"
    template.write_text("original template\n")
    (installation.source / "untracked.txt").write_text("untracked work\n")
    with pytest.raises(InstallationError, match="dirty"):
        installation.preflight()
    assert (installation.source / "untracked.txt").read_text() == "untracked work\n"


def test_candidate_resources_keep_final_runtime_paths(tmp_path):
    from claudlobby.resources import get_resources, selected_cli
    installation = _source_installation(tmp_path)
    candidate = tmp_path / "candidate"
    _run(["git", "clone", "--quiet", "--no-hardlinks", installation.source, candidate], tmp_path)
    (candidate / "templates/candidate.txt").write_text("candidate bytes\n")
    _run(["git", "-C", candidate, "add", "."], tmp_path)
    _run(["git", "-C", candidate, "-c", "user.name=Example", "-c", "user.email=example@example.invalid",
          "commit", "--quiet", "-m", "Candidate"], tmp_path)
    resources = get_resources(installation=installation, checkout=candidate)
    assert (resources.templates / "candidate.txt").read_text() == "candidate bytes\n"
    assert resources.native == candidate / "claudlobby/_runtime_scripts"
    assert resources.runtime_native == installation.native
    assert resources.runtime_library == installation.source / "library"
    assert resources.source_revision != installation.preflight()
    rendered_paths = f"{selected_cli(installation=installation)}\n{resources.runtime_native}\n{resources.runtime_library}\n"
    assert str(candidate) not in rendered_paths
    assert str(installation.bin) in rendered_paths and str(installation.source) in rendered_paths


def test_installation_selection_is_explicit_and_data_stays_separate(tmp_path, monkeypatch):
    from claudlobby.installation import Installation, InstallationError
    from claudlobby.resources import get_resources, selected_cli
    installation = _source_installation(tmp_path)
    monkeypatch.setenv("CLAUDLOBBY_INSTALLATION", str(installation.directory))
    monkeypatch.chdir(installation.data_root)
    assert get_resources().installation == installation
    assert selected_cli() == installation.cli
    monkeypatch.setenv("CLAUDLOBBY_INSTALLATION", str(tmp_path / "missing"))
    with pytest.raises(InstallationError):
        get_resources()
    for directory, data in ((installation.directory, installation.source / "data"),
                            (installation.data_root / "nested", installation.data_root)):
        with pytest.raises(InstallationError, match="separate"):
            Installation(directory, data)


def test_source_resource_reads_do_not_scan_dirty_checkout(tmp_path, monkeypatch):
    import claudlobby.installation as owner
    from claudlobby.resources import get_resources
    installation = _source_installation(tmp_path)
    (installation.source / "untracked.txt").write_text("pending operator work\n")
    commands = []
    run = owner.subprocess.run
    def recorded(argv, **kwargs):
        commands.append(argv)
        return run(argv, **kwargs)
    monkeypatch.setattr(owner.subprocess, "run", recorded)
    assert get_resources(installation=installation).source_revision
    assert not any("status" in command for command in commands)


def test_installation_record_cannot_rebind_data_or_accept_nested_git_root(tmp_path):
    from claudlobby.installation import Installation, InstallationError, write_installation
    installation = _source_installation(tmp_path)
    with pytest.raises(InstallationError, match="another data root"):
        write_installation(Installation(installation.directory, tmp_path / "other-data"))
    # Git's parent search must not silently accept a directory inside a repo.
    nested = Installation(installation.source / "nested-installation", tmp_path / "other-data")
    nested.source.mkdir(parents=True)
    with pytest.raises(InstallationError, match="exact Git checkout root"):
        nested.preflight()


def test_source_resources_refuse_unbound_candidate_and_escaping_resources(tmp_path):
    from claudlobby.installation import InstallationError
    from claudlobby.resources import get_resources
    installation = _source_installation(tmp_path)
    with pytest.raises(InstallationError, match="explicit installation"):
        get_resources(checkout=installation.source)
    templates = installation.source / "templates"
    shutil.rmtree(templates)
    outside = tmp_path / "foreign-templates"
    outside.mkdir()
    templates.symlink_to(outside, target_is_directory=True)
    with pytest.raises(InstallationError, match="resources are incomplete"):
        get_resources(installation=installation)


def _stable_launcher(tmp_path):
    from claudlobby.installation import install_launcher
    installation = _source_installation(tmp_path)
    install_launcher(installation, interpreter=Path(sys.executable).resolve())
    return installation


def test_stable_launcher_requires_explicit_host_interpreter(tmp_path):
    from claudlobby.installation import InstallationError, install_launcher
    installation = _source_installation(tmp_path)
    with pytest.raises(InstallationError, match="absolute host Python"):
        install_launcher(installation, interpreter=Path("python3"))
    assert installation.cli.read_text() == "# fixture launcher\n"


def _launch(installation, *args, **env):
    return subprocess.run([str(installation.cli), *map(str, args)], cwd=installation.data_root,
                          env=constructed_env(**env), capture_output=True, text=True, timeout=30)


def test_stable_launcher_executes_actual_source_cli_without_resource_copies(tmp_path):
    installation = _stable_launcher(tmp_path)
    shutil.copytree(REPO / "claudlobby", installation.source / "claudlobby", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("_artifact.json", "_resources", "__pycache__"))
    (installation.source / "claudlobby/__init__.py").write_text('__version__ = "source-fixture"\n')
    _run([sys.executable, "-m", "venv", "--without-pip", installation.environment], tmp_path)
    purelib = Path(_run([installation.environment / "bin/python", "-c",
                        "import sysconfig; print(sysconfig.get_path('purelib'))"], tmp_path).strip())
    # Reuse the already installed test dependencies, without an installer/network.
    (purelib / "test-dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n")
    outcome = _launch(installation, "--version")
    assert outcome.returncode == 0, outcome.stderr
    assert outcome.stdout == "claudlobby source-fixture\n"
    assert not (installation.source / "claudlobby/_artifact.json").exists()
    assert not (installation.source / "claudlobby/_resources").exists()
    (installation.data_root / "state").mkdir(exist_ok=True)
    (installation.data_root / "state/update.json").write_text('{"schema":1,"maintenance":false,"phase":"saving"}')
    # Quiescing bots still need the CLI until the updater raises maintenance.
    assert _launch(installation, "--version").stdout == "claudlobby source-fixture\n"


@pytest.mark.parametrize("record", [
    {"schema": 1, "maintenance": True, "phase": "stopping"},
    {"schema": 2, "maintenance": False, "phase": "complete"},
    {"schema": 1, "phase": "complete"},
    {"schema": 1, "maintenance": 0, "phase": "complete"},
    "not-json",
])
def test_stable_launcher_fences_before_loading_missing_source_or_environment(tmp_path, record):
    installation = _stable_launcher(tmp_path)
    (installation.data_root / "state").mkdir(exist_ok=True)
    (installation.data_root / "state/update.json").write_text(record if isinstance(record, str) else json.dumps(record))
    shutil.rmtree(installation.source)
    outcome = _launch(installation, "--version")
    assert outcome.returncode != 0
    assert "maintenance" in outcome.stderr and "Traceback" not in outcome.stderr
    assert not outcome.stdout


def test_stable_launcher_rejects_wrong_root_before_import(tmp_path):
    installation = _stable_launcher(tmp_path)
    other = tmp_path / "other-data"
    for args, env in ((("--root", other, "--version"), {}),
                      ((f"--root={other}", "--version"), {}),
                      (("--roo", other, "--version"), {}),
                      (("--version",), {"CLAUDLOBBY_ROOT": str(other)})):
        outcome = _launch(installation, *args, **env)
        assert outcome.returncode != 0 and "data root" in outcome.stderr
        assert "Traceback" not in outcome.stderr


@pytest.mark.parametrize("prefix", [(), ("--json",), ("--j",), ("--fl", "example"),
                                    ("--fl=example",), ("--verb",), ("--se",)],
                         ids=["plain", "json", "json-prefix", "fleet-prefix", "inline-fleet-prefix",
                              "verbose-prefix", "seed-prefix"])
def test_stable_launcher_update_route_is_independent_and_never_falls_back(tmp_path, prefix):
    installation = _stable_launcher(tmp_path)
    missing = _launch(installation, *prefix, "host", "update", "status")
    assert missing.returncode != 0 and "updater is unavailable" in missing.stderr
    updater = installation.bin / "claudlobby-update"
    updater.write_text(f"#!{Path(sys.executable).resolve()}\nimport sys\nprint('independent update ' + sys.argv[-1])\n")
    updater.chmod(0o755)
    shutil.rmtree(installation.source)
    (installation.data_root / "state").mkdir(exist_ok=True)
    (installation.data_root / "state/update.json").write_text('{"schema":1,"maintenance":true,"phase":"cutover"}')
    outcome = _launch(installation, *prefix, "host", "update", "status")
    assert outcome.returncode == 0 and outcome.stdout == "independent update status\n"
    for action in ("runtime", "siblings"):
        outcome = _launch(installation, *prefix, "host", "update", action)
        assert outcome.returncode != 0 and "maintenance" in outcome.stderr
        assert "independent update" not in outcome.stdout


def _wheel_payload(path):
    with zipfile.ZipFile(path) as wheel:
        return {
            item.filename: (wheel.read(item), (item.external_attr >> 16) & 0o111)
            for item in wheel.infolist() if item.filename.startswith("claudlobby/")
        }


def _asset_hashes(package):
    paths = [package / "system.yaml", package / "_artifact.json"]
    for directory in ("_resources", "_runtime_scripts"):
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
    for directory in ("claudlobby", "library", "templates", "missions", "examples"):
        shutil.copytree(REPO / directory, source / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_artifact.json", "_resources"))
    for name in ("pyproject.toml", "setup.py", "README.md", ".gitignore", "fleet.yaml.seed", "fleet.yaml.example",
                 "projects.yaml.seed", ".env.seed.example"):
        shutil.copy2(REPO / name, source / name)
    removed_module = source / "claudlobby/_removed_for_build_smoke.py"
    removed_module.write_text("REMOVED_BUILD_SMOKE = True\n")
    # A private local revision lets the real bundle consumer validate its wheel identity.
    _run(["git", "init", "--quiet"], source)
    _run(["git", "add", "."], source)
    assert "examples/teams/revenue-web-app/fleet.yaml" in _run(["git", "ls-files"], source).splitlines()
    _run(["git", "-c", "user.name=Build fixture", "-c", "user.email=fixture@example.invalid",
          "commit", "--quiet", "-m", "Record isolated package fixture"], source)
    ignored = ("voices/local/operator.md", "library/skills/printify/config.json",
               "claudlobby/_runtime_scripts/fleet-state.json", "examples/teams/private/fleet.yaml")
    for name in ignored:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE-OPERATOR-FIXTURE-DO-NOT-PACKAGE\n")
    assert _run(["git", "check-ignore", "examples/teams/private/fleet.yaml"], source).strip() == "examples/teams/private/fleet.yaml"

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
    for directory in ("library", "templates"):
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
    # Voices are library content (#2150): they ship there and nowhere else.
    assert "claudlobby/_resources/library/voices/vito-corleone.md" in payload
    assert not any(name.startswith("claudlobby/_resources/voices/") for name in payload)
    for name in ("fleet.yaml.seed", "fleet.yaml.example", "projects.yaml.seed", ".env.seed.example",
                 "missions/fleet.md.seed", "examples/teams/revenue-web-app/fleet.yaml"):
        assert payload["claudlobby/_resources/seeds/" + name][0] == (source / name).read_bytes()
        source_inputs.add(source / name)
    assert payload["claudlobby/system.yaml"][0] == (source / "claudlobby/system.yaml").read_bytes()

    native = {name.removeprefix("claudlobby/_runtime_scripts/") for name in payload
              if name.startswith("claudlobby/_runtime_scripts/")}
    # Runtime scripts install at their authored package path, never twice.
    assert not any(name.startswith("claudlobby/_native/") for name in payload)
    with zipfile.ZipFile(wheel) as archive:
        assert not any(name.startswith("lib/") for name in archive.namelist())
    assert {"keepalive.sh", "lib-common.sh", "supervisor.sh", "git-credential-github-app",
            "env-tiers.sh", "plane-socket-client.py"} <= native
    assert not native.intersection({"setup-fleet", "setup-fleets"})
    assert not any(name.startswith(("rehearse-", "ab-", "boot-strand-", "plane-canary-"))
                   for name in native)
    assert not native.intersection({
        "CLAUDE.md", "coldstart-harness.sh", "freshbox-boot-gate.sh",
        "naked-bot-observe.py", "plane-durability-driver.py", "send-size-probe.sh",
        "validate-bot-change.sh", "vault-git-base-rate.py", "personal/finance-presync.sh",
    })
    for name in native:
        path = source / "claudlobby/_runtime_scripts" / name
        assert payload["claudlobby/_runtime_scripts/" + name] == (path.read_bytes(), path.stat().st_mode & 0o111)
        source_inputs.add(path)
    for name in payload:
        if not name.startswith(("claudlobby/_resources/", "claudlobby/_runtime_scripts/")):
            if name != "claudlobby/_artifact.json":
                source_inputs.add(source / name)

    metadata = json.loads(payload["claudlobby/_artifact.json"][0])
    digest = hashlib.sha256()
    for path in sorted(source_inputs):
        record = [path.relative_to(source).as_posix(), path.stat().st_mode & 0o111,
                  hashlib.sha256(path.read_bytes()).hexdigest()]
        digest.update((json.dumps(record, separators=(",", ":")) + "\n").encode())
    assert metadata["content_sha256"] == digest.hexdigest()
    revision = _run(["git", "rev-parse", "HEAD"], source).strip()
    assert metadata["source_revision"] == revision
    assert metadata["artifact_id"] == revision + "-" + digest.hexdigest()
    assert "release_id" not in metadata
    assert not set(metadata["resource_sources"]).intersection(ignored)
    with tarfile.open(next(rebuilt.glob("*.tar.gz"))) as archive:
        prefix = archive.getnames()[0].split("/")[0] + "/"
        assert json.load(archive.extractfile(prefix + "claudlobby/_artifact.json")) == metadata
        assert all(prefix + path.relative_to(source).as_posix() in archive.getnames()
                   for path in source_inputs)
        assert prefix + "bin/claudlobby" not in archive.getnames()
        assert not any(prefix + name in archive.getnames() for name in ignored)
        assert not any(prefix + "claudlobby/_runtime_scripts/" + name in archive.getnames()
                       for name in ("CLAUDE.md", "personal/finance-presync.sh"))
        assert len(archive.getnames()) == len(set(archive.getnames()))

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
assert (r.seeds / 'examples/teams/revenue-web-app/fleet.yaml').is_file()
assert not {'yaml', 'jinja2', 'pydantic'} & sys.modules.keys()
assert not hasattr(r, 'release_id')
print(json.dumps({'artifact_id': r.artifact_id, 'content_sha256': r.content_sha256}))
"""
    installed_identity = json.loads(_run(
        [python, "-I", "-S", "-B", "-c", script, installed], tmp_path))
    assert installed_identity == {key: metadata[key] for key in installed_identity}
    # The installed consumer works with the source tree unavailable, not by
    # resolving a nearby repository recipe.
    _run([python, "-I", "-B", "-c", """
from pathlib import Path
from claudlobby.commands.setup import _template_config
from claudlobby.config import load_fleet_snapshot
content = _template_config('revenue-web-app', 'shop', 'example-org/shop', 'Improve checkout tests')
fleet, _ = load_fleet_snapshot(Path('/unused/fleet.yaml'), content, None)
assert fleet.name == 'shop' and fleet.service_prefix == 'com.claudlobby.shop'
assert set(fleet.bots) == {'lead', 'engineer', 'reviewer'}
assert all(bot.scope.repos == ['example-org/shop'] for bot in fleet.bots.values())
assert 'Improve checkout tests' in fleet.mission
assert all(not bot.channels and not bot.remote_control for bot in fleet.bots.values())
"""], tmp_path)
    template_help = _run([cli, "fleet", "setup", "--help"], tmp_path)
    assert "--template {revenue-web-app}" in template_help and "--repository OWNER/REPO" in template_help

    # Real first-command observation from a synthetic local bundle. Reuse the
    # installed core dependency closure above; no registry/manager/provider calls.
    bundle = tmp_path / "trusted-bundle"
    bundle.mkdir()
    (bundle / "bootstrap.py").write_bytes((installed / "claudlobby/bootstrap.py").read_bytes())
    shutil.copy2(wheel, bundle / wheel.name)
    wheelhouse = bundle / "wheelhouse"
    wheelhouse.mkdir()
    pins = []
    for distribution in importlib_metadata.distributions(path=[str(installed)]):
        if canonicalize_name(distribution.metadata["Name"]) not in dependencies:
            continue
        tag = next(line[5:] for line in distribution.read_text("WHEEL").splitlines() if line.startswith("Tag: "))
        name = canonicalize_name(distribution.metadata["Name"]).replace("-", "_")
        dependency = wheelhouse / f"{name}-{distribution.version}-{tag}.whl"
        with zipfile.ZipFile(dependency, "w") as archive:
            for relative in distribution.files:
                path = Path(distribution.locate_file(relative))
                if path.is_file() and path.resolve().is_relative_to(installed.resolve()):
                    archive.write(path, Path(relative).as_posix())
        pins.append(f"{distribution.metadata['Name']}=={distribution.version} --hash=sha256:{hashlib.sha256(dependency.read_bytes()).hexdigest()}")
    assert pins
    (bundle / "dependency.lock").write_text("\n".join(sorted(pins)) + "\n")
    (bundle / "rehearsal.json").write_text(json.dumps({"passed": True, "source_revision": metadata["source_revision"],
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}))
    (bundle / "built-on.txt").write_text(f"python {platform.python_version()}\nplatform {sysconfig.get_platform()}\nlibc unknown\n")
    files = sorted(path for path in bundle.rglob("*") if path.is_file())
    (bundle / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{path.relative_to(bundle).as_posix()}\n" for path in files))
    host_root = (tmp_path / "no-native-host").resolve()
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    # Canonical native inventory refuses without launchctl/systemctl on PATH.
    # This reaches the actual installed CLI but cannot observe the real manager.
    outcome = subprocess.run([sys.executable, "-I", "-B", str(bundle / "bootstrap.py"), "--root", str(host_root), "--json"],
        cwd=tmp_path, env=constructed_env(PATH=str(empty_path)), text=True, capture_output=True, timeout=180)
    result = json.loads(outcome.stdout)
    supported = sys.version_info[:2] == (3, 11) and ((sys.platform == "darwin" and platform.machine() == "arm64")
        or (sys.platform == "linux" and platform.machine() == "aarch64"))
    assert not result["ok"] and not host_root.exists()
    assert result["error"]["code"] == ("unavailable" if supported else "invalid_bundle")
    assert outcome.returncode == (6 if supported else 2)
    print("offline bootstrap observation:", json.dumps({"supported_host": supported, "exit": outcome.returncode, "canonical_error": result["error"], "root_absent": not host_root.exists()}, sort_keys=True))
    assert not list(Path(__import__("os").environ["TMPDIR"]).glob("claudlobby-bootstrap-*"))

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
    _run([cli, "--root", data, "library", "create", "--kind", "skill", "--name", "installed-smoke",
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
from claudlobby.plane.owner_access import VerifiedReader
from claudlobby.plane.owner_messages import OwnerMessages
assert "starlette" not in sys.modules
from claudlobby.resources import get_resources, selected_cli
root, package, cli = map(pathlib.Path, sys.argv[1:4])
assert pathlib.Path(claudlobby.__file__).resolve().parent == package.resolve()
for name in sys.argv[4:]:
    distribution = importlib.metadata.distribution(name)
    assert pathlib.Path(distribution.locate_file('')).resolve() == package.parent.resolve()
resources = get_resources()
assert resources.native == package / '_runtime_scripts'
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
