"""Unit tests for lib/dispatch-overdue.py — the dispatch watchdog matcher,
including the P4 task-id join matrix (semantics: overdue_all docstring).

THE PLANE IS THE ONLY SOURCE (F18 closure, R2a). Every fixture below is still
written in the legacy ledger shape (`dispatch_row` / `report_row`, the rows
the doors once appended to `dispatch-log.jsonl` and `report-back.jsonl`) and
is LANDED on a throwaway plane exactly as the live doors would have landed
it — a dispatch as its work item + assignment + communication (keyed
`dispatch-log:<task_id>`, or `dispatch-log:sha:<key>` for an id-less send),
a report as its `report` communication plus the task event the report door
lands when it can link the report (an id'd terminal report on its own
assignment; an id-less terminal report on EVERY open id-less assignment of
the bot, the door's `--open-idless` closer; a linked progress report as a
`progress` task event), else the `report_status` marker; a `--supersedes`
declaration as a terminal `superseded` task event on the retired assignment.
The matcher then answers from that plane. The join semantics pinned here are
the matcher's contract; only the source of the rows changed.

Retired with the legacy readers, and why:
  * TestMissingIdCounter — `missing_id_count` counted terminal report rows
    carrying no task id off the report ledger; there is no ledger.
  * TestReportLedgerRefusal — the absent / unopenable / directory-shaped
    report ledger and the #1418 stale-head rule were refusals about a FILE;
    the plane's equivalent (unreachable is not empty, rc 3, nothing on
    stdout) is pinned in TestUnreachableIsNotEmpty.
  * the `--source` refusals and the ledger-slot ordering probes — there is
    no source to choose and no ledger slots to mis-order. The bot-slot SHAPE
    gate (#1187) stays: a path, a `.jsonl` name or an empty string in the bot
    slot is still rc 2, and single-bot mode (which shared the hazard) is gone.
  * single-bot mode — the plane readers answer per fleet; one bot is
    `--open` / `--open-task`, every bot is `--all` / `--orphans` /
    `--unassigned`.
Two semantics moved from the matcher to the DISPATCH and REPORT DOORS, where
the plane records them at emission time, and are no longer this module's to
enforce (see the notes on the tests that pinned them): a terminal report that
names an id the plane cannot link closes nothing — not even an id-less
dispatch (the legacy ledger's blanket "any later terminal report" rule went
with it), and a `--supersedes` retires whatever assignment carries the named
id when the door records it.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claudlobby.plane.emit_api import emit_batch
from tests.conftest import (
    dispatch_row as _dispatch,
    load_lib_module,
    report_row as _report,
)
from tests.plane_fixtures import F, MATCHER, plane_root

dispatch_overdue = load_lib_module("dispatch-overdue")

_TERMINAL = {"completed": "completed", "failed": "failed", "blocked": "returned_blocked"}


# --- the rig: legacy-shaped rows, landed as the doors land them ----------------

def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()


def _epoch(ts: str) -> int:
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())


class _Plane:
    """One throwaway plane per test. `land(dispatches, reports)` applies rows in
    ledger order and tracks what the doors would have known at each instant
    (which assignments exist, which are still open), so the report door's
    id-less closer and the dispatch door's supersession land on the same rows
    the live doors would have chosen."""

    def __init__(self, tmp_path: Path):
        self.root = plane_root(tmp_path)
        self.n = 0
        self.by_id: dict[tuple[str, str], tuple[str, str, int]] = {}   # (bot, task_id) -> (wi, asg, at)
        self.idless: list[tuple[str, str, str, int]] = []              # (bot, wi, asg, at)
        self.closed: set[str] = set()                                  # assignment ids closed (any terminal)

    def _mint(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_{self.n:0>32x}"

    def _emit(self, events: list[dict]) -> None:
        out = emit_batch(self.root, events)
        bad = [o for o in out if o.status not in ("committed", "duplicate")]
        assert not bad, bad

    def dispatch(self, row: dict) -> None:
        bot = str(row.get("bot", ""))
        key = bot.lower()
        da = row.get("dispatched_at")
        if not isinstance(da, int):
            return                                  # the legacy first gate: no instant, no row
        tid = row.get("task_id")
        wi, asg, msg = self._mint("wi"), self._mint("asg"), self._mint("msg")
        if tid:
            ref = f"dispatch-log:{tid}"
        else:
            ref = "dispatch-log:sha:" + hashlib.sha256(f"{bot}:{da}:{self.n}".encode()).hexdigest()[:32]
        at = _iso(da)
        exp = row.get("expected_by")
        payload_asg = {"assignment_id": asg, "work_item_id": wi, "assignee": f"bot:{F}/{bot}",
                       "assigned_by": f"bot:{F}/lead", "dispatch_msg_id": msg}
        if isinstance(exp, int):
            payload_asg["expected_by"] = _iso(exp)
        events = [
            {"event_type": "work_item", "emitter": "dispatch-task", "fleet": F, "source_ref": ref,
             "occurred_at": at, "payload": {"work_item_id": wi, "title": str(row.get("task") or "t"),
                                            "created_by": f"bot:{F}/lead"}},
            {"event_type": "assignment", "emitter": "dispatch-task", "fleet": F, "source_ref": ref,
             "occurred_at": at, "payload": payload_asg},
            {"event_type": "communication", "emitter": "dispatch-task", "fleet": F, "source_ref": ref,
             "occurred_at": at, "payload": {"msg_id": msg, "sender": f"bot:{F}/lead",
                                            "recipient": f"bot:{F}/{bot}", "message_class": "task_request",
                                            "command_type": "task", "work_item_id": wi,
                                            "assignment_id": asg, "body": "t"}},
        ]
        # --supersedes: the door records a terminal `superseded` on the retired
        # assignment when the plane holds the named id (scoped to the bot here,
        # the rule this suite pins; the door's own lookup is by id alone).
        sup = row.get("supersedes")
        if sup and (key, str(sup)) in self.by_id:
            _wi, old_asg, _at = self.by_id[(key, str(sup))]
            events.append({"event_type": "task", "emitter": "dispatch-task", "fleet": F, "source_ref": ref,
                           "occurred_at": at, "payload": {"work_item_id": _wi, "assignment_id": old_asg,
                                                          "event": "superseded", "successor_id": asg}})
            self.closed.add(old_asg)
        self._emit(events)
        if tid:
            self.by_id[(key, str(tid))] = (wi, asg, da)
        else:
            self.idless.append((key, wi, asg, da))

    def report(self, row: dict) -> None:
        bot = str(row.get("bot", ""))
        key = bot.lower()
        ts = str(row.get("ts", ""))
        at_epoch = _epoch(ts)
        at = _iso(at_epoch)
        status = str(row.get("status", ""))
        tid = row.get("task_id")
        msg = self._mint("msg")
        ref = f"report-back:{msg}"
        actor = f"bot:{F}/{bot}"
        link = self.by_id.get((key, str(tid))) if tid else None
        comm_payload = {"msg_id": msg, "sender": actor, "recipient": f"bot:{F}/lead",
                        "recipient_raw": "lead", "message_class": "report", "body": "r"}
        if link:
            comm_payload.update({"work_item_id": link[0], "assignment_id": link[1]})
        events = [{"event_type": "communication", "emitter": "report-back", "fleet": F, "source_ref": ref,
                   "occurred_at": at, "payload": comm_payload}]
        ev = _TERMINAL.get(status) or ("progress" if status == "progress" else None)
        if link and ev:
            events.append({"event_type": "task", "emitter": "report-back", "fleet": F, "source_ref": ref,
                           "occurred_at": at, "payload": {"work_item_id": link[0], "assignment_id": link[1],
                                                          "event": ev, "actor": actor}})
            if ev != "progress":
                self.closed.add(link[1])
        elif not tid and status in _TERMINAL:
            # the id-less closer: every OPEN id-less assignment of the bot, as of now
            landed = 0
            for b, wi, asg, da in self.idless:
                if b == key and asg not in self.closed and da <= at_epoch:
                    events.append({"event_type": "task", "emitter": "report-back", "fleet": F,
                                   "source_ref": ref, "occurred_at": at,
                                   "payload": {"work_item_id": wi, "assignment_id": asg,
                                               "event": _TERMINAL[status], "actor": actor}})
                    self.closed.add(asg)
                    landed += 1
            if not landed:
                events.append(self._marker(ref, actor, status, msg, at))
        elif status in _TERMINAL:
            # an id'd terminal report the plane cannot link: the marker only
            events.append(self._marker(ref, actor, status, msg, at))
        self._emit(events)

    @staticmethod
    def _marker(ref, actor, status, msg, at):
        return {"event_type": "system", "emitter": "report-back", "fleet": F, "source_ref": ref,
                "occurred_at": at, "payload": {"event": "report_status", "subject_kind": "actor",
                                               "subject": actor, "data": {"status": status, "msg_id": msg}}}

    def land(self, dispatches=(), reports=()) -> "_Plane":
        for d in dispatches:
            self.dispatch(d)
        for r in reports:
            self.report(r)
        return self


def _land(tmp_path, dispatches=(), reports=()) -> _Plane:
    return _Plane(tmp_path).land(dispatches, reports)


def _overdue(plane: _Plane, bot: str, now: int, max_age=None) -> list:
    """The retired single-bot mode, as a filter over the fleet's overdue set."""
    kw = {"fleet": F, "root": str(plane.root)}
    if max_age is not None:
        return dispatch_overdue.overdue_all(now, max_age, **kw).get(bot.lower(), [])
    return dispatch_overdue.overdue_all(now, **kw).get(bot.lower(), [])


