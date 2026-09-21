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
import sqlite3

import pytest

from claudlobby.plane.contracts import PR_ROLES, ContractViolation, TaskEvent
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import _apply_capture, emit_batch
from claudlobby.plane.registries import CONTENT_FIELDS, FIELD_POLICY
from tests.test_plane_door_e2e import _bash, _plane_row, armed  # noqa: F401

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

class TestTheWriter:
    """The real `lib/report-back.sh`, through the real plane."""

    def _details(self, tmp_path, where):
        """Stored details matching *where*.

        A refused report emits nothing, so on that path the db is never created
        at all — that absence IS the evidence, and it is spelled out here
        rather than swallowed: the helper returns [] only for a plane that was
        never written to, and any other sqlite failure still raises.
        """
        if not db_path(tmp_path).exists():
            return []
        conn = connect(db_path(tmp_path))
        try:
            rows = conn.execute(
                f"SELECT detail FROM events WHERE {where} AND detail IS NOT NULL").fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return []
        finally:
            conn.close()
        return [json.loads(r[0]) for r in rows]

    def _task_details(self, tmp_path):
        return self._details(tmp_path, "kind='task'")

    def _marker_details(self, tmp_path):
        return self._details(tmp_path, "kind='system' AND event='report_status'")

    def test_an_idd_report_lands_the_role_on_the_task_event(self, tmp_path, armed):
        libdir, env = armed
        assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "work"', env).returncode == 0
        task_id = _plane_row(tmp_path)["task_id"]
        r = _bash(f'"{libdir}/report-back.sh" w1 completed "done"'
                  f' --pr https://github.com/o/r/pull/9 --pr-role authored --task {task_id}', env)
        assert r.returncode == 0, r.stderr
        detail = [d for d in self._task_details(tmp_path) if d.get("pr_url")][0]
        assert detail["pr_role"] == "authored"

    def test_an_IDLESS_report_lands_the_role_on_the_marker_leg(self, tmp_path, armed):
        """The ad-hoc case, and the one the marker leg exists for: a review
        posted for work never dispatched with an id resolves no task. Riding
        only the task leg would drop the role for exactly these — leaving
        `absent`, which a consumer must refuse on, for a report that DECLARED
        a role."""
        libdir, env = armed
        r = _bash(f'"{libdir}/report-back.sh" w1 completed "ad-hoc review"'
                  f' --pr https://github.com/o/r/pull/77 --pr-role reviewed', env)
        assert r.returncode == 0, r.stderr
        markers = self._marker_details(tmp_path)
        assert markers, "positive control: the id-less report landed a marker"
        assert markers[0]["pr_role"] == "reviewed"
        assert markers[0]["pr_url"].endswith("/77")

    def test_a_report_with_no_role_records_none(self, tmp_path, armed):
        """Constraint B at the writer: not passing the flag must leave the key
        absent, never default to a value."""
        libdir, env = armed
        r = _bash(f'"{libdir}/report-back.sh" w1 completed "no role declared"'
                  f' --pr https://github.com/o/r/pull/5', env)
        assert r.returncode == 0, r.stderr
        markers = self._marker_details(tmp_path)
        assert markers and markers[0].get("pr_url"), "positive control: the row landed"
        assert "pr_role" not in markers[0]

    @pytest.mark.parametrize("bad", ["merged", "unknown", "Authored"])
    def test_an_unrecognised_role_is_refused_before_anything_is_sent(self, tmp_path, armed, bad):
        libdir, env = armed
        r = _bash(f'"{libdir}/report-back.sh" w1 completed "x"'
                  f' --pr https://github.com/o/r/pull/1 --pr-role {bad}', env)
        assert r.returncode == 2
        assert "authored or reviewed" in r.stderr
        assert not self._task_details(tmp_path) and not self._marker_details(tmp_path)

    def test_the_flags_may_be_given_in_either_order(self, tmp_path, armed):
        """The requires-a-PR guard runs after the whole parse loop, so
        --pr-role before --pr must be accepted. A guard that checked as it
        parsed would refuse this, and refuse it only sometimes."""
        libdir, env = armed
        r = _bash(f'"{libdir}/report-back.sh" w1 completed "role first"'
                  f' --pr-role reviewed --pr https://github.com/o/r/pull/8', env)
        assert r.returncode == 0, r.stderr
        markers = self._marker_details(tmp_path)
        assert markers and markers[0]["pr_role"] == "reviewed"

    def test_a_role_without_a_pr_is_refused(self, tmp_path, armed):
        """A role names nothing without the PR it is about: the consumer joins
        the two through `pr_url`, so such a row could never be read back."""
        libdir, env = armed
        r = _bash(f'"{libdir}/report-back.sh" w1 completed "x" --pr-role authored', env)
        assert r.returncode == 2
        assert "needs --pr" in r.stderr
