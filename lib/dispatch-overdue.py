#!/usr/bin/env python3
"""Find overdue dispatches for a bot — the matcher behind the fleet-pulse watchdog.

A dispatch (from state/dispatch-log.jsonl) is OVERDUE when:
  - now > expected_by, AND
  - no terminal report (status in completed|failed|blocked) for the same bot
    (case-insensitive) with report.ts >= dispatch.dispatched_at exists in the
    report-back ledger.

Prints one line per overdue dispatch: "<dispatched_at> <expected_by> <elapsed_seconds>".
No output (and exit 0) when nothing is overdue.

Usage: dispatch-overdue.py <bot_id> <dispatch_log> <report_ledger> [<now_epoch>]

Kept as a standalone, dependency-free script so it is unit-testable in isolation
and callable from fleet-pulse.sh (which already depends on python3 via bot-vitals).
"""
from __future__ import annotations

import datetime
import json
import sys

_TERMINAL = {"completed", "failed", "blocked"}


def _iso_to_epoch(ts: str) -> int | None:
    try:
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
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


def overdue(bot: str, dispatch_log: str, report_ledger: str, now: int) -> list[tuple[int, int, int]]:
    bot = bot.lower()
    dispatches = [d for d in _load_jsonl(dispatch_log) if str(d.get("bot", "")).lower() == bot]
    report_epochs = [
        e
        for r in _load_jsonl(report_ledger)
        if str(r.get("bot", "")).lower() == bot and r.get("status") in _TERMINAL
        for e in (_iso_to_epoch(r.get("ts", "")),)
        if e is not None
    ]
    out: list[tuple[int, int, int]] = []
    for d in dispatches:
        exp, da = d.get("expected_by"), d.get("dispatched_at")
        if not isinstance(exp, int) or not isinstance(da, int):
            continue
        if now <= exp:  # not yet due
            continue
        if any(e >= da for e in report_epochs):  # answered after dispatch → closed
            continue
        out.append((da, exp, now - exp))
    return out


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2
    bot, dlog, rlog = sys.argv[1], sys.argv[2], sys.argv[3]
    now = (
        int(sys.argv[4])
        if len(sys.argv) > 4
        else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    )
    for da, exp, elapsed in overdue(bot, dlog, rlog, now):
        print(f"{da} {exp} {elapsed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
