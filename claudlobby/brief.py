"""claudlobby brief — one read door over the state the fleet already writes.

``claudlobby brief [--bot X] [--json]`` composes five sections off the
plane (once five unrelated files, read by hand-rolled jq against stale schemas):
mission pointers, canonical fleet work, workstreams, unacked reports, and recent critical
events. Skills consume THIS, never the plane db by hand — that is the coupling
the door exists to kill. Explicit ``--usage-since`` adds a bounded transcript
count for the selected viewer; default and boot reads do not scan transcripts.

Read-only by construction. Report acknowledgements use
``claudlobby fleet reports ack``; the pure ``ack_request`` encoder remains
here for the plane event's established shape.

THE TRUST RULE (epic #1102 phase R0)
------------------------------------
The door must never serve a number known to be wrong. Where a trust
prerequisite has not landed, the affected field is either

  * **LABELED** — served, with its bound stated: the value is real but provably
    incomplete; or
  * **OMITTED** — absent, because serving it would print a retention artifact
    as truth.

Both land in the envelope's ``degraded`` list, so a consumer reading only
``--json`` still sees every bound. A field that is neither present nor listed
does not exist: silence is never how this door reports a gap.

Which gates bite, and how each is DETECTED rather than assumed — a hardcoded
"still broken" flag would keep crying after the fix landed, which is the same
class of untruth it was added to prevent:

  ``#911`` ledger escaping
      RETIRED with the ledgers (F18 closure): the plane's rows are typed and
      validated at ingest, so a malformed row is refused by the contract and
      recorded as such, never dropped silently by a reader. The re-scan this
      module carried, and its label, went with the files.

  ``#903`` event-type SSOT
      DETECTED, structurally. ``CRITICAL_TYPES`` is a hand-maintained
      nine-literal list that omits every host-job alert type (``disk_high``,
      ``memory_high``, ``briefing_failed``, ...), so the alert section is
      incomplete by construction and no measurement taken here could show it —
      the missing rows are exactly the ones the filter never returns. #903
      ships an event-type registry in ``known_values``; the label is keyed on
      that symbol existing, so it clears when the SSOT lands and not before.

  ``#891`` uptime windows
      OMITTED. ``claudlobby fleet uptime`` counts missing keepalive history as
      downtime, so its percentages are retention artifacts. The cost/utilization
      section is cut from v1 for that reason and the YAGNI one (nothing
      meaningfully writes the utilization file). Recorded as an explicit
      omission rather than left to be inferred from absence, so a consumer
      asking "where is utilization?" gets an answer instead of archaeology.

  ``#894`` fleet-state keying
      NOT REACHED. No field served here reads ``fleet-state.json``; the door's
      only fleet-state contact is the *directory* its cursor lives in. Nothing
      to degrade — stated so the next reader need not re-derive it.

CONSUMING THE SHARED DOORS
--------------------------
Canonical fleet work comes from ``task_state.read_tasks`` in one snapshot.
The native watchdog contributes deadline, progress-grace and restart observations
by current assignment ID; it does not decide task lifecycle. Missing timing
evidence is disclosed separately from the canonical open set.

The reports, alerts and workstreams sections read the plane through the same
rule (``plane_conn``: no flag, no retirement fact, no file; unreachable is not
empty) and OMIT with the note when it cannot answer — ``unacked (0)`` from a
plane that could not be read would be #949 and #1024 exactly, re-created by
the surface built to close them. A missing bots directory prevents reading
``.spawn`` mtimes (#1014); affected assignment attention is labeled unknown
rather than claiming that no work was lost to a restart.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .paths import Paths, load_lib_module
from .source_state import (
    SOURCE_ABSENT,
    SOURCE_OK,
    SOURCE_UNREADABLE,
    probe_dir,
    probe_source,
)

SCHEMA_VERSION = 2


class BriefIdentityMismatch(ValueError):
    """The Plane view does not match the selected activation's identity."""


def _assert_selected_identity(plane, fleet: str, bot_id: str,
                              expected: tuple[str, str]) -> None:
    """Check frozen fleet and viewer IDs in the already-open Plane snapshot."""
    try:
        fleet_uid = plane.pr.fleet_uid(plane.conn, fleet)
    except plane.pr.PlaneUnreachable as exc:
        raise BriefIdentityMismatch("selected fleet identity is absent from the Plane") from exc
    entry = plane.roster.get(bot_id.lower())
    if fleet_uid != expected[0] or entry is None or entry.get("actor") != expected[1]:
        raise BriefIdentityMismatch("Plane fleet or viewer identity differs from selected activation")

# Critical-event lookback. Fleet events carry no resolution state, so recency is
# the only available proxy for "still unresolved" — which is why the rendered
# header says "last 24h" and not "unresolved". Naming the proxy accurately is
# the honest move; a section titled "unresolved" would be asserting a fact no
# row on the plane records.
ALERT_WINDOW_H = 24

# Rows any ONE text section will print before truncating. The JSON envelope is
# never capped — R4 consumes that and wants everything.
#
# Measured need, not a round number: this fleet's live brief rendered 335 unacked
# reports as 335 lines. The read door's job is to route attention, and a section
# that has to be scrolled past has already failed at it — the 2026-08-02
# assessment named exactly this ("reinvented the wall-of-text problem at the boot
# layer"), where the payload is re-read for the life of every session that
# carries it. Rows are ordered oldest-first everywhere, so the head of a
# truncated list is the end that is rotting: the report that has gone unacted-on
# longest is the #1024 incident shape, not the one that just arrived.
TEXT_ROW_LIMIT = 10