def _open(plane: _Plane, bot: str) -> list:
    return dispatch_overdue.open_dispatches(bot, fleet=F, root=str(plane.root))


def _head(plane: _Plane, bot: str):
    return dispatch_overdue.open_task_id(bot, fleet=F, root=str(plane.root))


def _argv(plane: _Plane, *args) -> list[str]:
    return ["dispatch-overdue.py", *args, "--fleet", F, "--root", str(plane.root)]


# --- overdue ---------------------------------------------------------------------


class TestOverdue:
    def test_not_yet_due(self, tmp_path):
        p = _land(tmp_path, [_dispatch("eng-1", 1000, 2000)])
        # now (1500) < expected_by (2000) → not overdue
        assert _overdue(p, "eng-1", 1500) == []

    def test_overdue_no_report(self, tmp_path):
        p = _land(tmp_path, [_dispatch("eng-1", 1000, 2000)])
        assert _overdue(p, "eng-1", 2600) == [(1000, 2000, 600, "-")]

    def test_closed_by_terminal_report(self, tmp_path):
        # report at 2026-05-27T10:30:00Z is after dispatch → closes it
        p = _land(tmp_path, [_dispatch("eng-1", 1000, 2000)],
                  [_report("eng-1", "2026-05-27T10:30:00Z", "completed")])
        assert _overdue(p, "eng-1", 9999999999) == []

    def test_stale_report_before_dispatch_does_not_close(self, tmp_path):
        # dispatched_at corresponds to a time AFTER this old report
        p = _land(tmp_path, [_dispatch("eng-1", 1800000000, 1800000600)],
                  [_report("eng-1", "2020-01-01T00:00:00Z", "completed")])
        assert len(_overdue(p, "eng-1", 1800001000)) == 1

    def test_progress_report_does_not_close(self, tmp_path):
        p = _land(tmp_path, [_dispatch("eng-1", 1000, 2000)],
                  [_report("eng-1", "2026-05-27T10:30:00Z", "progress")])
        assert len(_overdue(p, "eng-1", 2600)) == 1

    def test_other_bot_ignored(self, tmp_path):
        p = _land(tmp_path, [_dispatch("eng-2", 1000, 2000)])
        assert _overdue(p, "eng-1", 2600) == []

    def test_case_insensitive_bot_match(self, tmp_path):
        # report (lowercase) should close dispatch (mixed case)
        p = _land(tmp_path, [_dispatch("Eng-1", 1000, 2000)],
                  [_report("eng-1", "2026-05-27T10:30:00Z", "completed")])
        assert _overdue(p, "eng-1", 9999999999) == []

    def test_no_plane_is_unreachable_not_empty(self, tmp_path):
        """The retired `test_missing_files` inverted: two missing ledgers used
        to read as an empty fleet; a missing plane REFUSES."""
        with pytest.raises(dispatch_overdue.PlaneUnreachable):
            dispatch_overdue.overdue_all(99, fleet=F, root=str(tmp_path / "no-plane"))


class TestExpiryCap:
    """#460: a never-closing dispatch must stop being reported overdue past max_age,
    so fleet-pulse stops re-emitting overdue_dispatch every cycle forever."""

    def test_stale_open_dispatch_expires(self, tmp_path):
        p = _land(tmp_path, [_dispatch("eng-1", 1000, 2000)])
        # Past deadline, never reported, but age (100000s) > cap (3600s) → expired.
        assert _overdue(p, "eng-1", 101000, max_age=3600) == []

    def test_within_max_age_still_overdue(self, tmp_path):
        p = _land(tmp_path, [_dispatch("eng-1", 1000, 2000)])
        # age (3000s) < cap (3600s), past deadline → still overdue.
        assert _overdue(p, "eng-1", 4000, max_age=3600) == [(1000, 2000, 2000, "-")]

    def test_default_cap_is_24h(self, tmp_path):
        p = _land(tmp_path, [_dispatch("mason", 1000, 2000)])
        # Just under 24h → overdue; just over → expired — under the DEFAULT (no max_age passed).
        assert len(_overdue(p, "mason", 1000 + 86399)) == 1
        assert _overdue(p, "mason", 1000 + 86401) == []

    def test_cap_disabled_with_zero(self, tmp_path):
        p = _land(tmp_path, [_dispatch("eng-1", 1000, 2000)])
        # max_age=0 disables the cap → an ancient open dispatch still counts as overdue.
        assert len(_overdue(p, "eng-1", 10_000_000, max_age=0)) == 1

    def test_never_closing_dispatch_stops_being_flagged(self, tmp_path):
        """#460 anchor: a never-reported dispatch drops out of the overdue set past the
        cap, so fleet-pulse (which emits only what the matcher returns) stops re-emitting
        overdue_dispatch. The matcher is stateless, so one past-cap empty result is the
        guarantee for this cycle and every later one."""
        p = _land(tmp_path, [_dispatch("eng-1", 1000, 2000)])
        # Far past the default 24h cap, never reported → nothing to emit.
        assert _overdue(p, "eng-1", 1000 + 500000) == []

    def test_closed_report_recognized_even_when_aged(self, tmp_path):
        """A dispatch closed by a terminal report is closed, not merely expired —
        the closure still short-circuits before the age cap."""
        p = _land(tmp_path, [_dispatch("eng-1", 1000, 2000)],
                  [_report("eng-1", "2026-05-27T10:30:00Z", "completed")])
        assert _overdue(p, "eng-1", 1000 + 999999) == []


# --- join matrix ------------------------------------------------------------------


class TestJoinMatrix:
    NOW = 2000

    def _run(self, tmp_path, dispatches, reports):
        p = _land(tmp_path, dispatches, reports)
        return dispatch_overdue.overdue_all(self.NOW, fleet=F, root=str(p.root))

    def test_id_report_closes_exactly_its_own_dispatch(self, tmp_path):
        out = self._run(
            tmp_path,
            [
                _dispatch("w1", 100, 1000, task_id="t-100-aaaa"),
                _dispatch("w1", 200, 1000, task_id="t-200-bbbb"),
            ],
            [_report("w1", "1970-01-01T00:05:00Z", task_id="t-100-aaaa")],
        )
        assert [d[0] for d in out.get("w1", [])] == [200], (
            "the un-reported sibling dispatch must stay open"
        )

    def test_idless_terminal_never_closes_an_id_dispatch(self, tmp_path):
        # The #447 fix, preserved: one id-less report must not blanket-close.
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
            [_report("w1", "1970-01-01T00:05:00Z")],  # no task_id
        )
        assert out.get("w1"), "id'd dispatch must remain overdue"

    def test_idless_terminal_closes_idless_dispatch(self, tmp_path):
        # An id-less dispatch closes on the bot's next terminal report — the
        # report door lands the terminal event on every open id-less assignment.
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000)],
            [_report("w1", "1970-01-01T00:05:00Z")],
        )
        assert not out.get("w1")

    def test_an_unlinkable_id_report_closes_nothing_not_even_an_idless_dispatch(self, tmp_path):
        """MOVED SEMANTIC (F18 R2a). The legacy ledger's blanket rule — ANY
        later terminal report, id'd or not, closed an id-less dispatch — is
        gone with the ledger: the report door closes id-less assignments only
        from an id-LESS terminal report (`--open-idless`), and an id'd report
        whose id the plane cannot link lands its status marker and nothing
        else. The old assertion (`not out.get("w1")`) inverts here."""
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000)],
            [_report("w1", "1970-01-01T00:05:00Z", task_id="t-999-ffff")],
        )
        assert out.get("w1"), "an unlinkable id'd report closed an id-less dispatch"

    def test_report_from_wrong_bot_does_not_close(self, tmp_path):
        # Review finding (#518): the id join must be scoped by (bot, id) —
        # a peer echoing (or mishearing) another bot's task id must not
        # silence the watchdog on the real owner's still-open dispatch.
        out = self._run(
            tmp_path,
            [_dispatch("worker-a", 100, 1000, task_id="t-100-aaaa")],
            [_report("worker-b", "1970-01-01T00:05:00Z", task_id="t-100-aaaa")],
        )
        assert out.get("worker-a"), "wrong-bot report must not close the dispatch"

    def test_wrong_id_does_not_close(self, tmp_path):
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
            [_report("w1", "1970-01-01T00:05:00Z", task_id="t-777-dddd")],
        )
        assert out.get("w1")

    def test_progress_report_with_id_defers_but_never_closes(self, tmp_path):
        """Progress DEFERS the alarm while it is fresh and never closes the dispatch.

        Both halves matter. Asserting only the deferral would pass against a change
        that silenced the row permanently — which is closing by another name, and the
        thing this test originally existed to forbid.
        """
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
                  [_report("w1", "1970-01-01T00:05:00Z", status="progress", task_id="t-100-aaaa")])
        kw = {"fleet": F, "root": str(p.root)}
        # progress at epoch 300; at NOW=2000 it is 1700s old, inside the grace
        assert not dispatch_overdue.overdue_all(self.NOW, **kw).get("w1")
        # once the grace lapses the row is overdue again — deferred, never closed
        later = 300 + dispatch_overdue.DEFAULT_PROGRESS_GRACE_S + 1
        assert dispatch_overdue.overdue_all(later, **kw).get("w1"), (
            "a progress report must never permanently close a dispatch"
        )


