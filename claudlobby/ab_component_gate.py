"""Strict composed-delta gate for the frozen channel-brevity experiment.

The fixture uses the stock template and an unexpanded token-efficiency item.
Unsupported markup/layout refuses the experiment; it is never normalized away.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

from .component_sources import END_MARKER, attribute
from .loader import load_library_item

SOURCE = "shared/library/protocols/token-efficiency.md"
_START = re.compile(r"<!-- claudlobby:source ([^\r\n<>]+) -->")


def _components(markdown: str):
    sources, ends = [], 0
    for line in markdown.splitlines():
        if "claudlobby:source" not in line:
            continue
        start = _START.fullmatch(line)
        if start:
            sources.append(start[1])
        elif line == END_MARKER:
            ends += 1
        else:
            raise ValueError("unsupported source-marker syntax")
    result = attribute(markdown)
    if (not result.available or ends != len(result.components)
            or sources != [part.source for part in result.components]):
        raise ValueError("unavailable or incomplete source attribution")
    return result.components


def assert_component_only(without: str, with_component: str, source: Path) -> None:
    before, after = _components(without), _components(with_component)
    target = [part for part in after if part.source == SOURCE]
    if any(part.source == SOURCE for part in before) or len(target) != 1:
        raise ValueError("declared component must be absent WITHOUT and appear once WITH")
    item = load_library_item(source)
    if item is None:
        raise ValueError("declared component source is missing")
    expected = (f"<!-- claudlobby:source {SOURCE} -->\n"
                f"### {item.title}\n\n{item.body.strip()}\n{END_MARKER}\n\n")
    # The stock section macro includes exactly two LF bytes after the closing
    # marker. Keep all other bytes, including other source markers and order.
    lines = with_component.splitlines(keepends=True)
    start, end = target[0].start_line - 1, target[0].end_line + 1
    if "".join(lines[start:end]) != expected:
        raise ValueError("declared component differs from its loaded template block")
    if "".join(lines[:start] + lines[end:]) != without:
        raise ValueError("composed outputs differ outside the declared component")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: ab_component_gate WITHOUT WITH COMPONENT_SOURCE", file=sys.stderr)
        return 2
    try:
        # read_text normalizes CRLF; an isolation gate must compare actual bytes.
        without, with_component = [Path(p).read_bytes().decode("utf-8") for p in argv[:2]]
        assert_component_only(without, with_component, Path(argv[2]))
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"ab-comms-eval: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
