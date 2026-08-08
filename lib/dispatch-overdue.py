#!/usr/bin/env python3
"""Find overdue dispatches — the matcher behind the fleet-pulse watchdog.

A dispatch (from state/dispatch-log.jsonl) is OVERDUE when:
  - now > expected_by, AND
  - no terminal report (status in completed|failed|blocked) for the same bot
    (case-insensitive) with report.ts >= dispatch.dispatched_at exists in the
    report-back ledger, AND
  - it has not aged out: now - dispatched_at <= max_age. A dispatch that never
    receives a terminal report would otherwise stay overdue forever and make
    fleet-pulse re-emit an overdue_dispatch event every cycle without bound
    (issue #460). Past max_age the watchdog gives up on the abandoned task and
    the entry goes inert (still in the ledger, like any closed dispatch).
    max_age defaults to 24h and is env-tunable via DISPATCH_OVERDUE_MAX_AGE_S;
    max_age <= 0 disables the cap (unbounded, legacy behavior).

Single-bot mode (original):
  dispatch-overdue.py <bot_id> <dispatch_log> <report_ledger> [<now_epoch>]
  Prints: "<dispatched_at> <expected_by> <elapsed_seconds>"

All-bots mode (fleet-pulse optimization -- reads files once):
  dispatch-overdue.py --all <dispatch_log> <report_ledger> [<now_epoch>]
  Prints: "<bot_id> <dispatched_at> <expected_by> <elapsed_seconds> <task_id>"

Orphan mode (#835) -- past-deadline rows whose worker RESPAWNED after dispatch,
split out of --all so they stop alarming, and listable so they are not simply
deleted:
  dispatch-overdue.py --orphans <dispatch_log> <report_ledger> [<now_epoch>] --bots-dir <dir>

Open-task mode (#835) -- the id report-back.sh should echo when --task is
omitted, so the common path closes its dispatch by default:
  dispatch-overdue.py --open-task <bot_id> <dispatch_log> <report_ledger>
  Prints one task id, or nothing when the bot has none open.

Open-list mode (#904) -- every still-open id'd row, not just the one the
resolver would pick, so "open but not yet due" is readable by the read door:
  dispatch-overdue.py --open <bot_id> <dispatch_log> <report_ledger>
  Prints: "<dispatched_at> <expected_by> <task_id>" per row, oldest first
  (expected_by is "-" when the row carries none). Deadline-blind, so this is a
  strict superset of --all's rows for the same bot; --open-task is its head.

`--bots-dir <dir>` goes last and enables respawn detection; without it no row is
ever classified as an orphan. It is also the one input that is NOT one of the two
ledgers: orphan classification reads `.spawn` mtimes, so that mode alone depends
on ambient filesystem state. Everything else stays a pure function of
(dispatch log, report ledger, clock).

No output (and exit 0) when nothing is overdue.

Kept as a standalone, stdlib-only script so it is unit-testable in isolation and
callable from fleet-pulse.sh.
"""

from __future__ import annotations

