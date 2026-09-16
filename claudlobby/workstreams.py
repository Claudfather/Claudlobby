"""Read-only view of the per-fleet workstream registry — the PLANE's rendering.

Since the F18 closure the registry is materialized from the plane's workstream
events (``lib/plane-readers.py::workstream_registry``); there is no file. Writes
go exclusively through ``lib/workstream-update.sh`` (the single writer) and the
``/workstream`` manager skill that wraps it; this module only renders.
"""

from __future__ import annotations

import os

from .paths import Paths


def lease_days_env() -> int:
    """The lease window the registry is rendered with (``WORKSTREAM_LEASE_DAYS``,
    default 14) — one definition for every reader."""
    try:
        return int(os.environ.get("WORKSTREAM_LEASE_DAYS") or 14)
    except ValueError:
        return 14


def plane_workstreams(paths: Paths, plane=None):
    """(entries, None) — the ``{id: entry}`` map from the plane — or (None, note)
    when the plane cannot answer: unreachable, no stdlib readers, no fleet, or a
    fleet the plane holds no bot of. Never an empty map for a plane that could
    not be read (the caller omits with the note, or refuses). *plane* is a
    caller's open session (``brief.plane_session``); else one is opened here."""
    from .brief import plane_session
    session, note = (plane, None) if plane is not None else plane_session(paths)
    if session is None:
        return None, note
    try:
        reg = session.pr.workstream_registry(session.conn, session.fleet, lease_days=lease_days_env())
    except Exception as exc:
        return None, f"the plane cannot answer: {exc}"
    finally:
        if plane is None:
            session.close()
    return reg.get("workstreams", {}), None


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
