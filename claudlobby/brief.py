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
      MEASURED. Unescaped writer fields produce invalid JSONL rows that every
      reader silently ``continue``s past, so any count derived from them can
      under-report. This module re-scans the two ledgers it consumes and
      reports how many rows failed to parse. Self-clearing: once the writers
      escape, the count is 0 and the disclosure disappears on its own. This is
      NOT #911's fix — the shared readers still drop those rows, and this does
      not change that. It measures how many they dropped.

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

CONSUMING THE SHARED DOORS DEFENSIVELY
--------------------------------------
The dispatch sections come from ``lib/dispatch-overdue.py`` rather than a second
join, which is correct and non-negotiable — but those doors **fail open**, and a
reader that trusts them inherits it. Both modes are measured, not assumed:

  * A report ledger that is **absent** makes the matcher answer confidently with
    nothing to join against, so no dispatch can be closed and every
    past-deadline row in history returns overdue at rc 0 with no warning. Five
    dispatches closed by five terminal reports come back as five overdue rows.
  * A ledger that is **unreadable** fails the opposite way and raises out of the
    matcher, which would take out a read-only command.

The realistic way to reach the first is #526, not a typo: the dispatch log is
host-global at ``state/dispatch-log.jsonl`` while report ledgers are per-fleet at
``local/<fleet>/runtime/report-back.jsonl``. Pair the host-wide log with one
fleet's ledger — the obvious invocation — and every *other* fleet's bots read as
permanently overdue against a file this brief never opens.

So both ledgers are probed before the matcher is called, and the section is
**omitted** when either is absent or unreadable: not zero, which is a false
all-clear, and not everything, which is a wall of finished work presented as
outstanding. The line is **presence, not emptiness** — an existing ledger with
no rows is a fleet that has not reported yet, and for it "every dispatch is
still open" is the true answer.

