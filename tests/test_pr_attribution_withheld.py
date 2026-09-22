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
import re
import sqlite3
from pathlib import Path

import pytest

from claudlobby.plane.contracts import PR_WITHHELD_REASONS, TaskEvent
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import _apply_capture
from claudlobby.plane.registries import CONTENT_FIELDS, FIELD_POLICY
from tests.test_plane_door_e2e import _bash, _plane_row, armed  # noqa: F401

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


class TestOneDiscriminatorOnly:
    def test_the_stamp_derives_from_the_gate_it_describes(self):
        """It must be decided off the SAME `TASK_NAMED` the refusal uses, never
        a second predicate. Two places deciding one fact is how they drift, and
        here they would drift silently — both answers look equally plausible on
        a stored row (#1713's rule, applied again)."""
        src = (REPO / "lib" / "report-back.sh").read_text()
        stamps = re.findall(r'pr_attribution_withheld\\":\\"guessed_link', src)
        assert len(stamps) == 1, f"stamped in {len(stamps)} places; must be one"
        # it sits in the ELSE of the attribution gate, not in a new condition
        gate = re.search(
            r'if \[ -n "\$TASK_NAMED" \]; then.*?pr_attribution_withheld',
            src, re.S)
        assert gate, "the stamp is not inside the TASK_NAMED attribution gate"


class TestTheWriter:
    def _declared(self, libdir, env, summary, *, task=None):
        flags = f'--pr {PR} --pr-role reviewed'
        if task:
            flags += f" --task {task}"
        return _bash(
            f'"{libdir}/report-back.sh" w1 completed "{summary}" {flags}', env)

    def test_declared_and_AUTO_RESOLVED_stamps_the_withholding(self, tmp_path, armed):
        """The case the field exists for."""
        libdir, env = armed
        assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "tracked"', env).returncode == 0
        r = self._declared(libdir, env, "withheld here")
        assert r.returncode == 0, r.stderr
        rows = [d for d in _task_details(tmp_path) if d.get("summary") == "withheld here"]
        assert rows, "positive control: the report produced a task event at all"
        assert rows[0].get("link_source") == "auto-resolved", "precondition"
        assert rows[0].get("pr_attribution_withheld") == "guessed_link"
        # and the attribution itself is still refused — that gate is unchanged
        assert "pr_role" not in rows[0] and "pr_url" not in rows[0]

    def test_declared_and_NAMED_does_NOT_stamp_it(self, tmp_path, armed):
        """Positive control. Nothing was withheld, so the marker must be
        absent — and the attribution itself rides instead. Without this a
        stamp applied unconditionally passes the test above."""
        libdir, env = armed
        assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "tracked"', env).returncode == 0
        tid = _plane_row(tmp_path)["task_id"]
        r = self._declared(libdir, env, "attributed", task=tid)
        assert r.returncode == 0, r.stderr
        rows = [d for d in _task_details(tmp_path) if d.get("summary") == "attributed"]
        assert rows and rows[0].get("pr_role") == "reviewed"
        assert "pr_attribution_withheld" not in rows[0], rows[0]

    def test_NOTHING_declared_does_NOT_stamp_it(self, tmp_path, armed):
        """THE discrimination this field is for, and the one `link_source`
        cannot make. Same auto-resolved link as the first test, but nothing was
        declared — so nothing was withheld, and the row must say so by staying
        absent. If this stamped too, the field would mean 'the link was
        guessed', which `link_source` already says."""
        libdir, env = armed
        assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "tracked"', env).returncode == 0
        r = _bash(f'"{libdir}/report-back.sh" w1 completed "nothing declared"', env)
        assert r.returncode == 0, r.stderr
        rows = [d for d in _task_details(tmp_path) if d.get("summary") == "nothing declared"]
        assert rows, "positive control: a task event landed"
        assert rows[0].get("link_source") == "auto-resolved", "precondition"
        assert "pr_attribution_withheld" not in rows[0], rows[0]
