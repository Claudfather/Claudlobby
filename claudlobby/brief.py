"""claudlobby brief — one read door over the state the fleet already writes.

``claudlobby brief --bot X [--json] [--ack]`` composes five sections that today
live in five unrelated files and are read (where they are read at all) by
hand-rolled jq against stale schemas: mission pointers, dispatches, workstreams,
unacked reports, and recent critical events. Skills consume THIS, never the raw
files — that is the coupling the door exists to kill.

Read-only by construction. The whole module performs exactly one write: the
``--ack`` cursor (``brief-cursor-<bot>.json``), single-purpose and atomic. No
ledger, registry, or event file is written by any path here.

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
      OMITTED. ``claudlobby uptime`` counts missing keepalive history as
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
The dispatch sections come from ``lib/dispatch-overdue.py`` rather than a second
join, which is correct and non-negotiable. Since the F18 closure (R2a) those
doors read the PLANE and nothing else, and they refuse rather than answer when
they cannot reach it — no db, no schema, a plane that holds no bot of the
fleet — so the one failure mode left is the loud one. This module opens ONE
plane session for the section (``open_plane`` → ``_classify_all`` +
``open_dispatches``, ``plane=`` shared) and, when the matcher refuses or the
install's matcher predates the plane-only reader, withholds the WHOLE section
with all three fields named in ``degraded[]``: not zero, which is a false
all-clear, and not everything, which is a wall of finished work presented as
outstanding.

The reports, alerts and workstreams sections read the plane through the same
rule (``plane_conn``: no flag, no retirement fact, no file; unreachable is not
empty) and OMIT with the note when it cannot answer — ``unacked (0)`` from a
plane that could not be read would be #949 and #1024 exactly, re-created by
the surface built to close them. Orphan classification still returns a clean
empty set when it has no bots dir to read ``.spawn`` mtimes from (#1014's
family), which is indistinguishable from "no work was lost to a restart" —
labeled, since open and overdue stay sound.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .paths import Paths
from .source_state import (
    SOURCE_ABSENT,
    SOURCE_OK,
    SOURCE_UNREADABLE,
    probe_dir,
    probe_source,
)

SCHEMA_VERSION = 1

# Critical-event lookback. Fleet events carry no resolution state, so recency is
# the only available proxy for "still unresolved" — which is why the rendered
# header says "last 24h" and not "unresolved". Naming the proxy accurately is
# the honest move; a section titled "unresolved" would be asserting a fact no
# row in the ledger records.
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

# Workstream staleness window, in days. Matches lib/workstream-update.sh:56's
# default; a read-side threshold that disagreed with the writer's lease would
# flag as stalled exactly the workstreams the writer still considers fresh.
DEFAULT_LEASE_DAYS = 14


def _lease_days(fleet) -> int:
    """Lease window in days, resolved the way the writer resolves it.

    ``WORKSTREAM_LEASE_DAYS`` wins because that is the only thing
    ``workstream-update.sh`` reads — inside a bot session it is the composed
    value, and if it has been overridden there, the lease actually written was
    the override. A bare CLI run has no bot.conf sourced, so it falls back to
    ``fleet.workstreams.lease_days``, which is what the composer emits into
    that variable in the first place.
    """
    raw = os.environ.get("WORKSTREAM_LEASE_DAYS")
    if raw is not None:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
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
# two can never drift: these strings are emitted verbatim in the schema-1
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
    """Import ``lib/dispatch-overdue.py`` as a module, or None when unreadable.

    The same ``spec_from_file_location`` seam ``tests/conftest.py`` uses: those
    doors are a standalone stdlib script with no package, and re-implementing
    the join here is precisely what this issue forbids — a second copy would
    drift from the watchdog and the two would disagree about which dispatches
    are open.

    None (rather than a raise) when the file is missing, because the caller has
    a better answer than a traceback: an unavailable door degrades the dispatch
    section *loudly*. Printing "0 open" because the matcher could not be loaded
    would be the exact failure this door exists to prevent.
    """
    return load_lib_module(paths, "dispatch-overdue.py")


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


def plane_conn(paths: Paths):
    """(conn, readers, note): an OPEN read-only plane connection plus the
    install's stdlib readers when the plane can answer for this fleet, else
    (None, None, note). The plane is the ONLY source (F18 closure, R2b): no
    flag, no retirement fact, no file to fall back on — and unreachable is
    not empty. No db, no schema, an unreadable lib/, no fleet name, or a
    plane that holds no bot of the fleet (a wrong root is not "nothing
    recorded" — the matcher's rule, #1014's class) all return the note, and
    the section is OMITTED with it. The caller closes the connection."""
    fleet = resolve_fleet_name(paths)
    if not fleet:
        return None, None, ("no fleet name resolved (no overlay, no fleet.yaml naming one, no"
                            " CLAUDLOBBY_FLEET / FLEET_NAME) — the plane's rows are per fleet")
    pr = load_lib_module(paths, "plane-readers.py")
    if pr is None:
        return None, None, f"lib/plane-readers.py is not readable under {paths.lib}"
    try:
        conn = pr.connect(str(paths.root))
    except Exception as exc:
        return None, None, (f"the plane is unreachable ({exc}) — restore state/plane/plane.db under"
                            f" {paths.root} or name the right root")
    try:
        roster = pr.roster(conn, fleet)
    except Exception as exc:                        # a schema the readers cannot read: omit, never guess
        conn.close()
        return None, None, f"the plane could not answer for fleet {fleet!r} ({exc})"
    if not roster:
        conn.close()
        return None, None, (f"the plane at {paths.root} holds no bot of fleet {fleet!r} — wrong root, or a"
                            " fleet it has never seen")
    return conn, pr, None


def load_lib_module(paths: Paths, filename: str):
    """Import one of the INSTALL's stdlib ``lib/*.py`` scripts as a module,
    or None when unreadable — ``load_dispatch_doors``'s seam, generalised
    (Phase B: the plane readers ride it too). Always the install's ``lib/``,
    never the importing checkout's copy: the install's scripts are what the
    bash doors run, and a checkout fallback changes which install answers."""
    import importlib.util

    src = paths.lib / filename
    if not src.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location(
            filename.replace("-", "_").removesuffix(".py"), src)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except (OSError, SyntaxError, ImportError):
        return None


# --- the ack cursor (the module's only write) ---------------------------------


def cursor_path(paths: Paths, bot: str) -> Path:
    """Where a viewer's ack cursor lives. Per-bot: two managers acking the same
    ledger must not clobber each other's read position."""
    return paths.fleet_state / f"brief-cursor-{bot}.json"


def read_cursor(paths: Paths, bot: str) -> str | None:
    """Last acked timestamp, or None when the viewer has never acked.

    A corrupt or unreadable cursor reads as None — i.e. "you have acked
    nothing", which over-reports unacked work. That direction is deliberate:
    the failure this door closes (#1024, #949) is a report going *unseen*, so
    a broken cursor must fail toward showing too much, never too little.
    """
    p = cursor_path(paths, bot)
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    ts = data.get("last_seen_ts")
    return ts if isinstance(ts, str) else None


def write_cursor(paths: Paths, bot: str, last_seen_ts: str) -> Path:
    """Advance the viewer's cursor. Atomic tmp+rename, the module's ONE write."""
    p = cursor_path(paths, bot)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"last_seen_ts": last_seen_ts}) + "\n")
    tmp.replace(p)
    return p


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