class TestOrphanSplit:
    """#835 — a past-deadline row whose worker RESPAWNED after dispatch.

    The session holding the task id is gone, so the id can never be echoed and
    the row would alarm every cycle until it aged out. Split it out of overdue,
    but keep it listable: a task lost to a restart is evidence, not noise.
    """

    NOW = 2000

    def _bots_dir(self, tmp_path, bot, spawn_epoch):
        data = tmp_path / "bots" / bot / "data"
        data.mkdir(parents=True, exist_ok=True)
        marker = data / ".spawn"
        marker.write_text("")
        os.utime(marker, (spawn_epoch, spawn_epoch))
        return str(tmp_path / "bots")

    def _sets(self, p, bots=None):
        kw = {"fleet": F, "root": str(p.root)}
        return (dispatch_overdue.overdue_all(self.NOW, bots_dir=bots, **kw),
                dispatch_overdue.orphaned_all(self.NOW, bots_dir=bots, **kw))

    def test_respawn_after_dispatch_moves_row_out_of_overdue(self, tmp_path):
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")])
        bots = self._bots_dir(tmp_path, "w1", 500)  # respawned AFTER dispatch
        over, orphans = self._sets(p, bots)
        assert over == {}
        assert [d[3] for d in orphans["w1"]] == ["t-100-aaaa"], (
            "the orphan must stay listable — reaping it silently deletes the "
            "evidence that a task was lost to a restart"
        )

    def test_same_incarnation_still_reports_overdue(self, tmp_path):
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")])
        bots = self._bots_dir(tmp_path, "w1", 50)  # spawned BEFORE dispatch
        over, orphans = self._sets(p, bots)
        assert "w1" in over and orphans == {}

    def test_without_bots_dir_nothing_is_orphaned(self, tmp_path):
        """No marker access => keep alarming. Never retire a row on a guess."""
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")])
        over, orphans = self._sets(p)
        assert "w1" in over and orphans == {}

    def test_missing_spawn_marker_keeps_row_overdue(self, tmp_path):
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")])
        (tmp_path / "bots").mkdir()
        over, _ = self._sets(p, str(tmp_path / "bots"))
        assert "w1" in over

    def test_idless_dispatch_never_orphans(self, tmp_path):
        """An id-less row closes on the bot's next terminal report, so a
        respawned worker's next report still retires it — nothing to remember,
        nothing to orphan."""
        p = _land(tmp_path, [_dispatch("w1", 100, 1000)])
        bots = self._bots_dir(tmp_path, "w1", 500)
        over, orphans = self._sets(p, bots)
        assert "w1" in over and orphans == {}

    def test_a_closed_row_is_neither_overdue_nor_orphan(self, tmp_path):
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
                  [_report("w1", "1970-01-01T00:05:00Z", task_id="t-100-aaaa")])
        bots = self._bots_dir(tmp_path, "w1", 500)
        assert self._sets(p, bots) == ({}, {})


class TestOpenTaskResolution:
    """#835 — the id report-back.sh supplies when the worker omits --task."""

    def test_resolves_the_oldest_open_dispatch(self, tmp_path):
        """Oldest, not newest: the oldest is the row past its deadline and
        alarming, and it is what a serial FIFO worker just finished. Rows are
        landed out of dispatch order to pin that this is time-ordered, not
        ingest-ordered."""
        p = _land(tmp_path, [
            _dispatch("w1", 300, 1000, task_id="t-300-cccc"),
            _dispatch("w1", 100, 1000, task_id="t-100-aaaa"),
            _dispatch("w1", 200, 1000, task_id="t-200-bbbb"),
        ])
        assert _head(p, "w1") == "t-100-aaaa"

    def test_concurrent_dispatches_retire_in_dispatch_order(self, tmp_path):
        """The normal case, not an edge one — most active bots carry 2-3 open.
        Each report closes exactly one row, oldest first, so a sequence of
        reports drains the queue in the order it was sent."""
        p = _land(tmp_path, [
            _dispatch("w1", 100, 1000, task_id="t-100-aaaa"),
            _dispatch("w1", 200, 1000, task_id="t-200-bbbb"),
            _dispatch("w1", 300, 1000, task_id="t-300-cccc"),
        ])
        drained = []
        for _ in range(3):
            tid = _head(p, "w1")
            drained.append(tid)
            p.report(_report("w1", "1970-01-01T00:20:00Z", task_id=tid))
        assert drained == ["t-100-aaaa", "t-200-bbbb", "t-300-cccc"]
        assert _head(p, "w1") is None

    def test_skips_already_closed_dispatches(self, tmp_path):
        p = _land(tmp_path, [
            _dispatch("w1", 100, 1000, task_id="t-100-aaaa"),
            _dispatch("w1", 300, 1000, task_id="t-300-cccc"),
        ], [_report("w1", "1970-01-01T00:10:00Z", task_id="t-300-cccc")])
        assert _head(p, "w1") == "t-100-aaaa"

    def test_none_when_nothing_open(self, tmp_path):
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
                  [_report("w1", "1970-01-01T00:10:00Z", task_id="t-100-aaaa")])
        assert _head(p, "w1") is None

    def test_scoped_to_the_bot(self, tmp_path):
        """A peer's open dispatch must never be handed to this bot — that is
        the cross-bot leak the watchdog join is deliberately scoped against."""
        p = _land(tmp_path, [_dispatch("w2", 300, 1000, task_id="t-300-cccc")])
        assert _head(p, "w1") is None

    def test_idless_dispatches_are_not_resolvable(self, tmp_path):
        p = _land(tmp_path, [_dispatch("w1", 100, 1000)])
        assert _head(p, "w1") is None

    def test_an_unanswered_idless_dispatch_suppresses_the_resolver(self, tmp_path):
        """#1190: while the bot's NEWEST assignment is id-less and unanswered, a
        terminal report most plausibly answers THAT, so resolving an older
        id'd row would be a false completion — the resolver returns nothing
        until the bot's next terminal report discharges the id-less row."""
        p = _land(tmp_path, [
            _dispatch("w1", 100, 1000, task_id="t-100-aaaa"),
            _dispatch("w1", 200, 1000),                       # a peer note, id-less, newest
        ])
        assert _head(p, "w1") is None
        p.report(_report("w1", "1970-01-01T00:10:00Z"))     # id-less terminal: discharges it
        assert _head(p, "w1") == "t-100-aaaa"

    def test_a_peers_report_does_not_close_this_bots_dispatch(self, tmp_path):
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
                  [_report("w2", "1970-01-01T00:10:00Z", task_id="t-100-aaaa")])
        assert _head(p, "w1") == "t-100-aaaa"


