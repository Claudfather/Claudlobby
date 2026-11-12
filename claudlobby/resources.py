"""Explicit source resources, with installed-artifact support during cutover.

Git resources require a recorded installation, never cwd discovery. Candidate
reads may use a temporary checkout while runtime destinations stay stable.
Neither source provenance nor a resource path grants operational authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys

from .installation import Installation, InstallationError, checkout_revision, read_installation


@dataclass(frozen=True)
class PackageResources:
    library: Path
    voices: Path
    templates: Path
    seeds: Path
    native: Path
    system_yaml: Path
    artifact_id: str
    source_revision: str | None
    content_sha256: str
    installation: Installation | None = None

    @property
    def runtime_native(self) -> Path:
        """Final generated-unit destination, even during candidate rendering."""
        return self.installation.native if self.installation is not None else self.native

    @property
    def runtime_library(self) -> Path:
        """Final library reference; library itself is the selected read tree."""
        return self.installation.source / "library" if self.installation is not None else self.library


def _installation(explicit: Installation | None) -> Installation | None:
    if explicit is not None:
        return explicit
    selected = os.environ.get("CLAUDLOBBY_INSTALLATION")
    return read_installation(Path(selected)) if selected is not None else None


def selected_cli(*, installation: Installation | None = None) -> Path:
    """Stable installation CLI or transitional interpreter CLI, never PATH.

    Keep the venv spelling of sys.executable: resolving its interpreter symlink
    would select the base Python installation instead of the active release.
    """
    installation = _installation(installation)
    executable = installation.cli if installation is not None else Path(sys.executable).absolute().parent / "claudlobby"
    if not executable.is_file():
        raise RuntimeError(f"Selected release has no CLI entrypoint: {executable}")
    return executable


def get_resources(*, installation: Installation | None = None,
                  checkout: Path | None = None) -> PackageResources:
    """Resolve explicit source or transitional installed resources using stdlib.

    Ordinary reads do not scan working files. The update owner calls installation
    preflight before effects; Git HEAD is provenance, not a seal over dirty code.
    """
    installation = _installation(installation)
    if installation is not None:
        source = installation.source if checkout is None else Path(checkout)
        revision = checkout_revision(source)
        paths = [source / name for name in ("library", "library/voices", "templates")]
        paths.extend((source, source / "claudlobby/_runtime_scripts", source / "claudlobby/system.yaml"))
        seeds = [source / name for name in ("fleet.yaml.seed", "projects.yaml.seed", ".env.seed.example")]
        if (any(not path.is_dir() for path in paths[:-1]) or not paths[-1].is_file()
                or any(not path.is_file() for path in seeds)
                or any(not path.resolve().is_relative_to(source) for path in [*paths, *seeds])):
            raise InstallationError("Git checkout runtime resources are incomplete")
        return PackageResources(*paths, artifact_id="", source_revision=revision,
                                content_sha256="", installation=installation)
    if checkout is not None:
        raise InstallationError("candidate checkout requires an explicit installation")
    package = Path(__file__).resolve().parent
    try:
        metadata = json.loads((package / "_artifact.json").read_text())
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Claudlobby release resources are not built. Install a wheel or sdist "
            "into the selected release; an editable checkout is not a release."
        ) from exc
    assets = package / "_resources"
    paths = [assets / name for name in ("library", "library/voices", "templates", "seeds")]
    paths.extend((package / "_runtime_scripts", package / "system.yaml"))
    if metadata.get("schema") != 1 or any(not path.exists() for path in paths):
        raise RuntimeError("Claudlobby release resources are incomplete or unsupported")
    return PackageResources(*paths, artifact_id=metadata["artifact_id"],
                            source_revision=metadata["source_revision"],
                            content_sha256=metadata["content_sha256"])