def _dispatch_section(
    doors, paths: Paths, bot_id: str, now: int, degraded: list[Degradation],
    fleet_name: str | None = None,
) -> dict:
    """open / overdue / orphaned, all three from the #835 doors.

    ``doors`` is passed in rather than loaded here: importing the matcher
    executes a module, and the caller needs it too.

    OPEN is deadline-blind and therefore a superset of OVERDUE; a row can be
    open and past its deadline without appearing under overdue, because the
    watchdog additionally applies the progress-grace window, the max-age expiry
    cap, and the orphan split. The rendered ``past_due`` flag means literally
    ``now > expected_by`` and is not a claim that the watchdog is alarming.
    """
    # THE PLANE IS THE ONLY SOURCE (F18 R2a). The two ledgers this section once
    # probed — omitting itself when either was absent, because the matcher
    # failed OPEN on a missing report ledger and served a wall of finished
    # work as outstanding — no longer exist. The matcher now refuses when the
    # plane cannot answer (PlaneUnreachable), and this section OMITS on that,
    # with the remedy named: never zero (a false all-clear), never a guess.
    if doors is None:
        degraded.append(
            Degradation(
                field="dispatches",
                mode="omitted",
                reason=(
                    f"the shared matcher at {paths.lib / 'dispatch-overdue.py'} "
                    "could not be loaded; dispatch state is unknown, and an empty "
                    "list would read as 'nothing open'"
                ),
                issue="#835",
            )
        )
        return {}

    # probe_dir, not is_dir(): a bots dir that stats fine and raises on listing
    # makes every .spawn lookup fail, so orphan detection returns empty for a
    # reason that has nothing to do with the fleet.
    _bots_probe = probe_dir(paths.runtime_bots)
    bots_dir = (
        str(paths.runtime_bots) if _bots_probe.state == SOURCE_OK else None
    )

    # Resolve the expiry cap the way the CLI does. The matcher's Python API
    # takes max_age as a defaulted argument and only its main() consults
    # DISPATCH_OVERDUE_MAX_AGE_S, so a fleet that tunes the cap would get a
    # brief disagreeing with the watchdog it is supposed to mirror — and
    # "byte-consistent with dispatch-overdue.py --all" is the contract. .env is
    # already loaded into the environment by the caller.
    max_age = getattr(
        doors, "_resolve_max_age", lambda: doors.DEFAULT_OVERDUE_MAX_AGE_S
    )()

    # Both sets and the open list from ONE plane session: the matcher's
    # providers are plane-only (F18 R2a) and `_classify_all` is "THE
    # in-process door when a caller wants both sets" — the underscore is
    # scope, not privacy. The fleet the matcher reads: the overlay's, else
    # the manifest's (root mode names its fleet in fleet.yaml alone), else
    # the carriers inside the matcher itself.
    plane_ctx = {"fleet": paths.fleet_name or fleet_name, "root": str(paths.root)}
    fields = ("dispatches.overdue", "dispatches.orphaned", "dispatches.open")

    def _withhold(reason: str, issue: str) -> dict:
        # Every list is named and the WHOLE section is withheld: three empty
        # lists would render as "0 open", a false all-clear — the shape the
        # renderer reads as "unavailable" is the empty dict. A field neither
        # present nor listed does not exist, so the three are always named
        # together (the structural lens found a second open's failure naming
        # one of them).
        for field in fields:
            degraded.append(Degradation(field=field, mode="omitted", reason=reason, issue=issue))
        return {}

    # The matcher is resolved from the INSTALL's lib/, not from wherever this
    # package was imported from — deliberately, since the door and the watchdog
    # must agree byte-for-byte. The two therefore version independently: a
    # root whose lib/ predates the plane-only reader has none of these doors,
    # and calling its ledger-era signatures would raise out of a read-only
    # command on a fleet whose install is simply a few pulls behind.
    missing = [n for n in ("open_plane", "PlaneUnreachable", "_classify_all", "open_dispatches")
               if not hasattr(doors, n)]
    if missing:
        return _withhold(
            f"the matcher installed at {paths.lib / 'dispatch-overdue.py'} predates the"
            f" plane-only reader (no {', '.join(missing)}) — pull the install and re-run,"
            " so no dispatch state is served rather than a wrong one", "#1467")
    try:
        with doors.open_plane(**plane_ctx) as plane:
            over, orph = doors._classify_all(now, max_age, bots_dir, plane=plane)
            open_rows = doors.open_dispatches(bot_id, plane=plane)
    except doors.PlaneUnreachable as exc:
        return _withhold(
            f"the plane cannot answer: {exc} — restore the plane db (state/plane/plane.db)"
            f" under {paths.root} or name the right root, so no dispatch state is served"
            " rather than a wrong one", "#1467")
    overdue_rows = over.get(bot_id.lower(), [])
    orphan_rows = orph.get(bot_id.lower(), [])

    # Orphan classification reads .spawn mtimes, and the matcher returns a clean
    # EMPTY set when it has no bots dir to read them from (#1014's family) —
    # indistinguishable from "no work was lost to a restart". Labeled rather
    # than omitted, because open and overdue are still sound without it.
    if bots_dir is None:
        degraded.append(
            Degradation(
                field="dispatches.orphaned",
                mode="labeled",
                reason=(
                    (
                        f"no bots directory at {paths.runtime_bots}"
                        if _bots_probe.state == SOURCE_ABSENT
                        else f"the bots directory at {paths.runtime_bots} "
                        "exists but cannot be listed"
                    )
                    + ", so respawn cannot be detected and the orphaned list "
                    "is empty by construction rather than by measurement"
                ),
                issue="#1014",
            )
        )

    return {
        "open": [
            {
                "task_id": tid,
                "dispatched_at": _iso(da),
                "expected_by": _iso(exp),
                "past_due": exp is not None and now > exp,
            }
            for da, exp, tid in open_rows
        ],
        "overdue": [
            {
                "task_id": tid,
                "dispatched_at": _iso(da),
                "expected_by": _iso(exp),
                "overdue_by_s": elapsed,
            }
            for da, exp, elapsed, tid in overdue_rows
        ],
        "orphaned": [
            {
                "task_id": tid,
                "dispatched_at": _iso(da),
                "expected_by": _iso(exp),
                "overdue_by_s": elapsed,
            }
            for da, exp, elapsed, tid in orphan_rows
        ],
    }