class TestProgressLiveness:
    """The #1390-shaped question: does the alarm distinguish BUSY from STUCK?

    A fixed 30-minute budget was applied to every dispatch (measured 2026-08-04: 66 of
    66), while real tasks routinely ran longer — so the watchdog fired at T+30 on work
    still being done, and by the time the manager read the page the work was merged.
    Paging on finished work trains the reader to ignore the alarm, which costs the day
    something genuinely IS stuck.

    The two halves are inseparable. Silencing a busy worker is only safe if a dead one
    still alarms; a test for either alone passes against a change that breaks the other.

    ON THE PLANE the liveness signal is a `progress` task event by the bot's
    actor, which the report door lands only for a progress report it can
    LINK (one carrying the task id); the fixtures here carry it. An id-less
    progress report lands a communication and no event — see the R2a
    findings on #1467.
    """

    GRACE = 2700  # DEFAULT_PROGRESS_GRACE_S; asserted equal below so it cannot drift
    TID = "t-1000-aaaa"

    def _overdue(self, tmp_path, reports, now):
        # dispatched at 1000, due at 2800 — the real 30-minute budget
        p = _land(tmp_path, [_dispatch("eng-1", 1000, 2800, task_id=self.TID)], reports)
        return _overdue(p, "eng-1", now)

    def _progress(self, ts):
        return _report("eng-1", ts, status="progress", task_id=self.TID)

    def test_grace_matches_the_measured_default(self):
        assert dispatch_overdue.DEFAULT_PROGRESS_GRACE_S == self.GRACE

    def test_a_busy_worker_is_silenced(self, tmp_path):
        """Past deadline, but reported progress 10 minutes ago → working, not stuck."""
        res = self._overdue(tmp_path, [self._progress("1970-01-01T01:00:00Z")],   # epoch 3600
                            now=3600 + 600)
        assert res == [], "a worker reporting progress 10min ago was paged as overdue"

    def test_a_dead_worker_still_alarms(self, tmp_path):
        """Same dispatch, same single progress report — but the worker then went silent.

        This is the half that makes the other half safe. Deferral is bounded by the
        worker's own reporting: stop, and the alarm returns.
        """
        res = self._overdue(tmp_path, [self._progress("1970-01-01T01:00:00Z")],   # epoch 3600
                            now=3600 + self.GRACE + 1)
        assert res, "a worker silent for longer than the grace must still alarm"

    def test_progress_before_the_dispatch_does_not_defer(self, tmp_path):
        """Liveness must be evidence about THIS dispatch's lifetime.

        A progress report from before the work was even handed out says nothing about
        whether the worker is on it now — the same reasoning as the existing
        stale-report-before-dispatch guard for terminal reports.
        """
        # epoch 900 — BEFORE the dispatch at 1000, but only 2100s before now=3000, so
        # it is comfortably inside the 2700s grace. Only the `da < lp` bound can reject
        # it. An earlier draft used epoch 30, which the grace bound rejected on age
        # alone: the test passed while saying nothing about the bound it names, and the
        # mutation run caught it.
        res = self._overdue(tmp_path, [self._progress("1970-01-01T00:15:00Z")], now=3000)
        assert res, "a progress report predating the dispatch deferred the alarm"

    def test_a_future_dated_progress_report_cannot_mute_the_alarm(self, tmp_path):
        """Clock skew or a hand-edited record must not buy silence.

        A report dated ahead of `now` yields a negative age, which satisfies any grace
        bound and would suppress the row forever — a permanent silent mute, the one
        outcome this change must never produce.
        """
        res = self._overdue(tmp_path, [self._progress("2099-01-01T00:00:00Z")], now=3000)
        assert res, "a future-dated progress report muted the alarm"

    def test_grace_of_zero_disables_deferral(self, tmp_path, monkeypatch):
        """The escape hatch, mirroring DISPATCH_OVERDUE_MAX_AGE_S's `<= 0 disables`."""
        monkeypatch.setenv("DISPATCH_PROGRESS_GRACE_S", "0")
        res = self._overdue(tmp_path, [self._progress("1970-01-01T01:00:00Z")], now=3600 + 600)
        assert res, "grace=0 must restore the pre-change behaviour exactly"

    def test_terminal_report_still_closes_regardless_of_progress(self, tmp_path):
        """Deferral must not shadow closure: a finished dispatch reads closed, not
        deferred, so the row never reappears when the grace lapses."""
        res = self._overdue(
            tmp_path,
            [self._progress("1970-01-01T01:00:00Z"),
             _report("eng-1", "1970-01-01T01:05:00Z", status="completed", task_id=self.TID)],
            now=3600 + self.GRACE + 5000,
        )
        assert res == [], "a completed dispatch reappeared after the grace lapsed"


class TestOpenList:
    """#904 — the read door's list form. Same join as the resolver, wider set."""

    def test_lists_every_open_row_oldest_first(self, tmp_path):
        """Landed out of dispatch order, to pin that this is time-ordered."""
        p = _land(tmp_path, [
            _dispatch("w1", 300, 1000, task_id="t-300-cccc"),
            _dispatch("w1", 100, 1000, task_id="t-100-aaaa"),
            _dispatch("w1", 200, 1000, task_id="t-200-bbbb"),
        ])
        assert [t for _, _, t in _open(p, "w1")] == ["t-100-aaaa", "t-200-bbbb", "t-300-cccc"]

    def test_open_task_id_is_this_lists_head(self, tmp_path):
        """One loop, not two: a resolver that could hand back an id this list
        does not contain is the desync class the module exists to prevent."""
        p = _land(tmp_path, [
            _dispatch("w1", 300, 1000, task_id="t-c"),
            _dispatch("w1", 100, 1000, task_id="t-a"),
        ])
        rows = _open(p, "w1")
        assert _head(p, "w1") == rows[0][2]

    def test_is_deadline_blind(self, tmp_path):
        """A row inside its deadline is OPEN but not overdue — the distinction
        the door was added to make readable."""
        p = _land(tmp_path, [_dispatch("w1", 100, 9_999_999, task_id="t-early")])
        assert [t for _, _, t in _open(p, "w1")] == ["t-early"]
        assert _overdue(p, "w1", 1000) == []

    def test_is_a_superset_of_overdue(self, tmp_path):
        p = _land(tmp_path, [
            _dispatch("w1", 100, 1000, task_id="t-late"),
            _dispatch("w1", 200, 9_999_999, task_id="t-early"),
        ])
        open_ids = {t for _, _, t in _open(p, "w1")}
        overdue_ids = {t for *_, t in _overdue(p, "w1", 5000)}
        assert overdue_ids == {"t-late"}
        assert overdue_ids <= open_ids

    def test_terminal_report_removes_the_row(self, tmp_path):
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-a")],
                  [_report("w1", "2026-05-27T10:05:00Z", task_id="t-a")])
        assert _open(p, "w1") == []

    def test_a_peers_report_does_not_close_this_bots_row(self, tmp_path):
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-a")],
                  [_report("w2", "2026-05-27T10:05:00Z", task_id="t-a")])
        assert [t for _, _, t in _open(p, "w1")] == ["t-a"]

    def test_idless_rows_are_not_listed(self, tmp_path):
        """Same gate as the resolver: only id'd rows are addressable."""
        p = _land(tmp_path, [_dispatch("w1", 100, 1000)])
        assert _open(p, "w1") == []

    def test_missing_expected_by_is_None_not_a_filter(self, tmp_path):
        """A row the resolver would still hand back must remain listable, or
        the door hides work that can still be closed."""
        row = _dispatch("w1", 100, 1000, task_id="t-a")
        del row["expected_by"]
        p = _land(tmp_path, [row])
        assert _open(p, "w1") == [(100, None, "t-a")]
        assert _head(p, "w1") == "t-a"

    def test_scoped_to_the_bot(self, tmp_path):
        p = _land(tmp_path, [
            _dispatch("w1", 100, 1000, task_id="t-mine"),
            _dispatch("w2", 100, 1000, task_id="t-theirs"),
        ])
        assert [t for _, _, t in _open(p, "w1")] == ["t-mine"]

    def test_cli_open_mode_prints_rows(self, tmp_path, monkeypatch, capsys):
        row = _dispatch("w1", 100, 1000, task_id="t-a")
        no_deadline = _dispatch("w1", 150, 1000, task_id="t-b")
        del no_deadline["expected_by"]
        p = _land(tmp_path, [row, no_deadline])
        monkeypatch.setattr("sys.argv", _argv(p, "--open", "w1"))
        assert dispatch_overdue.main() == 0
        assert capsys.readouterr().out == "100 1000 t-a\n150 - t-b\n"

    def test_cli_open_mode_is_silent_when_nothing_is_open(self, tmp_path, monkeypatch, capsys):
        p = _land(tmp_path, [_dispatch("w9", 100, 1000, task_id="t-z")])   # the fleet exists; w1 holds nothing
        monkeypatch.setattr("sys.argv", _argv(p, "--open", "w1"))
        assert dispatch_overdue.main() == 0
        assert capsys.readouterr().out == ""

    def test_ties_keep_ingest_order_matching_the_old_strict_min(self, tmp_path):
        """The tie-break was asserted from reading the sort's stability; this
        pins it. `open_task_id` USED to scan with a strict `<`, which keeps the
        FIRST row seen on a tie; the plane orders by instant then ingest
        order, so it must agree — if it ever did not, the resolver would close
        a different dispatch than the one the list shows first, silently."""
        p = _land(tmp_path, [
            _dispatch("w1", 100, 1000, task_id="t-ccc"),
            _dispatch("w1", 100, 1000, task_id="t-bbb"),
            _dispatch("w1", 100, 1000, task_id="t-aaa"),
        ])
        rows = _open(p, "w1")
        assert [t for _, _, t in rows] == ["t-ccc", "t-bbb", "t-aaa"]
        assert _head(p, "w1") == "t-ccc"

    def test_a_tie_at_the_head_still_resolves_to_the_head(self, tmp_path):
        """Tie at the oldest timestamp, with a younger row landed first — so
        ingest order and time order disagree and only the sort can be right."""
        p = _land(tmp_path, [
            _dispatch("w1", 500, 1000, task_id="t-young"),
            _dispatch("w1", 100, 1000, task_id="t-old-z"),
            _dispatch("w1", 100, 1000, task_id="t-old-a"),
        ])
        rows = _open(p, "w1")
        assert [t for _, _, t in rows] == ["t-old-z", "t-old-a", "t-young"]
        assert _head(p, "w1") == rows[0][2] == "t-old-z"

    # --- #1124: the identical-dispatched_at tie-break -------------------------
    #
    # open_task_id resolves the dispatch an id-less report-back closes, which is
    # MOST reports (report-back.sh omits --task in the common path, #847). The
    # resolver is `rows[0]` off open_dispatches()'s instant-then-ingest order.
    # Live harm class, not hypothetical (#878): on 2026-08-08 three ai-platform
    # reports resolved to task ids dispatched 2026-08-04 — a 4.6-day gap. A
    # tie-break regression adds one more way for an id-less report to close the
    # wrong dispatch.
    #
    # THE IDS ARE CHOSEN SO INGEST ORDER AND ALPHABETICAL ORDER DISAGREE. An
    # implementation that broke ties by sorting on task_id would satisfy a
    # same-order-only test by coincidence; with t-bbb landed first, ingest
    # order says t-bbb and alphabetical says t-aaa, so the two are separable.

    def _tied(self, tmp_path, first, second):
        """Two dispatches to one bot with IDENTICAL dispatched_at, in ingest order."""
        return _land(tmp_path, [
            _dispatch("w1", 100, 1000, task_id=first),
            _dispatch("w1", 100, 1000, task_id=second),
        ])

    def test_tie_resolves_to_the_row_landed_first(self, tmp_path):
        p = self._tied(tmp_path, "t-bbb", "t-aaa")
        # Ingest order, NOT the alphabetically-smaller id.
        assert _head(p, "w1") == "t-bbb"

    def test_tie_reversed_order_resolves_to_the_other_row(self, tmp_path):
        """Same two rows, order swapped, other answer. Without this a
        constant-by-coincidence implementation passes."""
        p = self._tied(tmp_path, "t-aaa", "t-bbb")
        assert _head(p, "w1") == "t-aaa"

    def test_tie_answer_is_order_dependent_not_a_fixed_value(self, tmp_path):
        """States the property directly: the two orders must disagree. A single
        assertion no implementation can satisfy by returning a constant."""
        first = _head(self._tied(tmp_path / "a", "t-bbb", "t-aaa"), "w1")
        second = _head(self._tied(tmp_path / "b", "t-aaa", "t-bbb"), "w1")
        assert first != second, "tie-break is not order-dependent"
        assert {first, second} == {"t-aaa", "t-bbb"}

    def test_tie_head_of_open_dispatches_matches_the_resolver(self, tmp_path):
        """The resolver is the list's head — the invariant #904 created and the
        reason a tie-break regression would desync them rather than just
        reorder a display."""
        for i, (first, second) in enumerate((("t-bbb", "t-aaa"), ("t-aaa", "t-bbb"))):
            p = self._tied(tmp_path / str(i), first, second)
            rows = _open(p, "w1")
            assert [t for _, _, t in rows] == [first, second]
            assert _head(p, "w1") == rows[0][2]


