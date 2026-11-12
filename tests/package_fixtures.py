"""Explicit source assets for tests; never production resource discovery."""

from pathlib import Path
from dataclasses import replace

from claudlobby.installation import Installation
from claudlobby.resources import PackageResources


def source_package() -> PackageResources:
    """Select this isolated test checkout's assets as its package base."""
    root = Path(__file__).resolve().parents[1]
    return PackageResources(
        library=root / "library",
        voices=root / "library" / "voices",
        templates=root / "templates",
        seeds=root,
        native=root / "claudlobby" / "_runtime_scripts",
        system_yaml=root / "claudlobby" / "system.yaml",
        artifact_id="test-source",
        source_revision=None,
        content_sha256="",
    )


def git_package(root: Path) -> PackageResources:
    """Bind pure renderer tests to private managed paths; never launch a runtime.

    Operational tests use current_runtime, which prepares actual config and
    identities. This entry intentionally refuses execution.
    """
    root = Path(root).absolute()
    installation = Installation(root.parent / (".render-installation-" + root.name), root)
    installation.bin.mkdir(parents=True, exist_ok=True)
    installation.cli.write_text("#!/bin/sh\nexit 99\n")
    installation.cli.chmod(0o755)
    return replace(source_package(), installation=installation,
                   artifact_id="", source_revision="a" * 40)
