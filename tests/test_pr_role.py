"""`pr_role` — the field and its writer (#1666).

A fleet shares one GitHub login, so GitHub cannot answer the one question the
`--admin` merge gate's rung 1 asks: *is the reviewer a different bot than the
author?* Nothing can backfill it either — the plane cannot record a role nobody
wrote down — so the role is captured at the moment it happens or it is
permanently unrecoverable.

Two constraints are load-bearing and each has its own test here, because each
fails SILENTLY and in the passing direction:

**A. METADATA, never CONTENT.** Content is what a metadata-mode capture strips.
A stripped role reads as "no role recorded"; a consumer reads that as "not an
author" and merges. That is the exact defect the field exists to prevent,
re-entering through its own remedy.

**B. Absent must stay distinguishable from `reviewed`.** 19% of in-epoch PRs
have no citing report at all. Those rows are unattributable and the eventual
consumer must REFUSE for them. A vocabulary with an `unknown` member, or a
writer that defaulted a role, would convert an absence the consumer can refuse
on into a value it might accept.
"""

from __future__ import annotations

import json

import pytest

from tests.plane_setup import initialize_plane

from claudlobby.plane.contracts import PR_ROLES, ContractViolation, TaskEvent
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import _apply_capture, emit_batch
from claudlobby.plane.registries import CONTENT_FIELDS, FIELD_POLICY

WI = "wi_" + "a" * 32


# --- Constraint A: a metadata capture must not strip it ------------------------

class TestConstraintAMetadataNotContent:
    def test_the_registry_classifies_it_METADATA(self):
        """Registered EXPLICITLY, not merely left out. An unregistered field is
        absent from CONTENT_FIELDS and so survives by accident; this one must
        survive by rule, so that reclassifying it is a visible edit."""
        assert FIELD_POLICY[("task", "pr_role")]["class"] == "METADATA"
        assert "pr_role" not in CONTENT_FIELDS.get("task", ())

    def test_the_real_capture_door_does_NOT_strip_it(self):
        """THE test for constraint A, through `_apply_capture` itself.

        The positive control is the point: `summary` must come back stripped in
        the same call. Without it a door that did nothing at all — wrong mode,
        wrong family, an early return — would satisfy every assertion about
        pr_role surviving.
        """
        raw = {
            "event_type": "task",
            "fleet": "f",
            "payload": {
                "work_item_id": WI, "event": "completed",
                "summary": "reviewed the PR and requested changes",
                "pr_url": "https://github.com/o/r/pull/1", "pr_role": "reviewed",
            },
        }
        out = _apply_capture(raw, {"*": "metadata"})
        payload = out["payload"]

        assert "summary" not in payload, "positive control: the door must have stripped content"
        assert payload["pr_role"] == "reviewed"
        assert payload["pr_url"] == "https://github.com/o/r/pull/1"

    def test_it_survives_a_metadata_capture_all_the_way_into_the_db(self, tmp_path):
        """The same claim end to end: emit under a metadata-mode capture and
        read the stored row back. The unit test above proves the door; this
        proves nothing downstream un-does it."""
        initialize_plane(tmp_path)
        cap = tmp_path / "state" / "plane" / "capture.json"
        cap.parent.mkdir(parents=True, exist_ok=True)
        cap.write_text('{"*": "metadata"}')
        emit_batch(tmp_path, [{
            "event_type": "task", "emitter": "t", "fleet": "f",
            "payload": {
                "work_item_id": WI, "event": "completed",
                "summary": "some prose the policy withholds",
                "pr_url": "https://github.com/o/r/pull/2", "pr_role": "authored",
            },
        }])
        conn = connect(db_path(tmp_path))
        detail = json.loads(
            conn.execute("SELECT detail FROM events WHERE kind='task'").fetchone()[0]
        )
        conn.close()

        assert "summary" not in detail, "positive control: content must be withheld"
        assert detail["pr_role"] == "authored"


# --- Constraint B: absent is its own state -------------------------------------

class TestConstraintBAbsentIsDistinguishable:
    def test_absent_is_not_reviewed_and_not_authored(self):
        absent = TaskEvent(work_item_id=WI, event="completed")
        reviewed = TaskEvent(work_item_id=WI, event="completed", pr_role="reviewed")
        assert absent.pr_role is None
        assert reviewed.pr_role == "reviewed"
        assert absent.pr_role != reviewed.pr_role

    def test_an_absent_role_is_not_stored_at_all_rather_than_stored_empty(self, tmp_path):
        """A row carrying `pr_role: ""` or `pr_role: null` would make a
        consumer's "is this field present" test answer yes for a report that
        declared nothing. The detail dict drops None, so absent means the key
        is simply not there."""
        initialize_plane(tmp_path)
        emit_batch(tmp_path, [{
            "event_type": "task", "emitter": "t", "fleet": "f",
            "payload": {"work_item_id": WI, "event": "completed",
                        "pr_url": "https://github.com/o/r/pull/3"},
        }])
        conn = connect(db_path(tmp_path))
        detail = json.loads(
            conn.execute("SELECT detail FROM events WHERE kind='task'").fetchone()[0]
        )
        conn.close()
        assert "pr_url" in detail, "positive control: the row was stored"
        assert "pr_role" not in detail

    def test_the_vocabulary_has_no_unknown_member(self):
        """An `unknown` member would be a writable value meaning "no answer",
        which is precisely what must stay unwritable — absence is the consumer's
        signal to refuse."""
        assert set(PR_ROLES) == {"authored", "reviewed"}

    @pytest.mark.parametrize("bad", ["merged", "unknown", "", "AUTHORED", "author"])
    def test_an_unrecognised_role_is_refused_not_recorded(self, bad):
        with pytest.raises((ContractViolation, ValueError)):
            TaskEvent(work_item_id=WI, event="completed", pr_role=bad)


# --- The writer ----------------------------------------------------------------
