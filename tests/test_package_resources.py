"""Installed resource smoke, including the source-distribution rebuild route.

This deliberately does not claim outside-checkout composition or native service
operation: their callers move to the resource seam together in P2.
"""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

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


def test_installed_resources_match_direct_and_sdist_wheels(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for directory in ("claudlobby", "lib", "library", "voices", "templates", "missions"):
        shutil.copytree(REPO / directory, source / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("pyproject.toml", "setup.py", "README.md", ".gitignore", "fleet.yaml.seed",
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
    for name in ("fleet.yaml.seed", "projects.yaml.seed", ".env.seed.example",
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

    installed = tmp_path / "installed"
    _run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
          "--no-compile", "--target", installed, wheel], tmp_path)
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
        [sys.executable, "-I", "-S", "-B", "-c", script, installed], tmp_path))
    assert installed_identity == {key: metadata[key] for key in installed_identity}
