"""`pr_attribution_withheld` — an attribution was DECLARED and refused (#1711 B).

#1706 case 2 withholds `pr_url`/`pr_role` when the task link was a guess, and
that refusal is right: a wrong attribution is worse than an absent one, because
absent is refusable and wrong makes a reader act. What was wrong is the
CHANNEL. The refusal was announced only on **stderr**, which the shim's own
contract gives no door a way to read — so on the stored row, a report that
declared an attribution and had it withheld was byte-identical to one that
declared nothing at all. Those need opposite responses: the first says an
attribution exists and `--task` recovers it; the second says none exists.

`link_source` does NOT close this. It says the link was GUESSED, not that
anything was WITHHELD, and most auto-resolved reports declare no PR fields at
all — so `link_source: auto-resolved` with no `pr_role` remains ambiguous
between the two states. That ambiguity is what this pins.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from claudlobby.plane.contracts import PR_WITHHELD_REASONS, TaskEvent
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import _apply_capture
from claudlobby.plane.registries import CONTENT_FIELDS, FIELD_POLICY

REPO = Path(__file__).resolve().parent.parent
WI = "wi_" + "a" * 32
PR = "https://github.com/o/r/pull/7"


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
    def test_it_is_a_string_enum_so_ABSENT_survives(self):
        """A boolean would collapse absent with false under any falsy test a
        reader writes, and absent is load-bearing: nothing declared, or a
        writer predating the field."""
        assert set(PR_WITHHELD_REASONS) == {"guessed_link"}
        absent = TaskEvent(work_item_id=WI, event="completed")
        held = TaskEvent(work_item_id=WI, event="completed",
                         pr_attribution_withheld="guessed_link")
        assert absent.pr_attribution_withheld is None
        assert held.pr_attribution_withheld == "guessed_link"

    @pytest.mark.parametrize("bad", ["true", "yes", "withheld", ""])
    def test_an_unknown_reason_is_refused(self, bad):
        with pytest.raises(Exception):
            TaskEvent(work_item_id=WI, event="completed",
                      pr_attribution_withheld=bad)

    def test_it_is_METADATA_and_survives_a_metadata_capture(self):
        """By RULE, not by accident. A stripped marker reads as "nothing was
        withheld", which a consumer reads as "no attribution was ever
        declared" — the exact false clear this field exists to end, re-entering
        through its own remedy."""
        assert FIELD_POLICY[("task", "pr_attribution_withheld")]["class"] == "METADATA"
        assert "pr_attribution_withheld" not in CONTENT_FIELDS.get("task", ())
        out = _apply_capture(
            {"event_type": "task", "fleet": "f",
             "payload": {"work_item_id": WI, "event": "completed",
                         "summary": "prose",
                         "pr_attribution_withheld": "guessed_link"}},
            {"*": "metadata"},
        )["payload"]
        assert "summary" not in out, "positive control: the door must strip content"
        assert out["pr_attribution_withheld"] == "guessed_link"