class TestBotSlotShapeGate:
    """#1187 — right count, wrong order was silent; wrong count never was.

    --open and --open-task each name one bot and take it first; --all,
    --orphans and --unassigned name none. Calling a bot-slot mode with a path
    in the bot slot used to keep the arity valid, match nothing, and print
    nothing at rc 0 — the same output as a genuinely empty result. That is how
    a manager checking whether its closures had worked read a full backlog as
    all-clear. The ledger slots are gone (R2a) but the class is not: a path in
    the bot slot is still refused, loudly.
    """

    def _rows(self, tmp_path):
        return _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-a")])

    @pytest.mark.parametrize("mode", ["--open", "--open-task"])
    def test_a_path_in_the_bot_slot_is_refused_loudly_not_silently(
        self, tmp_path, monkeypatch, capsys, mode
    ):
        """THE regression this gate exists for. Both doors share the grammar,
        so both share the hazard; a fix on one only would leave the other."""
        p = self._rows(tmp_path)
        monkeypatch.setattr("sys.argv", _argv(p, mode, str(tmp_path / "state" / "dispatch-log.jsonl"), "1786700000"))
        assert dispatch_overdue.main() == 2
        out = capsys.readouterr()
        assert out.out == ""  # never a partial result alongside a refusal
        assert "expects <bot_id> first" in out.err
        # The refusal must name the remedy, not merely reject: the operator's
        # actual error is not knowing the two grammars differ.
        assert "name no bot at all" in out.err

    @pytest.mark.parametrize(
        "bad,label",
        [
            ("/abs/path/dispatch-log.jsonl", "a path"),
            ("relative/dir/name", "a path"),
            ("dispatch-log.jsonl", "a ledger file"),
            ("   ", "an empty bot id"),
        ],
    )
    def test_every_refused_shape_names_what_it_saw(
        self, tmp_path, monkeypatch, capsys, bad, label
    ):
        """A bare filename carries no separator, so the '/' test alone would
        miss the commonest form — invoking from the ledger's own directory."""
        p = self._rows(tmp_path)
        monkeypatch.setattr("sys.argv", _argv(p, "--open", bad))
        assert dispatch_overdue.main() == 2
        assert label in capsys.readouterr().err

    # -- positive control: the gate must not refuse a real bot id ------------

    @pytest.mark.parametrize("bot", ["w1", "gilfoyle", "bot-2", "Worker_3", "a.b"])
    def test_real_bot_ids_pass_the_gate(self, bot):
        """Without this, a gate that refused everything would pass the tests
        above. `a.b` guards the suffix test against becoming a bare dot test."""
        assert dispatch_overdue._not_a_bot_id(bot) is None

    def test_gate_is_inert_for_the_report_back_call_shape(self, tmp_path, monkeypatch, capsys):
        """report-back.sh passes its own $BOT first. Same rc, same stdout as
        before the gate — its fail-open contract is untouched."""
        p = self._rows(tmp_path)
        monkeypatch.setattr("sys.argv", _argv(p, "--open-task", "w1"))
        assert dispatch_overdue.main() == 0
        assert capsys.readouterr().out == "t-a\n"

    @pytest.mark.parametrize("mode", ["--open", "--open-task"])
    def test_a_missing_bot_is_a_usage_error(self, tmp_path, monkeypatch, capsys, mode):
        """No bot at all was already rc 2 before the gate and stays so. Pinned so
        the shape gate is never mistaken for the thing that made misuse loud."""
        p = self._rows(tmp_path)
        monkeypatch.setattr("sys.argv", _argv(p, mode))
        assert dispatch_overdue.main() == 2
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("mode", ["--all", "--orphans", "--unassigned"])
    def test_every_bot_modes_have_no_bot_slot_and_fail_LOUDLY(
        self, tmp_path, monkeypatch, capsys, mode
    ):
        """These name no bot, so there is nothing for the gate to check — and
        handing them one is already loud: the name lands in the `now` slot
        and is refused as not an instant, rc 2. Pins WHY they are excluded, so
        "ungated" is never re-read as "silently broken like #1187"."""
        p = self._rows(tmp_path)
        monkeypatch.setattr("sys.argv", _argv(p, mode, "w1"))
        assert dispatch_overdue.main() == 2
        out = capsys.readouterr()
        assert out.out == "" and "<now_epoch> must be an integer" in out.err

    def test_an_unknown_mode_is_a_usage_error_that_names_the_modes(self, tmp_path, monkeypatch, capsys):
        """The retired single-bot grammar (`<bot> <dlog> <rlog>`) must not fall
        through to anything: a first positional that is not a mode is rc 2."""
        p = self._rows(tmp_path)
        monkeypatch.setattr("sys.argv", _argv(p, "w1", "2000"))
        assert dispatch_overdue.main() == 2
        out = capsys.readouterr()
        assert out.out == "" and "--open-task" in out.err


