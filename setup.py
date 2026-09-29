"""Stage immutable runtime assets from their canonical sources at build time.

P1 packaging only: callers still use the checkout until P2 switches context,
authoring and generated units together. No generated copies belong in source.
The artifact ID identifies these sources and assets, not an installed host
release. P2's release assembler must bind the dependency-lock digest and exact
installed package/runtime assembly into the host release identity.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path
import runpy
import shutil
import subprocess

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist
from setuptools.errors import SetupError


ROOT = Path(__file__).resolve().parent
# Load only the stdlib version owner, never application/dependency imports.
RUNTIME_COMPATIBILITY = runpy.run_path(
    str(ROOT / "claudlobby/runtime_versions.py")
)["runtime_declaration"]()
ASSET_DIRS = ("library", "voices", "templates")
SEEDS = ("fleet.yaml.seed", "fleet.yaml.example", "projects.yaml.seed", ".env.seed.example",
         "missions/fleet.md.seed")
# Development and measurement instruments live in harness/ and never enter
# the installed native runtime. These two remaining lib sources are likewise
# not runtime dependencies.
NATIVE_EXCLUDED = {"CLAUDE.md", "personal/finance-presync.sh"}


@cache
def _resource_sources():
    """Use authored files only; never discover operator data recursively."""
    if (ROOT / ".git").exists():
        try:
            output = subprocess.check_output(
                ["git", "ls-files", "-z", "--", *ASSET_DIRS, "lib", *SEEDS],
                cwd=ROOT, text=True, stderr=subprocess.PIPE, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SetupError("Cannot read the canonical resource Git index") from exc
        names = output.rstrip("\0").split("\0") if output else []
    else:
        frozen = ROOT / "claudlobby/_artifact.json"
        if not frozen.is_file():
            raise SetupError("Resource builds require a Git index or a built sdist "
                             "with a frozen resource inventory")
        names = json.loads(frozen.read_text()).get("resource_sources")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise SetupError("sdist has no valid frozen resource inventory")
    sources = []
    for name in sorted(set(names)):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise SetupError(f"Resource path escapes source tree: {name}")
        if relative.parts[:1] == ("lib",):
            if relative.relative_to("lib").as_posix() in NATIVE_EXCLUDED:
                continue
        elif name not in SEEDS and relative.parts[:1] not in [(d,) for d in ASSET_DIRS]:
            raise SetupError(f"Unexpected resource source: {name}")
        source = ROOT / relative
        if not source.resolve().is_relative_to(ROOT):
            raise SetupError(f"Resource symlink escapes source tree: {name}")
        if not source.is_file():
            raise SetupError(f"Resource source is missing or not a file: {name}")
        sources.append(name)
    for required in SEEDS:
        if required not in sources:
            raise SetupError(f"Resource inventory is missing seed: {required}")
    for directory in (*ASSET_DIRS, "lib"):
        if not any(name.startswith(directory + "/") for name in sources):
            raise SetupError(f"Resource inventory is missing directory: {directory}")
    return tuple(sources)


def _assets():
    """Pairs of canonical source and installed package-relative destination."""
    for name in _resource_sources():
        source = ROOT / name
        if name.startswith("lib/"):
            target = Path("_native") / source.relative_to(ROOT / "lib")
        elif name in SEEDS:
            target = Path("_resources/seeds") / name
        else:
            target = Path("_resources") / name
        yield source, target


def _artifact_metadata(sources):
    # Include paths and executable bits as well as bytes. Git is provenance at
    # build time only; exports without Git have an equally content-bound ID.
    digest = hashlib.sha256()
    for source in sorted(set(sources)):
        record = [source.relative_to(ROOT).as_posix(),
                  source.stat().st_mode & 0o111,
                  hashlib.sha256(source.read_bytes()).hexdigest()]
        digest.update((json.dumps(record, separators=(",", ":")) + "\n").encode())
    content_hash = digest.hexdigest()
    frozen = ROOT / "claudlobby/_artifact.json"
    if frozen.is_file():
        metadata = json.loads(frozen.read_text())
        if metadata.get("content_sha256") != content_hash:
            raise SetupError("sdist contents differ from their frozen artifact identity")
        if metadata.get("compatibility") != RUNTIME_COMPATIBILITY:
            raise SetupError("sdist runtime compatibility differs from its version owner")
        return metadata
    revision = None
    if (ROOT / ".git").exists():
        try:
            revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                stderr=subprocess.DEVNULL, timeout=5,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return {"schema": 1, "source_revision": revision,
            "compatibility": RUNTIME_COMPATIBILITY,
            "content_sha256": content_hash,
            "resource_sources": list(_resource_sources()),
            "artifact_id": f"{revision or 'source'}-{content_hash}"}


def _write_metadata(destination, metadata):
    destination.write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n")


class ResourceBuild(build_py):
    def get_source_files(self):
        # sdist consumes this inventory too, so rebuilding it needs no checkout.
        paths = set(super().get_source_files())
        paths.update(str(source.relative_to(ROOT)) for source, _ in _assets())
        paths.add("claudlobby/system.yaml")
        paths.update(str(path.relative_to(ROOT)) for path in
                     (ROOT / "claudlobby/plane/migrations").glob("*.sql"))
        paths.update(str(path.relative_to(ROOT)) for path in
                     (ROOT / "claudlobby/plane/ui").glob("*") if path.is_file())
        paths.update(("pyproject.toml", "setup.py"))
        return sorted(paths)

    def artifact_metadata(self):
        return _artifact_metadata(ROOT / path for path in self.get_source_files())

    def run(self):
        if self.editable_mode:
            # Editable imports use source; they deliberately have no artifact ID.
            return
        package = Path(self.build_lib) / "claudlobby"
        if not package.is_absolute():
            package = ROOT / package
        # Clear only this package's conventional build output. Otherwise a
        # removed Python module survives into a wheel with a different digest.
        if not package.is_relative_to(ROOT / "build") or ".." in package.parts:
            raise SetupError("Resource build output must be inside this source's build/ tree")
        ancestor = package
        while ancestor != ROOT:
            if ancestor.is_symlink():
                raise SetupError(f"Resource build output traverses a symlink: {ancestor}")
            ancestor = ancestor.parent
        if package.exists():
            shutil.rmtree(package)
        super().run()
        for source, relative in _assets():
            target = package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        _write_metadata(package / "_artifact.json", self.artifact_metadata())


class ResourceSdist(sdist):
    def make_release_tree(self, base_dir, files):
        metadata = self.get_finalized_command("build_py").artifact_metadata()
        # An old egg-info/SOURCES.txt can retain entries from a prior build.
        canonical = set(_resource_sources())
        files = [name for name in files if Path(name).parts[0] not in (*ASSET_DIRS, "lib")
                 or name in canonical]
        super().make_release_tree(base_dir, files)
        _write_metadata(Path(base_dir) / "claudlobby/_artifact.json", metadata)


setup(cmdclass={"build_py": ResourceBuild, "sdist": ResourceSdist})
