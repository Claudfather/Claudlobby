"""Fleet inventory + per-bot equipment (#1405; the Phase-6 equipment slice,
sharpened by the operator's wording: "a viz over the bot directory / composed
config", per bot and rolled up per fleet).

The data is ALREADY recorded: every bot keyframe carries an ``equipment``
dict (expertise, skills, mcp, guardrails, protocols, hooks, …) plus posture
and org, and every library item / project is keyframed too. The v1 rail
briefly listed all of it (208 library items flooding the participant rail),
was filtered to participants, and the operator asked for the CONTENT to get
its own room. This module is that room's query layer — a PURE read over
chunk B's F11-validated registry doors (`registry_read`), never a table,
never a scan.

Two doors:

- ``fleet_inventory`` — what is active on a fleet: bots (compact equipment
  summary), projects, and library items IN USE with a ``used_by`` rollup
  derived by joining each item's (category, name) against every bot's
  equipment list. An item no bot equips still appears (it is active in the
  library) with ``used_by=[]`` — a fact, not an omission.
- ``bot_equipment`` — what ONE bot is composed of (its current keyframe's
  equipment/posture/org/schedule/composed_hashes) plus its registry HISTORY
  as consecutive field-level diffs, so "what changed on this bot last
  Tuesday" is answerable from the SCD views without re-scanning.

Alias-first (§11): the ``short`` name rides every row; uids stay out of
the story surface.
"""

from __future__ import annotations

from . import registry_read as _rr

# equipment categories rendered on the card, in a stable operator-facing
# order (the keyframe's dict order is the emitter's business)
EQUIPMENT_KEYS = (
    "expertise", "skills", "mcp", "integrations", "guardrails", "protocols",
    "resources", "lessons", "principles", "post_actions", "tools", "plugins",
    "voice",
)


def _short(alias: str) -> str:
    # bot:<fleet>/<name> -> name ; shared/<cat>/<name> -> name ; else alias
    if alias.startswith("bot:"):
        return alias.rsplit("/", 1)[-1]
    return alias.rsplit("/", 1)[-1] if "/" in alias else alias


def _equipment_of(payload: dict) -> dict:
    eq = payload.get("equipment") or {}
    return eq if isinstance(eq, dict) else {}


def _bot_row(ent: dict) -> dict:
    p = ent.get("payload") or {}
    eq = _equipment_of(p)
    org = p.get("org") if isinstance(p.get("org"), dict) else {}
    posture = p.get("posture") if isinstance(p.get("posture"), dict) else {}
    return {
        "alias": ent["entity_alias"],
        "short": _short(ent["entity_alias"]),
        "model": p.get("model"),
        "account": p.get("account"),
        "mission": org.get("mission"),
        "reports_to": org.get("reports_to"),
        "manages": org.get("manages") or [],
        "group": org.get("group"),
        "permissions_mode": posture.get("permissions_mode"),
        "counts": {k: len(eq.get(k) or []) if isinstance(eq.get(k), list)
                   else (1 if eq.get(k) else 0) for k in EQUIPMENT_KEYS},
        "last_seen": ent.get("occurred_at"),
    }


def fleet_inventory(conn, fleet: str | None = None) -> dict:
    """What is active on a fleet (or every fleet when ``fleet`` is None)."""
    bots = _rr.current_entities(conn, entity_type="bot", fleet=fleet)
    projects = _rr.current_entities(conn, entity_type="project", fleet=fleet)
    items = _rr.current_entities(conn, entity_type="library_item",
                                 fleet=fleet)

    bot_rows = [_bot_row(b) for b in bots]
    # (category, name) -> [bot short names] — the used_by join
    equips: dict[tuple, list] = {}
    for b in bots:
        eq = _equipment_of(b.get("payload") or {})
        short = _short(b["entity_alias"])
        for cat, names in eq.items():
            if isinstance(names, list):
                for n in names:
                    equips.setdefault((cat, str(n)), []).append(short)
            elif isinstance(names, str) and names:
                equips.setdefault((cat, names), []).append(short)

    library = []
    for it in items:
        p = it.get("payload") or {}
        cat, name = p.get("category"), p.get("name")
        library.append({
            "alias": it["entity_alias"],
            "category": cat,
            "name": name,
            "tier": p.get("source_tier"),
            "title": p.get("title"),
            "used_by": sorted(equips.get((cat, name), [])),
        })
    library.sort(key=lambda r: (str(r["category"]), str(r["name"])))

    proj_rows = []
    for pr in projects:
        p = pr.get("payload") or {}
        proj_rows.append({
            "alias": pr["entity_alias"], "key": p.get("key"),
            "title": p.get("title"), "tier": p.get("tier"),
            "repos": p.get("repos") or [],
        })

    return {
        "fleet": fleet,
        "bots": sorted(bot_rows, key=lambda r: r["short"]),
        "projects": sorted(proj_rows, key=lambda r: str(r["key"])),
        "library": library,
        "counts": {"bots": len(bot_rows), "projects": len(proj_rows),
                   "library": len(library),
                   "library_in_use": sum(1 for r in library if r["used_by"])},
    }


def bot_equipment(conn, alias: str) -> dict | None:
    """One bot's composition + its change history. None when the bot has
    no current keyframe (absent ≠ empty: the caller renders panel-state)."""
    current = [e for e in _rr.current_entities(conn, entity_type="bot")
               if e["entity_alias"] == alias]
    if not current:
        return None
    ent = current[0]
    p = ent.get("payload") or {}
    eq = _equipment_of(p)
    history = _rr.entity_history(conn, alias)   # oldest..newest
    changes = []
    prev = None
    for h in history:
        hp = h.get("payload") or {}
        if prev is not None:
            d = _rr.diff_fields(prev, hp)
            if d:
                changes.append({"occurred_at": h.get("occurred_at"),
                                "scan_id": h.get("scan_id"),
                                "fields": sorted(d.keys())})
        prev = hp
    changes.reverse()   # newest first for the card
    return {
        "alias": alias, "short": _short(alias),
        "model": p.get("model"), "account": p.get("account"),
        "service": p.get("service"),
        "equipment": {k: eq.get(k) for k in EQUIPMENT_KEYS if k in eq},
        "posture": p.get("posture") or {},
        "org": p.get("org") or {},
        "schedule": p.get("schedule") or {},
        "composed_hashes": p.get("composed_hashes") or {},
        "vault_rev": p.get("vault_rev"),
        "last_seen": ent.get("occurred_at"),
        "versions": len(history),
        "changes": changes,
    }
