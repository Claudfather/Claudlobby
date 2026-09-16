"""Fleet inventory + per-bot equipment (#1405; the Phase-6 equipment slice,
sharpened by the operator's wording: "a viz over the bot directory / composed
config", per bot and rolled up per fleet).

The data is ALREADY recorded: every bot keyframe carries an ``equipment``
dict (expertise, skills, mcp, guardrails, protocols, …) plus posture
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


def short_alias(alias: str | None) -> str | None:
    """THE alias-first presentation (§11), one definition (M-A fold, F14 —
    there were four): `bot:<fleet>/<name>` -> name, `shared/<cat>/<name>` ->
    name, `human:chris` -> chris (#1402: the operator renders by name, the
    same grammar as every bot). A bare alias has no `/` and no `human:` and
    comes back whole; a falsy alias comes back as it arrived, so a caller can
    still tell None from "". The full alias stays in the payload."""
    if not alias:
        return alias
    if "/" in alias:
        return alias.rsplit("/", 1)[-1]
    if alias.startswith("human:"):
        return alias[len("human:"):] or alias
    return alias


_short = short_alias


def fleet_of(alias: str) -> str | None:
    """`bot:<fleet>/<name>` -> fleet; None for anything else (a human, a
    bare alias, a library item). THE Python spelling of the fleet axis —
    queries.fleet_alias_range is the SQL one; nothing else re-parses it."""
    if alias.startswith("bot:") and "/" in alias:
        return alias[4:].rsplit("/", 1)[0]
    return None


_fleet_of = fleet_of


def qualified(alias: str | None) -> str | None:
    """`bot:<fleet>/<name>` -> `fleet/name`; anything without a fleet keeps
    its short form — there is no fleet to qualify it by."""
    if not alias:
        return alias
    fleet = fleet_of(alias)
    return f"{fleet}/{_short(alias)}" if fleet else _short(alias)


def qualified_labels(aliases) -> dict[str, str]:
    """alias -> presentation label wherever two fleets MEET (U1/U2): when
    the aliases in a read span more than one fleet, every bot reads
    ``fleet/name``, so the reader always knows which fleet a card, a task
    or a message belongs to; a read that holds one fleet only (a room, or
    a single-fleet host — most installs) keeps the caller's short names,
    which are unambiguous there. Only aliases WITH a fleet
    are in the result: a human, a bare alias, a library item keep the
    caller's short form (a human has no fleet to qualify by, and the view's
    `_short` strips `human:` where this module's does not). Qualifying
    everything, not only twins, is the rule: a bare name among qualified
    ones cannot say whether it is unique or simply un-fleeted, and the #526
    collision (an `erlich` on each fleet) is covered by construction."""
    uniq = [a for a in dict.fromkeys(a for a in aliases if a) if fleet_of(a)]
    if len({fleet_of(a) for a in uniq}) < 2:
        return {}   # one fleet in the read: bare names are unambiguous
    return {a: qualified(a) for a in uniq}


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
    # A short name that two fleets both use is qualified as fleet/name in
    # every label (`qualified_labels`, the one definition), so an all-fleets
    # read never shows two indistinguishable cards or attributes one bot's
    # equipment to its twin (gauntlet, probed).
    # only a read that SPANS fleets qualifies (a room shows bare names)
    labels = {} if fleet else qualified_labels(b["entity_alias"] for b in bots)

    def _label(alias: str) -> str:
        return labels.get(alias) or _short(alias)
    for r in bot_rows:
        r["short"] = _label(r["alias"])
    # (category, name) -> [bot labels] — the used_by join
    equips: dict[tuple, list] = {}
    for b in bots:
        eq = _equipment_of(b.get("payload") or {})
        short = _label(b["entity_alias"])
        for cat, names in eq.items():
            if isinstance(names, list):
                for n in names:
                    equips.setdefault((cat, str(n)), []).append(short)
            elif isinstance(names, str) and names:
                equips.setdefault((cat, names), []).append(short)

    # library_search_dirs is overlay-first: when a fleet overlay carries the
    # same (category, name) as a shared item, only the OVERLAY is composed.
    # The shared copy must not read "in use" or the operator keeps a dead
    # file (gauntlet, probed): it is marked shadowed with used_by=[].
    overlay_keys = {(p.get("category"), p.get("name"))
                    for p in ((it.get("payload") or {}) for it in items)
                    if p.get("source_tier") not in (None, "shared")}
    library = []
    for it in items:
        p = it.get("payload") or {}
        cat, name = p.get("category"), p.get("name")
        shadowed = (p.get("source_tier") == "shared"
                    and (cat, name) in overlay_keys)
        library.append({
            "alias": it["entity_alias"],
            "category": cat,
            "name": name,
            "tier": p.get("source_tier"),
            "title": p.get("title"),
            "shadowed": shadowed,
            "used_by": [] if shadowed else sorted(equips.get((cat, name), [])),
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
    # The history door aliases the row's instant as valid_from (SCD2), NOT
    # occurred_at — reading the wrong key rendered every change with an
    # empty timestamp (gauntlet SEV-2: "what changed last Tuesday" needs
    # the when). A tombstone transition is TYPED deleted/recreated rather
    # than rendered as a 10-field storm — mirroring recent_changes.
    changes = []
    prev, prev_tomb = None, None
    for h in history:
        hp = h.get("payload") or {}
        tomb = bool(h.get("tombstone"))
        when, sid = h.get("valid_from"), h.get("scan_id")
        if prev is not None:
            if tomb and not prev_tomb:
                changes.append({"occurred_at": when, "scan_id": sid,
                                "kind": "deleted", "fields": []})
            elif prev_tomb and not tomb:
                changes.append({"occurred_at": when, "scan_id": sid,
                                "kind": "recreated", "fields": []})
            elif not tomb:
                d = _rr.diff_fields(prev, hp)
                if d:
                    changes.append({"occurred_at": when, "scan_id": sid,
                                    "kind": "updated",
                                    "fields": sorted(d.keys())})
        prev, prev_tomb = hp, tomb
    changes.reverse()   # newest first for the card
    live_versions = sum(1 for h in history if not h.get("tombstone"))
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
        "versions": live_versions,
        "changes": changes,
    }
