"""Relative component provenance and attribution of final rendered marker spans.

This is not source-tree provenance (#953) or a second prompt budget (#1603).
Unmarked template content stays explicitly unattributed; missing/broken markers
never fall back to measuring input files as though they were rendered output.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
import os
import re
from urllib.parse import quote

from .prompt_budget import measure

END_MARKER = "<!-- /claudlobby:source -->"
_MARKERS = re.compile(
    r"(?m)^<!-- claudlobby:source (?P<source>[^\r\n<>]+) -->(?:\r\n|\n|\r)"
    r"|(?:\r\n|\n|\r)<!-- /claudlobby:source -->"
)


def source_label(path: Path, paths) -> str:
    """Name the selected logical source tier/path, never an absolute host path.

    Keep the lookup path rather than resolving symlinks to host-specific targets.
    Percent escaping prevents a filename from closing or adding a comment line.
    """
    path = Path(os.path.abspath(path))
    for tier, root in (("fleet", paths.fleet_dir), ("shared", paths.root)):
        if root is None:
            continue
        for kind in ("library", "voices", "templates"):
            try:
                relative = path.relative_to(Path(os.path.abspath(root / kind)))
            except ValueError:
                continue
            return f"{tier}/{kind}/" + quote(relative.as_posix(), safe="/-._")
    return "unknown/" + quote(path.name, safe="-._")


def start_marker(path: Path, paths) -> str:
    return f"<!-- claudlobby:source {source_label(path, paths)} -->"


def source_block(body: str, path: Path, paths) -> str:
    """Wrap a nonempty expertise body; removing only markers restores its bytes."""
    return f"{start_marker(path, paths)}\n{body}\n{END_MARKER}" if body else body


def template_label(paths) -> str:
    if paths.fleet_dir:
        overlay = paths.fleet_dir / "templates/claude.md.j2"
        if overlay.is_file():
            return source_label(overlay, paths)
    return source_label(paths.root / "templates/claude.md.j2", paths)


@dataclass(frozen=True)
class Component:
    source: str
    start_line: int
    end_line: int
    nbytes: int
    marker_bytes: int


@dataclass(frozen=True)
class Attribution:
    total_bytes: int
    components: tuple[Component, ...]
    unattributed_bytes: int
    available: bool
    reason: str = ""


def attribute(markdown: str) -> Attribution:
    """Attribute actual final spans, including their two marker lines."""
    total = len(markdown.encode("utf-8"))
    line_starts = [0] + [m.end() for m in re.finditer(r"\r\n|\r|\n", markdown)]
    components = []
    opened = None
    reason = ""
    for marker in _MARKERS.finditer(markdown):
        if marker["source"] is not None:
            if opened is not None:
                reason = "nested source markers"
                break
            opened = marker
        elif opened is None:
            reason = "unmatched end marker"
            break
        else:
            block = markdown[opened.start():marker.end()]
            overhead = opened.group() + marker.group()
            components.append(Component(
                opened["source"], bisect_right(line_starts, opened.start()),
                bisect_right(line_starts, marker.end()), len(block.encode("utf-8")),
                len(overhead.encode("utf-8"))))
            opened = None
    if opened is not None and not reason:
        reason = "unclosed source marker"
    if not components and not reason:
        reason = "no source markers (custom template attribution unsupported)"
    if reason:
        return Attribution(total, (), total, False, reason)
    return Attribution(total, tuple(components), total - sum(c.nbytes for c in components), True)


def duplicate_heading_sources(markdown: str, template_source: str) -> list[str]:
    """One diagnostic per repeated H2, retaining every source and line number."""
    attributed = attribute(markdown)
    groups: dict[str, list[str]] = {}
    for section in measure(markdown).sections:
        source = f"{template_source} (template/unattributed)"
        for component in attributed.components:
            if component.start_line <= section.start_line <= component.end_line:
                source = component.source
                break
        groups.setdefault(section.name, []).append(f"{source} line {section.start_line}")
    return [f"duplicate H2 '{name[:120]}': " + "; ".join(sources)
            for name, sources in groups.items() if len(sources) > 1]
