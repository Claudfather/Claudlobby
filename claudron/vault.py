"""Vault detection, scaffolding, validation, and status.

A vault is a directory containing ``_shared/`` at its root. Detection
walks up from a starting path (like git walks up looking for ``.git/``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml


# ── reserved directory names ──────────────────────────────────────────

_RESERVED = frozenset({"_shared", "projects", ".git", ".github", "__pycache__"})

_GITIGNORE_CONTENT = """\
# claudron vault — gitignored runtime & secrets
*/runtime/
*/.env
.claudron/
"""

# ── data model ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Vault:
    """Immutable snapshot of a detected vault."""

    root: Path
    shared: Path  # root / "_shared"
    projects: dict[str, Path]  # {name: root / "projects" / name}
    fleets: dict[str, Path]  # {name: root / name} where fleet.yaml exists


# ── detection ─────────────────────────────────────────────────────────


def detect(path: Path | None = None) -> Vault | None:
    """Walk up from *path* (or CWD) looking for ``_shared/``.

    Returns a :class:`Vault` on success, ``None`` if no vault found.
    """
    start = (path or Path.cwd()).resolve()
    for candidate in [start, *start.parents]:
        if (candidate / "_shared").is_dir():
            return _scan_vault(candidate)
    return None


def _scan_vault(root: Path) -> Vault:
    shared = root / "_shared"

    # Discover projects/
    projects: dict[str, Path] = {}
    projects_dir = root / "projects"
    if projects_dir.is_dir():
        projects = {
            d.name: d
            for d in sorted(projects_dir.iterdir())
            if d.is_dir() and not d.name.startswith(".")
        }

    # Discover fleet overlays (dirs containing fleet.yaml)
    fleets: dict[str, Path] = {}
    for d in sorted(root.iterdir()):
        if d.is_dir() and d.name not in _RESERVED and not d.name.startswith("."):
            if (d / "fleet.yaml").is_file():
                fleets[d.name] = d

    return Vault(root=root, shared=shared, projects=projects, fleets=fleets)


# ── scaffolding ───────────────────────────────────────────────────────


class VaultError(Exception):
    """Raised when a vault operation fails."""


def init(path: str | Path, *, adopt: bool = False) -> Path:
    """Create a new vault at *path*.

    Raises :class:`VaultError` if the directory is non-empty and
    *adopt* is ``False``, or if ``_shared/`` already exists.

    Returns the vault root.
    """
    root = Path(path).resolve()

    if root.is_dir() and (root / "_shared").is_dir():
        raise VaultError(
            f"vault already exists at {root}\n"
            f"  (has _shared/ directory — run `claudron status --vault {root}` to inspect)"
        )

    if root.is_dir() and any(root.iterdir()) and not adopt:
        raise VaultError(
            f"directory not empty: {root}\n"
            f"  use --adopt to turn an existing directory into a vault"
        )

    for subdir in (
        root / "_shared" / "knowledge",
        root / "_shared" / "decisions",
        root / "_shared" / "runbooks",
        root / "projects",
    ):
        subdir.mkdir(parents=True, exist_ok=True)

    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE_CONTENT)

    return root


# ── status ────────────────────────────────────────────────────────────


def status(vault: Vault, *, stale_days: int = 90) -> dict:
    """Vault health summary.

    Returns a dict with doc counts per tier, stale counts, and warnings.
    """
    today = date.today()
    tiers: dict[str, dict] = {}
    warnings: list[str] = []

    def _count_tier(name: str, base: Path) -> None:
        docs = 0
        stale = 0
        if not base.is_dir():
            tiers[name] = {"docs": 0, "stale": 0, "path": str(base)}
            return
        for md in base.rglob("*.md"):
            if md.name in ("INDEX.md", "README.md"):
                continue
            docs += 1
            if _is_stale(md, today, stale_days):
                stale += 1
        tiers[name] = {"docs": docs, "stale": stale, "path": str(base)}

    # Shared tiers
    _count_tier("shared/knowledge", vault.shared / "knowledge")
    _count_tier("shared/decisions", vault.shared / "decisions")
    _count_tier("shared/runbooks", vault.shared / "runbooks")

    # Projects
    for name, proj_path in vault.projects.items():
        _count_tier(f"projects/{name}", proj_path)

    # Fleets
    for name, fleet_path in vault.fleets.items():
        fleet_shared = fleet_path / "shared"
        if fleet_shared.is_dir():
            _count_tier(f"{name}/shared", fleet_shared)

    total_docs = sum(t["docs"] for t in tiers.values())
    total_stale = sum(t["stale"] for t in tiers.values())

    if total_docs == 0:
        warnings.append("vault is empty — no knowledge docs found")

    # Check index freshness
    index_path = vault.root / ".claudron" / "index.json"
    index_fresh = False
    if index_path.is_file():
        index_fresh = not _index_is_stale(vault, index_path)

    return {
        "root": str(vault.root),
        "tiers": tiers,
        "total_docs": total_docs,
        "total_stale": total_stale,
        "projects": list(vault.projects.keys()),
        "fleets": list(vault.fleets.keys()),
        "index_present": index_path.is_file(),
        "index_fresh": index_fresh,
        "warnings": warnings,
    }


def _is_stale(path: Path, today: date, default_ttl_days: int) -> bool:
    """Check if a doc is expired based on frontmatter or mtime."""
    try:
        text = path.read_text()
    except OSError:
        return False

    fm = _quick_frontmatter(text)
    if not fm:
        # No frontmatter — use mtime
        mtime = datetime.fromtimestamp(path.stat().st_mtime).date()
        return (today - mtime).days > default_ttl_days

    # Explicit expires field
    expires = fm.get("expires")
    if expires:
        if isinstance(expires, date):
            return today > expires
        try:
            return today > date.fromisoformat(str(expires))
        except (ValueError, TypeError):
            pass

    # Status-based: terminal statuses aren't "stale", they're done
    st = fm.get("status", "")
    if st in ("completed", "superseded", "archived"):
        return False

    # Fall back to updated/created date + TTL
    for field in ("updated", "created"):
        val = fm.get(field)
        if val:
            try:
                d = val if isinstance(val, date) else date.fromisoformat(str(val))
                return (today - d).days > default_ttl_days
            except (ValueError, TypeError):
                pass

    return False


def _quick_frontmatter(text: str) -> dict | None:
    """Fast frontmatter extraction (no body return)."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return None
    lines = text.splitlines(keepends=True)
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            end = i
            break
    if end is None:
        return None
    try:
        fm = yaml.safe_load("".join(lines[1:end]))
        return fm if isinstance(fm, dict) else None
    except yaml.YAMLError:
        return None


def _index_is_stale(vault: Vault, index_path: Path) -> bool:
    """True if any .md under vault root is newer than the index."""
    index_mtime = index_path.stat().st_mtime
    for md in vault.root.rglob("*.md"):
        if md.stat().st_mtime > index_mtime:
            return True
    return False
