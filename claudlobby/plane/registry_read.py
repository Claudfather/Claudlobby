"""Registry lane, the read half (chunk B — spec §9 lines 143-146, §18).

Read doors over ``registry_snapshots`` + the F11-validated Lane C queries.
Everything here is DERIVED and disposable: the SQL in ``queries.py`` is the
one definition of validity and ordering; this module parses payloads,
computes field-level diffs, and answers trust questions (invalid
tombstones, last-scan health, projection-vs-estate hash drift).

Scope filtering reuses the EMITTER'S scope predicate (``_in_scope``) —
scope membership has exactly one definition, or a fleet filter here would
drift from tombstone eligibility there the first time an alias convention
moves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .queries import (
    REG_CHANGES_SQL,
    REG_CURRENT_POINT_SQL,
    REG_CURRENT_SQL,
    REG_HISTORY_SQL,
    REG_INVALID_TOMBSTONES_SQL,
)


def _q(conn, sql: str, params=()) -> list[dict]:
    """Rows as dicts regardless of the caller's row_factory — this module
    takes any plane connection and must not assume sqlite3.Row."""
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _parse(row: dict) -> dict:
    out = dict(row)
    if out.get("payload"):
        out["payload"] = json.loads(out["payload"])
    if out.get("prev_payload"):
        out["prev_payload"] = json.loads(out["prev_payload"])
    return out


def _read_scope(entity_type: str, alias: str, fleet_name: str) -> bool:
    """Reader-side fleet filter: the emitter's ``_in_scope`` with the vault
    rule relaxed to any-vault — the emitter's vault rule needs the scan's
    own enumeration set, which a listing reader does not have. Every other
    rule IS the emitter's, called directly (one scope definition)."""
    from .registry_emit import _in_scope
    if entity_type == "vault":
        return True
    return _in_scope(entity_type, alias, fleet_name, set())


def current_entities(conn, *, entity_type: str | None = None,
                     fleet: str | None = None) -> list[dict]:
    """Current registry state (F11-valid, tombstone-aware). ``fleet``
    filters through the emitter's own scope rules; host/vault rows are
    host-global and included for any fleet, matching scan scope."""
    rows = [_parse(r) for r in _q(conn, REG_CURRENT_SQL)]
    if entity_type is not None:
        rows = [r for r in rows if r["entity_type"] == entity_type]
    if fleet is not None:
        rows = [r for r in rows
                if _read_scope(r["entity_type"], r["entity_alias"], fleet)]
    return rows


def entity_history(conn, ident: str) -> list[dict]:
    """SCD2 windows for one entity, by alias or uid — tombstone rows open
    the deleted period and are rendered, never filtered."""
    rows = [_parse(r) for r in _q(conn, REG_HISTORY_SQL)]
    return [r for r in rows
            if r["entity_alias"] == ident or r["entity_uid"] == ident]


def diff_fields(prev, curr, prefix: str = "") -> dict[str, tuple]:
    """Field-level diff as dotted leaf paths -> (old, new). Recurses dicts;
    lists and scalars compare atomically (a list's element identity is the
    payload author's business, not a diff heuristic's). Deterministic
    ordering for stable rendering."""
    if not isinstance(prev, dict) or not isinstance(curr, dict):
        return {} if prev == curr else {prefix or ".": (prev, curr)}
    out: dict[str, tuple] = {}
    for key in sorted(set(prev) | set(curr)):
        path = f"{prefix}.{key}" if prefix else key
        if key not in prev:
            out[path] = (None, curr[key])
        elif key not in curr:
            out[path] = (prev[key], None)
        elif isinstance(prev[key], dict) and isinstance(curr[key], dict):
            out.update(diff_fields(prev[key], curr[key], path))
        elif prev[key] != curr[key]:
            out[path] = (prev[key], curr[key])
    return out


def recent_changes(conn, *, limit: int = 50) -> list[dict]:
    """The registry_changes view: consecutive-row pairs, newest first, each
    carrying its field-level diff. Transitions to/from tombstone render as
    ``deleted`` / ``recreated``, a first-in-partition row as
    ``first_observed`` (spec's derivation name — honestly first-OBSERVED,
    not created) — never a field storm."""
    rows = [_parse(r) for r in _q(conn, REG_CHANGES_SQL)[:limit]]
    out = []
    for r in rows:
        first = (r.get("prev_payload") is None
                 and r.get("prev_tombstone") is None)
        if r["tombstone"]:
            r["change"] = "deleted"
            r["fields"] = {}
        elif first:
            r["change"] = "first_observed"
            r["fields"] = {}
        elif r.get("prev_tombstone"):
            r["change"] = "recreated"
            r["fields"] = {}
        else:
            r["change"] = "updated"
            r["fields"] = diff_fields(r.get("prev_payload") or {},
                                      r.get("payload") or {})
        out.append(r)
    return out


