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
    ents = _rr.current_entities(conn, entity_type="fleet", fleet=fleet)
    if fleet is not None:
        ents = [e for e in ents if e["entity_alias"] == fleet] or ents
    if not ents:
        return None
    ent = ents[0]
    p = ent.get("payload") or {}
    edges = [e for e in (p.get("org_edges") or [])
             if isinstance(e, dict) and e.get("bot")]
    roster = set(p.get("roster") or [e["bot"] for e in edges])
    children: dict[str, list] = defaultdict(list)
    for e in edges:
        if e.get("reports_to") in roster:
            children[e["reports_to"]].append(e["bot"])
    roots = sorted(e["bot"] for e in edges
                   if not e.get("reports_to") or e["reports_to"] not in roster)
    cycles: list[str] = []

    def node(bot: str, seen: frozenset) -> dict:
        if bot in seen:
            cycles.append(bot)
            return {"bot": bot, "reports": [], "cycle": True}
        return {"bot": bot,
                "reports": [node(c, seen | {bot})
                            for c in sorted(children.get(bot, []))]}

    tree = [node(r, frozenset()) for r in roots]
    # a pure cycle (no root at all) would otherwise vanish: surface it
    reached = set()

    def walk(n):
        reached.add(n["bot"])
        for c in n["reports"]:
            walk(c)
    for n in tree:
        walk(n)
    orphaned = sorted(set(e["bot"] for e in edges) - reached)
    for b in orphaned:
        cycles.append(b)
        tree.append(node(b, frozenset({b})))
    return {"fleet": ent["entity_alias"], "manager": p.get("manager"),
            "groups": p.get("groups") or [], "roots": tree,
            "bots": len(edges), "cycles": sorted(set(cycles)),
            "last_seen": ent.get("occurred_at")}
