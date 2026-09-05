#!/usr/bin/env python3
"""Find overdue dispatches — the matcher behind the fleet-pulse watchdog.

THE PLANE IS THE ONLY SOURCE (F18 closure, R2a). Every question this module
answers is asked of the host's plane db through the stdlib readers beside it
(`plane-readers.py`): the fleet's roster, its open assignments, the overdue
rules, the resolver's head, the idle-worker mirror. The dispatch log and the
report ledger this module was born reading no longer exist — no door writes
them (R1) — and this module no longer knows how to read a file at all.

A dispatch is OVERDUE when:
  - now > expected_by, AND
  - the assignment is not terminal (no completed | failed | blocked task
    event closed it, and no later dispatch retired it with --supersedes), AND
  - the worker has not reported progress inside the grace window
    (DISPATCH_PROGRESS_GRACE_S, default 45min — a reporting worker is alive,
    and a stuck one stops reporting), AND
  - it has not aged out: now - dispatched_at <= max_age. A dispatch that never
    receives a terminal report would otherwise stay overdue forever and make
    fleet-pulse re-emit an overdue_dispatch event every cycle without bound
    (issue #460). max_age defaults to 24h and is env-tunable via
    DISPATCH_OVERDUE_MAX_AGE_S; max_age <= 0 disables the cap.

Grammar — every mode reads the plane of ONE fleet, named by --fleet or the
carriers (CLAUDLOBBY_FLEET, the timer units' stamp; else FLEET_NAME, a
session's), under --root or CLAUDLOBBY_ROOT; --bots-dir <dir> may trail any
mode and enables the respawn (orphan) split:

  dispatch-overdue.py --all [<now_epoch>] [--fleet F] [--root R] [--bots-dir D]
      "<bot_id> <dispatched_at> <expected_by> <elapsed_seconds> <task_id>" per
      overdue row (task_id is "-" for an id-less dispatch).

  dispatch-overdue.py --orphans [<now_epoch>] --bots-dir <dir> [--fleet F] [--root R]
      Past-deadline rows whose worker RESPAWNED after dispatch (#835): the
      session that received the id is gone, so the row can never close — split
      out of --all so it stops alarming, and listable so it is not deleted.
      --bots-dir is REQUIRED and refused when missing or unreadable, at rc 3
      (#1014): orphan-ness is a comparison against <bots_dir>/<bot>/data/.spawn,
      so without one the answer is UNKNOWN, and an empty set at rc 0 would be
      "cannot look" byte-identical to "nothing was lost to a restart".

  dispatch-overdue.py --open <bot_id> [--fleet F] [--root R]
      Every still-open id'd assignment, OLDEST FIRST, deadline-blind (#904):
      "<dispatched_at> <expected_by> <task_id>" (expected_by "-" when none).
      A strict superset of --all's rows for the bot; --open-task is its head.
      States its scope on STDERR on every run (#1187), so an empty result
      names what it filtered on. Stdout stays rows-only for machine callers.

  dispatch-overdue.py --open-task <bot_id> [<now_epoch>] [--fleet F] [--root R]
      The id report-back.sh should echo when --task is omitted (#835): the
      OLDEST open id'd assignment, or nothing — nothing also while the bot's
      NEWEST assignment is id-less and unanswered (#1190: a terminal report
      then most plausibly answers that, and stamping an older id'd row would
      be a false completion, the one outcome worse than an open row).
      Silent (rc 0, no stdout) when nothing resolves; the plane path discloses
      its answer on stderr.

  dispatch-overdue.py --unassigned [<now_epoch>] [--fleet F] [--root R]
      The MIRROR of overdue (#1024): workers whose newest report is terminal
      and were never re-tasked. Purely temporal — newest dispatch instant vs
      newest report instant; it never asks whether a dispatch is open.
      "<bot_id> <reported_at> <idle_seconds> <task_id> <status>" per worker.

WHICH MODES HAVE A BOT SLOT — the grammar trap behind #1187: --open and
--open-task take ONE bot FIRST; --all, --orphans and --unassigned name none.
The bot slot refuses what a bot id can never be (a path, a `.jsonl` name, an
empty string) at rc 2, lexically and never by roster — a plausible but wrong
name still answers zero rows, which is why --open states its scope.

UNREACHABLE IS NOT EMPTY. A plane that cannot be opened, holds no schema, or
holds no bot of the named fleet (a wrong root, or a fleet it has never seen)
REFUSES at rc 3 with empty stdout — never "nothing open", never a file. rc 2
is a malformed call. Callers that parse stdout (fleet-pulse's caches,
report-back's resolver) read the rc; the refusal rides stderr.

Kept as a standalone, stdlib-only script so it is unit-testable in isolation
and callable from fleet-pulse.sh without importing the package.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import time

_TERMINAL = {"completed", "failed", "blocked"}

# A never-closing dispatch stops counting as overdue past this age (seconds from
# dispatched_at), bounding the watchdog's re-emission. 24h default; override with
# DISPATCH_OVERDUE_MAX_AGE_S; <= 0 disables the cap.
DEFAULT_OVERDUE_MAX_AGE_S = 86400


# A dispatch whose worker reported progress within this window is treated as ALIVE,
# not overdue. Measured, not chosen round (2026-08-04, 33 progress reports over 7 days
# across 5 bots): the gap from a progress report to that bot's next report of any kind
# clusters at <=30min (29 of 33; median 9, p75 22) and then jumps straight to 60, 76,
# 357, 616 — an empty band between 30 and 60 separating "still working" from "stopped".
# 45min sits in that band: 1.5x margin over the worst observed in-work cadence, while
# still alarming 15min before the shortest gap that looked like a stall.
#
# WHY THIS DOES NOT HIDE A DEAD WORKER, which is the property that matters more than
# the number: a stuck or crashed session emits nothing, so its last progress report
# ages out of this window and the dispatch alarms exactly as it does today, just later
# by at most the grace. Only a worker actively reporting can defer, and it can only
# defer as long as it keeps reporting.
DEFAULT_PROGRESS_GRACE_S = 2700


def _resolve_progress_grace() -> int:
    """Read the progress grace window from env, falling back to the default.

    Same TypeError/ValueError funnel as _resolve_max_age: an unset var and a malformed
    one take the same path. <= 0 disables progress-based deferral entirely.
    """
    try:
        return int(os.environ.get("DISPATCH_PROGRESS_GRACE_S"))
    except (TypeError, ValueError):
        return DEFAULT_PROGRESS_GRACE_S


def _resolve_max_age() -> int:
    """Read the expiry cap from env, falling back to the default.

    int(None) raises TypeError, so an unset var funnels through the same guard as
    a malformed one.
    """
    try:
        return int(os.environ.get("DISPATCH_OVERDUE_MAX_AGE_S"))
    except (TypeError, ValueError):
        return DEFAULT_OVERDUE_MAX_AGE_S


# --- the plane: the one source -------------------------------------------------

class PlaneUnreachable(RuntimeError):
    """The plane must serve and cannot — unreachable, not empty (rc 3)."""


def _plane_readers():
    """Import the stdlib plane reader beside this file (never the package)."""
    import importlib.util
    src = os.path.join(os.path.dirname(os.path.realpath(__file__)), "plane-readers.py")
    spec = importlib.util.spec_from_file_location("plane_readers", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plane_context(fleet: str | None, root: str | None) -> tuple[str, str]:
    """(fleet, root) for a plane read, or a loud refusal — the plane's rows are
    per fleet alias, so a read without a fleet cannot be answered. The fleet:
    --fleet, else the timer units' CLAUDLOBBY_FLEET, else a session's
    FLEET_NAME; the root: --root, else CLAUDLOBBY_ROOT."""
    fleet = fleet or os.environ.get("CLAUDLOBBY_FLEET") or os.environ.get("FLEET_NAME") or ""
    root = root or os.environ.get("CLAUDLOBBY_ROOT") or ""
    if not fleet or not root:
        raise PlaneUnreachable("the plane needs a fleet (--fleet / CLAUDLOBBY_FLEET /"
                               " FLEET_NAME) and a root (--root / CLAUDLOBBY_ROOT)")
    return fleet, root


class _Plane:
    """One read-only plane session: connection + roster. Every plane failure
    is PlaneUnreachable; a schema-valid plane that holds no bot of the fleet
    is refused too — an answer of "nothing open" from a plane that never saw
    the fleet would be absence read as clean (#1014's class)."""

    def __init__(self, fleet: str | None, root: str | None):
        self.fleet, self.root = _plane_context(fleet, root)
        self.pr = _plane_readers()
        try:
            self.conn = self.pr.connect(self.root)
            self.roster = self.pr.roster(self.conn, self.fleet)
        except (self.pr.PlaneUnreachable, sqlite3.Error, OSError) as exc:
            raise PlaneUnreachable(str(exc)) from exc
        if not self.roster:
            self.conn.close()
            raise PlaneUnreachable(f"the plane at {self.root} holds no bot of fleet"
                                   f" {self.fleet!r} — wrong root, or a fleet it has never seen")

    def close(self) -> None:
        self.conn.close()


def open_plane(fleet: str | None = None, root: str | None = None) -> _Plane:
    """The plane session every provider below reads from. Callers that ask
    several questions in one breath (brief, fleet-pulse's pre-sweep) open ONE
    and pass it as ``plane=``; each provider closes what it opened itself."""
    return _Plane(fleet, root)


def _plane_bot(p: _Plane, bot: str, what: str) -> dict | None:
    """The bot's registry entry in an open plane session, or None after the
    disclosure: a bot the plane does not know has nothing by the plane's
    account (if it is declared, its dispatches never reached the plane)."""
    entry = p.roster.get(bot.lower())
    if entry is None:
        print(f"dispatch-overdue: no identity row for bot:{p.fleet}/{bot} in the plane —"
              f" nothing {what} by the plane's account", file=sys.stderr)
    return entry


def _spawn_epoch(bots_dir: str, bot: str) -> int | None:
    """Mtime of <bots_dir>/<bot>/data/.spawn, or None when unreadable.

    start-bot.sh touches that marker on EVERY start, so it dates the current
    incarnation of the session (the same marker bridge_down_state graces from).
    """
    try:
        return int(os.path.getmtime(os.path.join(bots_dir, bot, "data", ".spawn")))
    except OSError:
        return None


def _session(plane: _Plane | None, fleet: str | None, root: str | None) -> tuple[_Plane, bool]:
    """The session to read from and whether THIS call owns (and must close) it."""
    if plane is not None:
        return plane, False
    return open_plane(fleet, root), True


# --- the providers ---------------------------------------------------------------

def open_dispatches(
    bot: str,
    *,
    fleet: str | None = None,
    root: str | None = None,
    plane: _Plane | None = None,
) -> list[tuple[int, int | None, str]]:
    """The bot's still-open id'd assignments, OLDEST FIRST.

    Each entry is (dispatched_at, expected_by, task_id). ``expected_by`` is
    None when the assignment carries none — deliberately NOT a filter, so this
    set stays a strict superset of what ``open_task_id`` considers: a row that
    can supply the resolver's id must also be listable here.

    OPEN is deadline-blind, and that is the whole point of this door: an
    assignment is open until it is CLOSED (a terminal task event) or RETIRED
    (a later dispatch's --supersedes), whether or not ``now`` has passed
    ``expected_by`` — strictly wider than the watchdog's OVERDUE, so "carrying
    three tasks, none late yet" is readable. Deadline-blind is NOT
    supersede-blind (#1357): a retired assignment is gone from both doors.
    The SQL is the plane's own definition of open (`queries.OPEN_ASSIGNMENTS_AT_SQL`,
    pinned byte-identical in `plane-readers.py`), so this door and the
    resolver can never disagree about what open means.
    """
    p, own = _session(plane, fleet, root)
    try:
        entry = _plane_bot(p, bot, "open")
        return p.pr.open_rows(p.conn, p.fleet, bot, entry=entry) if entry else []
    except sqlite3.Error as exc:
        raise PlaneUnreachable(f"plane db unreadable: {exc}") from exc
    finally:
        if own:
            p.close()


def _classify_all(
    now: int,
    max_age: int = DEFAULT_OVERDUE_MAX_AGE_S,
    bots_dir: str | None = None,
    *,
    fleet: str | None = None,
    root: str | None = None,
    plane: _Plane | None = None,
) -> tuple[
    dict[str, list[tuple[int, int, int, str]]],
    dict[str, list[tuple[int, int, int, str]]],
]:
    """Shared core: (overdue, orphaned) for EVERY bot the plane knows of the
    fleet — one roster scan, the overdue rules as the plane reader applies
    them (deadline passed, not terminal, the progress grace, the expiry cap),
    then the SAME orphan split the watchdog has always applied: a dispatch
    older than the bot's `.spawn` is the orphan list's, never paged as overdue
    (#835). THE in-process door when a caller wants both sets; the contracts
    live on overdue_all / orphaned_all.
    """
    grace = _resolve_progress_grace()
    spawn_cache: dict[str, int | None] = {}
    p, own = _session(plane, fleet, root)
    try:
        out: dict[str, list[tuple[int, int, int, str]]] = {}
        orphans: dict[str, list[tuple[int, int, int, str]]] = {}
        for bot, entry in sorted(p.roster.items()):
            for da, exp, elapsed, tid in p.pr.overdue_rows(
                    p.conn, p.fleet, bot, now=now, max_age=max_age, progress_grace=grace, entry=entry):
                row = (da, exp, elapsed, tid if tid else "-")
                # Orphan split (#835). Only id'd rows can orphan: an id-less
                # dispatch closes on ANY later terminal report, so a respawned
                # worker's next report still retires it.
                if tid and bots_dir:
                    if bot not in spawn_cache:
                        spawn_cache[bot] = _spawn_epoch(bots_dir, bot)
                    spawn = spawn_cache[bot]
                    if spawn is not None and spawn > da:
                        orphans.setdefault(bot, []).append(row)
                        continue
                out.setdefault(bot, []).append(row)
        return out, orphans
    except sqlite3.Error as exc:
        raise PlaneUnreachable(f"plane db unreadable: {exc}") from exc
    finally:
        if own:
            p.close()


def overdue_all(
    now: int,
    max_age: int = DEFAULT_OVERDUE_MAX_AGE_S,
    bots_dir: str | None = None,
    *,
    fleet: str | None = None,
    root: str | None = None,
    plane: _Plane | None = None,
) -> dict[str, list[tuple[int, int, int, str]]]:
    """Overdue dispatches for ALL bots of the fleet, one plane read.

    Each entry is (dispatched_at, expected_by, elapsed_past_deadline, task_id)
    — task_id is "-" for an id-less dispatch, so shell consumers can always
    read a stable 4th field.

    Join matrix (goal-aware plan P4): an id'd dispatch is closed ONLY by a
    terminal task event on its own assignment — an id-less terminal report
    never closes it (blanket-closing was exactly the #447 bug class). An
    id-less dispatch closes on the bot's next terminal report of any kind
    (the report door lands that event on every open id-less assignment).

    With <bots_dir>, rows whose worker respawned after dispatch are NOT
    returned here — see orphaned_all.
    """
    return _classify_all(now, max_age, bots_dir, fleet=fleet, root=root, plane=plane)[0]


def orphaned_all(
    now: int,
    max_age: int = DEFAULT_OVERDUE_MAX_AGE_S,
    bots_dir: str | None = None,
    *,
    fleet: str | None = None,
    root: str | None = None,
    plane: _Plane | None = None,
) -> dict[str, list[tuple[int, int, int, str]]]:
    """Past-deadline dispatches whose worker RESPAWNED after they were sent.

    The session that received the id is gone, so the worker cannot echo what it
    can no longer see: the row could never close, and the watchdog would flag it
    every cycle until max_age. The predicate is respawn, NOT session-absence — a
    restarted bot keeps its session NAME, so an existence check reads a fresh
    incarnation as the original one. Only id'd rows can orphan.

    Split out of overdue_all rather than deleted: an aged-out row (#460) is an
    abandoned task, but an orphan is work the fleet lost to its own restart,
    which is actionable. Same row shape as overdue_all.

    Empty without <bots_dir> — respawn cannot be determined without the marker,
    and the safe default is to keep reporting a row overdue rather than silently
    retiring one that might still be live. (The CLI mode refuses instead: #1014.)
    """
    return _classify_all(now, max_age, bots_dir, fleet=fleet, root=root, plane=plane)[1]


def unassigned_all(
    now: int,
    idle_threshold: int = 0,
    *,
    fleet: str | None = None,
    root: str | None = None,
    plane: _Plane | None = None,
) -> dict[str, tuple[int, int, str, str]]:
    """Workers that reported terminal and were never re-tasked — the #1024 mirror.

    Returns {bot: (reported_at, idle_seconds, task_id, status)}.

    overdue_all answers "work was sent and never came back". This answers the
    mirror: work came back and nothing was sent. THE PREDICATE IS PURELY
    TEMPORAL — the newest dispatch instant against the newest report instant;
    it never asks whether any dispatch is OPEN, because a manager amending a
    task re-dispatches repeatedly and every replaced row stays open forever
    (verified against a real six-dispatch chain: five stale ids open the whole
    time while the worker was demonstrably working). A bot whose newest report
    is `progress` is NOT returned: it is working, or stalled mid-task, and the
    stall is overdue_all's to report.

    Threshold filtering defaults OFF (0 = report every match with its idle
    time); fleet-pulse applies the threshold and the staleness cap PER BOT.
    """
    p, own = _session(plane, fleet, root)
    try:
        return p.pr.unassigned_rows(p.conn, p.fleet, now=now, idle_threshold=idle_threshold)
    except sqlite3.Error as exc:
        raise PlaneUnreachable(f"plane db unreadable: {exc}") from exc
    finally:
        if own:
            p.close()


def open_task_id(
    bot: str,
    *,
    fleet: str | None = None,
    root: str | None = None,
    now: int | None = None,
    plane: _Plane | None = None,
) -> str | None:
    """The bot's OLDEST still-open id'd dispatch, or None.

    What report-back.sh resolves when the worker omits --task, so the common
    path closes its dispatch by default instead of by discipline. OLDEST, not
    newest: a worker is a serial session draining a queued buffer in FIFO
    order, so the dispatch it just finished is the oldest one still open; and
    the oldest is the one actually past its deadline and alarming. FIFO also
    makes a wrong guess self-correcting — N reports for N queued tasks retire
    them in the order they were sent.

    Deliberately NOT a loosening of the join: the plane reader's ``head`` is
    the head of the same open list ``open_dispatches`` returns, so the
    resolver can never hand back an id the list does not contain.

    SUPPRESSED (None) while the bot's NEWEST assignment is id-less and
    unanswered (#1190): the most recent thing asked of the bot carried no id,
    so a terminal report now most plausibly answers that, and stamping an
    older id'd row would be a false completion — the one outcome worse than
    an open row. The cost is one-directional and deliberate: a report that WAS
    the missing echo leaves its row open until the next report (UNTRACKED,
    the degradation direction #1187 chose; the watchdog still surfaces it).
    ``now`` is accepted for callers that pass one; the plane reader answers
    for the present instant.
    """
    del now  # the plane reader answers at its own instant; kept for the callers' grammar
    p, own = _session(plane, fleet, root)
    try:
        entry = _plane_bot(p, bot, "to resolve")
        return p.pr.head(p.conn, p.fleet, bot, entry=entry) if entry else None
    except sqlite3.Error as exc:
        raise PlaneUnreachable(f"plane db unreadable: {exc}") from exc
    finally:
        if own:
            p.close()


# --- the CLI --------------------------------------------------------------------

def _take_bots_dir(argv: list[str]) -> tuple[list[str], str | None]:
    """Strip a trailing `--bots-dir <dir>` from argv. Trailing-only by design:
    a valueless flag anywhere else would survive the strip and then be read
    as a positional."""
    if len(argv) >= 2 and argv[-2] == "--bots-dir":
        return argv[:-2], argv[-1]
    return argv, None


def _take_plane_opts(argv: list[str]) -> tuple[list[str], str | None, str | None]:
    """Strip `--fleet <name>` and `--root <dir>` from argv, wherever they sit."""
    out: list[str] = []
    fleet = root = None
    i = 0
    while i < len(argv):
        if argv[i] in ("--fleet", "--root"):
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise SystemExit(f"dispatch-overdue: {argv[i]} needs a value")
            if argv[i] == "--fleet":
                fleet = argv[i + 1]
            else:
                root = argv[i + 1]
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out, fleet, root


def _not_a_bot_id(value: str) -> str | None:
    """Why this first positional cannot be a bot id — or None if it might be.

    Lexical only, and deliberately NOT a roster lookup: rejects only what a
    bot id can NEVER be (a bot id names a directory under runtime/bots/ and a
    tmux session, so it holds no "/" and does not end ".jsonl"), so it cannot
    refuse a real one. A plausible but wrong name — a typo, or a live name of
    another fleet — still answers zero rows, which is why --open states its
    scope on stderr (#1187).
    """
    if not value.strip():
        return "an empty bot id"
    if "/" in value:
        return f"a path: {value!r}"
    if value.endswith(".jsonl"):
        return f"a ledger file: {value!r}"
    return None


def _reject_bot_slot(mode: str, value: str) -> bool:
    """Print the #1187 shape refusal for `mode`, or return False to proceed."""
    why = _not_a_bot_id(value)
    if why is None:
        return False
    print(
        f"dispatch-overdue.py: {mode} expects <bot_id> first, got {why}\n"
        f"  usage: dispatch-overdue.py {mode} <bot_id> [--fleet F] [--root R]\n"
        "  note:  --all/--orphans/--unassigned name no bot at all.",
        file=sys.stderr,
    )
    return True


def _refuse_undeterminable_orphans(bots_dir: str | None) -> bool:
    """True (and says why) when `--orphans` cannot determine orphan-ness at all.

    Orphan-ness is decided by comparing a dispatch against
    `<bots_dir>/<bot>/data/.spawn`, so with no readable bots dir there is
    nothing to compare and the answer is UNKNOWN — printing an empty set at
    rc 0 made "cannot look" byte-identical to "no work was lost to a restart"
    (#1014; measured: three such states returned 0 bytes at rc 0, and a fourth
    — a dir that stats but cannot be listed — was found in review, #1227).
    `orphaned_all` itself keeps returning {} without a bots dir (brief calls
    it directly and labels the gap its own way); the refusal lives in the CLI
    mode, on STDERR because this mode's stdout is parsed (fleet-pulse reads it
    into an orphan cache). rc 3, not 2: the flag is optional in the grammar,
    so this is not a malformed call — it is a question this run cannot answer.
    """
    if bots_dir is None:
        print(
            "dispatch-overdue.py: --orphans cannot determine orphans without "
            "--bots-dir <dir>\n"
            "  orphan-ness is a comparison against <bots_dir>/<bot>/data/.spawn; "
            "with no bots dir there is nothing to compare, so the answer is "
            "UNKNOWN, not 'none'.\n"
            "  usage: dispatch-overdue.py --orphans [<now>] --bots-dir <dir>",
            file=sys.stderr,
        )
        return True
    if not os.path.isdir(bots_dir):
        print(
            f"dispatch-overdue.py: --orphans cannot read the bots dir: "
            f"{bots_dir!r} is not a directory\n"
            "  every row would classify as not-an-orphan for want of a .spawn "
            "marker, which is indistinguishable from a fleet that lost nothing.",
            file=sys.stderr,
        )
        return True
    try:
        os.listdir(bots_dir)
    except OSError as exc:
        print(
            f"dispatch-overdue.py: --orphans cannot read the bots dir: "
            f"{bots_dir!r} exists but cannot be listed ({exc.strerror})\n"
            "  every row would classify as not-an-orphan for want of a .spawn "
            "marker, which is indistinguishable from a fleet that lost nothing.",
            file=sys.stderr,
        )
        return True
    return False


_USAGE = __doc__.strip().splitlines()[0]
_MODES = ("--all", "--orphans", "--open", "--open-task", "--unassigned")


def _now_arg(argv: list[str], i: int) -> int | None:
    """argv[i] as an epoch, the present instant when absent, or None (and the
    usage) when it is not an integer."""
    if len(argv) <= i:
        return int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    try:
        return int(argv[i])
    except ValueError:
        print(f"dispatch-overdue: <now_epoch> must be an integer, not {argv[i]!r}", file=sys.stderr)
        return None


def main() -> int:
    try:
        argv, fleet_opt, root_opt = _take_plane_opts(sys.argv)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    argv, bots_dir = _take_bots_dir(argv)
    if len(argv) < 2 or argv[1] not in _MODES:
        print(_USAGE, file=sys.stderr)
        print(f"  modes: {' '.join(_MODES)}", file=sys.stderr)
        return 2
    mode = argv[1]
    max_age = _resolve_max_age()

    def _trailing() -> bool:
        # Trailing arguments a mode does not take are a usage error, never
        # ignored: a stale caller passing the retired `--source jsonl` or the
        # two ledger paths must hear it (found by the cutover suites' port).
        # Asked AFTER the bot-slot shape gate, so a path in the bot slot keeps
        # its own, more specific refusal (#1187).
        _max = {"--open": 3, "--open-task": 4, "--all": 3, "--orphans": 3, "--unassigned": 3}[mode]
        if len(argv) > _max:
            print(f"dispatch-overdue: {mode} takes no {argv[_max]!r} — usage: {_USAGE}", file=sys.stderr)
            return True
        return False

    if mode in ("--open", "--open-task"):
        if len(argv) < 3:
            print(_USAGE, file=sys.stderr)
            return 2
        bot = argv[2]
        if _reject_bot_slot(mode, bot):
            return 2
        if _trailing():
            return 2
        try:
            if mode == "--open":
                rows = open_dispatches(bot, fleet=fleet_opt, root=root_opt)
                for da, exp, tid in rows:
                    print(f"{da} {exp if exp is not None else '-'} {tid}")
                # The scope, ALWAYS and on STDERR (#1187): report-back.sh pipes
                # this stdout through `awk {print $3}`, so a prose line there
                # becomes a phantom open row. An empty result that names what
                # it filtered on can never be read as "nothing exists".
                print(f"--open: bot={bot!r} -> {len(rows)} open id'd dispatch(es) [source=plane]",
                      file=sys.stderr)
                return 0
            now = _now_arg(argv, 3)
            if now is None:
                return 2
            tid = open_task_id(bot, fleet=fleet_opt, root=root_opt, now=now)
            if tid:
                print(tid)
            print(f"dispatch-overdue: --open-task: bot={bot!r} -> {tid or '-'} [source=plane]",
                  file=sys.stderr)
            return 0
        except PlaneUnreachable as exc:
            print(f"dispatch-overdue: {mode}: the plane is UNREACHABLE — {exc}", file=sys.stderr)
            return 3

    if _trailing():
        return 2
    now = _now_arg(argv, 2)
    if now is None:
        return 2
    if mode == "--orphans" and _refuse_undeterminable_orphans(bots_dir):
        return 3
    try:
        if mode == "--unassigned":
            rows = unassigned_all(now, fleet=fleet_opt, root=root_opt)
            for bot_id, (rts, idle, tid, status) in sorted(rows.items()):
                print(f"{bot_id} {rts} {idle} {tid} {status}")
            return 0
        over, orph = _classify_all(now, max_age, bots_dir, fleet=fleet_opt, root=root_opt)
    except PlaneUnreachable as exc:
        print(f"dispatch-overdue: {mode}: the plane is UNREACHABLE — {exc}", file=sys.stderr)
        return 3
    rows = over if mode == "--all" else orph
    for bot_id, entries in sorted(rows.items()):
        for da, exp, elapsed, tid in entries:
            print(f"{bot_id} {da} {exp} {elapsed} {tid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
