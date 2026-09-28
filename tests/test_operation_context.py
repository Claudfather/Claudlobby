"""Existing identities on real SQLite; no emit, lazy repair, or native runtime."""

import json
import sqlite3
from uuid import uuid4

import pytest

from claudlobby.operation_context import OperationContextError, resolve_task_context
from claudlobby.active_config import context_from_plan
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.plane.identity import resolve
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.task_operations import TaskConflictError, _identities, assign
from tests.package_fixtures import source_package
from tests.plane_setup import initialize_plane
from tests.test_task_audit import _insert


@pytest.fixture
def estate(tmp_path, monkeypatch):
    for key in ("FLEET_NAME", "CLAUDLOBBY_FLEET", "BOT_ID", "BOT_NAME", "BOT_DIR", "FLEET_ROOT"):
        monkeypatch.delenv(key, raising=False)
    path = initialize_plane(tmp_path)
    host = ensure_host_uid(tmp_path / "state")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    ids = {}
    def identity(kind, alias, parent=None):
        uid = resolve(conn, kind, alias, now="2026-09-28T00:00:00Z", parent_uid=parent)
        conn.execute("UPDATE identity_registry SET provisional=0 WHERE uid=?", (uid,))
        ids[kind, alias] = uid
        return uid
    def snapshot(kind, alias, uid, fleet_uid, payload):
        _insert(conn, "registry_snapshots", entity_type=kind, entity_alias=alias,
                entity_uid=uid, fleet_uid=fleet_uid, payload=json.dumps(payload),
                payload_hash="fixture-hash", cause="generate", scan_id="fixture-scan")
        conn.execute("UPDATE registry_snapshots SET host_uid=?", (host,))
    for name in ("origin", "target"):
        directory = tmp_path / "local" / name
        directory.mkdir(parents=True)
        (directory / "fleet.yaml").write_text(json.dumps({"fleet": {
            "name": name, "service_prefix": name, "manager": "manager", "system_defaults": False,
            "bots": {bot: {"expertise": ["software-engineering"], "channels": []}
                     for bot in ("manager", "worker")}}}))
        fleet_uid = identity("fleet", name)
        snapshot("fleet", name, fleet_uid, fleet_uid, {
            "alias": name, "service_prefix": name, "manager": "manager", "roster": ["manager", "worker"],
            "defaults_summary": {"model": "", "account": "default", "list_tier_hashes": {}},
            "vault_binding": {}, "declared_hash": "fixture", "schema_version": "1"})
        for bot in ("manager", "worker"):
            alias = f"bot:{name}/{bot}"
            # Ingest normally leaves these null; retain a real origin parent
            # on the caller to catch destination-scoped authority substitution.
            identity("actor", alias, fleet_uid if name == "origin" else None)
            instance = identity("bot_instance", alias)
            snapshot("bot", alias, instance, fleet_uid, {
                "alias": alias, "account": "default", "service": name + "." + bot, "model": "opus",
                "posture": {"permissions_mode": "plan"}, "composed_hashes": {},
                "declared_hash": "fixture", "schema_version": "1"})
    identity("actor", "human:operator", ids["fleet", "origin"])
    conn.execute("UPDATE identity_registry SET provisional=1 WHERE alias='human:operator'")
    builder = ConfigPlanBuilder(tmp_path, "r-" + "a" * 64, "b" * 64,
                                ("origin", "target"), effects={"fleet_manifests": {}, "fleet_sources": {}})
    for name in ("origin", "target"):
        directory = tmp_path / "local" / name
        manifest, projects = directory / "fleet.yaml", directory / "projects.yaml"
        builder.effects["fleet_manifests"][name] = str(manifest)
        builder.effects["fleet_sources"][name] = {
            "fleet": {"path": str(manifest), "sha256": builder.input_content(manifest)},
            "projects": {"path": str(projects), "sha256": builder.input_content(projects)},
        }
    plan = builder.seal()
    # Selection/admission has separate real-journal coverage. Keep the actual
    # frozen parser and registry here, with an explicit recorded-plan boundary.
    def active_context(*, root=None, fleet=None, bot=None, package=None):
        assert root is None or root == tmp_path
        return context_from_plan(plan, fleet, bot=bot, package=package)
    monkeypatch.setattr("claudlobby.operation_context.resolve_active_context", active_context)
    yield tmp_path, conn, ids
    conn.close()


def _generated(monkeypatch, root):
    for key, value in {"CLAUDLOBBY_ROOT": str(root), "FLEET_NAME": "origin", "BOT_ID": "manager",
                       "BOT_NAME": "Manager Display Name", "FLEET_ROOT": str(root / "local/origin"),
                       "BOT_DIR": str(root / "local/origin/runtime/bots/manager")}.items():
        monkeypatch.setenv(key, value)