class TestOpenScopeDisclosure:
    """#1187 — an empty result that names its own scope cannot be misread.

    The shape gate above kills the instance; this kills the class. A plausible
    but WRONG bot — a typo, or a live name of another fleet — passes every
    shape test and still returns zero rows at rc 0. Coverage honesty applied
    to a read door: state the bound.
    """

    def test_empty_result_names_the_bot_it_filtered_on(self, tmp_path, monkeypatch, capsys):
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-a")])
        monkeypatch.setattr("sys.argv", _argv(p, "--open", "typo-bot"))
        assert dispatch_overdue.main() == 0
        out = capsys.readouterr()
        assert out.out == ""
        assert "typo-bot" in out.err and "0 open" in out.err

    def test_scope_is_stated_on_a_NON_empty_result_too(self, tmp_path, monkeypatch, capsys):
        """Always, not only when empty. 'which bot' is part of reading the
        answer, and a line that appears only on zero makes its own presence
        the signal."""
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-a")])
        monkeypatch.setattr("sys.argv", _argv(p, "--open", "w1"))
        assert dispatch_overdue.main() == 0
        out = capsys.readouterr()
        assert out.out == "100 1000 t-a\n"
        assert "w1" in out.err and "1 open" in out.err

    def test_scope_line_NEVER_reaches_stdout(self, tmp_path, monkeypatch, capsys):
        """Load-bearing, not stylistic. report-back.sh pipes this stdout
        through `awk {print $3}` to decide whether a supplied --task id is open,
        and only a NON-EMPTY open set may contradict the caller (#1146). On
        stdout the scope line becomes a phantom row whose field 3 is "->".

        NOTE THE SHAPE: the bot holding NOTHING open is not incidental. A bot
        that holds an open row still matches its own id — the phantom adds an
        entry rather than displacing the real one — so that case stays clean
        and would read as proof the placement is free. It bites with nothing
        open, where a valid id meets an open set of exactly ["->"] and a
        correct report is flagged `supplied-id-not-open`.

        Assert the whole stream, so no header can slip in beside the rows.
        """
        p = _land(tmp_path, [_dispatch("w9", 100, 1000, task_id="t-z")])   # the fleet exists; w1 holds nothing
        monkeypatch.setattr("sys.argv", _argv(p, "--open", "w1"))
        assert dispatch_overdue.main() == 0
        out = capsys.readouterr()
        assert out.out == ""
        assert out.err != ""  # the disclosure went somewhere — just not stdout

    def test_open_task_prints_one_id_or_nothing_and_discloses_on_stderr(
        self, tmp_path, monkeypatch, capsys
    ):
        """The resolver is machine-consumed: one id or nothing on stdout. Its
        answer is disclosed on stderr — `[source=plane]` — because the report
        door discards stderr and a reader auditing by hand must be able to
        see which side answered (the plane, now the only one)."""
        p = _land(tmp_path, [_dispatch("w9", 100, 1000, task_id="t-z")])
        monkeypatch.setattr("sys.argv", _argv(p, "--open-task", "w1"))
        assert dispatch_overdue.main() == 0
        out = capsys.readouterr()
        assert out.out == "" and "[source=plane]" in out.err and "'w1'" in out.err


class TestSupersession:
    """A re-dispatch replaces an earlier task, so the older row is never answered.

    It then ages out and pages the manager about work that already shipped — six such
    rows in one night, five of them one thread re-dispatched five times. Asking workers
    to echo the right id was broadcast three times and did not hold, which is #835's
    own argument arriving: correct by default beats correct by discipline.

    THE BOUNDARY IS THE WHOLE DESIGN. Retiring too eagerly converts a false-page bug
    into a silently-dropped-task bug, which is strictly worse — nobody chases a task
    that looks finished. So retirement happens ONLY on an explicit declaration from the
    dispatcher, never on a pattern inferred afterwards. Inference was measured against
    the real ledger and rejected: of 189 closed rows, 14 had a later row close first and
    were still answered after, 3 unambiguously genuine (work answered 6-7h late).

    ON THE PLANE the declaration is a terminal `superseded` task event the
    dispatch door lands on the retired assignment at re-dispatch; the matcher
    reads it as any other terminal event. `_superseded_ids`' per-bot scoping
    (#518) was the legacy matcher's; on the plane the scoping is the dispatch
    door's lookup — see the R2a findings on #1467 — so the two "scoped by
    bot" pins are retired from THIS suite rather than certified by a rig that
    would be enforcing the rule itself.
    """

    NOW = 5000

    def _overdue(self, tmp_path, dispatches):
        p = _land(tmp_path, dispatches)
        return dispatch_overdue.overdue_all(self.NOW, fleet=F, root=str(p.root))

    def _both_open_doors(self, tmp_path, dispatches, bot="w1", reports=None):
        """(open list ids, resolver id) — the PRODUCT the class never asserted.

        A helper returning one door would let a hole reopen shifted by one;
        this returns both because the #1357 defect was the DISAGREEMENT
        between them, not either door's own behaviour.
        """
        p = _land(tmp_path, dispatches, reports or [])
        return [r[2] for r in _open(p, bot)], _head(p, bot)

    def test_an_explicitly_superseded_row_is_retired(self, tmp_path):
        """The stranded row goes quiet; the replacement stays accountable."""
        out = self._overdue(tmp_path, [
            _dispatch("w1", 100, 1000, task_id="t-100-old"),
            _dispatch("w1", 200, 1100, task_id="t-200-new", supersedes="t-100-old"),
        ])
        ids = [row[3] for row in out.get("w1", [])]
        assert "t-100-old" not in ids, "the superseded row still pages"
        assert "t-200-new" in ids, (
            "the REPLACEMENT was retired too — a re-dispatch must still be answered, "
            "or supersession becomes a way to silence live work"
        )

    def test_a_queued_fifo_dispatch_is_NOT_retired(self, tmp_path):
        """The property that keeps this from becoming a dropped-task generator.

        Same shape as above minus the declaration: two dispatches to one bot, nothing
        saying the second replaces the first. Both are owed, both must stay visible.
        This is what the oldest-first resolver exists to serve, and it is exactly the
        case timing-based inference got wrong.
        """
        out = self._overdue(tmp_path, [
            _dispatch("w1", 100, 1000, task_id="t-100-a"),
            _dispatch("w1", 200, 1100, task_id="t-200-b"),
        ])
        ids = sorted(row[3] for row in out.get("w1", []))
        assert ids == ["t-100-a", "t-200-b"], (
            f"a queued dispatch was retired without any declaration: {ids}"
        )

    def test_omitting_the_field_reproduces_prior_behaviour_exactly(self, tmp_path):
        """The inert-failure property: a forgotten flag costs a false page, never a
        dropped task. Rows with no `supersedes` key at all must behave as before."""
        rows = [_dispatch("w1", 100, 1000, task_id="t-100-a")]
        assert "supersedes" not in rows[0]
        assert [r[3] for r in self._overdue(tmp_path, rows).get("w1", [])] == ["t-100-a"]

    def test_an_empty_supersedes_retires_nothing(self, tmp_path):
        """The dispatcher always emits the key, so the common row carries
        `supersedes: ""`. Empty must be falsy, not a wildcard."""
        out = self._overdue(tmp_path, [
            _dispatch("w1", 100, 1000, task_id="t-100-a", supersedes=""),
            _dispatch("w1", 200, 1100, task_id="t-200-b", supersedes=""),
        ])
        assert sorted(r[3] for r in out.get("w1", [])) == ["t-100-a", "t-200-b"]

    def test_a_chain_of_re_dispatches_retires_every_link_but_the_last(self, tmp_path):
        """The observed shape: one thread re-dispatched five times leaves four strays.

        Each link names its immediate predecessor, so the chain collapses to the row
        that is actually live.
        """
        out = self._overdue(tmp_path, [
            _dispatch("w1", 100, 200, task_id="t-1"),
            _dispatch("w1", 300, 400, task_id="t-2", supersedes="t-1"),
            _dispatch("w1", 500, 600, task_id="t-3", supersedes="t-2"),
            _dispatch("w1", 700, 800, task_id="t-4", supersedes="t-3"),
            _dispatch("w1", 900, 1000, task_id="t-5", supersedes="t-4"),
        ])
        assert [r[3] for r in out.get("w1", [])] == ["t-5"]

    # ------------------------------------------------------------------
    # #1357 — the open doors honour the same retirement the overdue path does.
    # Supersession and the open list were each tested, never together, so the
    # two doors disagreed about OPEN under a green suite: a retired row was
    # invisible to alerting and simultaneously the preferred close target.
    # ------------------------------------------------------------------

    def test_a_retired_row_is_gone_from_BOTH_open_doors(self, tmp_path):
        """The core regression, stated as the product rather than as one door."""
        ids, head = self._both_open_doors(tmp_path, [
            _dispatch("w1", 100, 1000, task_id="t-100-old"),
            _dispatch("w1", 200, 1100, task_id="t-200-new", supersedes="t-100-old"),
        ])
        assert ids == ["t-200-new"], f"the retired row is still listed open: {ids}"
        assert head == "t-200-new", (
            f"the resolver hands back the RETIRED row ({head}) — an id-less report "
            "would close the row we declared dead and strand the live one"
        )

    def test_the_two_doors_agree_on_what_retirement_means(self, tmp_path):
        """The desync assertion — neither door alone can express this."""
        dispatches = [
            _dispatch("w1", 100, 1000, task_id="t-100-old"),
            _dispatch("w1", 200, 1100, task_id="t-200-new", supersedes="t-100-old"),
        ]
        overdue_ids = {r[3] for r in self._overdue(tmp_path / "o", dispatches).get("w1", [])}
        open_ids, head = self._both_open_doors(tmp_path / "d", dispatches)
        assert "t-100-old" not in overdue_ids, (
            "positive control failed: the overdue path stopped retiring the row, "
            "so a green agreement assertion below would prove nothing"
        )
        assert "t-100-old" not in set(open_ids), "OVERDUE retired the row and OPEN did not — the doors disagree"
        assert head != "t-100-old", "the resolver inherited the wrong answer"
        assert set(overdue_ids) <= set(open_ids), (
            "open must stay a strict superset of overdue while sharing its "
            f"retirement rule: open={open_ids} overdue={sorted(overdue_ids)}"
        )

    def test_an_undeclared_queue_is_NOT_retired_from_the_open_doors(self, tmp_path):
        """The boundary, restated for the door that carries the resolver: an
        over-broad gate would silently mark live work `completed`. Two
        dispatches, no declaration — both stay open, oldest still first."""
        ids, head = self._both_open_doors(tmp_path, [
            _dispatch("w1", 100, 1000, task_id="t-100-a"),
            _dispatch("w1", 200, 1100, task_id="t-200-b"),
        ])
        assert ids == ["t-100-a", "t-200-b"], f"a queued dispatch was retired: {ids}"
        assert head == "t-100-a", "FIFO resolution broke"

    def test_a_chain_leaves_no_phantom_at_the_head(self, tmp_path):
        """The sharpest live reproduction: nothing went wrong operationally.

        The manager superseded correctly at every hop and the worker reported
        both live rows with explicit ids, so both closed. The first row was
        retired two hops back, will never be reported against, and was still
        the head of the open list — so the bot's next id-less report closed a
        row from three hours earlier.
        """
        ids, head = self._both_open_doors(
            tmp_path,
            [
                _dispatch("w1", 100, 200, task_id="t-1"),
                _dispatch("w1", 300, 400, task_id="t-2", supersedes="t-1"),
                _dispatch("w1", 500, 600, task_id="t-3", supersedes="t-2"),
            ],
            reports=[
                _report("w1", "2026-05-27T11:00:00Z", "completed", task_id="t-3"),
                _report("w1", "2026-05-27T11:01:00Z", "completed", task_id="t-2"),
            ],
        )
        assert ids == [], f"phantom rows survived a fully-answered chain: {ids}"
        assert head is None, f"the resolver still offers a retired row: {head}"