import datetime
import json
import os
import sys

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
    a malformed one (mirrors _iso_to_epoch's except below).
    """
    try:
        return int(os.environ.get("DISPATCH_OVERDUE_MAX_AGE_S"))
    except (TypeError, ValueError):
        return DEFAULT_OVERDUE_MAX_AGE_S


def _iso_to_epoch(ts: str) -> int | None:
    try:
        # Use fromisoformat (handles offset/fractional‐seconds) — matches the
        # pattern in uptime.py/status.py rather than a strict strptime format.
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


def _load_jsonl(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return rows


def overdue(
    bot: str,
    dispatch_log: str,
    report_ledger: str,
    now: int,
    max_age: int = DEFAULT_OVERDUE_MAX_AGE_S,
    bots_dir: str | None = None,
) -> list[tuple[int, int, int, str]]:
    """Single-bot variant — delegates to overdue_all and filters."""
    return overdue_all(dispatch_log, report_ledger, now, max_age, bots_dir).get(
        bot.lower(), []
    )


def _terminal_reported_ids(reports: list[dict]) -> set[tuple[str, str]]:
    """(bot, task_id) pairs closed by a terminal report.

    THE definition of "this dispatch is closed", shared by the watchdog join and
    by open_task_id. Two copies would let the resolver hand back an id the
    watchdog still considers open — the same desync class this module exists to
    catch. Scoped by bot: a peer echoing (or mishearing) another bot's id must
    not close the real owner's dispatch (#518 review).
    """
    return {
        (str(r.get("bot", "")).lower(), str(r.get("task_id")))
        for r in reports
        if r.get("status") in _TERMINAL and r.get("task_id")
    }


def _spawn_epoch(bots_dir: str, bot: str) -> int | None:
    """Mtime of <bots_dir>/<bot>/data/.spawn, or None when unreadable.

    start-bot.sh touches that marker on EVERY start, so it dates the current
    incarnation of the session (the same marker bridge_down_state graces from).
    """
    try:
        return int(os.path.getmtime(os.path.join(bots_dir, bot, "data", ".spawn")))
    except OSError:
        return None


def _classify_all(
    dispatch_log: str,
    report_ledger: str,
    now: int,
    max_age: int = DEFAULT_OVERDUE_MAX_AGE_S,
    bots_dir: str | None = None,
) -> tuple[
    dict[str, list[tuple[int, int, int, str]]],
    dict[str, list[tuple[int, int, int, str]]],
]:
    """Shared core: return (overdue, orphaned) for ALL bots, files read once.

    THE in-process door when a caller wants both sets — overdue_all and
    orphaned_all each re-parse both ledgers, so calling them in pairs doubles
    the work. Contracts live on those two; this is the implementation.
    """
    dispatches = _load_jsonl(dispatch_log)
    reports = _load_jsonl(report_ledger)

    # Per-bot terminal-report epochs (legacy join) + terminal (bot, id) set.
    # The id join is scoped by bot exactly like the legacy join: a peer
    # echoing (or mishearing) another bot's task id must not silence the
    # watchdog on the real owner's still-open dispatch (#518 review).
    report_index: dict[str, list[int]] = {}
    reported_ids = _terminal_reported_ids(reports)
    # Latest progress report per bot. A progress report closes NOTHING — it is not
    # terminal and never will be — but it is proof the worker is alive, which is the
    # question the watchdog is actually asking. See _progress_grace below.
    last_progress: dict[str, int] = {}
    for r in reports:
        status = r.get("status")
        ep = _iso_to_epoch(r.get("ts", ""))
        if ep is None:
            continue
        bot_l = str(r.get("bot", "")).lower()
        if status == "progress":
            if ep > last_progress.get(bot_l, 0):
                last_progress[bot_l] = ep
            continue
        if status not in _TERMINAL:
            continue
        report_index.setdefault(bot_l, []).append(ep)

    out: dict[str, list[tuple[int, int, int, str]]] = {}
    orphans: dict[str, list[tuple[int, int, int, str]]] = {}
    # One stat per BOT, not per row: a bot with many open rows is the common
    # shape, and every row of a given bot reads the same marker. Populated
    # lazily, so a sweep where nothing is overdue stats nothing. Not
    # lru_cache — the memo must not outlive the call, since .spawn moves
    # between sweeps.
    spawn_cache: dict[str, int | None] = {}
    progress_grace = _resolve_progress_grace()
    for d in dispatches:
        bot_key = str(d.get("bot", "")).lower()
        exp, da = d.get("expected_by"), d.get("dispatched_at")
        if not isinstance(exp, int) or not isinstance(da, int):
            continue
        if now <= exp:
            continue
        tid = d.get("task_id")
        if tid:
            if (bot_key, str(tid)) in reported_ids:
                continue
        elif any(e >= da for e in report_index.get(bot_key, [])):
            continue
        # Liveness gate: the worker reported progress recently, so it is working, not
        # stuck. Deferral is bounded by the grace window and by the worker's own
        # silence — stop reporting and this stops suppressing. Checked AFTER the
        # report gate so a closed dispatch still reads as closed rather than as
        # deferred, and BEFORE the expiry cap so a deferred row keeps its real age.
        #
        # Scoped per BOT rather than per dispatch because progress reports carry no
        # task id (report-back.sh only resolves one for terminal statuses — resolving
        # a progress report would stamp an id no consumer reads). A bot working its
        # queue is alive for every row it holds, which is the honest reading: the
        # claim being made is "this session is not stuck", not "this task advanced".
        if progress_grace > 0:
            lp = last_progress.get(bot_key)
            # da < lp <= now. The upper bound is load-bearing: a future-dated report
            # (clock skew, a hand-edited ledger) would otherwise give a NEGATIVE age
            # that satisfies any grace and suppress the alarm permanently — a silent
            # mute, which is the one outcome this change must never produce.
            if lp is not None and da < lp <= now and (now - lp) <= progress_grace:
                continue

        # Expiry cap (#460): once a still-open dispatch is older than max_age, the
        # watchdog stops flagging it so fleet-pulse quits re-emitting every cycle.
        # Checked after the report gate so a closed dispatch reads as closed, not
        # expired. max_age <= 0 disables the cap.
        if max_age > 0 and (now - da) > max_age:
            continue
        row = (da, exp, now - exp, str(tid) if tid else "-")
        # Orphan split (#835). Only id'd rows can orphan: an id-less dispatch
        # closes on ANY later terminal report, so a respawned worker's next
        # report still retires it — there is nothing it must remember.
        if tid and bots_dir:
            if bot_key not in spawn_cache:
                spawn_cache[bot_key] = _spawn_epoch(bots_dir, bot_key)
            spawn = spawn_cache[bot_key]
            if spawn is not None and spawn > da:
                orphans.setdefault(bot_key, []).append(row)
                continue
        out.setdefault(bot_key, []).append(row)
    return out, orphans


def overdue_all(
    dispatch_log: str,
    report_ledger: str,
    now: int,
    max_age: int = DEFAULT_OVERDUE_MAX_AGE_S,
    bots_dir: str | None = None,
) -> dict[str, list[tuple[int, int, int, str]]]:
    """Overdue dispatches for ALL bots, reading each file once.

    Each entry is (dispatched_at, expected_by, elapsed_past_deadline,
    task_id) — task_id is "-" for legacy id-less rows, so shell consumers
    can always read a stable 4th field.

    Join matrix (goal-aware plan P4): an id'd dispatch is closed ONLY by a
    terminal report echoing the same task_id — an id-less terminal report
    never closes it (LLM echo non-compliance is normal, and blanket-closing
    was exactly the #447 bug class). An id-less dispatch — raw-mode sends
    mint these permanently, not just pre-migration rows — keeps the
    (bot, ts >= dispatched_at) semantics, and any terminal report — id'd or
    not — satisfies it. No flag-day.

    With <bots_dir>, rows whose worker respawned after dispatch are NOT
    returned here — see orphaned_all.
    """
    return _classify_all(dispatch_log, report_ledger, now, max_age, bots_dir)[0]


def orphaned_all(
    dispatch_log: str,
    report_ledger: str,
    now: int,
    max_age: int = DEFAULT_OVERDUE_MAX_AGE_S,
    bots_dir: str | None = None,
) -> dict[str, list[tuple[int, int, int, str]]]:
    """Past-deadline dispatches whose worker RESPAWNED after they were sent.

    The session that received the id is gone, so the worker cannot echo what it
    can no longer see: the row could never close, and the watchdog would flag it
    every cycle until max_age. The predicate is respawn, NOT session-absence — a
    restarted bot keeps its session NAME, so an existence check reads a fresh
    incarnation as the original one. Only id'd rows can orphan; an id-less
    dispatch closes on any later terminal report and so survives a respawn.

    Split out of overdue_all rather than deleted: an aged-out row (#460) is an
    abandoned task, but an orphan is work the fleet lost to its own restart,
    which is actionable. Same row shape as overdue_all.

    Empty without <bots_dir> — respawn cannot be determined without the marker,
    and the safe default is to keep reporting a row overdue rather than silently
    retiring one that might still be live.
    """
    return _classify_all(dispatch_log, report_ledger, now, max_age, bots_dir)[1]


def open_dispatches(
    bot: str,
    dispatch_log: str,
    report_ledger: str,
) -> list[tuple[int, int | None, str]]:
    """The bot's still-open id'd dispatches, OLDEST FIRST.

    Each entry is (dispatched_at, expected_by, task_id). ``expected_by`` is
    None when the row carries none (or a non-int one) — deliberately NOT a
    filter, so this set stays a strict superset of what ``open_task_id``
    considers. A row that can supply the resolver's id must also be listable
    here, or the door would hide work the resolver can still close.

    OPEN is deadline-blind, and that is the whole point of this door: a
    dispatch is open until a terminal report echoing its id closes it, whether
    or not ``now`` has passed ``expected_by``. That makes it strictly wider
    than the watchdog's OVERDUE — every overdue row is an open row — so
    "carrying three tasks, none late yet" becomes readable, which is a state
    no existing mode could express.

    THE loop behind ``open_task_id``, which is now just this list's head. Two
    loops would let the resolver hand back an id this list does not contain —
    the same desync class ``_terminal_reported_ids`` exists to prevent, one
    level up.

    The join is NOT loosened: only a terminal report carrying the same
    (bot, task_id) closes a row, exactly as in ``_classify_all``.
    """
    bot_key = bot.lower()
    reported = _terminal_reported_ids(_load_jsonl(report_ledger))
    rows: list[tuple[int, int | None, str]] = []
    for d in _load_jsonl(dispatch_log):
        if str(d.get("bot", "")).lower() != bot_key:
            continue
        tid, da = d.get("task_id"), d.get("dispatched_at")
        if not tid or not isinstance(da, int):
            continue
        if (bot_key, str(tid)) in reported:
            continue
        exp = d.get("expected_by")
        rows.append((da, exp if isinstance(exp, int) else None, str(tid)))
    # Stable sort, so rows dispatched in the same second keep ledger order —
    # matching the strict-< tie-break open_task_id used when it kept its own min.
    rows.sort(key=lambda r: r[0])
    return rows


def open_task_id(
    bot: str,
    dispatch_log: str,
    report_ledger: str,
) -> str | None:
    """The bot's OLDEST still-open id'd dispatch, or None.

    What report-back.sh resolves when the worker omits --task, so the common
    path closes its dispatch by default instead of by discipline — workers
    routinely omit the id, which leaves every id'd dispatch open until it ages
    out and the watchdog alarms over finished work.

    OLDEST, not newest. A worker is a serial session draining a queued buffer
    in FIFO order, so the dispatch it just finished is the oldest one still
    open; and the oldest is the one actually past its deadline and alarming,
    where the newest is usually still inside it. Resolving newest-first would
    close the quiet row and leave the loud one open — the fix would not reach
    the alarms it exists to stop. Multiple open dispatches are the normal case,
    not an edge one (measured: most active bots carry two or three).

    FIFO also makes a wrong guess self-correcting: one report closes exactly
    one dispatch, so N reports for N queued tasks retire them in the order they
    were sent. A worker that reports only once still leaves the unreported rows
    open, which is the honest outcome.

    Deliberately NOT a loosening of the join in _classify_all — that would be
    the #447 blanket-close bug again. The join stays exactly as strict; this
    only supplies the id the report should have carried, so the row that closes
    is one this bot actually has open, and the watchdog still verifies it
    independently. Deadline is irrelevant here: a report arriving before
    expected_by must close its dispatch too.

    SCOPE CAVEAT: the dispatch log is host-global while the report ledger is
    per-fleet, and the join is on bot name alone. Two fleets under one root that
    reuse a bot name would cross-resolve. Pre-existing in the watchdog join; it
    matters more here because this writes the result. Fleet-scoping the join
    needs fleet identity threaded through both readers — tracked separately.
    """
    rows = open_dispatches(bot, dispatch_log, report_ledger)
    return rows[0][2] if rows else None


def missing_id_count(report_ledger: str) -> int:
    """Terminal reports carrying no task_id. NOTE: while raw (id-less)
    dispatch remains a legitimate mode, this counts raw-mode reports AND
    echo erosion together — the P7 brief must contextualize it (or refine
    to id'd-dispatches-that-aged-out) before treating it as pure erosion."""
    return sum(
        1
        for r in _load_jsonl(report_ledger)
        if r.get("status") in _TERMINAL and not r.get("task_id")
    )


def _take_bots_dir(argv: list[str]) -> tuple[list[str], str | None]:
    """Strip a trailing `--bots-dir <dir>` from argv.

    Kept out of the positional grammar so the existing arg positions (and every
    caller written against them) are untouched. Trailing-only by design: a
    valueless flag anywhere else would survive the strip and then be read as a
    positional.
    """
    if len(argv) >= 2 and argv[-2] == "--bots-dir":
        return argv[:-2], argv[-1]
    return argv, None


def main() -> int:
    argv, bots_dir = _take_bots_dir(sys.argv)
    if len(argv) < 3:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2

    max_age = _resolve_max_age()

    # report-back.sh's resolver: print the id the omitted --task should carry.
    # Silent (rc 0, no output) when nothing is open, so the caller degrades to an
    # id-less report exactly as before rather than failing a report-back.
    if argv[1] == "--open-task":
        if len(argv) < 5:
            print(__doc__.strip().splitlines()[0], file=sys.stderr)
            return 2
        tid = open_task_id(argv[2], argv[3], argv[4])
        if tid:
            print(tid)
        return 0

    # The read door's list form (#904). Same silence-on-empty contract as
    # --open-task: no rows means no output and rc 0, never an error.
    if argv[1] == "--open":
        if len(argv) < 5:
            print(__doc__.strip().splitlines()[0], file=sys.stderr)
            return 2
        for da, exp, tid in open_dispatches(argv[2], argv[3], argv[4]):
            print(f"{da} {exp if exp is not None else '-'} {tid}")
        return 0

    if argv[1] in ("--all", "--orphans"):
        dlog, rlog = argv[2], argv[3]
        now = (
            int(argv[4])
            if len(argv) > 4
            else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        )
        over, orph = _classify_all(dlog, rlog, now, max_age, bots_dir)
        rows = over if argv[1] == "--all" else orph
        for bot_id, entries in sorted(rows.items()):
            for da, exp, elapsed, tid in entries:
                print(f"{bot_id} {da} {exp} {elapsed} {tid}")
        return 0

    if len(argv) < 4:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    bot, dlog, rlog = argv[1], argv[2], argv[3]
    now = (
        int(argv[4])
        if len(argv) > 4
        else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    )
    for da, exp, elapsed, tid in overdue(bot, dlog, rlog, now, max_age, bots_dir):
        print(f"{da} {exp} {elapsed} {tid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
