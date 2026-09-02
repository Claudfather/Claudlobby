"""Org chart — the fleet's reporting tree, a pure read over the fleet
keyframe (Phase-6 surface; F8 deferred it, the data has been recorded
since chunk A).

The fleet keyframe carries ``manager``, ``groups`` (name/manager/members)
and ``org_edges`` (``{bot, reports_to}`` per bot). This module folds the
edges into a tree: roots are bots whose ``reports_to`` is empty or names
someone outside the roster; a reporting CYCLE (a→b→a — a declaration
error the validator may not catch) is cut rather than recursed, and
disclosed, so a bad fleet.yaml can never hang the view.
"""

from __future__ import annotations

from collections import defaultdict

from . import registry_read as _rr


def org_tree(conn, fleet: str | None = None) -> dict | None:
    """The reporting tree for one fleet (the first keyframed fleet when
    ``fleet`` is None). None = no fleet keyframe yet (absent ≠ empty)."""
    available = sorted(r[0] for r in conn.execute(
        "SELECT alias FROM identity_registry WHERE kind='fleet'"
        " AND alias NOT LIKE '\\_%' ESCAPE '\\'"))
    if fleet is None:
        # deterministic: the first fleet by name, with the choice disclosed
        fleet = available[0] if available else None
        if fleet is None:
            return None
    ents = [e for e in _rr.current_entities(conn, entity_type="fleet", fleet=fleet)
            if e["entity_alias"] == fleet]
    if not ents:
        return None          # an unknown fleet is typed absent, never another fleet's tree
    ent = ents[0]
    p = ent.get("payload") or {}
    edges = [e for e in (p.get("org_edges") or [])
             if isinstance(e, dict) and e.get("bot")]
    roster = set(p.get("roster") or [e["bot"] for e in edges])
    edged = {e["bot"] for e in edges}
    children: dict[str, list] = defaultdict(list)
    for e in edges:
        if e.get("reports_to") in roster and e["bot"] not in children[e["reports_to"]]:
            children[e["reports_to"]].append(e["bot"])   # a duplicate edge lists once
    # roots: no reports_to, reports_to outside the roster, OR a roster bot
    # with no edge at all — that bot must not vanish from the chart (probed)
    roots = sorted({e["bot"] for e in edges
                    if not e.get("reports_to") or e["reports_to"] not in roster}
                   | (roster - edged))
    cycles: list[str] = []

    def node(bot: str, seen: frozenset) -> dict:
        if bot in seen:
            cycles.append(bot)
            return {"bot": bot, "reports": [], "cycle": True}
        return {"bot": bot,
                "reports": [node(c, seen | {bot})
                            for c in sorted(children.get(bot, []))]}

    tree = [node(r, frozenset()) for r in roots]
    # a pure cycle among EDGED bots (no root at all) would otherwise vanish:
    # surface it as a cycle. Roster bots with no edge are roots above, never
    # cycles (a mutant that dropped the roots clause re-added them here AS
    # cycles and stayed green — the pin now asserts cycles == [])
    reached = set()

    def walk(n):
        reached.add(n["bot"])
        for c in n["reports"]:
            walk(c)
    for n in tree:
        walk(n)
    orphaned = sorted(edged - reached)
    for b in orphaned:
        cycles.append(b)
        tree.append(node(b, frozenset({b})))
    return {"fleet": ent["entity_alias"], "manager": p.get("manager"),
            "groups": p.get("groups") or [], "roots": tree,
            "bots": len(roster | edged), "cycles": sorted(set(cycles)),
            "available": available, "last_seen": ent.get("occurred_at")}
