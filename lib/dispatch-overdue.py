#!/usr/bin/env python3
"""Find overdue dispatches — the matcher behind the fleet-pulse watchdog.

A dispatch (from state/dispatch-log.jsonl) is OVERDUE when:
  - now > expected_by, AND
  - no terminal report (status in completed|failed|blocked) for the same bot
    (case-insensitive) with report.ts >= dispatch.dispatched_at exists in the
    report-back ledger.

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
import sys

_TERMINAL = {"completed", "failed", "blocked"}


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
    bot: str, dispatch_log: str, report_ledger: str, now: int
) -> list[tuple[int, int, int]]:
    """Single-bot variant — delegates to overdue_all and filters."""
    return overdue_all(dispatch_log, report_ledger, now).get(bot.lower(), [])


def overdue_all(
    dispatch_log: str, report_ledger: str, now: int
) -> dict[str, list[tuple[int, int, int]]]:
    """Return overdue dispatches for ALL bots, reading each file once."""
    dispatches = _load_jsonl(dispatch_log)
    reports = _load_jsonl(report_ledger)

    # Build per-bot report epochs index
    report_index: dict[str, list[int]] = {}
    for r in reports:
        bot_key = str(r.get("bot", "")).lower()
        if r.get("status") in _TERMINAL:
            ep = _iso_to_epoch(r.get("ts", ""))
            if ep is not None:
                report_index.setdefault(bot_key, []).append(ep)

    out: dict[str, list[tuple[int, int, int]]] = {}
    for d in dispatches:
        bot_key = str(d.get("bot", "")).lower()
        exp, da = d.get("expected_by"), d.get("dispatched_at")
        if not isinstance(exp, int) or not isinstance(da, int):
            continue
        if now <= exp:
            continue
        if any(e >= da for e in report_index.get(bot_key, [])):
            continue
        out.setdefault(bot_key, []).append((da, exp, now - exp))
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 2

    if sys.argv[1] == "--all":
        dlog, rlog = sys.argv[2], sys.argv[3]
        now = (
            int(sys.argv[4])
            if len(sys.argv) > 4
            else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        )
        for bot_id, entries in sorted(overdue_all(dlog, rlog, now).items()):
            for da, exp, elapsed in entries:
                print(f"{bot_id} {da} {exp} {elapsed}")
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
    for da, exp, elapsed in overdue(bot, dlog, rlog, now):
        print(f"{da} {exp} {elapsed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