class TestUnassigned:
    """#1024 — the MIRROR of overdue: reported, then never re-dispatched.

    The join is the point, and so is what it refuses to look at. Semantics live
    in the unassigned_all docstring; these pin the behaviour that decides whether
    the check is useful or noise.
    """

    # Derived, never hardcoded: the two must agree exactly or every case below
    # silently becomes a different one — a stale dispatch epoch lands AFTER the
    # report and the row reads as re-tasked, which is a pass-shaped failure.
    T10 = "2026-05-27T10:00:00Z"
    E10 = int(_dt.datetime.fromisoformat(T10.replace("Z", "+00:00")).timestamp())

    def _unassigned(self, p, now, threshold=0):
        return dispatch_overdue.unassigned_all(now, threshold, fleet=F, root=str(p.root))

    def test_reported_and_never_retasked_is_flagged(self, tmp_path):
        p = _land(tmp_path, [_dispatch("eng-1", self.E10 - 3600, self.E10 - 3000)],
                  [_report("eng-1", self.T10, "completed")])
        res = self._unassigned(p, self.E10 + 7300)
        assert "eng-1" in res
        reported_at, idle, _tid, status = res["eng-1"]
        assert reported_at == self.E10
        assert idle == 7300
        assert status == "completed"

    def test_retasked_after_reporting_is_not_flagged(self, tmp_path):
        """The positive control. Without it, a check that fired on every
        terminal report would satisfy every other test in this class."""
        p = _land(tmp_path, [
            _dispatch("eng-1", self.E10 - 3600, self.E10 - 3000),
            _dispatch("eng-1", self.E10 + 60, self.E10 + 660),
        ], [_report("eng-1", self.T10, "completed")])
        assert self._unassigned(p, self.E10 + 7300) == {}

    def test_five_stale_open_dispatches_do_not_mask_the_strand(self, tmp_path):
        """THE case the design exists for, and it is a real one.

        Six dispatches to one worker inside 2143s for a single evolving task;
        the worker answers only the last id, so five rows stay open afterwards.
        A predicate keyed on "has an open dispatch" reads those five as
        still-busy and never fires — the #1024 incident recurring inside its own
        watchdog. The fixture below is that shape, reduced.
        """
        p = _land(tmp_path, [
            _dispatch("eng-1", self.E10 - 2100 + i * 300, self.E10 - 1500 + i * 300, task_id=f"t-{i}")
            for i in range(6)
        ], [_report("eng-1", self.T10, "completed", task_id="t-5")])   # t-0..t-4 remain open
        assert "eng-1" in self._unassigned(p, self.E10 + 7300), "five stale open rows masked a genuine strand"

    def test_progress_as_newest_report_is_not_flagged(self, tmp_path):
        """Still working, or stalled mid-task — the stall is overdue's to report."""
        p = _land(tmp_path, [_dispatch("eng-1", self.E10 - 3600, self.E10 - 3000)],
                  [_report("eng-1", "2026-05-27T09:00:00Z", "completed"),
                   _report("eng-1", self.T10, "progress")])
        assert self._unassigned(p, self.E10 + 7300) == {}

    def test_a_bot_that_never_reported_is_not_flagged(self, tmp_path):
        """No terminal report means nothing came back, so there is nothing to be
        unassigned FROM. That case belongs to overdue_all, not here."""
        p = _land(tmp_path, [_dispatch("eng-1", self.E10 - 3600, self.E10 - 3000)])
        assert self._unassigned(p, self.E10 + 7300) == {}

    def test_threshold_filters_when_asked(self, tmp_path):
        p = _land(tmp_path, [_dispatch("eng-1", self.E10 - 3600, self.E10 - 3000)],
                  [_report("eng-1", self.T10, "completed")])
        now = self.E10 + 100
        assert self._unassigned(p, now, 7200) == {}
        # ...and defaults to reporting everything, because fleet-pulse applies
        # the threshold per bot and one scan must serve differently-tuned bots.
        assert "eng-1" in self._unassigned(p, now)

    def test_never_dispatched_bot_that_reported_is_flagged(self, tmp_path):
        """No dispatch at all is still an unassigned worker, not an error."""
        p = _land(tmp_path, [], [_report("eng-1", self.T10, "blocked")])
        res = self._unassigned(p, self.E10 + 7300)
        assert res["eng-1"][3] == "blocked"

    def test_bot_scoping(self, tmp_path):
        """One bot's dispatch must not clear another's strand."""
        p = _land(tmp_path, [_dispatch("eng-2", self.E10 + 60, self.E10 + 660)],
                  [_report("eng-1", self.T10, "completed")])
        assert "eng-1" in self._unassigned(p, self.E10 + 7300)

    def test_no_plane_is_unreachable_not_empty(self, tmp_path):
        """The retired `test_missing_ledgers_are_empty_not_fatal` inverted:
        two missing ledgers used to read as an idle-free fleet; a missing
        plane REFUSES — an empty answer here would read as 'no idle workers'."""
        with pytest.raises(dispatch_overdue.PlaneUnreachable):
            dispatch_overdue.unassigned_all(self.E10, fleet=F, root=str(tmp_path / "no-plane"))


