"""Bounded reads of retained generate observations, in recording order."""
from __future__ import annotations

import json

from .contracts import CompositionObservation

COVERAGE_NOTE = (
    "Retained completion receipts only, newest recorded first. Disabled or failed "
    "emissions, aborted generates, older receipts without provenance and retention "
    "can leave gaps. Clean observations cannot rule out a transient checkout/rebase "
    "repaired before any generate observed it. Registry complete describes enumeration, "
    "not attempt coverage."
)


def history_result(fleet_alias: str, limit: int, *, available=True, error=None) -> dict:
    if not isinstance(fleet_alias, str) or not fleet_alias.strip():
        raise ValueError("compositions requires a nonempty fleet alias")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("composition limit must be between 1 and 1000")
    return {"fleet": fleet_alias, "limit": limit, "available": available,
            "observations": [], "coverage": {"complete_attempt_history": False,
            "note": COVERAGE_NOTE, "missing_composition": 0, "unreadable": 0},
            **({"error": error} if error else {})}


def read_compositions(conn, fleet_alias: str, *, limit: int = 20) -> dict:
    """Limit SQL before parsing; no registry scan, migration or state creation.

    Include old completion receipts with missing observations rather than making
    them disappear into a falsely complete history. ingest_seq is the stable
    order of recording, deliberately not a claim about event-time chronology.
    """
    result = history_result(fleet_alias, limit)
    rows = conn.execute(
        "SELECT e.event_id, e.ingest_seq, e.occurred_at, e.detail, e.detail_truncated "
        "FROM events e JOIN identity_registry f ON f.uid=e.fleet_uid "
        "WHERE e.kind='declaration' AND e.event='scan_completed' "
        "AND f.kind='fleet' AND f.alias=? ORDER BY e.ingest_seq DESC LIMIT ?",
        (fleet_alias, limit)).fetchall()
    for event_id, seq, occurred_at, raw, truncated in rows:
        item = {"event_id": event_id, "ingest_seq": seq, "recorded_event_time": occurred_at}
        try:
            if truncated:
                raise ValueError("truncated declaration")
            detail = json.loads(raw)
            if not isinstance(detail, dict):
                raise ValueError("declaration is not an object")
            item.update(scan_id=detail.get("scan_id"), registry_complete=detail.get("complete"))
            observation = detail.get("composition")
            if observation is None:
                item.update(status="not_recorded", composition=None)
                result["coverage"]["missing_composition"] += 1
            else:
                value = CompositionObservation.model_validate(observation)
                if value.fleet != fleet_alias:
                    raise ValueError("composition names another fleet")
                item.update(status="recorded", composition=value.model_dump(mode="json", by_alias=True))
        except (ValueError, TypeError):
            item.update(status="unreadable", composition=None)
            result["coverage"]["unreadable"] += 1
        result["observations"].append(item)
    return result
