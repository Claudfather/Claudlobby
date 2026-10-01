"""`link_source` — was this report's task link NAMED or GUESSED? (#1710)

Both paths produce an identical `work_item_id`/`assignment_id` pair on the
stored row, so the question is **unrecoverable after the fact**: vera
established that by trying, against the whole population. It is stamped at
write time or it cannot be asked.

**What it is for, which is not the question it came from.** It answers how
often #835's auto-resolve is GUESSING AT ALL — the input to deciding whether
that heuristic should exist. The proxy that motivated it (auto-resolved links
carrying a `pr_url`) died with #1706's case-2 fix, which stops them carrying
one; the field outlives that.

Three states, not two, exactly as `pr_role`: `named`, `auto-resolved`, and
**absent** — a writer that predates the field. A reader must be able to tell
absent from "not auto-resolved", which is why this is a string enum and not a
boolean: `NOT json_extract(detail,'$.auto_resolved')` is true for NULL and for
0 alike, and the whole point is that a guess must never read as a certainty.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from claudlobby.plane.contracts import LINK_SOURCES, TaskEvent
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import _apply_capture
from claudlobby.plane.registries import CONTENT_FIELDS, FIELD_POLICY

REPO = Path(__file__).resolve().parent.parent
WI = "wi_" + "a" * 32


def _task_details(tmp_path):
    if not db_path(tmp_path).exists():
        return []
    conn = connect(db_path(tmp_path))
    try:
        rows = conn.execute(
            "SELECT detail FROM events WHERE kind='task' AND detail IS NOT NULL"
            " ORDER BY ingest_seq").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []
    finally:
        conn.close()
    return [json.loads(r[0]) for r in rows]


class TestTheContract:
    def test_the_vocabulary_is_a_string_enum_not_a_boolean(self):
        assert set(LINK_SOURCES) == {"named", "auto-resolved"}

    def test_absent_is_distinguishable_from_either_value(self):
        absent = TaskEvent(work_item_id=WI, event="completed")
        auto = TaskEvent(work_item_id=WI, event="completed", link_source="auto-resolved")
        named = TaskEvent(work_item_id=WI, event="completed", link_source="named")
        assert absent.link_source is None
        assert absent.link_source != auto.link_source != named.link_source

    @pytest.mark.parametrize("bad", ["guessed", "", "Named", "auto_resolved", "true"])
    def test_an_unrecognised_source_is_refused(self, bad):
        with pytest.raises(Exception):
            TaskEvent(work_item_id=WI, event="completed", link_source=bad)

    def test_it_is_METADATA_and_survives_a_metadata_capture(self):
        """Provenance, not prose. A metadata capture that stripped it would
        collapse `auto-resolved` into `absent`, and absent is load-bearing."""
        assert FIELD_POLICY[("task", "link_source")]["class"] == "METADATA"
        assert "link_source" not in CONTENT_FIELDS.get("task", ())
        out = _apply_capture(
            {"event_type": "task", "fleet": "f",
             "payload": {"work_item_id": WI, "event": "completed",
                         "summary": "prose", "link_source": "auto-resolved"}},
            {"*": "metadata"},
        )["payload"]
        assert "summary" not in out, "positive control: the door must strip content"
        assert out["link_source"] == "auto-resolved"
