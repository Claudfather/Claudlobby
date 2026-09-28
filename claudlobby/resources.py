"""Stable paths and artifact identity for an installed, unpacked package.

Generated units must keep these real paths after composition exits. Zip imports
and unbuilt source checkouts are deliberately unsupported; there is no temporary
resource extraction or fallback to a nearby checkout. P2 switches runtime users
to this seam together with the writable data/source boundary.

The artifact ID binds source and resource contents only. P2's release assembler
owns the dependency-lock digest and exact installed package/runtime assembly
identity; this module does not claim to identify a complete host release.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys


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


def selected_cli() -> Path:
    """Console entrypoint in this interpreter's release, never ambient PATH.

    Keep the venv spelling of sys.executable: resolving its interpreter symlink
    would select the base Python installation instead of the active release.
    """
    executable = Path(sys.executable).absolute().parent / "claudlobby"
    if not executable.is_file():
        raise RuntimeError(f"Selected release has no CLI entrypoint: {executable}")
    return executable


def get_resources() -> PackageResources:
    """Read the installed build identity without importing compositor deps."""
    package = Path(__file__).resolve().parent
    try:
        metadata = json.loads((package / "_artifact.json").read_text())
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Claudlobby release resources are not built. Install a wheel or sdist "
            "into the selected release; an editable checkout is not a release."
        ) from exc
    assets = package / "_resources"
    paths = [assets / name for name in ("library", "voices", "templates", "seeds")]
    paths.extend((package / "_native", package / "system.yaml"))
    if metadata.get("schema") != 1 or any(not path.exists() for path in paths):
        raise RuntimeError("Claudlobby release resources are incomplete or unsupported")
    return PackageResources(*paths, artifact_id=metadata["artifact_id"],
                            source_revision=metadata["source_revision"],
                            content_sha256=metadata["content_sha256"])