def _workstream_section(
    fleet, paths: Paths, now: int, degraded: list[Degradation]
) -> dict:
    """Active workstreams with the stall flags the pulse consumer never shipped.

    Read-only: the registry is the plane's rendering (``plane_workstreams``)
    and nothing here writes it back. ``stalled`` means no progress within the lease window; ``lease
    expired`` means the lease itself has run out. They are independent — a
    renewed workstream keeps its lease while its ``last_progress_ts`` stays put
    (``lib/workstream-update.sh:249`` is explicit that renew does not advance
    progress), which is exactly the state worth surfacing.
    """
    from .workstreams import plane_workstreams
    workstreams, note = plane_workstreams(paths)      # the plane, the only source (F18 R2b)
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
    return {"active": active, "stalled": stalled}


def _reports_section(
    paths: Paths, cursor: str | None, terminal: set[str], degraded: list[Degradation]
) -> dict:
    """Terminal reports newer than the viewer's cursor — fleet-wide, on purpose.

    Note the deliberate asymmetry with the sections above: dispatches and
    mission are *about* ``--bot X``, while this one is *for* ``--bot X to act
    on*. The question #1024 and #949 left unanswered is "what did my workers
    finish that I have not acted on", so filtering to the viewer's own reports
    would answer the wrong one. Every row carries ``bot``, so a consumer that
    does want a narrower view can take it.
    """
    # The plane, the only source (F18 R2b): the same row shape the ledger had
    # (the readers render `ts` in the legacy form, so the cursor keeps
    # comparing). A plane that cannot answer OMITS the section: "unacked (0)"
    # would assert that no worker is waiting on a decision — #949 and #1024
    # exactly, re-created by the surface built to close them.
    conn, pr, note = plane_conn(paths)
    if conn is None:
        degraded.append(Degradation(field="reports", mode="omitted",
                                    reason=f"{note}; '0 unacked' would assert that no worker is waiting"
                                           " on a decision, which is the incident class this section"
                                           " exists to surface",
                                    issue="#1467"))
        return {}
    try:
        rows = pr.report_rows(conn, resolve_fleet_name(paths))
    except Exception as exc:
        degraded.append(Degradation(field="reports", mode="omitted",
                                    reason=f"the plane cannot answer: {exc}", issue="#1467"))
        return {}
    finally:
        conn.close()
    stripped = sum(1 for r in rows if r.get("_body_stripped") and r.get("_source") != "task_event")
    if stripped:
        degraded.append(Degradation(field="reports", mode="labeled",
                                    reason=f"{stripped} report(s) hold no summary on the plane (the capture"
                                           " policy kept no body and no task event named one)",
                                    issue="#1444"))
    unacked = [
        {"ts": r["ts"], "bot": r["bot"], "status": r["status"], "task_id": r["task_id"],
         "summary": r["summary"], "pr_url": r["pr_url"]}
        for r in rows
        if r.get("status") in terminal and (cursor is None or r["ts"] > cursor)
    ]
    unacked.sort(key=lambda r: r["ts"])
    return {"cursor": cursor, "unacked": unacked, "source": "plane"}


