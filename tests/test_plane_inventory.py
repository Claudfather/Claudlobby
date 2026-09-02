"""Fleet inventory + per-bot equipment (#1405) — the query layer and its two
endpoints, over a REAL registry scan ingested through emit_batch.

Laws pinned: the inventory is a pure read over chunk B's F11-validated
doors (a tombstoned bot is gone); ``used_by`` is derived by joining each
library item's (category, name) against every bot's equipment; an item no
bot equips still appears with used_by=[] (a fact, not an omission); the
equipment door returns the current composition plus consecutive field-
level diffs across the bot's history; a bot with no keyframe is a TYPED
idle state, never a blank {}.
"""

from __future__ import annotations

from pathlib import Path

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.inventory import bot_equipment, fleet_inventory

FLEET = "f"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    return root


def _bot(alias, *, skills, mcp=(), guardrails=(), reports_to=None,
         model="opus", scan="s1", mission=None):
    return {"event_type": "registry_snapshot", "emitter": "t", "fleet": FLEET,
            "payload": {"entity_type": "bot", "entity_alias": alias,
                        "cause": "generate", "scan_id": scan,
                        "payload": {
                            "alias": alias, "account": "a", "service": "svc",
                            "model": model,
                            "equipment": {"skills": list(skills),
                                          "mcp": list(mcp),
                                          "guardrails": list(guardrails)},
                            "posture": {"permissions_mode": "plan",
                                        "tool_allow": ["Read"],
                                        "tool_deny": []},
                            "org": {"reports_to": reports_to, "manages": [],
                                    "mission": mission, "group": None},
                            "composed_hashes": {"CLAUDE.md": "h1"},
                            "declared_hash": "d", "schema_version": "1"}}}


def _lib(category, name, scan="s1"):
    alias = f"shared/{category}/{name}"
    return {"event_type": "registry_snapshot", "emitter": "t", "fleet": FLEET,
            "payload": {"entity_type": "library_item", "entity_alias": alias,
                        "cause": "generate", "scan_id": scan,
                        "payload": {"category": category, "name": name,
                                    "source_tier": "shared",
                                    "content_hash": "c", "title": None,
                                    "description": None,
                                    "declared_hash": "d",
                                    "schema_version": "1"}}}


def _proj(key, title, scan="s1"):
    return {"event_type": "registry_snapshot", "emitter": "t", "fleet": FLEET,
            "payload": {"entity_type": "project",
                        "entity_alias": f"{FLEET}/{key}",
                        "cause": "generate", "scan_id": scan,
                        # tier is a closed enum + validation_hash required
                        # (ProjectPayload contract; matches the live capture)
                        "payload": {"key": key, "title": title,
                                    "repos": ["r1", "r2"], "tier": "review",
                                    "validation_hash": "v",
                                    "declared_hash": "d",
                                    "schema_version": "1"}}}


def _done(scan="s1", complete=True):
    # the REAL scan_completed shape (copied from the registry_read fixture,
    # not reconstructed — a from-memory version failed the Declaration
    # contract, the fixtures-certifying-non-reality class)
    return {"event_type": "declaration", "emitter": "t", "fleet": FLEET,
            "payload": {"event": "scan_completed", "subject_kind": "host",
                        "subject": "h1", "scan_id": scan, "scope": FLEET,
                        "counts": {}, "complete": complete}}


def _seed(root):
    emit_batch(root, [
        _bot(f"bot:{FLEET}/erlich", skills=("dispatch", "pulse"),
             mcp=("github",), guardrails=("no-push-main",),
             mission="run the fleet"),
        _bot(f"bot:{FLEET}/dinesh", skills=("pulse",),
             reports_to="erlich"),
        _lib("skills", "dispatch"), _lib("skills", "pulse"),
        _lib("skills", "orphan-skill"),          # nobody equips it
        _lib("mcp", "github"), _lib("guardrails", "no-push-main"),
        _proj("surfaces", "Product surfaces"),
        _done(),
    ])


