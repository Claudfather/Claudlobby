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
  --bots-dir is REQUIRED here and refused when missing or unreadable, at rc 3
  (#1014): orphan-ness is a comparison against <bots_dir>/<bot>/data/.spawn, so
  without one the answer is UNKNOWN, and printing an empty set at rc 0 made
  "cannot look" byte-identical to "nothing was lost to a restart". Measured: all
  three states returned 0 bytes at rc 0 against a 295-row log. rc 3 rather than
  the usage code 2, because the flag is optional in the grammar and this is not a
  malformed call -- it is a question this run cannot answer. The refusal is on
  STDERR because this mode's stdout is parsed (fleet-pulse.sh reads it into an
  orphan cache); orphaned_all() itself is UNCHANGED and still returns {} without
  a bots dir, since brief.py calls it directly and labels the gap its own way.

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
  Deadline-blind is NOT supersede-blind (#1357): a row retired by a later
  dispatch's --supersedes is gone from BOTH doors. It used to be gone from the
  overdue path ONLY, because _superseded_ids was applied inside a loop gated on
  the deadline -- so a retired row could not page and was simultaneously first
  in line for the resolver, which is what report-back.sh writes into the ledger.
  Also states its scope on STDERR ("--open: bot=... -> N open ...") on every
  run, so an empty result names what it filtered on and can never be read as
  "nothing exists" (#1187). Stdout stays rows-only for machine callers.

Unassigned mode (#1024) -- the MIRROR of overdue: a worker that reported and was
never re-tasked. Purely temporal (newest dispatch vs newest report); it never
reads whether a dispatch is open, because superseded rows stay open forever and
that signal is noise in both directions. See unassigned_all:
  dispatch-overdue.py --unassigned <dispatch_log> <report_ledger> [<now_epoch>]
  Prints: "<bot_id> <reported_at> <idle_seconds> <task_id> <status>"

WHICH MODES HAVE A BOT SLOT -- the grammar trap behind #1187. The split is not
bot-first vs logs-first; it is whether a mode names ONE bot at all:

  has a bot slot, taken FIRST   --open, --open-task, and SINGLE-BOT MODE
  no bot slot at all            --all, --orphans, --unassigned (every bot)

Single-bot mode shares --open and --open-task's grammar EXACTLY (main() reads
`bot, dlog, rlog = argv[1], argv[2], argv[3]`, same as they do) and therefore
shares the hazard exactly: three positionals parse cleanly with a path in the
bot slot, nothing matches, rc 0, no output -- indistinguishable from a genuine
empty result. It reaches that state most easily by FORGETTING a flag, since
`<dlog> <rlog> <now>` with no mode falls straight through to it.

Only --open/--open-task are gated so far (see _not_a_bot_id). SINGLE-BOT MODE
IS NOT -- though #1232's report-ledger guard now closes HALF of it: the
forgotten-flag route (`<dlog> <rlog> <now>`) refuses at rc 3, because `now`
lands in the rlog slot and a timestamp is not a readable file. A path in the
BOT slot beside a genuinely readable ledger is still silent at rc 0, and
closing THAT is the same one-line _reject_bot_slot call, not a
harder dlog/rlog-swap detection -- stated because an earlier version of this
paragraph filed single-bot mode under "logs-first" and would have sent the
next reader looking for the harder fix.

The other three need no gate: with no bot slot there is nothing to check, and
mis-ordering them is already LOUD rather than silent -- a ledger path lands in
the `now` slot and int() raises, rc 1 (measured, all three). So single-bot
mode is the only silent shape left in this module. Pinned by
tests/test_dispatch_overdue.py::TestBotSlotShapeGate, including a tripwire that
FAILS when single-bot mode is finally gated, so this paragraph cannot go stale
the way its first version did.

`--bots-dir <dir>` goes last and enables respawn detection; without it no row is
ever classified as an orphan. It is also the one input that is NOT one of the two
ledgers: orphan classification reads `.spawn` mtimes, so that mode alone depends
on ambient filesystem state. Everything else stays a pure function of
(dispatch log, report ledger, clock).

No output (and exit 0) when nothing is overdue. --open is the one exception and
deliberately so: it exits 0 with no STDOUT rows, but always writes its scope
line to STDERR (above).

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


def _superseded_ids(dispatches: list[dict]) -> set[tuple[str, str]]:
    """(bot, task_id) pairs a LATER dispatch explicitly declared it replaces.

    The dispatcher records `supersedes` at the moment of re-dispatch, because that is
    the only moment the intent exists. Two dispatches to one bot are indistinguishable
    in this ledger — the second may replace the first or queue behind it — so a
    superseded row can only be recognised if the caller said so. Inferring it from
    timing was measured and rejected: 14 of 189 closed rows had a later row close
    first and were still answered afterwards, 3 of them genuine work answered 6-7h
    late (2026-08-05). Retiring on that signal would drop tasks someone still owed.

    Scoped by bot for the same reason `_terminal_reported_ids` is: one bot's dispatch
    must not retire another's row, however the id was typed (#518 review).
    """
    return {
        (str(d.get("bot", "")).lower(), str(d["supersedes"]))
        for d in dispatches
        if d.get("supersedes")
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
    superseded_ids = _superseded_ids(dispatches)
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
            # Retired by declaration: a later dispatch to this bot said it replaces
            # this row, so nobody will ever report against it. Checked only for id'd
            # rows — an id-less row already closes on any later terminal report, so it
            # has nothing to be stranded by.
            if (bot_key, str(tid)) in superseded_ids:
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
    dispatch is open until it is CLOSED (a terminal report echoing its id) or
    RETIRED (a later dispatch to the same bot declaring ``supersedes``),
    whether or not ``now`` has passed ``expected_by``. That makes it strictly
    wider than the watchdog's OVERDUE — every overdue row is an open row — so
    "carrying three tasks, none late yet" becomes readable, which is a state
    no existing mode could express.

    DEADLINE-BLIND IS NOT SUPERSEDE-BLIND, and conflating the two was a live
    defect (#1357). This door consulted ``_terminal_reported_ids`` but not
    ``_superseded_ids``, which is applied inside ``_classify_all``'s loop —
    a loop gated on ``now <= exp``, so the retirement rule was reachable only
    from the deadline-bound path. A retired row was therefore **invisible to
    alerting** (filtered by ``_classify_all``, so it never pages) and
    simultaneously the **preferred close target** (head of this list, which is
    what ``open_task_id`` returns and what ``report-back.sh`` resolves an
    id-less report to). Both halves fail quietly, in opposite directions: the
    next id-less terminal report closes the row declared dead while the live
    successor strands. Measured on four supersede pairs across three fleets;
    the more disciplined the manager is about ``--supersedes``, the older the
    row this door hands back, because retired rows accumulate at the head.

    THE loop behind ``open_task_id``, which is now just this list's head. Two
    loops would let the resolver hand back an id this list does not contain —
    the same desync class ``_terminal_reported_ids`` exists to prevent, one
    level up. #1357 is that class one level out again: two doors disagreeing
    about what OPEN means, with the resolver inheriting the wrong answer. So
    both gates live in shared helpers rather than being restated here.

    The join is NOT loosened: only a terminal report carrying the same
    (bot, task_id) closes a row, and only a same-bot ``supersedes`` retires
    one, exactly as in ``_classify_all``.
    """
    bot_key = bot.lower()
    reported = _terminal_reported_ids(_load_jsonl(report_ledger))
    dispatches = _load_jsonl(dispatch_log)
    # Read once and reuse: the retirement set is derived from the SAME rows this
    # loop walks, so a second read could only introduce skew.
    superseded = _superseded_ids(dispatches)
    rows: list[tuple[int, int | None, str]] = []
    for d in dispatches:
        if str(d.get("bot", "")).lower() != bot_key:
            continue
        tid, da = d.get("task_id"), d.get("dispatched_at")
        if not tid or not isinstance(da, int):
            continue
        if (bot_key, str(tid)) in reported:
            continue
        # Retired by declaration — same gate, same helper, same order as
        # _classify_all. Only id'd rows can be superseded, and this loop has
        # already dropped the id-less ones.
        if (bot_key, str(tid)) in superseded:
            continue
        exp = d.get("expected_by")
        rows.append((da, exp if isinstance(exp, int) else None, str(tid)))
    # Stable sort, so rows dispatched in the same second keep ledger order —
    # matching the strict-< tie-break open_task_id used when it kept its own min.
    rows.sort(key=lambda r: r[0])
    return rows


def unassigned_all(
    dispatch_log: str,
    report_ledger: str,
    now: int,
    idle_threshold: int = 0,
) -> dict[str, tuple[int, int, str, str]]:
    """Workers that reported terminal and were never re-tasked — the #1024 mirror.

    Returns {bot: (reported_at, idle_seconds, task_id, status)}.

    overdue_all answers "work was sent and never came back". This answers the
    mirror, which had no detector at all: **work came back and nothing was
    sent.** A worker that finishes cleanly and is then forgotten is
    indistinguishable from one legitimately idle, and activity_stuck cannot see
    it — a genuinely idle bot IS idle, so keepalive re-stamps `.idle` and that
    branch never fires. Correct for its purpose, and precisely why this case was
    invisible for 16 hours.

    THE PREDICATE IS PURELY TEMPORAL, AND THAT IS THE WHOLE DESIGN. It compares
    the newest dispatch instant against the newest report instant. It never asks
    whether any dispatch is OPEN.

    That restraint is the load-bearing part, not an optimisation. A manager
    amending a task re-dispatches repeatedly, and every replaced row stays open
    forever because the worker answers only the last id. Read "replaced" in the
    ordinary sense, not as the --supersedes flag: #1357 made open_dispatches
    honour DECLARED retirement, but declaration is rare (#1032 measured the flag
    retiring zero rows in a week), so the undeclared chain this paragraph
    describes is untouched and remains the common shape.

    Verified against a real chain rather than a fixture (vera, review of #1121):
    six dispatches to one worker inside 2143s for a single evolving task, five of
    the six ids still open afterwards, while that worker was demonstrably working
    throughout. Replaying this function over the same two ledgers truncated to
    successive cutoffs — not merely varying `now`, which cannot replay history —
    it stays silent through the busy stretch, raises at the real 4797s gap that
    followed, and goes silent again the instant the next dispatch lands. All five
    stale ids are open the entire time and change nothing.

    So "this bot has an open dispatch" carries no information about whether it is
    working. Keyed on it, this check fails in BOTH directions — page on every
    re-dispatching manager (the common case, not an edge one), or read those
    stale rows as "still busy" and never fire, which is the #1024 incident
    recurring inside its own watchdog.

    Comparing instants makes supersession irrelevant by construction: stale rows
    are all older than the report that closed the real task, so they can neither
    mask a strand nor manufacture one. This is also why nothing here consumes
    #1027's `supersedes` field, and why its absence costs nothing — there is no
    ambiguity left for a declaration to resolve.

    A bot whose newest report is `progress` is NOT returned: it is working, or it
    has stalled mid-task, and the stall is overdue_all's to report. Only a
    terminal newest report means the worker is done and waiting.

    Threshold filtering defaults OFF (0 = report every match with its idle time).
    fleet-pulse applies the threshold and the staleness cap PER BOT from
    bot.conf, the same split activity_stuck uses: this owns the join, the caller
    owns the policy. One scan therefore serves bots with different thresholds.
    """
    reports = _load_jsonl(report_ledger)
    dispatches = _load_jsonl(dispatch_log)

    # Newest report per bot, whatever its status — taking the newest TERMINAL one
    # instead would read a bot that reported progress after finishing as idle.
    newest_report: dict[str, tuple[int, str, str]] = {}
    for r in reports:
        bot = str(r.get("bot", "")).lower()
        ts = _iso_to_epoch(str(r.get("ts", "")))
        if not bot or ts is None:
            continue
        if ts >= newest_report.get(bot, (-1,))[0]:
            newest_report[bot] = (
                ts,
                str(r.get("status", "")),
                str(r.get("task_id") or "-"),
            )

    # Newest dispatch per bot. `dispatched_at` is the epoch the manager sent it;
    # fall back to `ts` for rows that predate that field.
    newest_dispatch: dict[str, int] = {}
    for d in dispatches:
        bot = str(d.get("bot", "")).lower()
        if not bot:
            continue
        da = d.get("dispatched_at")
        if not isinstance(da, int):
            da = _iso_to_epoch(str(d.get("ts", "")))
        if da is None:
            continue
        if da > newest_dispatch.get(bot, -1):
            newest_dispatch[bot] = da

    out: dict[str, tuple[int, int, str, str]] = {}
    for bot, (rts, status, tid) in newest_report.items():
        if status not in _TERMINAL:
            continue
        last_d = newest_dispatch.get(bot)
        if last_d is not None and last_d > rts:
            continue  # re-tasked after reporting — the loop is intact
        idle = now - rts
        if idle < 0 or idle < idle_threshold:
            continue
        out[bot] = (rts, idle, tid, status)
    return out


def _answering_an_idless_dispatch(
    bot: str,
    dispatch_log: str,
    report_ledger: str,
) -> bool:
    """True when this bot's most recent dispatch carries no id and is unanswered.

    The evidence that a terminal report is NOT the missing echo of an id'd row.
    It is read off the ledgers the fleet already writes — no wire-format field
    and no worker cooperation, which matters because a composed instruction does
    not reach a running bot until it restarts, while this file is read fresh on
    every report.

    UNANSWERED is the second half and it is what keeps #835 intact. Without it a
    single peer note would suppress the resolver for the rest of the bot's life,
    stranding every later report. A terminal report landing after the id-less
    dispatch discharges it; the report after that resolves normally again.

    Ties go to the later ledger line (`>=`), matching arrival order — the same
    tie-break open_dispatches takes with its stable sort.
    """
    bot_key = bot.lower()
    latest: dict | None = None
    latest_at: int | None = None
    for d in _load_jsonl(dispatch_log):
        if str(d.get("bot", "")).lower() != bot_key:
            continue
        da = d.get("dispatched_at")
        if not isinstance(da, int):
            continue
        if latest_at is None or da >= latest_at:
            latest, latest_at = d, da
    if latest is None or latest.get("task_id"):
        return False
    for r in _load_jsonl(report_ledger):
        if str(r.get("bot", "")).lower() != bot_key:
            continue
        if r.get("status") not in _TERMINAL:
            continue
        at = _iso_to_epoch(str(r.get("ts", "")))
        if at is not None and at >= latest_at:
            return False
    return True


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

    SUPPRESSED when the bot's most recent dispatch is an unanswered id-less one
    (#1190). The resolver's whole premise is "the worker finished the id'd row it
    was given and forgot to echo the id". A peer note breaks that premise: the
    most recent thing asked of the bot carried no id, so a terminal report now is
    most plausibly answering THAT, and stamping an older id'd row asserts
    something the ledger does not support. The result is not a stale row — it is
    a real, in-progress task silently marked `completed`, which is the one
    outcome worse than the never-closing row #1187 set out to stop: an open row
    is visible and legible, a false completion sends nobody to look.

    Measured through the real report-back.sh, worker compliant with Step 2 in
    both arms: `--type query` to a bot holding one open id'd task closed that
    task. So did a raw-text dispatch on main — the hole predates the envelope
    types and is not reachable from the transmit side at all, which is why this
    guard lives here and not in the envelope.

    Returning None NEVER violates the superset invariant this door shares with
    open_dispatches: the rule is that it must not hand back an id the list does
    not contain, and None contains nothing. --open, --all, --orphans and
    --unassigned are untouched; only the resolver reads this.

    The cost is deliberate and one-directional: a report that WAS the missing
    echo now leaves its row open until the next report. That is UNTRACKED, the
    degradation direction #1187 chose, and the watchdog still surfaces it.
    """
    if _answering_an_idless_dispatch(bot, dispatch_log, report_ledger):
        return None
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


def _not_a_bot_id(value: str) -> str | None:
    """Why this first positional cannot be a bot id — or None if it might be.

    #1187. `--open`, `--open-task` and single-bot mode each name ONE bot and
    take it FIRST; `--all`/`--orphans`/`--unassigned` name none. Calling a
    bot-slot mode with the every-bot grammar keeps the arity valid, so the
    existing count check passes, a path lands in the bot slot, nothing matches
    it, and the door prints nothing at rc 0 — byte-identical to a genuine
    "nothing open". That is how a manager read a full backlog as all-clear
    while checking whether its closures had worked.

    Callable for single-bot mode too, which has the identical grammar and is
    NOT yet wired to it; see the module docstring.

    So the missing check is on SHAPE, not count. Wrong ARITY already fails
    loudly and is not the hazard: measured, two positionals return rc 2 with the
    usage line. It is specifically right-count/wrong-order that is silent.

    Lexical only, and deliberately NOT a roster lookup. This module is a pure
    function of (dispatch log, report ledger, clock) — everything but --orphans
    reads no ambient state — and a manifest read here would both break that and
    re-import the #526 host-global-vs-per-fleet mismatch into a validator, where
    a legitimate cross-fleet name would then be refused outright.

    Rejects only what a bot id can NEVER be, so it cannot refuse a real one: a
    bot id names a directory under runtime/bots/ and a tmux session, so it holds
    no "/" and does not end ".jsonl". Both shapes are needed — a ledger passed
    by bare filename from its own directory carries no separator.
    """
    if not value.strip():
        return "an empty bot id"
    if "/" in value:
        return f"a path: {value!r}"
    if value.endswith(".jsonl"):
        return f"a ledger file: {value!r}"
    return None


def _refuse_undeterminable_orphans(bots_dir: str | None) -> bool:
    """True (and says why) when `--orphans` cannot determine orphan-ness at all.

    #1014, and it is the same defect as #1216 on a sibling command. Orphan-ness is
    decided by comparing a dispatch against `<bots_dir>/<bot>/data/.spawn`, so
    with no readable bots dir there is nothing to compare and the answer is
    *unknown* — but the mode printed an empty set at rc 0, which is byte-identical
    to "no work was lost to a restart". Measured on the reporting host: no
    `--bots-dir`, a real one with no orphans, and a `--bots-dir` naming a path
    that does not exist all returned 0 bytes at rc 0 against a 295-row dispatch
    log. A FOURTH was found in review (#1227): a bots dir that is present and
    stats as a directory but raises on listing, which `os.path.isdir` waves
    through. FOUR states, one output, and the collapsed ones read as good news.

    WHAT THIS DOES NOT CHANGE, deliberately. `orphaned_all` and `_classify_all`
    keep their contracts to the byte: `orphaned_all` still returns {} without a
    bots dir, and its docstring's reasoning still holds — a row that cannot be
    proven orphaned must stay in the OVERDUE set rather than be silently retired.
    `brief.py` calls that function directly and already labels the gap itself, so
    changing the join would have broken a caller that had it right. The refusal
    lives in the CLI mode, which is the surface a human or a new tool reads.

    Which is also why the disclosure is on **stderr** while #1216's is on stdout.
    Not a style choice — this mode's stdout is PARSED: `fleet-pulse.sh:142` reads
    it into an orphan cache consumed by `read -r`, and a prose line there becomes
    a phantom row. The module already settled this for `--open` (see the comment
    at the `--open` scope line); rc is what carries the refusal.

    rc **3**, not 2: rc 2 is this module's usage error and means "you called me
    wrong". A missing `--bots-dir` is not a usage error — the flag is optional by
    design and every shipped caller passes it — it means "I was asked a question I
    cannot answer with what I can reach". Distinct codes so a caller can tell a
    typo from an unreachable instrument, which is the whole rule being applied to
    the refusal's own reporting.

    Both existing callers pass a real `--bots-dir` and additionally use
    `|| true`, so **this cannot fire for either of them**. That is the intended
    blast radius, not a limitation to be worked around: the fix is for the direct
    reader that #1014 misled, and it is inert for the watchdog by construction.
    """
    if bots_dir is None:
        print(
            "dispatch-overdue.py: --orphans cannot determine orphans without "
            "--bots-dir <dir>\n"
            "  orphan-ness is a comparison against <bots_dir>/<bot>/data/.spawn; "
            "with no bots dir there is nothing to compare, so the answer is "
            "UNKNOWN, not 'none'.\n"
            "  usage: dispatch-overdue.py --orphans <dispatch_log> "
            "<report_ledger> [<now>] --bots-dir <dir>",
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
    # The FOURTH state: present, stats as a directory, raises on listing. Same
    # consequence as the two above -- every .spawn lookup fails, so every row
    # classifies as not-an-orphan -- but isdir() waves it through. Listability
    # is tested rather than isdir() for the reason probe_dir tests it in
    # claudlobby/source_state.py; this module stays stdlib-only and cannot
    # import that, so it carries the same check locally.
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


def _refuse_unreadable_report_ledger(
    mode: str, rlog: str, *, refuse_on_absent: bool = True
) -> bool:
    """#1232. Print the refusal for `mode` and return True, or False to proceed.

    ABSENT AND UNOPENABLE ARE DIFFERENT FACTS AND THE MODES SPLIT ON THEM. The
    first version refused on both everywhere and broke the #835 resolver: a
    fleet that has never reported has NO ledger file, and the first report is
    exactly the call that must still work.

      not a file      -> nothing was ever written there. On a never-reported
                         fleet EVERY id'd row genuinely IS open, so "all open"
                         is TRUE and the head of that list is the RIGHT id.
      exists, raises  -> rows exist that cannot be read, so some are certainly
                         closed and the head may be an already-CLOSED row.

    So `refuse_on_absent=False` is passed by --open-task alone, and the reason
    is that absence makes ITS answer more certain rather than less.

    THE COST OF THAT, STATED BECAUSE IT IS A CHOICE AND NOT A FREE ONE: a WRONG
    PATH on --open-task is now permanently SILENT. That is a real bug that
    really happened -- state/ passed where runtime/ belonged, for six hours --
    and under this design the resolver would have kept returning nothing
    without complaint.

    It is still the right trade, and the asymmetry is what justifies it:

      --open       with a wrong path LIES. It returns a confident inflated
                   number that a human reads and acts on.
      --open-task  with a wrong path only UNDER-DELIVERS. Reports stay id-less
                   and dispatches do not auto-close. Degradation, not
                   falsehood, and it fails safe.

    Refusing on --open prevents a lie. Not refusing on --open-task preserves the
    legitimate never-reported case at the price of a silent degradation. Do not
    "fix" the silence later without re-reading this: a silent resolver here is
    the intended cost, not the bug that was fixed.

    Exactly the failure class :830 already guards for --orphans, pointed at the
    input three managers actually pass by hand. The join closes a dispatch by
    finding its terminal report; with no readable ledger there is nothing to
    join against, so EVERY id'd row comes back open -- indistinguishable from a
    fleet that closed nothing. Substitute the nouns in the --orphans reasoning
    and it is the same sentence.

    The line is PRESENCE, not emptiness. A ledger that exists holding zero rows
    is a fleet that has not reported yet, and "every dispatch is still open" is
    TRUE for it. Only absence or an IO failure makes that answer a fabrication.

    Openability is tested rather than is_file() for the reason probe_source
    tests it in claudlobby/source_state.py: a path that stats fine and then
    raises is the mode that takes out a read door, and a stat-only probe
    certifies it. This module stays stdlib-only and cannot import that, so it
    carries the same check locally -- the same constraint :843 states.

    is_file() is still the FIRST gate, deliberately. A directory where a file
    belongs raises IsADirectoryError, which is an OSError, so probing first
    would report it as unreadable and send someone to run chmod on a path that
    is simply not a file.
    """
    if not os.path.isfile(rlog):
        if not refuse_on_absent:
            return False
        print(
            f"dispatch-overdue.py: {mode} cannot read the report ledger: "
            f"{rlog!r} is not a file\n"
            "  every id'd row would classify as OPEN for want of a terminal "
            "report, which is indistinguishable from a fleet that closed "
            "nothing.",
            file=sys.stderr,
        )
        return True
    try:
        with open(rlog, "rb"):
            pass
    except OSError as exc:
        print(
            f"dispatch-overdue.py: {mode} cannot read the report ledger: "
            f"{rlog!r} exists but cannot be opened ({exc.strerror})\n"
            "  every id'd row would classify as OPEN for want of a terminal "
            "report, which is indistinguishable from a fleet that closed "
            "nothing.",
            file=sys.stderr,
        )
        return True
    return False


def _reject_bot_slot(mode: str, value: str) -> bool:
    """Print the #1187 shape refusal for `mode`, or return False to proceed."""
    why = _not_a_bot_id(value)
    if why is None:
        return False
    print(
        f"dispatch-overdue.py: {mode} expects <bot_id> first, got {why}\n"
        f"  usage: dispatch-overdue.py {mode} <bot_id> <dispatch_log> <report_ledger>\n"
        "  note:  --all/--orphans/--unassigned take the LOGS first and name no "
        "bot at all; --open/--open-task take the BOT first.",
        file=sys.stderr,
    )
    return True


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
        # Shares --open's grammar, so it shares --open's hazard (#1187). Inert
        # for the only production caller: report-back.sh passes its own $BOT,
        # and a $BOT shaped like a path already resolved to nothing here. The
        # refusal is on stderr behind that caller's 2>/dev/null and its rc
        # behind `|| true`, so the fail-open contract above is untouched — a
        # report-back still degrades to an id-less report, never an error.
        if _reject_bot_slot("--open-task", argv[2]):
            return 2
        # ABSENT is legitimate here and must resolve: see the docstring.
        if _refuse_unreadable_report_ledger(
            "--open-task", argv[4], refuse_on_absent=False
        ):
            return 3
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
        if _reject_bot_slot("--open", argv[2]):
            return 2
        if _refuse_unreadable_report_ledger("--open", argv[4]):
            return 3
        rows = open_dispatches(argv[2], argv[3], argv[4])
        for da, exp, tid in rows:
            print(f"{da} {exp if exp is not None else '-'} {tid}")
        # State the scope, ALWAYS and on STDERR (#1187). Always, because the
        # shape gate above cannot reach the rest of the class: a plausible but
        # wrong bot -- a typo, or a live name belonging to another fleet under
        # #526 -- still returns zero rows at rc 0, which is coverage honesty's
        # exact failure. An empty result that names what it filtered on can no
        # longer be read as "nothing exists".
        #
        # STDERR is load-bearing, not convention, and the harm is MEASURED
        # rather than reasoned. report-back.sh (:117) pipes this stdout through
        # `awk {print $3}` to decide whether a supplied --task id is open. On
        # stdout this line is parsed as a row: field 3 is "->", so the open set
        # becomes non-empty and the "only a NON-EMPTY open set can contradict
        # the caller" fail-open (#1146) inverts.
        #
        # The trigger is narrower than it first looks, which is why it has to be
        # run rather than argued. A bot that DOES hold an open row still matches
        # its own id -- the phantom adds an entry, it does not remove the real
        # one -- so that case stays clean and reads as proof the placement does
        # not matter. It bites where the bot has NOTHING open: a valid id then
        # meets an open set of exactly ["->"], and a correct report is flagged
        # `supplied-id-not-open` with `open now: ->`. Measured on the real
        # script, both arms. That caller reads stdout and discards stderr, so
        # this line is inert for it by construction.
        print(
            f"--open: bot={argv[2]!r} -> {len(rows)} open id'd dispatch(es)",
            file=sys.stderr,
        )
        return 0

    # #1024 mirror watchdog. No threshold here: fleet-pulse applies its own per
    # bot, so one scan serves a fleet whose bots are tuned differently.
    if argv[1] == "--unassigned":
        if len(argv) < 4:
            print(__doc__.strip().splitlines()[0], file=sys.stderr)
            return 2
        now = (
            int(argv[4])
            if len(argv) > 4
            else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        )
        for bot_id, (rts, idle, tid, status) in sorted(
            unassigned_all(argv[2], argv[3], now).items()
        ):
            print(f"{bot_id} {rts} {idle} {tid} {status}")
        return 0

    if argv[1] in ("--all", "--orphans"):
        # Arity FIRST. Pre-existing (verified on main): these two modes indexed
        # argv[3] before any length check, so `--orphans <dlog>` died with an
        # uncaught IndexError at rc 1 instead of the usage line at rc 2. Found by
        # the #1014 test that asserts "cannot answer" (rc 3) is distinguishable
        # from "called wrong" (rc 2) — a distinction that needs rc 2 to actually
        # be reachable. Loud either way, so this was never the silent class; it is
        # fixed here because the refusal below depends on the contrast.
        if len(argv) < 4:
            print(__doc__.strip().splitlines()[0], file=sys.stderr)
            return 2
        dlog, rlog = argv[2], argv[3]
        now = (
            int(argv[4])
            if len(argv) > 4
            else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        )
        if argv[1] == "--orphans" and _refuse_undeterminable_orphans(bots_dir):
            return 3
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
    if _refuse_unreadable_report_ledger("single-bot mode", rlog):
        return 3
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
