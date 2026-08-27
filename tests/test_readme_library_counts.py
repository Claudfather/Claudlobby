"""The README's library inventory must match the library.

Every one of the six counts in ``README.md``'s "What this repo gives you" section
was stale when this test was written — all six understated, because the library
grows and prose does not. Hand-correcting them fixes today and rots again on the
next addition, so the numbers are pinned here instead: add a skill without
updating the README and this fails, naming the number to write.

**Counting rules, stated because they are the whole contract.**

A category's own ``README.md`` is documentation *about* the category, not a member
of it, so it is excluded. Getting this wrong is not hypothetical -- the
measurement that produced this test counted expertise/guardrails/protocols with a
bare ``*.md`` glob and reported each one high by exactly that file.

``lib/`` is claimed as "bash lifecycle scripts", so it counts ``*.sh`` plus the
extensionless files carrying a bash shebang, and excludes the ``.py`` modules and
``CLAUDE.md``. Counting every file in ``lib/`` would silently absorb the Python
doors into a figure the README calls bash.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
LIBRARY = REPO / "library"


def _md_members(category: str) -> list[Path]:
    """``*.md`` in a library category, excluding the category's own README."""
    return sorted(p for p in (LIBRARY / category).glob("*.md") if p.name != "README.md")


def _bash_scripts() -> list[Path]:
    """``lib/*.sh`` plus extensionless files with a bash shebang."""
    out = []
    for p in sorted((REPO / "lib").iterdir()):
        if not p.is_file():
            continue
        if p.suffix == ".sh":
            out.append(p)
        elif not p.suffix:
            try:
                first = p.read_text(encoding="utf-8", errors="replace").split("\n", 1)[
                    0
                ]
            except OSError:  # pragma: no cover - unreadable file
                continue
            if first.startswith("#!") and "bash" in first:
                out.append(p)
    return out


def _actual() -> dict[str, int]:
    return {
        "expertise profiles": len(_md_members("expertise")),
        "skills": len([p for p in (LIBRARY / "skills").iterdir() if p.is_dir()]),
        "MCP fragments": len(sorted((LIBRARY / "mcp").glob("*.json"))),
        "guardrails": len(_md_members("guardrails")),
        "protocols": len(_md_members("protocols")),
        "bash lifecycle scripts": len(_bash_scripts()),
    }


def _claimed() -> dict[str, int]:
    """Pull each ``<N> <label>`` claim out of the README.

    Anchored to the label so a reordering of the sentence does not break the
    test, and so a missing claim fails loudly as a KeyError rather than being
    silently skipped -- a count the README stopped making is the same defect as
    a count it makes wrongly.
    """
    text = README.read_text(encoding="utf-8")
    claims: dict[str, int] = {}
    for label in _actual():
        m = re.search(rf"(\d+)\s+{re.escape(label)}\b", text)
        if m:
            claims[label] = int(m.group(1))
    return claims


@pytest.mark.parametrize("label", sorted(_actual()))
def test_readme_count_matches_the_library(label: str) -> None:
    claimed, actual = _claimed(), _actual()
    assert label in claimed, (
        f"README.md no longer states a count for {label!r}. Either restore it "
        f"(the library has {actual[label]}) or drop this label from the test."
    )
    assert claimed[label] == actual[label], (
        f"README.md says {claimed[label]} {label}; the library has "
        f"{actual[label]}. Update README.md to say {actual[label]}."
    )


def test_each_label_is_claimed_exactly_once() -> None:
    """One claim per label, so two numbers cannot disagree.

    A second, stale mention elsewhere in the README would satisfy the search
    above (which takes the first match) while still showing a reader the wrong
    figure -- the same rot this file exists to stop, one level over.
    """
    text = README.read_text(encoding="utf-8")
    for label in sorted(_actual()):
        hits = re.findall(rf"\d+\s+{re.escape(label)}\b", text)
        assert len(hits) == 1, (
            f"README.md states a count for {label!r} {len(hits)} times ({hits}); "
            f"expected exactly one."
        )
