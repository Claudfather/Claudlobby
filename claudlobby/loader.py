"""Library file loading with YAML frontmatter.

Library files (protocols, guardrails, resources, lessons, integrations,
post_actions, voices) start with optional YAML frontmatter:

    ---
    title: Multi-Angle Orchestration
    description: Pattern for substrate-shaping decisions
    ---

    For big architectural decisions...

If frontmatter is missing, `title` is derived from the filename. The body
is the rest of the file (after the closing `---` if present).

Expertise files are an exception — they may have a leading H1 that
provides the bot's title label; see `parse_expertise_file`.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class LibraryItem:
    title: str
    description: str | None
    body: str
    source_path: Path


@dataclass
class ExpertiseItem:
    title_label: str | None  # text after "—" in the H1, if any
    body: str  # body without the H1
    source_path: Path


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a markdown file into (frontmatter dict, body str).

    Returns ({}, original_text) if no frontmatter present.
    """
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text
    # Find closing fence
    lines = text.splitlines(keepends=True)
    # First line is "---". Find the next line that is exactly "---".
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm_text = "".join(lines[1:end])
    body = "".join(lines[end + 1 :]).lstrip("\n")
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(fm, dict):
        return {}, text
    return fm, body


def _derive_title(stem: str) -> str:
    """Filename `multi-angle-orchestration` → `Multi-angle orchestration`."""
    s = stem.replace("-", " ").replace("_", " ").strip()
    if not s:
        return stem
    return s[:1].upper() + s[1:]


def _demote_headings(body: str) -> str:
    """Demote any leading-`#` headings in body by one level.

    Library file bodies live inside an `### <title>` block in the
    composed CLAUDE.md, so any `## ` or `###` headings the body has
    would conflict with the template's level. Demote so they nest
    correctly: `## ` → `### `, `### ` → `#### `, etc.

    Stops at `######` (max H6).
    """
    out_lines: list[str] = []
    in_fence = False
    fence_re = re.compile(r"^(```|~~~)")
    for line in body.splitlines():
        if fence_re.match(line):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue
        m = re.match(r"^(#{1,5})(\s)", line)
        if m:
            out_lines.append("#" + line)
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def load_library_item(path: Path) -> LibraryItem | None:
    """Load a single library file. Returns None if path doesn't exist.

    Body headings are demoted by one level so they nest correctly under
    the template's `### <title>` wrapper.
    """
    if not path.is_file():
        return None
    text = path.read_text()
    fm, body = parse_frontmatter(text)
    title = fm.get("title") or _derive_title(path.stem)
    description = fm.get("description")
    return LibraryItem(
        title=title,
        description=description,
        body=_demote_headings(body.rstrip()),
        source_path=path,
    )


def load_library_items(items: list[str], dir_path: Path) -> list[LibraryItem]:
    """Load `<item>.md` from `dir_path` for each name in `items`. Skips missing.

    LEGACY entry point — used by code that doesn't yet pass a Paths instance.
    Prefer `load_library_items_overlay` for overlay-aware lookup.
    """
    out: list[LibraryItem] = []
    for name in items:
        loaded = load_library_item(dir_path / f"{name}.md")
        if loaded is not None:
            out.append(loaded)
    return out


def load_library_items_overlay(items: list[str], paths, kind: str) -> list[LibraryItem]:
    """Overlay-aware loader with folder expansion.

    Each entry in `items` can be:
      - `name`       — single file at `<kind>/name.md`
      - `dir/name`   — single file at `<kind>/dir/name.md`
      - `dir/`       — folder expansion: every `*.md` under `<kind>/dir/`
                       (recursive). Overlay files override base files at
                       the same relative path.

    Overlay precedence: for any given file path, the fleet overlay wins
    over the public base.
    """
    out: list[LibraryItem] = []
    seen_paths: set[str] = set()  # tracks "dir/name" relative keys for folder loads

    for name in items:
        if name.endswith("/"):
            # Folder expansion. Walk overlay → base, dedupe by relative path.
            dir_name = name.rstrip("/")
            collected: dict[str, Path] = {}
            for search_dir in paths.library_search_dirs(kind):
                target = search_dir / dir_name if dir_name else search_dir
                if not target.is_dir():
                    continue
                for path in sorted(target.rglob("*.md")):
                    if path.stem.lower().startswith("readme"):
                        continue
                    rel_key = str(path.relative_to(target).with_suffix(""))
                    if rel_key not in collected:  # overlay (first) wins
                        collected[rel_key] = path
            for rel_key, path in sorted(collected.items()):
                full_key = f"{dir_name}/{rel_key}" if dir_name else rel_key
                if full_key in seen_paths:
                    continue
                seen_paths.add(full_key)
                item = load_library_item(path)
                if item is not None:
                    out.append(item)
        else:
            # Specific file (possibly nested like "dbt/dim-first-architecture").
            if name in seen_paths:
                continue
            seen_paths.add(name)
            path = paths.find_library_file(kind, name, ".md")
            if path is None:
                continue
            item = load_library_item(path)
            if item is not None:
                out.append(item)
    return out


def parse_expertise_file(path: Path) -> ExpertiseItem | None:
    """Load an expertise file. Strips the H1 if present, captures the title-label.

    H1 conventions:
      `# {{BOT_NAME}} — Manager / Orchestrator`  →  title_label = "Manager / Orchestrator"
      `# Manager`                                →  title_label = "Manager"
      no H1                                      →  title_label = None
    """
    if not path.is_file():
        return None
    text = path.read_text().rstrip()
    fm, text = parse_frontmatter(text)
    if fm.get("title_label"):
        # Frontmatter wins.
        return ExpertiseItem(title_label=fm["title_label"], body=text.lstrip(), source_path=path)
    # Otherwise look at the leading H1.
    lines = text.splitlines()
    if not lines:
        return ExpertiseItem(title_label=None, body="", source_path=path)
    if lines[0].startswith("# "):
        h1 = lines[0][2:].strip()
        # Strip "{{BOT_NAME}} —" or "{{BOT_NAME}} -"
        for sep in (" — ", " - ", " – "):
            if sep in h1:
                _, _, after = h1.partition(sep)
                title_label = after.strip()
                body = "\n".join(lines[1:]).lstrip("\n")
                return ExpertiseItem(title_label=title_label, body=body, source_path=path)
        # H1 didn't have separator — use it as the label, drop body H1
        body = "\n".join(lines[1:]).lstrip("\n")
        return ExpertiseItem(title_label=h1, body=body, source_path=path)
    return ExpertiseItem(title_label=None, body=text, source_path=path)


def load_voice(path: Path) -> LibraryItem | None:
    """Voice files are LibraryItems but also accept the legacy `## Voice: <Name>` H2 line."""
    if not path.is_file():
        return None
    text = path.read_text()
    fm, body = parse_frontmatter(text)
    if fm.get("name"):
        return LibraryItem(
            title=fm["name"],
            description=fm.get("description"),
            body=body.rstrip(),
            source_path=path,
        )
    # Legacy: `## Voice: <Name>` on the first non-blank line.
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "":
            continue
        if line.startswith("## Voice:"):
            name = line[len("## Voice:") :].strip()
            rest = "\n".join(lines[i + 1 :]).lstrip("\n").rstrip()
            return LibraryItem(
                title=name,
                description=None,
                body=rest,
                source_path=path,
            )
        # Any other first line: derive from filename and treat whole body as content.
        break
    return LibraryItem(
        title=_derive_title(path.stem),
        description=None,
        body=body.rstrip(),
        source_path=path,
    )
