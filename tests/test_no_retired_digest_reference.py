"""No ``library/`` file may reference the retired transcript-digest JSONL sink.

#1503 moved the session digest OFF ``state/transcript-digests/transcript-digest-
<date>.jsonl`` and ONTO the plane as a ``session_digest`` system event:
``lib/transcript-digest.sh`` emits it, ``claudlobby events --type
session_digest`` reads it. The sink cutover shipped, but its on-estate READER —
the ``fleet-digest`` skill and the ``fleet-monitoring`` protocol under
``library/`` — kept pointing at the deleted file, because the "no on-estate
consumer" census swept only ``lib/`` and ``claudlobby/`` and missed ``library/``.
That left the reader reading an empty window the moment a fleet armed the
digester, and the docs contradicting the code.

This pins the reader side: any ``library/**`` file that names the retired path
fails here, naming the file and line so the next drift is caught at the source
rather than in production.

**Scoped to ``library/**`` deliberately.** ``documentation/architecture/*``
legitimately mentions the old path as history ("was ``state/transcript-digests/
…``, retired by #1503") — those are not stale references and are out of scope.

The retired tokens, each chosen NOT to collide with a live surface:

- ``transcript-digest-*.jsonl`` — the dated file. Not ``transcript-digest.sh``
  (the live sink script, a DOT before ``sh``), not ``session_digest`` (the live
  event name).
- ``state/transcript-digests`` — the retired host directory.
- ``SESSION_DIGEST_LOG_DIR`` — the retired dir-override env knob. Not
  ``SESSION_DIGEST_ENABLED`` / ``SESSION_DIGEST_MIN_TURNS``, which are live.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIBRARY = REPO / "library"

# Each pattern names one retired surface; a match anywhere under library/ is a
# reader that #1503 left pointing at the deleted file.
RETIRED = (
    re.compile(r"transcript-digest-.*\.jsonl"),
    re.compile(r"state/transcript-digests"),
    re.compile(r"SESSION_DIGEST_LOG_DIR"),
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
        "session_digest --json`):\n" + "\n".join(offenders)
    )