def test_inventory_rolls_up_bots_projects_and_library_in_use(tmp_path):
    root = _root(tmp_path)
    _seed(root)
    conn = connect(db_path(root))
    try:
        inv = fleet_inventory(conn, FLEET)
    finally:
        conn.close()
    assert [b["short"] for b in inv["bots"]] == ["dinesh", "erlich"]
    erlich = next(b for b in inv["bots"] if b["short"] == "erlich")
    assert erlich["counts"]["skills"] == 2
    assert erlich["counts"]["mcp"] == 1
    assert erlich["mission"] == "run the fleet"
    dinesh = next(b for b in inv["bots"] if b["short"] == "dinesh")
    assert dinesh["reports_to"] == "erlich"
    assert [p["key"] for p in inv["projects"]] == ["surfaces"]
    lib = {(r["category"], r["name"]): r["used_by"] for r in inv["library"]}
    assert lib[("skills", "pulse")] == ["dinesh", "erlich"]   # both equip it
    assert lib[("skills", "dispatch")] == ["erlich"]
    assert lib[("skills", "orphan-skill")] == []              # present, unused
    assert inv["counts"] == {"bots": 2, "projects": 1, "library": 5,
                             "library_in_use": 4}


def test_a_tombstoned_bot_leaves_the_inventory(tmp_path):
    """F11: the inventory reads the validated current set — a bot
    tombstoned by a COMPLETE scan is gone."""
    root = _root(tmp_path)
    _seed(root)
    emit_batch(root, [
        {"event_type": "registry_snapshot", "emitter": "t", "fleet": FLEET,
         "payload": {"entity_type": "bot", "entity_alias": f"bot:{FLEET}/dinesh",
                     "cause": "generate", "scan_id": "s2", "tombstone": True}},
        _bot(f"bot:{FLEET}/erlich", skills=("dispatch", "pulse"),
             mcp=("github",), guardrails=("no-push-main",), scan="s2"),
        _done("s2"),
    ])
    conn = connect(db_path(root))
    try:
        inv = fleet_inventory(conn, FLEET)
    finally:
        conn.close()
    assert [b["short"] for b in inv["bots"]] == ["erlich"]


def test_equipment_detail_carries_composition_and_history_diffs(tmp_path):
    root = _root(tmp_path)
    _seed(root)
    # a second scan: erlich gains a skill and drops a guardrail
    emit_batch(root, [
        _bot(f"bot:{FLEET}/erlich", skills=("dispatch", "pulse", "restart"),
             mcp=("github",), guardrails=(), scan="s2",
             mission="run the fleet"),
        _done("s2"),
    ])
    conn = connect(db_path(root))
    try:
        eq = bot_equipment(conn, f"bot:{FLEET}/erlich")
        assert bot_equipment(conn, f"bot:{FLEET}/nobody") is None
    finally:
        conn.close()
    assert eq["short"] == "erlich"
    assert eq["equipment"]["skills"] == ["dispatch", "pulse", "restart"]
    assert eq["equipment"]["guardrails"] == []
    assert eq["posture"]["permissions_mode"] == "plan"
    assert eq["org"]["mission"] == "run the fleet"
    assert eq["versions"] == 2
    assert len(eq["changes"]) == 1
    changed = eq["changes"][0]["fields"]
    assert any("skills" in f for f in changed)
    assert any("guardrails" in f for f in changed)


def test_inventory_and_equipment_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app

    root = _root(tmp_path)
    _seed(root)
    client = TestClient(create_app(root))
    inv = client.get("/api/inventory", params={"fleet": FLEET}).json()
    assert inv["state"] == "ok"
    assert inv["data"]["counts"]["bots"] == 2
    eq = client.get("/api/equipment",
                    params={"alias": f"bot:{FLEET}/erlich"}).json()
    assert eq["state"] == "ok"
    assert eq["data"]["equipment"]["mcp"] == ["github"]
    # absent ≠ empty: an unknown bot is a TYPED state with a remedy
    miss = client.get("/api/equipment",
                      params={"alias": f"bot:{FLEET}/nobody"}).json()
    assert miss["state"] == "idle"
    assert "generate" in miss["remediation"]
    assert "data" not in miss


def test_absent_db_is_typed_never_zero(tmp_path):
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app

    root = tmp_path / "empty-root"
    root.mkdir()
    body = TestClient(create_app(root)).get("/api/inventory").json()
    assert body["state"] == "absent"
    assert "data" not in body