# Workstream staleness window, in days, shared with selected fleet policy.
DEFAULT_LEASE_DAYS = 14


def _lease_days(fleet) -> int:
    """Lease window from selected fleet policy, the canonical writer's input."""
    configured = getattr(getattr(fleet, "workstreams", None), "lease_days", None)
    return (
        configured
        if isinstance(configured, int) and configured > 0
        else DEFAULT_LEASE_DAYS
    )


@dataclass(frozen=True)
class Degradation:
    """One field this door refuses to serve as plain truth.

    ``mode`` is ``labeled`` (present, bounded) or ``omitted`` (absent by
    design). ``issue`` is the tracking issue whose fix retires the entry.

    ``count`` is how many rows the degradation covers, when that is knowable —
    and on an ``omitted`` entry it is the difference between a hidden true
    positive and a disclosed one. An omission suppresses real rows as well as
    false ones (it must: in that state no row can be adjudicated), so without a
    count "unavailable" reads the same whether it is hiding nothing or hiding a
    dispatch that has been silently rotting for a day. ``None`` means the count
    itself could not be taken, which is stated rather than rendered as 0.
    """

    field: str
    mode: str
    reason: str
    issue: str
    count: int | None = None

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "mode": self.mode,
            "reason": self.reason,
            "issue": self.issue,
            "count": self.count,
        }


# --- plane reading ------------------------------------------------------------