def _tree(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode)
            for p in root.rglob("*") if p.is_file()}


def test_cross_fleet_destination_preserves_frozen_origin_without_writes(estate, monkeypatch):
    root, conn, ids = estate
    _generated(monkeypatch, root)
    before = _tree(root)
    ctx = resolve_task_context(fleet="target", package=source_package())
    assert ctx.context.fleet.name == "target" and ctx.fleet_uid == ids["fleet", "target"]
    assert ctx.caller.alias == "bot:origin/manager" and ctx.caller.uid == ids["actor", ctx.caller.alias]
    assert ctx.caller_fleet_uid == ids["fleet", "origin"]
    assert ctx.bots["worker"].uid == ids["actor", "bot:target/worker"]
    _identities(ctx, conn, (ctx.caller, ctx.bots["worker"]))  # Same write-boundary identity check.
    with pytest.raises(TaskConflictError, match="only within its origin fleet"):
        assign(ctx, str(uuid4()), "wi_" + "a" * 32, bot_id="worker")
    assert not (root / "state/requests").exists()
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0
    with pytest.raises(TypeError):
        ctx.bots["worker"] = ctx.caller
    local = resolve_task_context(package=source_package())
    assert local.fleet_uid == local.caller_fleet_uid == ids["fleet", "origin"]
    with pytest.raises(OperationContextError, match="origin conflicts"):
        resolve_task_context(fleet="target", operator_alias="human:operator", package=source_package())
    assert _tree(root) == before


def test_explicit_human_remains_portable_and_missing_host_is_never_minted(estate):
    root, conn, ids = estate
    before = _tree(root)
    ctx = resolve_task_context(root=root, fleet="target", operator_alias="human:operator", package=source_package())
    assert ctx.caller.uid == ids["actor", "human:operator"] and ctx.caller_fleet_uid is None
    _identities(ctx, conn, (ctx.caller, ctx.bots["worker"]))
    with pytest.raises(OperationContextError, match="identify an existing human"):
        resolve_task_context(root=root, fleet="target", package=source_package())
    with pytest.raises(OperationContextError, match="identity is missing"):
        resolve_task_context(root=root, fleet="target", operator_alias="human:unknown", package=source_package())
    assert _tree(root) == before
    (root / "state/host-uid").unlink()
    missing = _tree(root)
    with pytest.raises(OperationContextError, match="host identity"):
        resolve_task_context(root=root, fleet="target", operator_alias="human:operator", package=source_package())
    assert _tree(root) == missing


@pytest.mark.parametrize(("key", "value"), [
    ("BOT_ID", ""), ("CLAUDLOBBY_FLEET", "target"), ("BOT_DIR", "/nonexistent/wrong-bot"),
    ("CLAUDLOBBY_ROOT", ""),
])
def test_explicit_destination_does_not_bypass_malformed_origin(estate, monkeypatch, key, value):
    root, _, _ = estate
    _generated(monkeypatch, root)
    monkeypatch.setenv(key, value)
    before = _tree(root)
    with pytest.raises(ValueError):
        resolve_task_context(root=root, fleet="target", package=source_package())
    assert _tree(root) == before


@pytest.mark.parametrize("damage", ["missing", "provisional", "foreign-parent", "foreign-host", "ambiguous"])
def test_registry_uncertainty_refuses_instead_of_minting_or_selecting_latest(estate, monkeypatch, damage):
    root, conn, ids = estate
    _generated(monkeypatch, root)
    alias = "bot:target/worker"
    if damage == "missing":
        conn.execute("DELETE FROM identity_registry WHERE kind='actor' AND alias=?", (alias,))
    elif damage == "provisional":
        conn.execute("UPDATE identity_registry SET provisional=1 WHERE kind='actor' AND alias=?", (alias,))
    elif damage == "foreign-parent":
        conn.execute("UPDATE identity_registry SET parent_uid=? WHERE kind='actor' AND alias=?",
                     (ids["fleet", "origin"], alias))
    elif damage == "foreign-host":
        conn.execute("UPDATE registry_snapshots SET host_uid=? WHERE entity_alias=?", ("host_" + "f" * 32, alias))
    else:
        row = dict(conn.execute("SELECT * FROM registry_snapshots WHERE entity_alias=?", (alias,)).fetchone())
        for key in ("ingest_seq", "event_id", "schema_version", "occurred_at", "ingested_at", "host_uid", "origin", "emitter"):
            row.pop(key)
        _insert(conn, "registry_snapshots", **row)
    before = _tree(root)
    with pytest.raises(OperationContextError):
        resolve_task_context(fleet="target", package=source_package())
    assert _tree(root) == before
