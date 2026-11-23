"""library/lessons/ is frozen: every note has one verdict in the README's triage record.

The record was the classification inside the lessons converter until that converter was
retired (#2959). This keeps what the converter's tests guarded: a new note cannot arrive
without a verdict, and a verdict cannot outlive its note.
"""

from __future__ import annotations

import re
from pathlib import Path

LESSONS = Path(__file__).resolve().parent.parent / "library" / "lessons"
ROW = re.compile(r"^\| `([^`]+\.md)` \| (referential|behavior) \|", re.M)


def _record() -> list[tuple[str, str]]:
    return ROW.findall((LESSONS / "README.md").read_text())


def test_every_note_has_exactly_one_verdict():
    recorded = [path for path, _ in _record()]
    on_disk = sorted(str(p.relative_to(LESSONS)) for p in LESSONS.rglob("*.md") if p.name != "README.md")
    assert sorted(recorded) == on_disk
    assert len(recorded) == len(set(recorded))


def test_the_record_keeps_the_frozen_counts():
    verdicts = [verdict for _, verdict in _record()]
    assert (verdicts.count("referential"), verdicts.count("behavior")) == (22, 3)


def test_the_behavior_notes_are_the_three_that_were_re_homed():
    behavior = {path for path, verdict in _record() if verdict == "behavior"}
    assert behavior == {"messaging-channel-discipline.md", "tmux-dispatch-shell-expansion.md",
                        "orchestration/consensus-before-escalation.md"}