class TestOrphansRefusesWhenItCannotLook:
    """#1014, and the same defect as #1216 on a sibling command.

    Orphan-ness is decided by comparing a dispatch against
    ``<bots_dir>/<bot>/data/.spawn``. Without a readable bots dir there is nothing
    to compare, so the honest answer is UNKNOWN — but the mode printed an empty
    set at rc 0, which is byte-identical to "no work was lost to a restart".
    Measured on the reporting host before the fix: no ``--bots-dir``, a real one
    with no orphans, and a ``--bots-dir`` naming a path that does not exist ALL
    returned 0 bytes at rc 0 against a 295-row dispatch log. Three states, one
    output, and the collapsed one reads as good news.
    """

    NOW = 3000

    def _plane(self, tmp_path):
        return _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-1")])

    def _main(self, p, argv, monkeypatch):
        monkeypatch.setattr("sys.argv", _argv(p, *argv))
        return dispatch_overdue.main()

    def test_no_bots_dir_refuses_at_rc_three(self, tmp_path, monkeypatch, capsys):
        p = self._plane(tmp_path)
        rc = self._main(p, ["--orphans", str(self.NOW)], monkeypatch)
        assert rc == 3
        assert "cannot determine orphans without --bots-dir" in capsys.readouterr().err

    def test_an_unreadable_bots_dir_refuses_too(self, tmp_path, monkeypatch, capsys):
        """The second silent state, and the one a real caller reaches: a
        --bots-dir that resolves to nothing (a moved fleet, a wrong root) looked
        identical to a healthy fleet with no orphans."""
        p = self._plane(tmp_path)
        rc = self._main(p, ["--orphans", str(self.NOW), "--bots-dir", str(tmp_path / "no-such-dir")], monkeypatch)
        assert rc == 3
        assert "cannot read the bots dir" in capsys.readouterr().err

    def test_a_real_bots_dir_with_no_orphans_still_answers_empty_at_rc_zero(
        self, tmp_path, monkeypatch, capsys
    ):
        """THE control that makes the two above mean something. Presence, not
        emptiness, is the line — a fleet that genuinely lost nothing must still
        get the true answer, or the fix has traded a false all-clear for a
        refusal that fires on healthy fleets."""
        p = self._plane(tmp_path)
        (tmp_path / "bots" / "w1" / "data").mkdir(parents=True)
        rc = self._main(p, ["--orphans", str(self.NOW), "--bots-dir", str(tmp_path / "bots")], monkeypatch)
        cap = capsys.readouterr()
        assert rc == 0
        assert cap.out == ""

    def test_a_real_orphan_is_still_listed(self, tmp_path, monkeypatch, capsys):
        """The positive control on the other side: the mode must still find what
        it exists to find. Without this, every assertion above would hold on a
        command that had stopped classifying anything at all."""
        p = self._plane(tmp_path)
        data = tmp_path / "bots" / "w1" / "data"
        data.mkdir(parents=True)
        spawn = data / ".spawn"
        spawn.write_text("")
        os.utime(spawn, (500, 500))  # respawned AFTER the dispatch at 100
        rc = self._main(p, ["--orphans", str(self.NOW), "--bots-dir", str(tmp_path / "bots")], monkeypatch)
        cap = capsys.readouterr()
        assert rc == 0
        assert "t-1" in cap.out

    def test_rc_three_is_distinct_from_the_usage_code(self, tmp_path, monkeypatch, capsys):
        """rc 2 means "you called me wrong"; rc 3 means "I cannot answer that".
        Collapsing them would re-create this very bug one level up — a caller
        could no longer tell a typo from an unreachable instrument."""
        p = self._plane(tmp_path)
        cannot_answer = self._main(p, ["--orphans", str(self.NOW)], monkeypatch)
        malformed = self._main(p, ["--orphans", "not-an-instant"], monkeypatch)
        capsys.readouterr()
        assert cannot_answer == 3
        assert malformed == 2

    def test_all_mode_is_untouched_by_the_orphan_refusal(self, tmp_path, monkeypatch, capsys):
        """--all legitimately runs without a bots dir (it just cannot split
        orphans out). Gating it would break the watchdog's primary call."""
        p = self._plane(tmp_path)
        rc = self._main(p, ["--all", str(self.NOW)], monkeypatch)
        cap = capsys.readouterr()
        assert rc == 0
        assert "t-1" in cap.out

    def test_orphaned_all_keeps_its_contract_exactly(self, tmp_path):
        """The refusal lives in the CLI mode, NOT the join. brief.py imports
        orphaned_all directly and labels this gap its own way, so changing the
        function would have broken a caller that already had it right."""
        p = self._plane(tmp_path)
        kw = {"fleet": F, "root": str(p.root)}
        assert dispatch_overdue.orphaned_all(self.NOW, **kw) == {}
        assert dispatch_overdue.orphaned_all(self.NOW, bots_dir=None, **kw) == {}


def test_orphans_refuses_on_an_unlistable_bots_dir(tmp_path):
    """The fourth state #1014 missed (#1227 review).

    --bots-dir present but unlistable is byte-identical to 'no orphans': rc 0,
    zero stdout, empty stderr. orphan-ness is decided by reaching
    <bots_dir>/<bot>/data/.spawn, which silently fails for every bot.
    """
    if os.geteuid() == 0:
        pytest.skip("root ignores the mode bits")
    p = _land(tmp_path, [_dispatch("a", 1786000000, 1786000600, task_id="t-1")])
    bots = tmp_path / "bots"
    (bots / "a" / "data").mkdir(parents=True)
    (bots / "a" / "data" / ".spawn").write_text("")
    bots.chmod(0o000)
    try:
        proc = subprocess.run(
            [sys.executable, str(MATCHER), "--orphans", "1787000000", "--bots-dir", str(bots),
             "--fleet", F, "--root", str(p.root)],
            capture_output=True, text=True,
        )
    finally:
        bots.chmod(0o755)
    assert proc.returncode == 3, f"expected refusal, got rc={proc.returncode}"
    assert proc.stdout == "", "stdout is parsed by fleet-pulse.sh; must stay empty"
    assert "unlistable" in proc.stderr.lower() or "cannot" in proc.stderr.lower()


class TestUnreachableIsNotEmpty:
    """The plane's twin of the retired report-ledger refusals (#1232's class):
    a reader that cannot reach its source must not answer as if it had found
    nothing. rc 3, nothing on stdout — stdout is parsed (fleet-pulse's caches,
    report-back's `awk`), so the refusal rides stderr and the rc.
    """

    @pytest.mark.parametrize("argv", [["--open", "w1"], ["--open-task", "w1"], ["--all", "5000"],
                                      ["--unassigned", "5000"]], ids=["open", "open-task", "all", "unassigned"])
    def test_no_plane_db_refuses_at_rc3_with_nothing_on_stdout(self, tmp_path, monkeypatch, capsys, argv):
        root = plane_root(tmp_path)                                   # the home exists; no db was ever created
        monkeypatch.setattr("sys.argv", ["dispatch-overdue.py", *argv, "--fleet", F, "--root", str(root)])
        assert dispatch_overdue.main() == 3
        cap = capsys.readouterr()
        assert cap.out == "", "refusal text on stdout becomes a phantom row"
        assert "UNREACHABLE" in cap.err

    def test_a_fleet_the_plane_never_saw_refuses_too(self, tmp_path, monkeypatch, capsys):
        """A schema-valid plane that holds no bot of the named fleet is a wrong
        root or a fleet it never saw — 'nothing open' from it would be absence
        read as clean (#1014's class)."""
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-a")])
        monkeypatch.setattr("sys.argv", ["dispatch-overdue.py", "--open", "w1", "--fleet", "ghost", "--root", str(p.root)])
        assert dispatch_overdue.main() == 3
        cap = capsys.readouterr()
        assert cap.out == "" and "holds no bot of fleet 'ghost'" in cap.err

    def test_no_fleet_and_no_root_is_a_refusal_not_an_empty_answer(self, tmp_path, monkeypatch, capsys):
        for k in ("CLAUDLOBBY_FLEET", "FLEET_NAME", "CLAUDLOBBY_ROOT"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr("sys.argv", ["dispatch-overdue.py", "--all", "5000"])
        assert dispatch_overdue.main() == 3
        cap = capsys.readouterr()
        assert cap.out == "" and "needs a fleet" in cap.err

    def test_the_carriers_name_the_plane_when_the_flags_are_absent(self, tmp_path, monkeypatch, capsys):
        """The timer units stamp CLAUDLOBBY_FLEET and CLAUDLOBBY_ROOT; a session
        carries FLEET_NAME — the matcher reads them exactly as a bare call does."""
        p = _land(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-a")])
        monkeypatch.setenv("CLAUDLOBBY_FLEET", F)
        monkeypatch.setenv("CLAUDLOBBY_ROOT", str(p.root))
        monkeypatch.setattr("sys.argv", ["dispatch-overdue.py", "--open", "w1"])
        assert dispatch_overdue.main() == 0
        assert capsys.readouterr().out == "100 1000 t-a\n"
