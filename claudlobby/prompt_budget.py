"""Exact accounting for the composed instruction file, not the loaded context.

#1603 owns the single policy: 30,000 UTF-8 bytes is the target; warn only
above 40,000. These are chosen budgets, not token estimates or role-dependent
ceilings. #1674's earlier 160k/110k growth ceilings are not a second policy.
The total includes template text, preamble and any provenance marker overhead.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

CLAUDE_MD_SOFT_BUDGET_BYTES = 30_000
CLAUDE_MD_WARN_BYTES = 40_000

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_H2 = re.compile(r"^ {0,3}##(?:[ \t]+(.*)|[ \t]*)$")


@dataclass(frozen=True)
class Section:
    name: str
    start_line: int
    lines: int
    nbytes: int


@dataclass(frozen=True)
class PromptSize:
    nbytes: int
    lines: int
    sections: tuple[Section, ...]

    @property
    def largest(self) -> tuple[Section, ...]:
        return tuple(sorted(self.sections, key=lambda s: (-s.nbytes, s.start_line))[:3])


def measure(markdown: str) -> PromptSize:
    """Count final UTF-8 bytes and H2 sections, ignoring fenced-code headings.

    A section includes its heading and all bytes until the next real H2.
    Preamble bytes count in the total but are not relabelled as an H2 section.
    Duplicate headings stay separate; detecting collisions belongs to #1674.
    """
    # Markdown line endings are CR/LF, not Unicode separators inside prose.
    lines = re.split(r"(?<=\n)|(?<=\r)(?!\n)", markdown)
    if lines and not lines[-1]:
        lines.pop()
    starts: list[tuple[int, str]] = []
    fence_char = ""
    fence_length = 0
    for i, line in enumerate(lines):
        text = line.rstrip("\r\n")
        fence = _FENCE.match(text)
        if fence_char:
            if (fence and fence[1][0] == fence_char
                    and len(fence[1]) >= fence_length and not fence[2].strip()):
                fence_char = ""
            continue
        if fence and (fence[1][0] == "~" or "`" not in fence[2]):
            fence_char, fence_length = fence[1][0], len(fence[1])
            continue
        heading = _H2.match(text)
        if heading:
            name = re.sub(r"(?:^|[ \t]+)#+[ \t]*$", "", heading[1] or "").strip()
            starts.append((i, name or "(untitled)"))
    sections = []
    for j, (start, name) in enumerate(starts):
        end = starts[j + 1][0] if j + 1 < len(starts) else len(lines)
        sections.append(Section(name, start + 1, end - start,
                                len("".join(lines[start:end]).encode("utf-8"))))
    return PromptSize(len(markdown.encode("utf-8")), len(lines), tuple(sections))


def summary(size: PromptSize) -> str:
    """A stable operator report; no content bodies or token estimates."""
    largest = "; ".join(
        f"{s.name[:120]}: {s.nbytes} bytes (line {s.start_line})" for s in size.largest
    ) or "no H2 sections"
    return f"{size.nbytes} bytes, {size.lines} lines; largest H2: {largest}"


def over_budget(size: PromptSize) -> str | None:
    if size.nbytes <= CLAUDE_MD_WARN_BYTES:
        return None
    return (f"composed CLAUDE.md is {summary(size)}; exceeds {CLAUDE_MD_WARN_BYTES}-byte "
            f"warning budget (target {CLAUDE_MD_SOFT_BUDGET_BYTES} bytes) — review large "
            "instruction components before moving procedures on demand")
