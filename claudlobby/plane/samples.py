"""#1644: one metric_samples family for one subject over a window.

Read-only by construction: `open_ro` (mode=ro plus query_only) and no
migrate(), so it can run against a live plane. Every row is fetched and the
connection closed before the caller renders anything, because a reader that
keeps its snapshot keeps the daemon's checkpoint from resetting the WAL
(#1905, #1912). A plane that records no subject of the kind refuses like an
unreachable one: a wrong root is not an empty window.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3

from .db import open_ro
from .identity import aliases_of_kind, lookup
from .queries import METRIC_SERIES_SQL
from .registries import METRIC_NAMES
from .schema_state import require_current_schema

SUBJECT_KINDS = ("host", "vault", "fleet", "actor", "bot_instance", "session")


class SamplesError(Exception):
    """A refusal with its public error code; raised only after the plane closed."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def subject_kind(metric: str, kind: str | None) -> str:
    if metric not in METRIC_NAMES:
        raise SamplesError("invalid_argument", f"unknown metric {metric!r};"
                           f" known: {', '.join(sorted(METRIC_NAMES))}")
    kind = kind or ("host" if metric.startswith("host.") else None)
    if kind not in SUBJECT_KINDS:
        raise SamplesError("invalid_argument", f"name the subject's --kind for {metric!r}"
                           f" (one of: {', '.join(SUBJECT_KINDS)})")
    return kind


def read(root: Path, metric: str, *, kind: str, subject: str | None,
         since: datetime, until: datetime) -> dict:
    """The window as plain data, oldest first; the plane is closed on return."""
    conn, why = open_ro(root)
    if conn is None:
        raise SamplesError("unavailable", f"the plane cannot answer: {why}")
    known, uid, rows = [], None, []
    try:
        require_current_schema(conn)
        known = aliases_of_kind(conn, kind)
        if subject is None and len(known) == 1:
            subject = known[0]
        uid = lookup(conn, kind, subject) if subject is not None else None
        if uid:
            rows = [(r["occurred_at"], r["value"], r["status"]) for r in conn.execute(
                METRIC_SERIES_SQL, (uid, metric, since.isoformat(), until.isoformat())).fetchall()]
    except sqlite3.Error as exc:
        why = str(exc)
    finally:
        conn.close()
    # Everything below runs with the plane released, a refusal included.
    if why is not None:
        raise SamplesError("unavailable", f"the plane cannot answer: {why}")
    if not known:
        raise SamplesError("unavailable", f"the plane cannot answer: it records no {kind}"
                           f" subject, so it holds no {metric} samples (a wrong --root,"
                           " or nothing has emitted one here)")
    if subject is None:
        raise SamplesError("invalid_argument", f"{len(known)} {kind} subjects are recorded;"
                           f" name one with --subject: {', '.join(known)}")
    if uid is None:
        raise SamplesError("invalid_argument", f"no {kind} subject named {subject!r};"
                           f" recorded: {', '.join(known)}")
    return {"metric": metric, "unit": METRIC_NAMES[metric].get("unit"), "kind": kind,
            "subject": subject, "since": since.isoformat(), "until": until.isoformat(),
            "samples": [{"occurred_at": at, "value": json.loads(value), "status": status}
                        for at, value, status in rows]}
