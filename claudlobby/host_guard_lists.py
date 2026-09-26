"""Shared host mention-guard inputs, with a conservative read-only preview.

Generation deliberately skips unreadable sibling manifests. A preview must
instead disclose incomplete coverage: the resulting narrower list is not proof
that the host's guard is unchanged. Selection/rendering remain shared; only the
preview's admissible filesystem and manifest shapes are stricter.
"""
from __future__ import annotations

import difflib
import re
import stat
from pathlib import Path

import yaml

from .paths import Paths, _iter_fleet_dirs

_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_FILES = ("bot-handles", "mention-allowlist")


class PreviewUnavailable(ValueError):
    """The source or runtime cannot support a complete, read-only comparison."""


def _kind(path: Path, root: Path) -> str:
    """Classify without following a link; never include raw exception content."""
    label = path.relative_to(root).as_posix()
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "absent"
    except OSError:
        raise PreviewUnavailable(f"{label}: unreadable metadata") from None
    if stat.S_ISLNK(mode):
        raise PreviewUnavailable(f"{label}: symlink topology is unsupported")
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    raise PreviewUnavailable(f"{label}: unsupported file type")


def _children(path: Path, root: Path) -> list[Path]:
    try:
        return sorted(path.iterdir())
    except OSError:
        raise PreviewUnavailable(
            f"{path.relative_to(root)}: unreadable directory"
        ) from None


def _checked_fleet_dirs(paths: Paths) -> list[Path]:
    """Preflight the existing depth-two discovery door before it can follow links.

    This validates the same candidate domain, then uses the writer's enumerator.
    It is not a filesystem snapshot or a concurrency/security boundary: an
    operator must keep inputs stable between preview and generation.
    """
    root = paths.root
    local = root / "local"
    kind = _kind(local, root)
    if kind == "absent":
        return []
    if kind != "directory":
        raise PreviewUnavailable("local: expected a directory")
    for entry in _children(local, root):
        if _kind(entry, root) != "directory":
            continue
        manifest_kind = _kind(entry / "fleet.yaml", root)
        if manifest_kind == "file":
            continue
        if manifest_kind != "absent":
            raise PreviewUnavailable(f"{entry.relative_to(root)}/fleet.yaml: not a file")
        for child in _children(entry, root):
            if _kind(child, root) != "directory":
                continue
            if _kind(child / "fleet.yaml", root) not in ("absent", "file"):
                raise PreviewUnavailable(f"{child.relative_to(root)}/fleet.yaml: not a file")
    return list(_iter_fleet_dirs(local))


def _declared_names(data: object, filename: str, *, preview: bool):
    if preview:
        if not isinstance(data, dict):
            raise PreviewUnavailable("manifest root is not a mapping")
        fleet = data.get("fleet")
        if fleet is None:
            fleet = {}
        if not isinstance(fleet, dict):
            raise PreviewUnavailable("fleet is not a mapping")
        if filename == "bot-handles":
            value = fleet.get("bots")
            if value is None:
                value = {}
            if not isinstance(value, dict) or any(not isinstance(n, str) for n in value):
                raise PreviewUnavailable("bots is not a mapping with string names")
        else:
            github = fleet.get("github")
            if github is None:
                github = {}
            if not isinstance(github, dict):
                raise PreviewUnavailable("github is not a mapping")
            value = github.get("mention_allowlist")
            if value is None:
                value = []
            if not isinstance(value, list) or any(not isinstance(n, str) for n in value):
                raise PreviewUnavailable("mention_allowlist is not a list of strings")
    # Preserve generation's existing skip/type semantics exactly. The stricter
    # shape gate above belongs only to the preview, never to another writer.
    if isinstance(data, dict):
        fleet = data.get("fleet") or {}
        if filename == "bot-handles":
            return fleet.get("bots") or {}
        github = fleet.get("github") or {}
        if isinstance(github, dict):
            return github.get("mention_allowlist") or []
    return ()


def collect_host_guard_names(paths: Paths, filename: str, *, preview: bool = False) -> set[str]:
    """Collect one host guard list without writing anything."""
    if filename not in _FILES:
        raise ValueError("unknown host guard list")
    directories = (_checked_fleet_dirs(paths) if preview
                   else _iter_fleet_dirs(paths.root / "local"))
    names: set[str] = set()
    for fleet_dir in directories:
        manifest = fleet_dir / "fleet.yaml"
        if not manifest.is_file():
            continue
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            if data is None or not preview:
                data = data or {}
        except (OSError, yaml.YAMLError):
            if preview:
                raise PreviewUnavailable(
                    f"{manifest.relative_to(paths.root)}: unreadable or invalid YAML"
                ) from None
            continue
        except UnicodeError:
            if preview:
                raise PreviewUnavailable(
                    f"{manifest.relative_to(paths.root)}: invalid text encoding"
                ) from None
            raise
        try:
            names.update(_declared_names(data, filename, preview=preview))
        except PreviewUnavailable as exc:
            raise PreviewUnavailable(f"{manifest.relative_to(paths.root)}: {exc}") from None
    return names


def render_host_guard_names(names: set[str]) -> str:
    """Keep the writer's exact filter and sorted, deduplicated line format."""
    safe = sorted(n for n in names if _HANDLE_RE.match(n or ""))
    return "".join(f"{n}\n" for n in safe)


def _runtime_text(paths: Paths, filename: str) -> str | None:
    for path in (paths.root / "runtime", paths.root / "runtime" / "_host"):
        kind = _kind(path, paths.root)
        if kind == "absent":
            return None
        if kind != "directory":
            raise PreviewUnavailable(f"{path.relative_to(paths.root)}: not a directory")
    target = paths.root / "runtime" / "_host" / filename
    kind = _kind(target, paths.root)
    if kind == "absent":
        return None
    if kind != "file":
        raise PreviewUnavailable(f"runtime/_host/{filename}: not a file")
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise PreviewUnavailable(f"runtime/_host/{filename}: unreadable text") from None


def diff_host_guard_lists(paths: Paths) -> str:
    """Report both host files once, including for ``diff --bot``."""
    lines = ["\n=== Host guard lists (host-wide, including generate --bot) ===",
             "Hooks read these lists on demand; generation changes the next use, with no restart gate."]
    for filename in _FILES:
        try:
            expected = render_host_guard_names(collect_host_guard_names(
                paths, filename, preview=True,
            ))
            actual = _runtime_text(paths, filename)
        except PreviewUnavailable as exc:
            lines.append(f"{filename}: preview unavailable — {exc}")
            continue
        if actual == expected:
            lines.append(f"{filename}: unchanged")
            continue
        lines.append(f"{filename}: {'missing' if actual is None else 'drift'}")
        lines.extend(difflib.unified_diff(
            expected.splitlines(), (actual or "").splitlines(),
            fromfile=f"host-composed {filename} (would be regenerated)",
            tofile=f"runtime/_host/{filename} (current)", lineterm="",
        ))
    lines.append("Scope: the same local/ fleet discovery as generation; no historical-source or concurrent-change guarantee.")
    return "\n".join(lines) + "\n"
