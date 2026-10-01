"""Explicit source assets for tests; never production resource discovery."""

from pathlib import Path

from claudlobby.resources import PackageResources


def source_package() -> PackageResources:
    """Select this isolated test checkout's assets as its package base."""
    root = Path(__file__).resolve().parents[1]
    return PackageResources(
        library=root / "library",
        voices=root / "voices",
        templates=root / "templates",
        seeds=root,
        native=root / "claudlobby" / "_runtime_scripts",
        system_yaml=root / "claudlobby" / "system.yaml",
        artifact_id="test-source",
        source_revision=None,
        content_sha256="",
    )
