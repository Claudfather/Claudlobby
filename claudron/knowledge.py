"""Knowledge retrieval engine.

Searches vault tiers by title, tags, filename, and content.
Uses a two-tier strategy:

- **Tier A** — frontmatter index (``.claudron/index.json``). Cheap
  title/tag/alias matching without reading file bodies.
- **Tier B** — full-text scan of markdown bodies (fallback when Tier A
  misses or scores below threshold).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .vault import Vault

# ── data models ───────────────────────────────────────────────────────


@dataclass
class KnowledgeDoc:
    title: str
    tags: list[str]
    body: str
    source_path: Path
    tier: str  # "shared" | "project:<name>" | "fleet:<name>" | "other"


@dataclass
class KnowledgeResult:
    doc: KnowledgeDoc
    score: int
    match_type: str  # "title" | "alias" | "tag" | "heading" | "filename" | "content"


# ── scoring weights ───────────────────────────────────────────────────

W_TITLE_EXACT = 100
W_ALIAS_EXACT = 90
W_TITLE_SUBSTR = 80
W_HEADING = 70
W_TAG_EXACT = 60
W_TITLE_WORD_OVERLAP = 50
W_TAG_PARTIAL = 40
W_FILENAME = 30
W_BODY = 20
W_BODY_EXTRA = 5  # per additional body match, capped
W_BODY_EXTRA_CAP = 25

TIER_A_THRESHOLD = 50  # if best Tier A score < this, fall back to Tier B

SCORE_CAP = 200


# ── frontmatter parsing ──────────────────────────────────────────────


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split markdown into (frontmatter dict, body str).

    Returns ``({}, text)`` if no frontmatter present.
    """
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text
    lines = text.splitlines(keepends=True)
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            end = i
            break
    if end is None:
        return {}, text
    try:
        fm = yaml.safe_load("".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(fm, dict):
        return {}, text
    body = "".join(lines[end + 1 :]).lstrip("\n")
    return fm, body


def _derive_title(stem: str) -> str:
    s = stem.replace("-", " ").replace("_", " ").strip()
    return (s[:1].upper() + s[1:]) if s else stem


# ── tier walking ─────────────────────────────────────────────────────


def _walk_knowledge_tier(base: Path, tier: str) -> list[KnowledgeDoc]:
    """Recursively collect knowledge docs under *base*."""
    docs: list[KnowledgeDoc] = []
    if not base.is_dir():
        return docs
    for md_path in sorted(base.rglob("*.md")):
        if md_path.name in ("INDEX.md", "README.md"):
            continue
        doc = _parse_doc(md_path, tier)
        if doc is not None:
            docs.append(doc)
    return docs


def _parse_doc(path: Path, tier: str) -> KnowledgeDoc | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    fm, body = _parse_frontmatter(text)
    title = fm.get("title") or _derive_title(path.stem)
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return KnowledgeDoc(
        title=title,
        tags=[str(t) for t in tags],
        body=body.rstrip(),
        source_path=path,
        tier=tier,
    )


# ── index (Tier A) ───────────────────────────────────────────────────


def _build_index(vault: "Vault") -> dict:
    """Build frontmatter-only index, write to ``.claudron/index.json``."""
    entries: list[dict] = []

    def _index_tier(base: Path, tier: str) -> None:
        if not base.is_dir():
            return
        for md in sorted(base.rglob("*.md")):
            if md.name in ("INDEX.md", "README.md"):
                continue
            try:
                text = md.read_text()
            except OSError:
                continue
            fm, _ = _parse_frontmatter(text)
            title = fm.get("title") or _derive_title(md.stem)
            entries.append(
                {
                    "title": title,
                    "tags": fm.get("tags") or [],
                    "aliases": fm.get("aliases") or [],
                    "status": fm.get("status", "active"),
                    "updated": str(fm.get("updated", "")),
                    "expires": str(fm.get("expires", "")),
                    "filename": md.stem,
                    "path": str(md.relative_to(vault.root)),
                    "tier": tier,
                }
            )

    # Walk all tiers
    for subdir in ("knowledge", "decisions", "runbooks"):
        _index_tier(vault.shared / subdir, "shared")
    for name, proj_path in vault.projects.items():
        _index_tier(proj_path, f"project:{name}")
    for name, fleet_path in vault.fleets.items():
        fleet_shared = fleet_path / "shared"
        if fleet_shared.is_dir():
            _index_tier(fleet_shared, f"fleet:{name}")

    # Also index unrecognized root dirs
    _SKIP = {"_shared", "projects", ".git", ".github", ".claudron", "__pycache__"}
    fleet_names = set(vault.fleets.keys())
    for d in sorted(vault.root.iterdir()):
        if d.is_dir() and d.name not in _SKIP and not d.name.startswith("."):
            if d.name not in fleet_names and d.name != "projects":
                _index_tier(d, f"other:{d.name}")

    index = {"version": 1, "entries": entries}
    index_dir = vault.root / ".claudron"
    index_dir.mkdir(exist_ok=True)
    index_path = index_dir / "index.json"
    index_path.write_text(json.dumps(index, indent=2, default=str))
    return index


def _load_index(vault: "Vault") -> dict | None:
    """Load index if it exists and is fresh. Returns None if stale or missing."""
    index_path = vault.root / ".claudron" / "index.json"
    if not index_path.is_file():
        return None
    index_mtime = index_path.stat().st_mtime
    # Check if any .md is newer
    for md in vault.root.rglob("*.md"):
        if md.name.startswith("."):
            continue
        if md.stat().st_mtime > index_mtime:
            return None  # stale
    try:
        data = json.loads(index_path.read_text())
        if isinstance(data, dict) and "entries" in data:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


# ── scoring ──────────────────────────────────────────────────────────


def _score_index_entry(query: str, entry: dict) -> tuple[int, str]:
    """Score a single index entry against *query*. Returns (score, match_type)."""
    q = query.lower()
    total = 0
    best_type = "none"

    title = entry.get("title", "").lower()
    tags = [str(t).lower() for t in entry.get("tags", [])]
    aliases = [str(a).lower() for a in entry.get("aliases", [])]
    filename = entry.get("filename", "").lower()

    # Try single-substring first (exact phrase match scores higher)
    if q == title:
        return min(W_TITLE_EXACT, SCORE_CAP), "title"
    if q in aliases:
        return min(W_ALIAS_EXACT, SCORE_CAP), "alias"

    # Single phrase substring
    if q in title:
        total += W_TITLE_SUBSTR
        best_type = "title"
    if any(q == t for t in tags):
        total += W_TAG_EXACT
        if best_type == "none":
            best_type = "tag"
    if any(q in t for t in tags) and not any(q == t for t in tags):
        total += W_TAG_PARTIAL
        if best_type == "none":
            best_type = "tag"
    if q in filename:
        total += W_FILENAME
        if best_type == "none":
            best_type = "filename"

    if total > 0:
        return min(total, SCORE_CAP), best_type

    # Multi-word tokenization: score each word independently
    tokens = q.split()
    if len(tokens) > 1:
        token_total = 0
        token_type = "none"
        for token in tokens:
            if token in title:
                token_total += W_TITLE_WORD_OVERLAP
                if token_type == "none":
                    token_type = "title"
            elif any(token == t for t in tags):
                token_total += W_TAG_EXACT
                if token_type == "none":
                    token_type = "tag"
            elif any(token in t for t in tags):
                token_total += W_TAG_PARTIAL
                if token_type == "none":
                    token_type = "tag"
            elif token in filename:
                token_total += W_FILENAME
                if token_type == "none":
                    token_type = "filename"
        if token_total > 0:
            return min(token_total, SCORE_CAP), token_type

    return 0, "none"


def _score_body(query: str, doc: KnowledgeDoc) -> tuple[int, str]:
    """Tier B: score against body content (headings + body text)."""
    q = query.lower()
    body = doc.body.lower()
    total = 0
    best_type = "none"

    # Check headings
    for line in doc.body.splitlines():
        if re.match(r"^#{1,3}\s", line) and q in line.lower():
            total += W_HEADING
            best_type = "heading"
            break

    # Body substring
    matches = body.count(q)
    if matches > 0:
        total += W_BODY
        if best_type == "none":
            best_type = "content"
        extra = min((matches - 1) * W_BODY_EXTRA, W_BODY_EXTRA_CAP)
        total += extra

    # Multi-word tokenization for body
    if total == 0:
        tokens = q.split()
        if len(tokens) > 1:
            token_hits = sum(1 for t in tokens if t in body)
            if token_hits > 0:
                total += W_BODY * token_hits // len(tokens)
                best_type = "content"

    return min(total, SCORE_CAP), best_type


# ── filtering ────────────────────────────────────────────────────────


def _is_excluded(
    entry: dict, include_archived: bool = False, include_expired: bool = False
) -> bool:
    """True if this entry should be excluded from results."""
    status = entry.get("status", "active")
    if not include_archived and status in ("completed", "superseded", "archived"):
        return True
    if not include_expired:
        expires = entry.get("expires", "")
        if expires:
            try:
                exp_date = date.fromisoformat(str(expires))
                if date.today() > exp_date:
                    return True
            except (ValueError, TypeError):
                pass
    return False


def _is_doc_excluded(
    doc: KnowledgeDoc, include_archived: bool = False, include_expired: bool = False
) -> bool:
    """Check exclusion for a parsed KnowledgeDoc (Tier B)."""
    fm, _ = _parse_frontmatter(doc.body)
    # Re-parse from original file for accurate frontmatter
    try:
        text = doc.source_path.read_text()
        fm, _ = _parse_frontmatter(text)
    except OSError:
        fm = {}
    return _is_excluded(
        {
            "status": fm.get("status", "active"),
            "expires": str(fm.get("expires", "")),
        },
        include_archived,
        include_expired,
    )


# ── main lookup ──────────────────────────────────────────────────────


def lookup(
    query: str,
    vault: "Vault",
    *,
    project: str | None = None,
    fleet: str | None = None,
    limit: int = 5,
    include_archived: bool = False,
    include_expired: bool = False,
) -> list[KnowledgeResult]:
    """Search vault knowledge. Returns ranked results."""
    # Ensure index is fresh
    index = _load_index(vault)
    if index is None:
        index = _build_index(vault)

    # ── Tier A: index-based scoring ──
    results: list[KnowledgeResult] = []
    best_a_score = 0

    for entry in index.get("entries", []):
        if _is_excluded(entry, include_archived, include_expired):
            continue
        score, match_type = _score_index_entry(query, entry)
        if score > 0:
            best_a_score = max(best_a_score, score)
            doc_path = vault.root / entry["path"]
            doc = _parse_doc(doc_path, entry.get("tier", "shared"))
            if doc is not None:
                results.append(
                    KnowledgeResult(doc=doc, score=score, match_type=match_type)
                )

    # ── Tier B: full-text fallback ──
    if best_a_score < TIER_A_THRESHOLD:
        seen_paths = {r.doc.source_path for r in results}
        for doc in _collect_all_docs(vault, project=project, fleet=fleet):
            if doc.source_path in seen_paths:
                continue
            if _is_doc_excluded(doc, include_archived, include_expired):
                continue
            score, match_type = _score_body(query, doc)
            if score > 0:
                # Merge with any Tier A score for the same doc
                results.append(
                    KnowledgeResult(doc=doc, score=score, match_type=match_type)
                )
                seen_paths.add(doc.source_path)

    # ── Sort: score desc, then tier priority ──
    tier_priority = {"project": 0, "fleet": 1, "shared": 2, "other": 3}

    def _sort_key(r: KnowledgeResult) -> tuple:
        tier_base = r.doc.tier.split(":")[0] if ":" in r.doc.tier else r.doc.tier
        return (-r.score, tier_priority.get(tier_base, 3))

    results.sort(key=_sort_key)
    return results[:limit]


def _collect_all_docs(
    vault: "Vault",
    *,
    project: str | None = None,
    fleet: str | None = None,
) -> list[KnowledgeDoc]:
    """Collect docs from all tiers in search order."""
    docs: list[KnowledgeDoc] = []

    # Project-local (most specific)
    if project and project in vault.projects:
        docs.extend(_walk_knowledge_tier(vault.projects[project], f"project:{project}"))

    # Fleet shared
    if fleet and fleet in vault.fleets:
        fleet_shared = vault.fleets[fleet] / "shared"
        docs.extend(_walk_knowledge_tier(fleet_shared, f"fleet:{fleet}"))

    # Vault shared (always)
    for subdir in ("knowledge", "decisions", "runbooks"):
        docs.extend(_walk_knowledge_tier(vault.shared / subdir, "shared"))

    # Unrecognized root dirs
    _SKIP = {"_shared", "projects", ".git", ".github", ".claudron", "__pycache__"}
    fleet_names = set(vault.fleets.keys())
    for d in sorted(vault.root.iterdir()):
        if d.is_dir() and d.name not in _SKIP and not d.name.startswith("."):
            if d.name not in fleet_names:
                docs.extend(_walk_knowledge_tier(d, f"other:{d.name}"))

    return docs