def current_hash(conn, host_uid: str, entity_type: str,
                 entity_uid: str) -> str | None:
    """The current row's payload_hash per the READER'S definition, or None
    when the entity is effectively deleted (F11-valid tombstone winning
    its partition) or never seen. The write side's two questions ride this
    one door: ingest's hash gate compares against it, and its is-not-None
    reading is the tombstone dedup's answer. The DEFINITION is also the
    emitter's (REG_CURRENT_KEYS_SQL, same underlying SQL) — its bulk form
    lives beside this one in queries.py."""
    row = conn.execute(REG_CURRENT_POINT_SQL,
                       (host_uid, entity_type, entity_uid)).fetchone()
    return None if row is None else row[0]


def invalid_tombstones(conn) -> list[dict]:
    """Trust: tombstones the F11 join does not validate — the reader is
    already ignoring them; this surfaces that they exist (a scan died
    between its tombstones and its completion)."""
    return _q(conn, REG_INVALID_TOMBSTONES_SQL)


def last_scan(conn) -> dict | None:
    """The newest scan_completed declaration, detail parsed — the registry
    lane's freshness fact (None = no scan has ever completed here)."""
    rows = _q(conn,
              "SELECT occurred_at, detail FROM events WHERE kind='declaration'"
              " AND event='scan_completed' ORDER BY ingest_seq DESC LIMIT 1")
    if not rows:
        return None
    out = json.loads(rows[0]["detail"])
    if not isinstance(out, dict):
        # valid JSON is not yet a valid detail (r4, probed: '42' parsed
        # fine and then TypeError'd PAST the doors' narrow catch set —
        # the same class _require_object documents); raise the CAUGHT
        # class with a real message instead
        raise ValueError(
            f"scan_completed detail is not an object: {out!r}")
    out["occurred_at"] = rows[0]["occurred_at"]
    return out


@dataclass
class VerifyReport:
    """Projection-vs-estate drift. ok = every assembled entity matches the
    projection's hash and nothing dangles on either side."""
    drifted: list[tuple[str, str]] = field(default_factory=list)
    missing_from_db: list[tuple[str, str]] = field(default_factory=list)
    missing_from_estate: list[tuple[str, str]] = field(default_factory=list)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not (self.drifted or self.missing_from_db
                    or self.missing_from_estate)


def verify_current(conn, assembled, *, fleet: str,
                   host_uid: str | None = None) -> VerifyReport:
    """Hash-verify the current projection against a re-derived estate
    (spec: "current-state, hash-verified"). ``assembled`` is the emitter's
    ``assemble_entities`` output — injected, so the unit seam needs no
    fleet config — and hashing goes through the ingest gate's OWN pair
    (canonical_hash over _gate_view): a third hash recipe here would
    manufacture phantom drift.

    Both sides are scoped by the emitter's TRUE predicate with the
    assembly as the scanned set — exactly the domain a scan is
    authoritative for. The reader-relaxed vault rule would be wrong here:
    on a multi-fleet host it would drag a sibling fleet's vault into
    ``missing_from_estate`` as phantom drift.
    """
    from .canonical import canonical_hash
    from .ingest import _gate_view
    from .registry_emit import _in_scope

    report = VerifyReport()
    # last-wins dedupe: an assembly cannot legitimately name one alias
    # twice, but an injected duplicate must not double-count `checked` or
    # report drift beside a match (gauntlet, probed)
    by_key = {(t, a): p for t, a, p in assembled}
    scanned = set(by_key)

    def scoped(etype: str, alias: str) -> bool:
        return _in_scope(etype, alias, fleet, scanned)

    projected = {
        (r["entity_type"], r["entity_alias"]): r["payload_hash"]
        for r in _q(conn, REG_CURRENT_SQL)
        # host scoping guards the (today-unreachable) multi-host db: a
        # foreign host's partition row must not read as phantom drift
        if (host_uid is None or r["host_uid"] == host_uid)
        and scoped(r["entity_type"], r["entity_alias"])
    }
    seen: set[tuple[str, str]] = set()
    for (etype, alias), payload in by_key.items():
        if not scoped(etype, alias):
            continue
        key = (etype, alias)
        seen.add(key)
        report.checked += 1
        want = canonical_hash(_gate_view(payload))
        got = projected.get(key)
        if got is None:
            report.missing_from_db.append(key)
        elif got != want:
            report.drifted.append(key)
    report.missing_from_estate = sorted(set(projected) - seen)
    return report