The same shape applies twice more. An unreadable ledger would render
``unacked (0)`` — an all-clear asserting no worker is waiting on a decision,
which is #949 and #1024 exactly, so that section omits too. And orphan
classification returns a clean empty set when it has no bots dir to read
``.spawn`` mtimes from (#1014's family), which is indistinguishable from "no
work was lost to a restart" — labeled, since open and overdue stay sound.

None of the above lives in the matcher — every probe and every omission is in
this module. Be precise about the neighbouring claim, though, because the two
are different and only one of them is true:

  * The matcher's **behaviour is unchanged**. `_classify_all`, `overdue_all`,
    `orphaned_all` and `_terminal_reported_ids` are byte-identical, and the
    `--all` / `--orphans` / `--open-task` contracts are what they were.
  * The **file is not untouched.** `lib/dispatch-overdue.py` is `+66/-14` here:
    a net-new `--open` mode (the one #904 specifies), and `open_task_id`
    refactored to be that list's head rather than a second loop over the same
    join — two copies is how a resolver ends up handing back an id the list
    does not contain. The 14 removed lines are that one function body.

The fail-open modes themselves are NOT fixed here — they are the doors' own,
shared with the watchdog, and #526, #1014 and #878 track them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .paths import Paths
from .workstreams import load_workstreams, registry_path

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


# --- ledger reading -----------------------------------------------------------


LEDGER_OK = "ok"
LEDGER_ABSENT = "absent"
LEDGER_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class LedgerRead:
    """A ledger's readability, its rows, and how many lines failed to parse.

    ``state`` is the load-bearing field, and the line it draws is
    **present-vs-not**, never empty-vs-not: a ledger that exists and holds zero
    rows is a legitimate state (a fleet that has not reported yet), and for
    that fleet "every dispatch is still open" is the TRUE answer. Only absence
    or an IO failure makes the same answer a fabrication.
    """

    state: str
    rows: list[dict]
    bad_lines: int


def _read_ledger(path: Path) -> LedgerRead:
    """Read a JSONL ledger, distinguishing absent from unreadable from empty.

    Two jobs, and they are separate on purpose.

    **Readability** is the defensive half. The shared matcher swallows
    ``FileNotFoundError`` and returns no rows, which makes a missing report
    ledger indistinguishable from one where nothing has been reported —
    measured on this branch: point it at a path that does not exist and five
    dispatches that were all closed by terminal reports come back as five
    overdue rows, rc 0, no warning. A ledger that exists but cannot be *read*
    fails the other way and raises out of the matcher entirely. Neither is
    something a read door may pass on, so both are surfaced here as state and
    the caller omits rather than answers.

    **Parse coverage** is the #911 half, and is deliberately NOT a fix for it:
    the shared readers still drop malformed rows silently and this changes
    nothing about that. It re-reads the same file to learn how many they
    dropped, so the brief can state its bound instead of printing a count that
    quietly under-reports.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return LedgerRead(LEDGER_ABSENT, [], 0)
    except OSError:
        return LedgerRead(LEDGER_UNREADABLE, [], 0)

    rows: list[dict] = []
    bad = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    return LedgerRead(LEDGER_OK, rows, bad)


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
    import importlib.util

    src = paths.lib / "dispatch-overdue.py"
    if not src.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("dispatch_overdue", src)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except (OSError, SyntaxError, ImportError):
        return None


def dispatch_ledger_path(paths: Paths) -> Path:
    """state/dispatch-log.jsonl — host-global, one file per CLAUDLOBBY_ROOT.

    Python twin of ``dispatch_ledger_path`` in lib-common.sh. The writer and
    both existing readers must agree byte-for-byte on this path; a third reader
    resolving a different file would report on dispatches nobody else can see.
    """
    return paths.root / "state" / "dispatch-log.jsonl"


def report_ledger_path(paths: Paths) -> Path:
    """report-back.jsonl — per-fleet; ``Paths.fleet_state`` owns that rule."""
    return paths.fleet_state / "report-back.jsonl"


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
    doors, paths: Paths, bot_id: str, now: int, degraded: list[Degradation]
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
    dlog, rlog = dispatch_ledger_path(paths), report_ledger_path(paths)

    # THE DEFENSIVE GATE. The #835 doors fail OPEN on a missing report ledger:
    # with nothing to join against, no dispatch can be closed, so every
    # past-deadline row in history comes back overdue at rc 0 with no warning.
    # That is not a degraded number, it is a wall of finished work presented as
    # outstanding — the exact defect this epic exists to end, arriving through
    # the door meant to fix it. So the section is OMITTED: not zero (a false
    # all-clear), not everything (a false alarm), and never silently either.
    #
    # The realistic way to hit it is #526, not a typo: the dispatch log is
    # host-global at state/dispatch-log.jsonl while report ledgers are per-fleet
    # at local/<fleet>/runtime/report-back.jsonl, so pairing the host-wide log
    # with one fleet's ledger makes every OTHER fleet's bots read as permanently
    # overdue. Reproduced live against this fleet.
    #
    # Existence, not emptiness, is the test — see LedgerRead. Checked here
    # rather than fixed in the matcher on purpose: the doors are shared with the
    # watchdog and are not this issue's to change.
    dispatch_read = _read_ledger(dlog)
    report_read = _read_ledger(rlog)

    for what, path, read, issue, consequence in (
        (
            "report ledger",
            rlog,
            report_read,
            "#526",
            "with no reports to join against, every past-deadline dispatch "
            "would read as overdue and every id'd dispatch as open",
        ),
        (
            "dispatch log",
            dlog,
            dispatch_read,
            "#1014",
            "with no dispatches to read, all three lists would be empty — a "
            "manufactured all-clear",
        ),
    ):
        if read.state == LEDGER_OK:
            continue

        # AN OMISSION SUPPRESSES TRUE ROWS TOO, and that is not optional: with
        # no ledger, a genuinely overdue dispatch and a finished one are the
        # same bytes, so nothing here can tell them apart. What IS avoidable is
        # letting the real one go unmentioned. Under-reporting is the worse
        # failure — a noisy watchdog gets audited, a silent one does not — so
        # the rows that could not be adjudicated are COUNTED even though they
        # cannot be classified. "12 dispatches past deadline, status
        # undeterminable" is actionable; a bare "unavailable" is not.
        #
        # The count deliberately claims nothing about overdue-ness. It is
        # None — stated, never rendered as 0 — when the dispatch log is the
        # unreadable side, because then even the denominator is unknown.
        unadjudicated = None
        if dispatch_read.state == LEDGER_OK:
            unadjudicated = sum(
                1
                for d in dispatch_read.rows
                if str(d.get("bot", "")).lower() == bot_id.lower()
                and isinstance(d.get("expected_by"), int)
                and now > d["expected_by"]
            )
        tail = (
            f"; {unadjudicated} dispatch row(s) for this bot are past deadline "
            "and could not be adjudicated either way"
            if unadjudicated
            else ""
        )
        degraded.append(
            Degradation(
                field="dispatches",
                mode="omitted",
                reason=(
                    f"the {what} is {read.state} at {path}; {consequence}, "
                    f"so no dispatch state is served rather than a wrong one{tail}"
                ),
                issue=issue,
                count=unadjudicated,
            )
        )
        return {}

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

    bots_dir = str(paths.runtime_bots) if paths.runtime_bots.is_dir() else None

    # Resolve the expiry cap the way the CLI does. The matcher's Python API
    # takes max_age as a defaulted argument and only its main() consults
    # DISPATCH_OVERDUE_MAX_AGE_S, so a fleet that tunes the cap would get a
    # brief disagreeing with the watchdog it is supposed to mirror — and
    # "byte-consistent with dispatch-overdue.py --all" is the contract. .env is
    # already loaded into the environment by the caller.
    max_age = getattr(
        doors, "_resolve_max_age", lambda: doors.DEFAULT_OVERDUE_MAX_AGE_S
    )()

    # This section wants BOTH sets, which is the case the matcher documents
    # _classify_all for: overdue_all and orphaned_all each re-parse both ledgers,
    # so calling the pair doubles the file reads. The underscore is scope, not
    # privacy — its own docstring names it "THE in-process door when a caller
    # wants both sets". Fall back to the public pair on an install that predates
    # it, since a slower answer beats no answer.
    classify = getattr(doors, "_classify_all", None)
    if classify is not None:
        over, orph = classify(str(dlog), str(rlog), now, max_age, bots_dir)
    else:
        over = doors.overdue_all(str(dlog), str(rlog), now, max_age, bots_dir)
        orph = doors.orphaned_all(str(dlog), str(rlog), now, max_age, bots_dir)
    overdue_rows = over.get(bot_id.lower(), [])
    orphan_rows = orph.get(bot_id.lower(), [])

    # The matcher is resolved from the INSTALL's lib/, not from wherever this
    # package was imported from — deliberately, since the door and the watchdog
    # must agree byte-for-byte. The two therefore version independently: a root
    # whose lib/ predates this issue has no open-list door at all. Degrade just
    # that list, loudly, instead of raising — overdue and orphaned are still
    # answerable, and an AttributeError here would take out a read-only command
    # on a fleet whose install is simply a few pulls behind.
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
                    f"no bots directory at {paths.runtime_bots}, so respawn "
                    "cannot be detected and the orphaned list is empty by "
                    "construction rather than by measurement"
                ),
                issue="#1014",
            )
        )

    open_door = getattr(doors, "open_dispatches", None)
    if open_door is None:
        degraded.append(
            Degradation(
                field="dispatches.open",
                mode="omitted",
                reason=(
                    f"the matcher installed at {paths.lib / 'dispatch-overdue.py'} "
                    "predates the open-list door, so still-open-but-not-yet-due "
                    "rows cannot be listed; overdue and orphaned are unaffected"
                ),
                issue="#904",
            )
        )
        open_rows = []
    else:
        open_rows = open_door(bot_id, str(dlog), str(rlog))

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

    Read-only: ``load_workstreams`` opens the registry and nothing here writes
    it back. ``stalled`` means no progress within the lease window; ``lease
    expired`` means the lease itself has run out. They are independent — a
    renewed workstream keeps its lease while its ``last_progress_ts`` stays put
    (``lib/workstream-update.sh:249`` is explicit that renew does not advance
    progress), which is exactly the state worth surfacing.
    """
    workstreams = load_workstreams(paths)

    # An empty result has two very different causes and load_workstreams cannot
    # distinguish them: a genuinely empty registry, or one whose JSON failed to
    # parse (it returns {} for both). Reporting "no workstreams" for a corrupt
    # file is the silent-drop failure this epic exists to close, so the corrupt
    # case is separated out here — checked only on the empty path, so the normal
    # path pays nothing.
    if not workstreams:
        reg = registry_path(paths)
        if reg.is_file():
            try:
                json.loads(reg.read_text())
            except (OSError, json.JSONDecodeError):
                degraded.append(
                    Degradation(
                        field="workstreams",
                        mode="omitted",
                        reason=(
                            f"the registry at {reg} exists but does not parse; "
                            "'no workstreams' would be indistinguishable from a "
                            "registry that failed to load"
                        ),
                        issue="#911",
                    )
                )
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
    ledger = report_ledger_path(paths)
    read = _read_ledger(ledger)

    # A ledger that cannot be read would render as "unacked (0)" — an all-clear
    # asserting that no worker is waiting on a decision. That is #949 and #1024
    # exactly, re-created by the surface built to close them, so it is omitted
    # instead. An existing-but-empty ledger is NOT this case and renders 0
    # honestly.
    if read.state != LEDGER_OK:
        degraded.append(
            Degradation(
                field="reports",
                mode="omitted",
                reason=(
                    f"the report ledger is {read.state} at {ledger}; "
                    "'0 unacked' would assert that no worker is waiting on a "
                    "decision, which is the incident class this section exists "
                    "to surface"
                ),
                issue="#526",
            )
        )
        return {}

    rows, bad = read.rows, read.bad_lines
    if bad:
        degraded.append(
            Degradation(
                field="reports",
                mode="labeled",
                reason=(
                    f"{bad} row(s) in {ledger.name} are not valid JSON and were "
                    "skipped by every reader, including this one — the unacked "
                    "list can under-report"
                ),
                issue="#911",
            )
        )

    unacked = [
        {
            "ts": r.get("ts"),
            "bot": r.get("bot"),
            "status": r.get("status"),
            "task_id": r.get("task_id"),
            "summary": r.get("summary"),
            "pr_url": r.get("pr_url"),
        }
        for r in rows
        if r.get("status") in terminal
        and isinstance(r.get("ts"), str)
        and (cursor is None or r["ts"] > cursor)
    ]
    unacked.sort(key=lambda r: r["ts"])
    return {"cursor": cursor, "unacked": unacked}


def _alerts_section(
    paths: Paths, bot_id: str, now: int, degraded: list[Degradation]
) -> list[dict]:
    """Critical events for the bot within the lookback window.

    Incomplete by construction until #903 lands — see the module docstring.
    The degradation is keyed on the SSOT symbol rather than a hardcoded flag,
    so it retires itself when the registry ships.
    """
    from .commands.events import collect_events

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

    if not paths.runtime_bots.is_dir():
        return []

    cutoff = (
        (datetime.fromtimestamp(now, timezone.utc) - timedelta(hours=ALERT_WINDOW_H))
        .isoformat()
        .replace("+00:00", "Z")
    )

    events = collect_events(
        paths.runtime_bots,
        bot=bot_id,
        critical_only=True,
        fleet_events_dir=paths.root / "state" / "events",
    )
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
    function of (ledgers, registry, clock) and every section is testable
    without freezing time globally.
    """
    bot = fleet.bots[bot_id]
    degraded: list[Degradation] = []

    doors = load_dispatch_doors(paths)
    terminal = set(
        getattr(doors, "_TERMINAL", None) or {"completed", "failed", "blocked"}
    )

    # The dispatch join reads BOTH ledgers, so a poisoned row in either can
    # leave a closed dispatch looking open. Measured on the dispatch log here;
    # the report ledger's own count is taken in _reports_section.
    dlog = dispatch_ledger_path(paths)
    bad_dispatch = _read_ledger(dlog).bad_lines
    if bad_dispatch:
        degraded.append(
            Degradation(
                field="dispatches",
                mode="labeled",
                reason=(
                    f"{bad_dispatch} row(s) in {dlog.name} are not valid JSON and "
                    "were skipped by the matcher — open/overdue counts can "
                    "under-report"
                ),
                issue="#911",
            )
        )

    brief = {
        "schema": SCHEMA_VERSION,
        "bot": bot_id,
        "fleet": fleet.name,
        "generated_at": _iso(now),
        "mission": _mission_section(fleet, bot, paths),
        "dispatches": _dispatch_section(doors, paths, bot_id, now, degraded),
        "workstreams": _workstream_section(fleet, paths, now, degraded),
        "reports": _reports_section(
            paths, read_cursor(paths, bot_id), terminal, degraded
        ),
        "alerts": _alerts_section(paths, bot_id, now, degraded),
    }

    # #526, the residence mismatch, as a standing bound whenever the section IS
    # served: the dispatch log is host-global while report ledgers are per-fleet,
    # and the join keys on bot name alone. A bot of ANOTHER fleet appears in this
    # log with its reports in a file this brief never opens, so its rows read as
    # permanently overdue — observed live at six false overdue rows for one bot.
    # Rows for THIS bot are sound, which is why the section is labeled rather
    # than omitted here; the omit path above covers the case where the whole
    # join has no ledger at all.
    if paths.fleet_dir is not None and brief["dispatches"]:
        degraded.append(
            Degradation(
                field="dispatches",
                mode="labeled",
                reason=(
                    "the dispatch log is host-global while report ledgers are "
                    "per-fleet and the join keys on bot name alone, so rows for a "
                    "bot of another fleet on this host — or a name reused across "
                    "fleets — cross-resolve against the wrong ledger"
                ),
                issue="#526",
            )
        )

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
        # Never render a count here: "unacked (0)" over an unreadable ledger is
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
        e
        for e in deg
        if e["field"] == section or e["field"].startswith(section + ".")
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
    so the boot mode computes it here from the same files the door already
    reads. When #1122 lands these facts move into the envelope and this helper
    is deleted.

    Same read discipline as the door: presence is distinguished from emptiness,
    and an absent source reports its state rather than a zero. The registry is
    read RAW here rather than via ``load_workstreams``, which flattens
    absent/corrupt/empty to ``{}`` — through it, a corrupt registry would
    render "0 entries", a false-quiet on exactly the property this helper
    exists to carry (#1122 owns the envelope-level fix).
    """

    def _row_epoch(v) -> int | None:
        # The raw ledger stores epoch seconds (the matcher's numeric
        # contract); ISO strings are tolerated so a future writer change
        # degrades to a parse rather than a silent zero.
        if isinstance(v, (int, float)):
            return int(v)
        return _epoch(v)

    ledger = _read_ledger(dispatch_ledger_path(paths))
    dl: dict = {"state": ledger.state}
    if ledger.state == LEDGER_OK:
        dl["rows_ever"] = len(ledger.rows)
        # A fixed 24h recency window, deliberately NOT the watchdog's
        # DISPATCH_OVERDUE_MAX_AGE_S mirror: the line self-describes as
        # "in 24h", a human-scale recency fact, not an open/overdue semantic.
        cutoff = now - 24 * 3600
        dl["rows_24h"] = sum(
            1
            for r in ledger.rows
            if (_row_epoch(r.get("dispatched_at") or r.get("ts")) or 0) >= cutoff
        )

    rp = registry_path(paths)
    reg: dict = {"present": rp.is_file()}
    if reg["present"]:
        try:
            raw = json.loads(rp.read_text())
            ws = raw.get("workstreams", []) if isinstance(raw, dict) else None
            reg["entries"] = len(ws) if isinstance(ws, list) else None
        except (OSError, json.JSONDecodeError):
            reg["entries"] = None
    return {"dispatch_ledger": dl, "registry": reg}


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
    silence, never a bare zero. "0 open (ledger: N rows ever)" and "no recorded
    fleet history" are different answers; the motivating incident was the gap
    between them.
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
            dl = prov.get("dispatch_ledger", {})
            if dl.get("state") == LEDGER_OK:
                led = (
                    f"ledger: {dl.get('rows_ever', 0)} rows ever, "
                    f"{dl.get('rows_24h', 0)} in 24h"
                )
            else:
                led = f"ledger: {dl.get('state', 'unknown')}"
            reg = prov.get("registry", {})
            if reg.get("present"):
                entries = reg.get("entries")
                reg_txt = (
                    f"registry: {entries} entr{'y' if entries == 1 else 'ies'}"
                    if entries is not None
                    else "registry: present (unreadable)"
                )
            else:
                reg_txt = "registry: absent on this fleet"
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
