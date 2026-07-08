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
  Prints: "<bot_id> <dispatched_at> <expected_by> <elapsed_seconds>"

No output (and exit 0) when nothing is overdue.

Kept as a standalone, dependency-free script so it is unit-testable in isolation
and callable from fleet-pulse.sh.
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
) -> list[tuple[int, int, int, str]]:
    """Single-bot variant — delegates to overdue_all and filters."""
    return overdue_all(dispatch_log, report_ledger, now, max_age).get(bot.lower(), [])


def overdue_all(
    dispatch_log: str,
    report_ledger: str,
    now: int,
    max_age: int = DEFAULT_OVERDUE_MAX_AGE_S,
) -> dict[str, list[tuple[int, int, int, str]]]:
    """Return overdue dispatches for ALL bots, reading each file once.

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
    """
    dispatches = _load_jsonl(dispatch_log)
    reports = _load_jsonl(report_ledger)

    # Per-bot terminal-report epochs (legacy join) + terminal (bot, id) set.
    # The id join is scoped by bot exactly like the legacy join: a peer
    # echoing (or mishearing) another bot's task id must not silence the
    # watchdog on the real owner's still-open dispatch (#518 review).
    report_index: dict[str, list[int]] = {}
    reported_ids: set[tuple[str, str]] = set()
    for r in reports:
        if r.get("status") not in _TERMINAL:
            continue
        bot_key = str(r.get("bot", "")).lower()
        tid = r.get("task_id")
        if tid:
            reported_ids.add((bot_key, str(tid)))
        ep = _iso_to_epoch(r.get("ts", ""))
        if ep is not None:
            report_index.setdefault(bot_key, []).append(ep)

    out: dict[str, list[tuple[int, int, int, str]]] = {}
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
        # Expiry cap (#460): once a still-open dispatch is older than max_age, the
        # watchdog stops flagging it so fleet-pulse quits re-emitting every cycle.
        # Checked after the report gate so a closed dispatch reads as closed, not
        # expired. max_age <= 0 disables the cap.
        if max_age > 0 and (now - da) > max_age:
            continue
        out.setdefault(bot_key, []).append(
            (da, exp, now - exp, str(tid) if tid else "-")
        )
    return out


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


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2

    max_age = _resolve_max_age()

    if sys.argv[1] == "--all":
        dlog, rlog = sys.argv[2], sys.argv[3]
        now = (
            int(sys.argv[4])
            if len(sys.argv) > 4
            else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        )
        for bot_id, entries in sorted(overdue_all(dlog, rlog, now, max_age).items()):
            for da, exp, elapsed, tid in entries:
                print(f"{bot_id} {da} {exp} {elapsed} {tid}")
        return 0

    if len(sys.argv) < 4:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    bot, dlog, rlog = sys.argv[1], sys.argv[2], sys.argv[3]
    now = (
        int(sys.argv[4])
        if len(sys.argv) > 4
        else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    )
    for da, exp, elapsed, tid in overdue(bot, dlog, rlog, now, max_age):
        print(f"{da} {exp} {elapsed} {tid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
