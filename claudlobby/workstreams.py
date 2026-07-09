"""Read-only view of the per-fleet workstream registry (workstreams.json).

Writes go exclusively through ``lib/workstream-update.sh`` (the single writer)
and the ``/workstream`` manager skill that wraps it; this module only renders.
Path resolution mirrors the report-back ledger (overlay vs. root mode).
"""

from __future__ import annotations

import json
from pathlib import Path

from .paths import Paths


def registry_path(paths: Paths) -> Path:
    """workstreams.json — see ``Paths.fleet_state`` for the overlay-vs-root rule."""
    return paths.fleet_state / "workstreams.json"


def load_workstreams(paths: Paths) -> dict:
    """Return the ``{id: entry}`` map, or empty on missing/corrupt registry."""
    p = registry_path(paths)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    # Valid JSON but not the expected object (e.g. a hand-mangled file that is a
    # list or scalar) → treat as empty rather than raising AttributeError.
    if not isinstance(data, dict):
        return {}
    ws = data.get("workstreams", {})
    return ws if isinstance(ws, dict) else {}


def _day(ts: str | None) -> str:
    """ISO timestamp -> YYYY-MM-DD for compact columns; empty on None."""
    return (ts or "")[:10]


def format_list(workstreams: dict) -> str:
    if not workstreams:
        return "No workstreams."
    # active first, then by open date — the portfolio the manager scans.
    order = {"active": 0, "blocked": 1, "done": 2, "abandoned": 3}
    rows = sorted(
        workstreams.values(),
        key=lambda w: (order.get(w.get("status", ""), 9), w.get("opened_ts", "")),
    )
    header = f"{'ID':<28} {'STATUS':<9} {'OWNER':<10} {'LEASE':<10} NEXT"
    lines = [header, "-" * len(header)]
    for w in rows:
        lines.append(
            f"{w.get('id', ''):<28} {w.get('status', ''):<9} "
            f"{(w.get('owner_bot') or '—'):<10} {_day(w.get('lease_expires_ts')):<10} "
            f"{w.get('next') or ''}"
        )
    return "\n".join(lines)


def format_show(w: dict) -> str:
    lines = [
        f"{w.get('id', '')} — {w.get('title', '')}",
        f"  status:   {w.get('status', '')}",
        f"  fleet:    {w.get('fleet') or '—'}",
        f"  project:  {w.get('project') or '—'}",
        f"  owner:    {w.get('owner_bot') or '—'}",
        f"  next:     {w.get('next') or '—'}",
        f"  opened:   {w.get('opened_ts', '')}",
        f"  progress: {w.get('last_progress_ts', '')}",
        f"  lease:    {w.get('lease_expires_ts', '')}",
    ]
    task_ids = w.get("task_ids") or []
    if task_ids:
        lines.append(f"  tasks:    {', '.join(task_ids)}")
    refs = w.get("refs") or {}
    if refs.get("issues") or refs.get("prs"):
        lines.append(
            f"  refs:     issues={refs.get('issues') or []} prs={refs.get('prs') or []}"
        )
    renewals = w.get("renewals") or []
    if renewals:
        lines.append(
            f"  renewals: {len(renewals)} (last: {renewals[-1].get('note', '')})"
        )
    return "\n".join(lines)