def _alerts_section(
    paths: Paths, bot_id: str, now: int, degraded: list[Degradation]
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
    from .commands.events import collect_plane_events, plane_events_conn
    try:
        conn, note = plane_events_conn(paths)
    except RuntimeError as exc:
        conn, note = None, str(exc)
    if conn is None:
        degraded.append(Degradation(field="alerts", mode="omitted",
                                    reason=f"the plane cannot answer: {note} — an empty list would mean"
                                           " 'could not look', not 'nothing is wrong'",
                                    issue="#1467"))
        return []
    try:
        events = collect_plane_events(conn, paths, bot=bot_id, critical_only=True, since=cutoff)
    except RuntimeError as exc:
        degraded.append(Degradation(field="alerts", mode="omitted",
                                    reason=f"the plane cannot answer: {exc}", issue="#1467"))
        return []
    finally:
        conn.close()
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


def build_brief(fleet, paths: Paths, bot_id: str, now: int) -> dict:
    """Compose the schema-1 envelope for one bot.

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

    brief = {
        "schema": SCHEMA_VERSION,
        "bot": bot_id,
        "fleet": fleet.name,
        "generated_at": _iso(now),
        "mission": _mission_section(fleet, bot, paths),
        "dispatches": _dispatch_section(doors, paths, bot_id, now, degraded, fleet_name=fleet.name),
        "workstreams": _workstream_section(fleet, paths, now, degraded),
        "reports": _reports_section(
            paths, read_cursor(paths, bot_id), terminal, degraded
        ),
        "alerts": _alerts_section(paths, bot_id, now, degraded),
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
        scope rule (a degradation on ``dispatches.open`` still degrades the
        DISPATCHES header) lives in ``_section_degraded``, shared with the
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

    d = brief.get("dispatches")
    out.append(f"DISPATCHES{mark('dispatches')}")
    if not d:
        # Carry the count up to the section, not just into the degraded block:
        # "unavailable" alone reads identically whether it is hiding nothing or
        # hiding a dispatch that has been rotting for a day.
        n = next(
            (
                e["count"]
                for e in deg
                if e["field"] == "dispatches"
                and e["mode"] == "omitted"
                and e.get("count")
            ),
            None,
        )
        if n:
            out.append(
                f"  (unavailable — {n} row(s) past deadline, status "
                f"undeterminable; see DEGRADED)"
            )
        else:
            out.append("  (unavailable — see DEGRADED)")
    else:
        shown, more = rows(d["open"])
        out.append(f"  open ({len(d['open'])})")
        for r in shown:
            flag = "  PAST DUE" if r["past_due"] else ""
            out.append(
                f"    {r['task_id']:<26} sent {_short(r['dispatched_at'])}"
                f"  due {_short(r['expected_by'])}{flag}"
            )
        out.extend(more)
        for label in ("overdue", "orphaned"):
            shown, more = rows(d[label])
            out.append(f"  {label} ({len(d[label])})")
            for r in shown:
                out.append(
                    f"    {r['task_id']:<26} sent {_short(r['dispatched_at'])}"
                    f"  +{r['overdue_by_s'] // 60}m past deadline"
                )
            out.extend(more)
    out.append("")

    w = brief.get("workstreams") or {}
    out.append(f"WORKSTREAMS{mark('workstreams')}")
    if not w:
        out.append("  (unavailable — see DEGRADED)")
    elif not w.get("active"):
        out.append("  (none active)")
    else:
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
            out.append(f"  -> claudlobby brief --bot {brief['bot']} --ack   to clear")
    out.append("")

    alerts = brief.get("alerts", [])
    out.append(
        f"ALERTS — critical events, last {ALERT_WINDOW_H}h ({len(alerts)}){mark('alerts')}"
    )
    shown, more = rows(alerts)
    for a in shown:
        out.append(f"  {_short(a['ts'])}  {a['type']:<20} {a.get('source') or ''}")
    out.extend(more)
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
    ``dispatches.open`` still degrades the DISPATCHES section — without it a
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

# Detail rows the boot payload will print across all dispatch classes.
# Priority when over: ORPHANED first (the respawned session reading this
# payload is the ONLY natural consumer of the orphan door — dispatch delivery
# is ephemeral tmux and the party that should act no longer exists anywhere
# else), then overdue, then open oldest-first.
BOOT_DETAIL_LIMIT = 3


def boot_provenance(paths: Paths, now: int) -> dict:
    """The door-side facts the boot payload's empty-state line renders.

    Interim for #1122: the never-vs-quiet distinction ("no work in flight" vs
    "no recorded fleet history" — different answers, and the gap between them
    is the motivating incident) is not expressible in the schema-1 envelope,
    so the boot mode computes it here. When #1122 lands these facts move into
    the envelope and this helper is deleted.

    Both facts come from the PLANE (F18 closure, R2b): the dispatches the
    dispatch door landed for the fleet's bots — ever, and in a fixed 24h
    window, a human-scale recency fact deliberately NOT the watchdog's expiry
    mirror — and the workstream registry's entry count. Same read discipline
    as the door: a plane that cannot answer reports its state, never a zero.
    """
    conn, pr, note = plane_conn(paths)
    if conn is None:
        return {"dispatches": {"state": "unreachable", "note": note},
                "registry": {"present": False, "note": note}}
    from .workstreams import lease_days_env
    fleet = resolve_fleet_name(paths)
    try:
        uids = [u for e in pr.roster(conn, fleet).values() for u in e["uids"]]
        marks = ",".join("?" * len(uids))
        since = datetime.fromtimestamp(now - 24 * 3600, timezone.utc).isoformat()
        ever = conn.execute(f"SELECT COUNT(*) FROM assignments WHERE assignee_uid IN ({marks})",
                            uids).fetchone()[0]
        recent = conn.execute(f"SELECT COUNT(*) FROM assignments WHERE assignee_uid IN ({marks})"
                              " AND occurred_at >= ?", (*uids, since)).fetchone()[0]
        entries = len(pr.workstream_registry(conn, fleet,
                                             lease_days=lease_days_env()).get("workstreams", {}))
    except Exception as exc:
        return {"dispatches": {"state": "unreachable", "note": str(exc)},
                "registry": {"present": False, "note": str(exc)}}
    finally:
        conn.close()
    return {"dispatches": {"state": "ok", "rows_ever": ever, "rows_24h": recent},
            "registry": {"present": True, "entries": entries}}


def _boot_detail_lines(d: dict, now: int) -> tuple[list[str], int]:
    """(detail lines in priority order, hidden-count) for the dispatch section."""
    # The door's `open` is deliberately a SUPERSET (deadline-blind, #904), so a
    # row can appear as both overdue and open; the boot payload shows each task
    # once, at its highest-priority class.
    seen: set = set()
    prioritized = []
    for label, rows_ in (
        ("ORPHANED", d["orphaned"]),
        ("overdue", d["overdue"]),
        ("open", sorted(d["open"], key=lambda r: r["dispatched_at"] or "")),
    ):
        for r in rows_:
            if r["task_id"] in seen:
                continue
            seen.add(r["task_id"])
            prioritized.append((label, r))
    lines = []
    for label, r in prioritized[:BOOT_DETAIL_LIMIT]:
        age_s = max(0, now - (_epoch(r.get("dispatched_at")) or now))
        age = f"{age_s // 3600}h" if age_s >= 3600 else f"{age_s // 60}m"
        note = ""
        if label == "ORPHANED":
            note = " (issued pre-restart; may need re-issue)"
        elif label == "overdue":
            note = f" (+{r.get('overdue_by_s', 0) // 60}m past due)"
        lines.append(f"  {label} {r['task_id']} — sent {age} ago{note}")
    return lines, max(0, len(prioritized) - BOOT_DETAIL_LIMIT)


def format_boot_brief(brief: dict, prov: dict) -> str:
    """The SessionStart boot payload — the locked O-B+r shape, and nothing else.

    Deliberately NOT ``format_brief``: that renderer is the on-demand full view
    (wall-capable — a live run rendered 335 report lines). This one is standing
    context re-read for the life of every session that carries it, so it is
    pointer-first, hard-capped, and mission-free (mission is already composed
    into every bot's CLAUDE.md — injecting it again is duplicate spend).

    The empty state is the point, not a collapse case (fork R3-F1, #1102): an
    all-quiet boot renders WHY it is quiet, with source provenance — never
    silence, never a bare zero. "0 open (plane: N dispatches ever)" and "no
    recorded fleet history" are different answers; the motivating incident was
    the gap between them.
    """
    bot = brief["bot"]
    door = f"full state: claudlobby brief --bot {bot} [--json]"
    header = (
        f"fleet-brief — {bot} @ {brief['fleet']} "
        f"(as of {_short(brief['generated_at'])}, schema {brief['schema']})"
    )

    d_deg = _section_degraded(brief.get("degraded", []), "dispatches")
    issues = ", ".join(sorted({e["issue"] for e in d_deg}))
    mark = f" [degraded: {issues}]" if issues else ""
    d = brief.get("dispatches")

    exempt: list[str] = [header]
    detail: list[str] = []

    if not d:
        # OMITTED by the door's trust rule. "Nothing to say" and "nothing
        # served" are textually identical unless degraded[] is consulted, so
        # this branch synthesizes its line from there — rendering a zero here
        # would re-manufacture the false all-clear the door refuses to emit.
        exempt.append(
            f"dispatches UNAVAILABLE — {issues or 'see door'} "
            "(fail-closed, not zero) — see door"
        )
    else:
        n_open, n_over, n_orph = len(d["open"]), len(d["overdue"]), len(d["orphaned"])
        if n_open or n_over or n_orph:
            exempt.append(
                f"dispatches: {n_open} open, {n_over} overdue, {n_orph} orphaned{mark}"
            )
            lines, hidden = _boot_detail_lines(d, _epoch(brief["generated_at"]) or 0)
            detail.extend(lines)
            if hidden:
                exempt.append(f"  (+{hidden} more — door)")
        else:
            # The all-quiet line, with provenance. Never a bare zero.
            dl = prov.get("dispatches", {})
            if dl.get("state") == "ok":
                led = (
                    f"plane: {dl.get('rows_ever', 0)} dispatches ever, "
                    f"{dl.get('rows_24h', 0)} in 24h"
                )
            else:
                led = f"plane: {dl.get('state', 'unknown')}"
            reg = prov.get("registry", {})
            if reg.get("present"):
                entries = reg.get("entries")
                reg_txt = (
                    f"registry: {entries} entr{'y' if entries == 1 else 'ies'}"
                    if entries is not None
                    else "registry: present (unreadable)"
                )
            else:
                reg_txt = "registry: unreachable"
            exempt.append(
                f"all quiet for this bot: 0 open dispatches ({led}); {reg_txt}{mark}"
            )

    # Token cap, enforced in characters at render time (the line cap alone
    # disagrees with the token cap ~2x at real path density). Detail lines
    # drop lowest-priority-first into the disclosed remainder, so truncation
    # is never silent. Re-rendering per drop is deliberate: the remainder
    # line's own width changes with the count, so measuring the real artifact
    # cannot drift the way an arithmetic model of it would.
    dropped = 0

    def _render() -> str:
        parts = list(exempt) + detail
        if dropped:
            parts.append(f"  (+{dropped} more capped — door)")
        parts.append(door)
        return "\n".join(parts)

    while len(_render()) > BOOT_CHAR_BUDGET and detail:
        detail.pop()  # lowest priority is last
        dropped += 1
    return _render()