# Re-exported from ``source_state``, which owns the rule now that five other
# readers need it too (#1216/#1014). Aliases rather than fresh literals so the
# two can never drift: these strings are emitted verbatim in the schema-2
# envelope (``provenance.*.state``) and asserted on by tests, so a second
# definition would be a wire-format fork waiting to happen.
def _iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return (
        datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _epoch(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError, AttributeError):
        return None


# --- the #835 doors -----------------------------------------------------------


def load_dispatch_doors(paths: Paths):
    """Import ``claudlobby/_runtime_scripts/dispatch-overdue.py`` as a module, or None when unreadable.

    The same ``spec_from_file_location`` seam ``tests/conftest.py`` uses: those
    doors are a standalone stdlib script with no package, and re-implementing
    the join here is precisely what this issue forbids — a second copy would
    drift from the watchdog and the two would disagree about which dispatches
    are open.

    None (rather than a raise) when the file is missing, because the caller has
    a better answer than a traceback: an unavailable door degrades work
    *loudly*. Printing "0 open" because the matcher could not be loaded
    would be the exact failure this door exists to prevent.
    """
    return load_lib_module(paths.lib, "dispatch-overdue.py")


def resolve_fleet_name(paths: Paths) -> str | None:
    """The fleet the plane's rows are keyed by: the overlay's name, else the
    root manifest's ``fleet.name``, else the carriers every session and timer
    carries (``CLAUDLOBBY_FLEET`` / ``FLEET_NAME``) — the matcher's own rule,
    so a root-mode command names the fleet the matcher would (``Paths``
    knows only the overlay's directory; root mode has none)."""
    if paths.fleet_name:
        return paths.fleet_name
    try:
        import yaml
        doc = yaml.safe_load(paths.fleet_yaml.read_text()) or {}
        name = (doc.get("fleet") or {}).get("name") if isinstance(doc, dict) else None
        if name:
            return str(name)
    except Exception:
        pass
    return os.environ.get("CLAUDLOBBY_FLEET") or os.environ.get("FLEET_NAME") or None


def plane_session(paths: Paths, fleet: str | None = None):
    """(plane, note): the matcher's plane session (`claudlobby/_runtime_scripts/dispatch-overdue.py`'s
    `open_plane` — connection, the stdlib readers, the resolved fleet and its
    roster, a context manager) when the plane can answer for this fleet, else
    (None, note). ONE door for every plane read in the package (F18 closure,
    R2b-1): no flag, no retirement fact, no file to fall back on — and
    unreachable is not empty. No db, no schema, an unreadable claudlobby/_runtime_scripts/, no fleet
    name, a matcher that predates the plane-only reader, or a plane that holds
    no bot of the fleet (a wrong root is not "nothing recorded" — the
    matcher's rule, #1014's class) all return the note, and the caller omits
    or refuses with it. The caller closes (or uses ``with``)."""
    # The order of the refusals is the order of the remedies' usefulness: a
    # missing db is named before a missing fleet (a root-mode call with no
    # plane and no manifest wants "no plane db", not "no fleet"), and a root
    # with neither an install nor a db is said to be a wrong root.
    db = paths.root / "state" / "plane" / "plane.db"
    doors = load_dispatch_doors(paths)
    if doors is None or not hasattr(doors, "open_plane"):
        if not db.is_file():
            return None, (f"no plane db at {db} and no claudlobby/_runtime_scripts/dispatch-overdue.py under {paths.root} —"
                          " a wrong root (restore state/plane/plane.db, or name the right root)")
        return None, (f"the matcher installed at {paths.lib / 'dispatch-overdue.py'} is unreadable or"
                      " predates the plane-only reader — pull the install and re-run")
    if not db.is_file():
        return None, f"no plane db at {db} — restore state/plane/plane.db under {paths.root} or name the right root"
    fleet = fleet or resolve_fleet_name(paths)
    if not fleet:
        return None, ("no fleet is named (--fleet <name>, or a fleet.yaml naming one) — the plane's"
                      " rows are per fleet")
    try:
        return doors.open_plane(fleet=fleet, root=str(paths.root)), None
    except doors.PlaneUnreachable as exc:
        return None, (f"{exc} — restore state/plane/plane.db under {paths.root} or name the"
                      " right root")


def plane_conn(paths: Paths, fleet: str | None = None):
    """(conn, readers, note) — ``plane_session`` for a caller that wants the
    bare connection and readers and closes the connection itself."""
    plane, note = plane_session(paths, fleet)
    if plane is None:
        return None, None, note
    return plane.conn, plane.pr, None


# `load_lib_module` now lives in `paths.py` — three consumers, one loader.


# --- report acknowledgement event encoder -------------------------------------


def ack_request(fleet: str, bot: str, *, acked_through_seq: int, acked_through_ts: str,
                count: int) -> dict:
    """Encode `reports_acked` on the viewer's own actor,
    with detail containing the plane's ordering
    authority — the `ingest_seq` the ack reaches (§4) — with the legacy-form
    ts riding for the render and the count for the story."""
    return {
        "event_type": "system", "emitter": "brief", "fleet": fleet,
        "payload": {"event": "reports_acked", "subject_kind": "actor",
                    "subject": f"bot:{fleet}/{bot}",
                    "data": {"acked_through_seq": int(acked_through_seq),
                             "acked_through_ts": acked_through_ts, "count": int(count)}},
    }


# --- sections -----------------------------------------------------------------


def _mission_section(fleet, bot, paths: Paths) -> dict:
    """Pointers, not inlined charters (#986 P2: pointers, never derivations).

    The fleet anchor paragraph is carried verbatim because it *is* one
    paragraph and it is what every bot already composes; the charter and any
    project mission files are emitted as resolved paths for the reader to open
    on demand. Inlining them would rebuild the wall-of-text problem at whatever
    surface consumes this.
    """
    charter = None
    if fleet.mission_file:
        charter = str(paths.fleet_config_dir / fleet.mission_file)

    # Projects the bot actually touches, joined on scope repos — the same join
    # the config already models (projects declare `repos`, bots declare scope).
    bot_repos = {r.lower() for r in (bot.scope.repos if bot.scope else [])}
    projects = []
    for key, proj in sorted(getattr(fleet, "projects", {}).items()):
        if not proj.mission_file:
            continue
        if bot_repos and not {r.lower() for r in proj.repos} & bot_repos:
            continue
        projects.append(
            {
                "project": key,
                "mission_file": str(paths.fleet_config_dir / proj.mission_file),
            }
        )

    return {
        "anchor": bot.mission or fleet.mission,
        "fleet_anchor": fleet.mission,
        "charter": charter,
        "projects": projects,
    }


def _work_section(
    doors, paths: Paths, fleet, bot_id: str, now: int,
    degraded: list[Degradation], *, plane,
) -> dict:
    """Fleet-owned open work from the canonical reducer, with watchdog attention."""
    import sqlite3
    from .task_state import TaskStateError, read_tasks

    try:
        fleet_uid = plane.pr.fleet_uid(plane.conn, plane.fleet)
        snapshot = read_tasks(plane.conn, fleet_uid=fleet_uid)
    except (sqlite3.Error, TaskStateError, ValueError, doors.PlaneUnreachable) as exc:
        degraded.append(Degradation(
            field="work", mode="omitted", issue="#1747",
            reason=f"canonical fleet work cannot be read: {exc}; no empty list is served"))
        return {}

    manager = bot_id == fleet.manager
    entry = plane.roster.get(bot_id.lower())
    bot_uids = set((entry or {}).get("uids", ()))
    if not manager and not bot_uids:
        degraded.append(Degradation(
            field="work", mode="omitted", issue="#1747",
            reason=f"selected worker {bot_id} has no resolved fleet actor; "
                   "its assignments cannot be selected by UID"))
        return {}

    tasks = sorted(
        (task for task in snapshot.tasks if task.open and
         (manager or (task.current_assignment is not None
                      and task.current_assignment.assignee_uid in bot_uids))),
        key=lambda task: (task.ingest_seq, task.task_id))
    observations: dict = {}
    assigned_tasks = [task for task in tasks if task.current_assignment is not None]
    if not hasattr(doors, "assignment_attention"):
        if assigned_tasks:
            degraded.append(Degradation(
                field="work.attention", mode="labeled", issue="#1747",
                reason="installed native watchdog has no assignment-keyed attention reader; "
                       "deadline and restart observations are unavailable"))
    else:
        probe = probe_dir(paths.runtime_bots)
        bots_dir = str(paths.runtime_bots) if probe.state == SOURCE_OK else None
        max_age = getattr(doors, "_resolve_max_age",
                          lambda: doors.DEFAULT_OVERDUE_MAX_AGE_S)()
        try:
            observations = doors.assignment_attention(
                plane, assigned_tasks, now=now, max_age=max_age, bots_dir=bots_dir)
        except (sqlite3.Error, ValueError, OSError, doors.PlaneUnreachable) as exc:
            degraded.append(Degradation(
                field="work.attention", mode="labeled", issue="#1747",
                reason=f"assignment attention could not be read: {exc}"))
        if bots_dir is None:
            degraded.append(Degradation(
                field="work.attention", mode="labeled", issue="#1014",
                reason=(f"no bots directory at {paths.runtime_bots}"
                        if probe.state == SOURCE_ABSENT else
                        f"bots directory at {paths.runtime_bots} cannot be listed")
                       + "; session-restart observations are unavailable"))

    items = []
    missing_attention = False
    unknown_attention: set[str] = set()
    for task in tasks:
        assignment = task.current_assignment
        attention = observations.get(assignment.assignment_id) if assignment else None
        if assignment and attention is None:
            missing_attention = True
        if attention and attention.get("status") == "unknown":
            unknown_attention.add(attention.get("reason") or "unspecified")
        items.append({
            "task_id": task.task_id, "title": task.title, "state": task.state,
            "admitted_at": task.occurred_at,
            "deadline": assignment.expected_by if assignment else None,
            "assignment": ({
                "assignment_id": assignment.assignment_id,
                "assignee_uid": assignment.assignee_uid,
                "assigned_by_uid": assignment.assigned_by_uid,
                "state": assignment.state,
                "dispatched_at": assignment.occurred_at,
            } if assignment else None),
            "attention": attention,
            "historical_references": list(task.display_ids),
            "issues": [asdict(issue) for issue in task.issues],
        })
    if missing_attention and not any(d.field == "work.attention" for d in degraded):
        degraded.append(Degradation(
            field="work.attention", mode="labeled", issue="#1747",
            reason="one or more current assignments have no native attention observation; "
                   "their status is unknown"))
    if unknown_attention and not any(d.field == "work.attention" for d in degraded):
        degraded.append(Degradation(
            field="work.attention", mode="labeled", issue="#1014",
            reason="assignment attention is unknown: "
                   + ", ".join(sorted(unknown_attention))))
    return {
        "scope": "fleet" if manager else "assigned", "items": items,
        "issues": [asdict(issue) for issue in snapshot.issues],
    }


def _workstream_section(
    fleet, paths: Paths, now: int, degraded: list[Degradation], plane=None
) -> dict:
    """Active workstreams with the stall flags the pulse consumer never shipped.

    Read-only: the registry is the plane's rendering (``plane_workstreams``)
    and nothing here writes it back. ``stalled`` means no progress within the lease window; ``lease
    expired`` means the lease itself has run out. They are independent — a
    renewed workstream keeps its lease while its ``last_progress_ts`` stays put
    (the workstream operation is explicit that renew does not advance
    progress), which is exactly the state worth surfacing.
    """
    from .workstreams import blocked_waits, plane_workstreams
    workstreams, note = plane_workstreams(paths, plane=plane, lease_days=_lease_days(fleet))
    if workstreams is None:
        # 'no workstreams' from a plane that could not be read is the silent
        # drop this door exists to refuse: omitted, with the note.
        degraded.append(Degradation(field="workstreams", mode="omitted", reason=note, issue="#1467"))
        return {}

    lease_s = _lease_days(fleet) * 86400
    active, stalled = [], []
    for w in sorted(workstreams.values(), key=lambda x: x.get("opened_ts", "")):
        if w.get("status") != "active":
            continue
        progress = _epoch(w.get("last_progress_ts"))
        lease = _epoch(w.get("lease_expires_ts"))
        entry = {
            "id": w.get("id"),
            "title": w.get("title"),
            "owner_bot": w.get("owner_bot"),
            "next": w.get("next"),
            "last_progress_ts": w.get("last_progress_ts"),
            "lease_expires_ts": w.get("lease_expires_ts"),
            "stalled": progress is not None and (now - progress) > lease_s,
            "lease_expired": lease is not None and lease < now,
        }
        active.append(entry)
        if entry["stalled"] or entry["lease_expired"]:
            stalled.append(entry)
    return {"active": active, "stalled": stalled,
            "blocked": blocked_waits(workstreams, now)}


def _reports_section(
    paths: Paths, bot_id: str, terminal: set[str], degraded: list[Degradation],
    plane=None,
) -> dict:
    """Terminal reports newer than the viewer's newest ack — fleet-wide, on purpose.

    Note the deliberate asymmetry with work: selected work is *about*
    ``--bot X``, while this one is *for* ``--bot X to act
    on*. The question #1024 and #949 left unanswered is "what did my workers
    finish that I have not acted on", so filtering to the viewer's own reports
    would answer the wrong one. Every row carries ``bot``, so a consumer that
    does want a narrower view can take it.
    """
    # The plane, the only source (F18 R2b) — for the reports AND for the
    # viewer's read position (chunk K: the newest `reports_acked` event on the
    # viewer's actor, compared on `ingest_seq`, the ordering authority; the
    # legacy-form `ts` rides for the render). A plane that cannot answer OMITS
    # the section: "unacked (0)" would assert that no worker is waiting on a
    # decision — #949 and #1024 exactly, re-created by the surface built to
    # close them.
    session, note = (plane, None) if plane is not None else plane_session(paths)
    if session is None:
        degraded.append(Degradation(field="reports", mode="omitted",
                                    reason=f"{note}; '0 unacked' would assert that no worker is waiting"
                                           " on a decision, which is the incident class this section"
                                           " exists to surface",
                                    issue="#1467"))
        return {}
    try:
        # the viewer's read position first (its uids from the session's roster,
        # spanning every alias variant), then only the rows past it
        entry = session.roster.get(bot_id.lower()) or {}
        ack = session.pr.newest_ack(session.conn, entry.get("uids", []))
        rows = session.pr.report_rows(session.conn, session.fleet,
                                      since_seq=ack["seq"] if ack else None)
    except Exception as exc:
        degraded.append(Degradation(field="reports", mode="omitted",
                                    reason=f"the plane cannot answer: {exc}", issue="#1467"))
        return {}
    finally:
        if plane is None:
            session.close()
    stripped = sum(1 for r in rows if r.get("_body_stripped") and r.get("_source") != "task_event")
    if stripped:
        degraded.append(Degradation(field="reports", mode="labeled",
                                    reason=f"{stripped} report(s) hold no summary on the plane (the capture"
                                           " policy kept no body and no task event named one)",
                                    issue="#1444"))
    # ONE rule with the overview card (plane-readers.unacked_rows): terminal or
    # status-less reports past the ack — what the manager sees is what the card
    # counts, and this ack clears both
    unacked = [
        {"ts": r["ts"], "seq": r.get("_seq"), "bot": r["bot"], "status": r["status"],
         "task_id": r["task_id"], "summary": r["summary"], "pr_url": r["pr_url"]}
        for r in session.pr.unacked_rows(rows, ack["seq"] if ack else None, terminal)
    ]
    # The reports section keeps its three keys (`cursor` = the ack's legacy-form ts);
    # the card, not the brief, carries when and by whom. Keys INSIDE a row may
    # be added (additive; `seq` is one); the top-level key set is the contract.
    return {"cursor": ack["ts"] if ack else None, "unacked": unacked, "source": "plane"}


def _alerts_section(
    paths: Paths, bot_id: str, now: int, degraded: list[Degradation], plane=None
) -> list[dict]:
    """Critical events for the bot within the lookback window.

    Incomplete by construction until #903 lands — see the module docstring.
    The degradation is keyed on the SSOT symbol rather than a hardcoded flag,
    so it retires itself when the registry ships.
    """
    try:
        from . import known_values

        has_ssot = hasattr(known_values, "FLEET_EVENT_TYPES")
    except ImportError:  # pragma: no cover - known_values is a sibling module
        has_ssot = False

    if not has_ssot:
        degraded.append(
            Degradation(
                field="alerts",
                mode="labeled",
                reason=(
                    "critical events are filtered by CRITICAL_TYPES, a "
                    "hand-maintained list that omits every host-job alert type "
                    "(disk_high, memory_high, briefing_failed, ...); alerts "
                    "shown are real, but absence of an alert is not evidence of "
                    "health"
                ),
                issue="#903",
            )
        )

    cutoff = (
        (datetime.fromtimestamp(now, timezone.utc) - timedelta(hours=ALERT_WINDOW_H))
        .isoformat()
        .replace("+00:00", "Z")
    )

    # The plane, the only source (F18 R2b): no flag, no bots dir, no files. A
    # plane that cannot answer OMITS — an empty list would mean "could not
    # look", not "nothing is wrong", and a false all-clear here is worse than
    # anywhere else in this module.
    from .commands.events import collect_plane_events
    session, note = (plane, None) if plane is not None else plane_session(paths)
    if session is None:
        degraded.append(Degradation(field="alerts", mode="omitted",
                                    reason=f"the plane cannot answer: {note} — an empty list would mean"
                                           " 'could not look', not 'nothing is wrong'",
                                    issue="#1467"))
        return []
    try:
        events = collect_plane_events(session.conn, paths, fleet=session.fleet, pr=session.pr,
                                      bot=bot_id, critical_only=True, since=cutoff)
    except RuntimeError as exc:
        degraded.append(Degradation(field="alerts", mode="omitted",
                                    reason=f"the plane cannot answer: {exc}", issue="#1467"))
        return []
    finally:
        if plane is None:
            session.close()
    return [
        {
            "ts": e.get("ts"),
            "type": e.get("type"),
            "source": e.get("source"),
            "data": e.get("data", {}),
        }
        for e in events
        if isinstance(e.get("ts"), str) and e["ts"] >= cutoff
    ]


# --- composition --------------------------------------------------------------


def build_brief(fleet, paths: Paths, bot_id: str, now: int, *,
                selected_identity: tuple[str, str] | None = None) -> dict:
    """Compose the schema-2 brief document for one bot.

    ``now`` is injected rather than read here so the whole door is a pure
    function of (the plane, clock) and every section is testable without
    freezing time globally.
    """
    bot = fleet.bots[bot_id]
    degraded: list[Degradation] = []

    doors = load_dispatch_doors(paths)
    terminal = set(
        getattr(doors, "_TERMINAL", None) or {"completed", "failed", "blocked"}
    )

    # ONE plane session for every section (a brief once opened the plane five
    # times and exec'd the readers six — the R2b-1 simplify lens); a plane that
    # cannot answer omits every plane-served section, each field named.
    plane, note = plane_session(paths, fleet.name)
    if plane is None:
        for field in ("work", "workstreams", "reports", "alerts"):
            degraded.append(Degradation(field=field, mode="omitted", issue="#1467",
                                        reason=f"the plane cannot answer: {note} — no state is"
                                               " served rather than a wrong one"))
        sections = {"work": {}, "workstreams": {}, "reports": {}, "alerts": []}
    else:
        with plane:
            # The native adapter caches a roster before entering the context.
            # Pin the actual read snapshot, then refresh that roster within it
            # so the identity guard and every section see the same Plane state.
            plane.conn.execute("BEGIN")
            plane.roster = plane.pr.roster(plane.conn, plane.fleet)
            if selected_identity is not None:
                _assert_selected_identity(plane, fleet.name, bot_id, selected_identity)
            sections = {
                "work": _work_section(doors, paths, fleet, bot_id, now, degraded,
                                      plane=plane),
                "workstreams": _workstream_section(fleet, paths, now, degraded, plane=plane),
                "reports": _reports_section(paths, bot_id, terminal, degraded, plane=plane),
                "alerts": _alerts_section(paths, bot_id, now, degraded, plane=plane),
            }
    brief = {
        "schema": SCHEMA_VERSION,
        "bot": bot_id,
        "fleet": fleet.name,
        "generated_at": _iso(now),
        "mission": _mission_section(fleet, bot, paths),
        **sections,
    }

    # Cut from v1 with two independent reasons pointing the same way; recorded
    # so its absence is an answer rather than a gap.
    degraded.append(
        Degradation(
            field="utilization",
            mode="omitted",
            reason=(
                "uptime/utilization percentages count missing keepalive history "
                "as downtime, and nothing meaningfully writes the utilization "
                "file; the door serves no cost signal rather than a retention "
                "artifact"
            ),
            issue="#891",
        )
    )

    brief["degraded"] = [d.as_dict() for d in degraded]
    return brief


# --- rendering ----------------------------------------------------------------


def _short(ts: str | None) -> str:
    return (ts or "—")[:19].replace("T", " ")


def format_brief(brief: dict) -> str:
    """Sectioned plain text. Degraded fields are marked at the section header
    AND listed in full at the end — the inline marker is where the reader's eye
    already is, the block is where the detail belongs."""
    deg = brief.get("degraded", [])

    def mark(section: str) -> str:
        """Marker for a section header, covering its sub-fields too — the
        scope rule (a degradation on ``work.attention`` still degrades the
        WORK header) lives in ``_section_degraded``, shared with the
        boot renderer: a separately-worded copy per renderer is how that
        invariant dies in one of them silently."""
        entries = _section_degraded(deg, section)
        if not entries:
            return ""
        issues = ", ".join(sorted({e["issue"] for e in entries}))
        return f"  [degraded: {issues}]"

    def rows(items: list) -> tuple[list, list[str]]:
        """First TEXT_ROW_LIMIT items, plus a disclosure line when truncated.

        Silent truncation reads as exhaustive coverage; a capped section that
        does not say so misrepresents what was read.
        """
        if len(items) <= TEXT_ROW_LIMIT:
            return items, []
        return items[:TEXT_ROW_LIMIT], [
            f"    ... showing the oldest {TEXT_ROW_LIMIT} of {len(items)} "
            f"— full list in --json"
        ]

    out: list[str] = []
    out.append(
        f"BRIEF — {brief['bot']} @ {brief['fleet']}   {_short(brief['generated_at'])}"
    )
    if deg:
        out.append(f"  ! {len(deg)} degraded field(s) — see DEGRADED below")
    out.append("")

    m = brief.get("mission") or {}
    out.append("MISSION")
    # Fleet anchor first when the bot has its own: "the mission this fleet
    # serves" is the frame, the bot's line is its slice of it.
    if m.get("fleet_anchor") and m.get("fleet_anchor") != m.get("anchor"):
        out.append(f"  fleet:    {m['fleet_anchor']}")
    if m.get("anchor"):
        out.append(f"  {m['anchor']}")
    if m.get("charter"):
        out.append(f"  charter:  {m['charter']}")
    for p in m.get("projects", []):
        out.append(f"  project:  {p['project']} -> {p['mission_file']}")
    out.append("")

    work = brief.get("work")
    out.append(f"WORK{mark('work')}")
    if not work:
        out.append("  (unavailable — see DEGRADED)")
    else:
        items = work["items"]
        scope = "fleet intake" if work["scope"] == "fleet" else "assigned to this bot"
        out.append(f"  {scope}: {len(items)} open task(s)")
        shown, more = rows(items)
        for item in shown:
            out.append(
                f"    {item['task_id']}  {item['state']}  {item['title']}"
            )
            assignment = item["assignment"]
            if assignment:
                attention = item["attention"]
                status = attention["status"] if attention else "unavailable"
                out.append(
                    f"      assignment {assignment['assignment_id']}"
                    f"  due {_short(item['deadline'])}  attention {status}"
                )
                if attention and attention.get("past_due"):
                    out.append("      PAST DUE (literal deadline; watchdog status above)")
            if item["historical_references"]:
                out.append(
                    "      historical references: "
                    + ", ".join(item["historical_references"])
                )
        out.extend(more)
        issues = work["issues"]
        if issues:
            out.append(f"  unresolved history: {len(issues)} issue(s)")
            shown_issues, more_issues = rows(issues)
            for issue in shown_issues:
                out.append(
                    f"    {issue['code']} task={issue['task_id']}"
                    f" assignment={issue['assignment_id'] or '—'}"
                )
            out.extend(more_issues)
        if items:
            out.append("  inspect: claudlobby task show TASK_ID")
            out.append("           claudlobby assignment show ASSIGNMENT_ID")
    out.append("  current escalations: claudlobby fleet inbox")
    out.append("")

    w = brief.get("workstreams") or {}
    out.append(f"WORKSTREAMS{mark('workstreams')}")
    if not w:
        out.append("  (unavailable — see DEGRADED)")
    else:
        if not w.get("active"):
            out.append("  (none active)")
        shown, more = rows(w["active"])
        for e in shown:
            flags = []
            if e["stalled"]:
                flags.append("STALLED")
            if e["lease_expired"]:
                flags.append("LEASE EXPIRED")
            suffix = ("  " + " ".join(flags)) if flags else ""
            out.append(
                f"  {e['id']:<28} owner={e['owner_bot'] or '—':<10} "
                f"next: {e['next'] or '—'}{suffix}"
            )
        out.extend(more)
        if w.get("blocked"):
            out.append("  BLOCKED WAITS")
            for item in w["blocked"]:
                age = f"{item['age_seconds']}s" if item["age_seconds"] is not None else "unknown age"
                out.append(f"  {item['id']} waiting_on={item['waiting_on'] or 'unknown'} "
                           f"age={age} note={item['note'] or '—'}")
    out.append("")

    r = brief.get("reports") or {}
    if not r:
        # Never render a count here: "unacked (0)" over a plane that could not be read is
        # precisely the all-clear this section exists to stop being wrong about.
        out.append(f"REPORTS{mark('reports')}")
        out.append("  (unavailable — see DEGRADED)")
    else:
        unacked = r.get("unacked", [])
        out.append(f"REPORTS — unacked ({len(unacked)}){mark('reports')}")
        if r.get("cursor"):
            out.append(f"  since {_short(r['cursor'])}")
        shown, more = rows(unacked)
        for row in shown:
            out.append(
                f"  {_short(row['ts'])}  {(row['bot'] or '?'):<12} "
                f"{(row['status'] or '?'):<10} {(row['summary'] or '')[:60]}"
            )
        out.extend(more)
        if unacked:
            out.append("  -> claudlobby --json fleet reports list --unacknowledged")
            out.append("     claudlobby fleet reports ack --through ACK_CURSOR --request-id UUID")
    out.append("")

    alerts = brief.get("alerts", [])
    if any(entry["mode"] == "omitted" for entry in _section_degraded(deg, "alerts")):
        out.append(f"ALERTS{mark('alerts')}")
        out.append("  (unavailable — see DEGRADED)")
    else:
        out.append(
            f"ALERTS — critical events, last {ALERT_WINDOW_H}h ({len(alerts)}){mark('alerts')}"
        )
        shown, more = rows(alerts)
        for a in shown:
            out.append(f"  {_short(a['ts'])}  {a['type']:<20} {a.get('source') or ''}")
        out.extend(more)
    out.append("")
    if "usage" in brief:
        usage = brief["usage"]
        counts = usage["usage"]
        coverage = usage["coverage"]
        out.append("USAGE — Claude transcript token counts")
        out.append(f"  {usage['window']['since']} to {usage['window']['until']}")
        out.append(f"  input={counts['input_tokens']} output={counts['output_tokens']} "
                   f"cache-write={counts['cache_creation_input_tokens']} "
                   f"cache-read={counts['cache_read_input_tokens']} "
                   f"turns={counts['turns']}")
        out.append(f"  coverage={coverage['status']} files={coverage['files_read']} "
                   f"skipped>={coverage['files_skipped_at_least']} "
                   f"issues={','.join(coverage['issues']) or '-'}")
        out.append("  quota=unavailable (no provider observation)")
        out.append("")
    if deg:
        out.append("DEGRADED — fields this door will not serve as plain truth")
        for e in deg:
            out.append(f"  {e['field']:<14} {e['mode']:<8} {e['issue']}  {e['reason']}")
        out.append("")

    return "\n".join(out)


# --- shared degraded-marker helpers (both renderers) ---------------------------


def _section_degraded(deg: list[dict], section: str) -> list[dict]:
    """Degradations scoped to ``section``, INCLUDING its sub-fields.

    The prefix match is the load-bearing half: a degradation scoped to
    ``work.attention`` still degrades the WORK section — without it a
    clean header floats above a zero that is not a measurement at all, the one
    output this door must never produce. Both renderers consume this; a
    separately-worded copy in each is how the invariant dies in one of them
    silently.
    """
    return [
        e for e in deg if e["field"] == section or e["field"].startswith(section + ".")
    ]


def _degraded_mark(entries: list[dict]) -> str:
    """`` [degraded: #x, #y]`` for a section header, or ``""`` when clean."""
    if not entries:
        return ""
    return f" [degraded: {', '.join(sorted({e['issue'] for e in entries}))}]"


# --- the boot payload (#1102 R3 / M1, locked fork R3-F1: O-B+r) ---------------

# Render-time budget for the boot payload, in characters (~4 chars/token, so
# ~250 tokens). Enforced by dropping DETAIL lines lowest-priority-first — never
# the header, the empty/degraded provenance lines, the overflow disclosure, or
# the door line, which are cap-exempt: coverage honesty must not lose by cap
# arithmetic. The constant lives here, alone, so the canary can move it.
BOOT_CHAR_BUDGET = 1000

# Detail rows the boot payload will print across all work states.
# Priority when over: ORPHANED first (the respawned session reading this
# payload is the ONLY natural consumer of the orphan door — dispatch delivery
# is ephemeral tmux and the party that should act no longer exists anywhere
# else), then overdue, then open oldest-first.
BOOT_DETAIL_LIMIT = 3


def boot_provenance(paths: Paths, now: int, *, fleet_name: str | None = None,
                    viewer: str | None = None,
                    selected_identity: tuple[str, str] | None = None) -> dict:
    """Canonical task history and registry facts for the boot no-work line."""
    from .task_state import read_tasks
    from .workstreams import lease_days_env

    plane, note = plane_session(paths, fleet_name)
    if plane is None:
        return {"work": {"state": "unreachable", "note": note},
                "registry": {"present": False, "note": note}}
    conn, pr, fleet = plane.conn, plane.pr, plane.fleet
    try:
        conn.execute("BEGIN")
        plane.roster = pr.roster(conn, fleet)
        if selected_identity is not None:
            if viewer is None:
                raise ValueError("selected brief provenance requires a viewer")
            _assert_selected_identity(plane, fleet, viewer, selected_identity)
        snapshot = read_tasks(conn, fleet_uid=pr.fleet_uid(conn, fleet))
        ever = len(snapshot.tasks)
        recent = sum(
            1 for task in snapshot.tasks
            if (epoch := _epoch(task.occurred_at)) is not None
            and epoch >= now - 24 * 3600
        )
        entries = len(pr.workstream_registry(
            conn, fleet, lease_days=lease_days_env()).get("workstreams", {}))
    except BriefIdentityMismatch:
        raise
    except Exception as exc:
        return {"work": {"state": "unreachable", "note": str(exc)},
                "registry": {"present": False, "note": str(exc)}}
    finally:
        conn.close()
    return {"work": {"state": "ok", "tasks_ever": ever, "tasks_24h": recent,
                     "history_issues": len(snapshot.issues)},
            "registry": {"present": True, "entries": entries}}


def _boot_detail_lines(work: dict, now: int) -> tuple[list[str], int]:
    """At most three canonical tasks, with the most urgent observations first."""
    def priority(item: dict) -> int:
        if item["state"] == "queued":
            return 2
        observation = item["attention"] or {}
        return {
            "orphaned": 0, "overdue": 1, "unknown": 3,
            "past_due": 4, "not_due": 5,
        }.get(observation.get("status"), 3)

    ordered = sorted(work["items"], key=lambda item: (priority(item), item["admitted_at"]))
    lines = []
    for item in ordered[:BOOT_DETAIL_LIMIT]:
        assignment = item["assignment"]
        observation = item["attention"] or {}
        status = "queued" if assignment is None else observation.get("status", "attention unavailable")
        sent_at = assignment["dispatched_at"] if assignment else item["admitted_at"]
        age_s = max(0, now - (_epoch(sent_at) or now))
        age = f"{age_s // 3600}h" if age_s >= 3600 else f"{age_s // 60}m"
        assignment_id = f" / {assignment['assignment_id']}" if assignment else ""
        note = ""
        if status == "orphaned":
            note = " (issued pre-restart; may need re-issue)"
        elif status == "overdue":
            overdue_s = observation.get("elapsed_past_deadline_s") or 0
            note = f" (+{overdue_s // 60}m past deadline)"
        lines.append(f"  {status.upper()} {item['task_id']}{assignment_id} — {age} old{note}")
    return lines, max(0, len(ordered) - BOOT_DETAIL_LIMIT)


def format_boot_brief(brief: dict, prov: dict) -> str:
    """Bounded SessionStart pointer to canonical work and its read limitations."""
    bot = brief["bot"]
    door = (f"full state: claudlobby brief --bot {bot} [--json]"
            " | current escalations and unseen reports: claudlobby fleet inbox")
    header = (
        f"fleet-brief — {bot} @ {brief['fleet']} "
        f"(as of {_short(brief['generated_at'])}, schema {brief['schema']})"
    )
    work_deg = _section_degraded(brief.get("degraded", []), "work")
    issues = ", ".join(sorted({entry["issue"] for entry in work_deg}))
    mark = f" [degraded: {issues}]" if issues else ""
    work = brief.get("work")
    exempt: list[str] = [header]
    detail: list[str] = []

    if not work:
        exempt.append(
            f"work UNAVAILABLE — {issues or 'see door'} "
            "(fail-closed, not zero) — see door"
        )
    else:
        items = work["items"]
        queued = sum(item["state"] == "queued" for item in items)
        overdue = sum(
            (item["attention"] or {}).get("status") == "overdue" for item in items)
        orphaned = sum(
            (item["attention"] or {}).get("status") == "orphaned" for item in items)
        history_issues = len(work["issues"])
        if items or history_issues:
            exempt.append(
                f"work: {len(items)} open, {queued} queued, {overdue} overdue, "
                f"{orphaned} orphaned, {history_issues} history issue(s){mark}"
            )
            detail, hidden = _boot_detail_lines(work, _epoch(brief["generated_at"]) or 0)
            if hidden:
                exempt.append(f"  (+{hidden} more — door)")
            if history_issues:
                exempt.append("  unresolved history — inspect full state")
        else:
            history = prov.get("work", {})
            if history.get("state") == "ok":
                source = (
                    f"plane: {history.get('tasks_ever', 0)} tasks ever, "
                    f"{history.get('tasks_24h', 0)} in 24h"
                )
            else:
                source = f"plane: {history.get('state', 'unknown')}"
            registry = prov.get("registry", {})
            if registry.get("present"):
                entries = registry.get("entries")
                registry_text = (
                    f"registry: {entries} entr{'y' if entries == 1 else 'ies'}"
                    if entries is not None else "registry: present (unreadable)"
                )
            else:
                registry_text = "registry: unreachable"
            exempt.append(
                f"no open tasks for this bot ({source}); "
                f"{registry_text}{mark}"
            )

    dropped = 0
    def _render() -> str:
        parts = list(exempt) + detail
        if dropped:
            parts.append(f"  (+{dropped} more capped — door)")
        parts.append(door)
        return "\n".join(parts)

    while len(_render()) > BOOT_CHAR_BUDGET and detail:
        detail.pop()
        dropped += 1
    return _render()
