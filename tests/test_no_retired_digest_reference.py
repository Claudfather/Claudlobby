"""The writer retired in #2145 P3; live monitoring reads session_summary through
claudlobby event list --type session_summary. No library reader may name the
retired session_digest, transcript-digest or SESSION_DIGEST_ surface.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIBRARY = REPO / "library"

# Each pattern names one retired surface; a match anywhere under library/ is a
# reader that #1503 left pointing at the deleted file.
RETIRED = (
    re.compile(r"\bsession_digest\b"),
    re.compile(r"transcript-digest"),
    re.compile(r"SESSION_DIGEST_"),
)


def _library_files() -> list[Path]:
    return sorted(p for p in LIBRARY.rglob("*") if p.is_file())


def test_no_library_file_references_the_retired_digest_jsonl() -> None:
    offenders: list[str] = []
    for path in _library_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - unreadable file
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(pat.search(line) for pat in RETIRED):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "library/ still references the retired transcript-digest JSONL sink "
        "(#1503 moved it to the plane; read it via `claudlobby events --type "
        "session_summary --json`):\n" + "\n".join(offenders)
    )
