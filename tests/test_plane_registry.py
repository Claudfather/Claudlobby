"""Phase 2b battery: the registry lane — contracts, ingest semantics, the
generate-time emitter, and the F11 scan matrix.

The load-bearing laws pinned here: the HASH GATE (unchanged state writes
nothing; deterministic assembly is what makes that true), IDENTITY
CONFIRMATION (Phase 1's loop closes — observed entities stop being
provisional, and a bot confirms instance AND actor), TOMBSTONE HONESTY
(F11: only a complete enumeration may tombstone; scope is what THIS scan is
authoritative for — another fleet's bots are never "missing"), and
DORMANCY (an unarmed fleet emits nothing).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from claudlobby.plane.contracts import (
    ContractViolation,
    validate_request,
)
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.registry_emit import bot_payload, run_generate_scan


REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """The cascade reads HOME's ~/.env (host tier) — redirect it so the
    operator's real host env can never arm or disarm a test; and clear the
    process-level knobs so ambient shells cannot either."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PLANE_EMIT_ENABLED", raising=False)
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)


def _fleet_root(tmp_path: Path, *, armed: bool = True,
                workers: str = "[worker-1]",
                worker_stanza: bool = True) -> Path:
    root = tmp_path / "claudlobby"
    root.mkdir(exist_ok=True)
    # PRODUCTION-SHAPED arming (gauntlet r1: the shipped check read a tier
    # the estate does not use and every test passed anyway): the REAL
    # lib/env-tiers.sh resolver + a root-tier .env — exactly the estate's
    # arming surface.
    if not (root / "lib").exists():
        (root / "lib").symlink_to(REPO / "lib")
    if armed:
        (root / ".env").write_text("PLANE_EMIT_ENABLED=1\n")
    worker = ("\n    worker-1:\n"
              "      expertise: [software-engineering]\n"
              "      reports_to: lead\n") if worker_stanza else "\n"
    env = 'env: {PLANE_EMIT_ENABLED: "1"}' if armed else "env: {}"
    (root / "fleet.yaml").write_text(
        "fleet:\n"
        "  name: test-fleet\n"
        "  service_prefix: com.test\n"
        "  defaults:\n"
        "    model: opus\n"
        f"    {env}\n"
        "  teams:\n"
        f"    eng: {{manager: lead, workers: {workers}}}\n"
        "  bots:\n"
        "    lead:\n"
        "      expertise: [orchestration]" + worker)
    if not (root / "library").is_dir():
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "orchestration.md").write_text(
            "---\ntitle: O\n---\n# O\n")
        (root / "library" / "skills" / "probe").mkdir(parents=True)
        (root / "library" / "skills" / "probe" / "SKILL.md").write_text("body")
    (root / "state" / "plane").mkdir(parents=True, exist_ok=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    return root


def bot_stub(alias: str) -> dict:
    return {"alias": alias, "account": "default", "service": "com.x.z",
            "model": "opus", "posture": {"permissions_mode": "acceptEdits"},
            "composed_hashes": {}, "declared_hash": "dh",
            "schema_version": "1"}


def _db(root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(root / "state" / "plane" / "plane.db")
    conn.row_factory = sqlite3.Row
    return conn


def _scan(root: Path):
    fleet, _ = load_fleet(root / "fleet.yaml")
    return run_generate_scan(Paths(root=root), fleet)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

def test_snapshot_payload_validated_per_entity_type():
    with pytest.raises(ContractViolation):
        validate_request({
            "event_type": "registry_snapshot", "emitter": "t", "fleet": "f",
            "payload": {"entity_type": "bot", "entity_alias": "bot:f/x",
                        "cause": "generate", "scan_id": "s",
                        "payload": {"alias": "bot:f/x"}}})  # missing required


def test_tombstone_carries_no_payload_and_vice_versa():
    with pytest.raises(ContractViolation):
        validate_request({
            "event_type": "registry_snapshot", "emitter": "t", "fleet": "f",
            "payload": {"entity_type": "fleet", "entity_alias": "f",
                        "tombstone": True, "payload": {"x": 1},
                        "cause": "generate", "scan_id": "s"}})
    with pytest.raises(ContractViolation):
        validate_request({
            "event_type": "registry_snapshot", "emitter": "t", "fleet": "f",
            "payload": {"entity_type": "fleet", "entity_alias": "f",
                        "cause": "generate", "scan_id": "s"}})


def test_scan_completed_requires_scan_id():
    """Round-3 F11: a completion that cannot join its tombstones validates
    nothing."""
    with pytest.raises(ContractViolation):
        validate_request({
            "event_type": "declaration", "emitter": "t", "fleet": "f",
            "payload": {"event": "scan_completed", "subject_kind": "host",
                        "subject": "h", "complete": True}})


# ---------------------------------------------------------------------------
# Ingest semantics
# ---------------------------------------------------------------------------

def test_hash_gate_suppresses_unchanged_and_chains_changed(tmp_path):
    root = _fleet_root(tmp_path)
    snap = {"event_type": "registry_snapshot", "emitter": "t",
            "fleet": "test-fleet",
            "payload": {"entity_type": "project",
                        "entity_alias": "test-fleet/huntress",
                        "payload": {"key": "huntress", "title": "H",
                                    "repos": [], "tier": "review",
                                    "validation_hash": "vh",
                                    "declared_hash": "dh",
                                    "schema_version": "1"},
                        "cause": "generate", "scan_id": "s1"}}
    assert [o.status for o in emit_batch(root, [snap])] == ["committed"]
    assert [o.status for o in emit_batch(root, [dict(snap)])] == ["duplicate"]
    changed = json.loads(json.dumps(snap))
    changed["payload"]["payload"]["tier"] = "human"
    changed["payload"]["scan_id"] = "s2"
    assert [o.status for o in emit_batch(root, [changed])] == ["committed"]
    conn = _db(root)
    assert conn.execute(
        "SELECT COUNT(*) FROM registry_snapshots").fetchone()[0] == 2
    conn.close()


def test_observation_confirms_instance_and_actor(tmp_path):
    """Phase 1's identity loop closes: a bot snapshot flips BOTH the
    instance and the logical actor to provisional=0 (§18)."""
    root = _fleet_root(tmp_path)
    # a door mints the actor lazily first (provisional=1)
    emit_batch(root, [{
        "event_type": "communication", "emitter": "t", "fleet": "test-fleet",
        "payload": {"msg_id": "msg_" + "a" * 32,
                    "sender": "bot:test-fleet/lead",
                    "recipient": "bot:test-fleet/worker-1",
                    "message_class": "chat", "body": "x"}}])
    conn = _db(root)
    assert conn.execute(
        "SELECT provisional FROM identity_registry WHERE kind='actor'"
        " AND alias='bot:test-fleet/lead'").fetchone()[0] == 1
    conn.close()
    assert _scan(root)["complete"] is True
    conn = _db(root)
    rows = dict(conn.execute(
        "SELECT kind, provisional FROM identity_registry"
        " WHERE alias='bot:test-fleet/lead'").fetchall())
    conn.close()
    assert rows == {"actor": 0, "bot_instance": 0}


# ---------------------------------------------------------------------------
# The generate scan + F11 matrix
# ---------------------------------------------------------------------------

def test_unchanged_rescan_suppresses_every_keyframe(tmp_path):
    """Determinism is what the hash gate rides on: an unchanged estate
    re-scanned writes NO keyframes (only the scan_completed declaration)."""
    root = _fleet_root(tmp_path)
    s1 = _scan(root)
    assert s1["complete"] is True and s1["outcomes"].get("duplicate") is None
    s2 = _scan(root)
    assert s2["outcomes"]["duplicate"] == s2["entities"]
    assert s2["outcomes"]["committed"] == 1          # scan_completed only


def test_roster_removal_tombstones_in_scope_only(tmp_path):
    root = _fleet_root(tmp_path)
    _scan(root)
    # ANOTHER fleet's bot exists in the db (out of scope for this scan)
    emit_batch(root, [{
        "event_type": "registry_snapshot", "emitter": "t", "fleet": "other",
        "payload": {"entity_type": "bot", "entity_alias": "bot:other/zed",
                    "payload": bot_stub("bot:other/zed"),
                    "cause": "generate", "scan_id": "sx"}}])
    # remove worker-1 from the roster
    root2 = _fleet_root(tmp_path, workers="[]", worker_stanza=False)
    s = _scan(root2)
    assert s["tombstoned"] == 1
    conn = _db(root)
    stones = [r["entity_alias"] for r in conn.execute(
        "SELECT entity_alias FROM registry_snapshots WHERE tombstone=1")]
    conn.close()
    assert stones == ["bot:test-fleet/worker-1"]     # NEVER bot:other/zed


def test_incomplete_enumeration_never_tombstones(tmp_path, monkeypatch):
    """F11: a partial scan must not read absence into deletion — an
    unreadable library item marks the scan incomplete, and an incomplete
    scan emits ZERO tombstones (its scan_completed says complete=false)."""
    from claudlobby.plane import registry_emit

    root = _fleet_root(tmp_path)
    _scan(root)
    root2 = _fleet_root(tmp_path, workers="[]", worker_stanza=False)
    monkeypatch.setattr(registry_emit, "library_items",
                        lambda *a, **k: ([], 3))     # 3 items unreadable
    s = _scan(root2)
    assert s["complete"] is False
    assert s["tombstoned"] == 0
    conn = _db(root)
    stones = conn.execute(
        "SELECT COUNT(*) FROM registry_snapshots WHERE tombstone=1"
    ).fetchone()[0]
    complete_flags = [r[0] for r in conn.execute(
        "SELECT json_extract(detail, '$.complete') FROM events"
        " WHERE kind='declaration' AND event='scan_completed'"
        " ORDER BY ingest_seq")]
    conn.close()
    assert stones == 0
    assert complete_flags[-1] == 0                   # the incomplete scan says so


def test_empty_but_complete_scan_tombstones_everything_in_scope(tmp_path):
    """F11's fourth arm: an empty fleet that ENUMERATED COMPLETELY is a
    true deletion of everything it owned."""
    root = _fleet_root(tmp_path)
    _scan(root)
    root2 = _fleet_root(tmp_path, workers="[]", worker_stanza=False)
    # also drop lead: an empty roster
    text = (root2 / "fleet.yaml").read_text().replace(
        "\n    lead:\n      expertise: [orchestration]\n", "\n"
    ).replace("manager: lead", "manager: ''")
    (root2 / "fleet.yaml").write_text(text)
    s = _scan(root2)
    assert s["complete"] is True
    conn = _db(root)
    stones = {r["entity_alias"] for r in conn.execute(
        "SELECT entity_alias FROM registry_snapshots WHERE tombstone=1")}
    conn.close()
    assert "bot:test-fleet/lead" in stones
    assert "bot:test-fleet/worker-1" in stones


def test_unarmed_fleet_emits_nothing(tmp_path):
    """Dormancy (estate rule): no PLANE_EMIT_ENABLED=1 -> None, zero db."""
    root = _fleet_root(tmp_path, armed=False)
    assert _scan(root) is None
    assert not (root / "state" / "plane" / "plane.db").exists()


def test_assembly_is_deterministic(tmp_path):
    from claudlobby.plane.canonical import canonical_hash

    root = _fleet_root(tmp_path)
    fleet, _ = load_fleet(root / "fleet.yaml")
    paths = Paths(root=root)
    a = bot_payload(paths, fleet, fleet.bots["lead"], "v1")
    b = bot_payload(paths, fleet, fleet.bots["lead"], "v1")
    assert canonical_hash(a) == canonical_hash(b)


def test_scan_completed_names_scope_and_counts(tmp_path):
    root = _fleet_root(tmp_path)
    _scan(root)
    conn = _db(root)
    row = conn.execute(
        "SELECT json_extract(detail,'$.scope'),"
        " json_extract(detail,'$.counts.bot'),"
        " json_extract(detail,'$.scan_id')"
        " FROM events WHERE kind='declaration' AND event='scan_completed'"
    ).fetchone()
    conn.close()
    assert row[0] == "host+shared+fleet:test-fleet"
    assert row[1] == 2
    assert row[2]                                     # joinable to tombstones


# ---------------------------------------------------------------------------
# Gauntlet round-1 pins (three-reviewer synthesis)
# ---------------------------------------------------------------------------

def test_defaults_env_tier_does_not_arm(tmp_path):
    """THE round-1 blocker: the shipped check read fleet.defaults['env'] —
    a tier the estate does not use — so the feature was dead in production
    while every test passed. Arming resolves ONLY through the runtime's
    .env tier cascade; the dead tier is regression-locked here."""
    root = _fleet_root(tmp_path, armed=False)
    text = (root / "fleet.yaml").read_text().replace(
        "    env: {}", '    env: {PLANE_EMIT_ENABLED: "1"}')
    (root / "fleet.yaml").write_text(text)
    assert _scan(root) is None                       # defaults.env ≠ arming


def test_vaultless_fleet_never_tombstones_a_vault(tmp_path):
    """Both probing reviewers, live on the 3-fleet estate: vault
    enumeration is fleet-binding-dependent, so a vaultless fleet's COMPLETE
    scan must never tombstone another fleet's vault (the ping-pong)."""
    root = _fleet_root(tmp_path)
    emit_batch(root, [{
        "event_type": "registry_snapshot", "emitter": "t", "fleet": "other",
        "payload": {"entity_type": "vault", "entity_alias": "shared-vault",
                    "payload": {"alias": "shared-vault", "role": "primary",
                                "mount_path": "/v", "remote": "",
                                "compat": {"floor": "0.3.0"},
                                "carries_fleets": True,
                                "gitignore_safe": True,
                                "schema_version": "1"},
                    "cause": "generate", "scan_id": "sv"}}])
    s = _scan(root)                                  # this fleet has no vault
    assert s["complete"] is True
    conn = _db(root)
    stones = conn.execute(
        "SELECT COUNT(*) FROM registry_snapshots"
        " WHERE tombstone=1 AND entity_type='vault'").fetchone()[0]
    conn.close()
    assert stones == 0


def test_vault_rev_only_change_is_suppressed(tmp_path):
    """Cleanup-measured: the vault commits daily and generate runs daily —
    a rev inside the hashed bytes turned every commit into a full keyframe
    set. The gate hashes the payload MINUS vault_rev; provenance rides
    revision_seen."""
    root = _fleet_root(tmp_path)
    base = {"event_type": "registry_snapshot", "emitter": "t",
            "fleet": "test-fleet",
            "payload": {"entity_type": "fleet", "entity_alias": "revfleet",
                        "payload": {"alias": "revfleet",
                                    "service_prefix": "com.r",
                                    "manager": "m", "roster": [],
                                    "defaults_summary": {
                                        "model": "opus", "account": "d",
                                        "list_tier_hashes": {}},
                                    "vault_binding": {"vault_uid": None,
                                                      "path": ""},
                                    "declared_hash": "dh",
                                    "vault_rev": "aaa1111",
                                    "schema_version": "1"},
                        "cause": "generate", "scan_id": "s1",
                        "vault_rev": "aaa1111"}}
    assert [o.status for o in emit_batch(root, [base])] == ["committed"]
    bumped = json.loads(json.dumps(base))
    bumped["payload"]["payload"]["vault_rev"] = "bbb2222"
    bumped["payload"]["vault_rev"] = "bbb2222"
    assert [o.status for o in emit_batch(root, [bumped])] == ["duplicate"]


def test_tombstones_never_confirm_and_double_tombstone_suppresses(tmp_path):
    """Risk r1: a tombstone for a never-seen alias minted-and-confirmed a
    ghost; repeated tombstones each committed a row."""
    root = _fleet_root(tmp_path)
    stone = {"event_type": "registry_snapshot", "emitter": "t",
             "fleet": "test-fleet",
             "payload": {"entity_type": "bot",
                         "entity_alias": "bot:test-fleet/ghost",
                         "tombstone": True, "cause": "generate",
                         "scan_id": "sg"}}
    assert [o.status for o in emit_batch(root, [stone])] == ["committed"]
    conn = _db(root)
    prov = conn.execute(
        "SELECT provisional FROM identity_registry"
        " WHERE kind='bot_instance' AND alias='bot:test-fleet/ghost'"
    ).fetchone()[0]
    conn.close()
    assert prov == 1                                 # NEVER confirmed
    assert [o.status for o in emit_batch(
        root, [json.loads(json.dumps(stone))])] == ["duplicate"]


def test_compat_is_never_a_fabricated_verdict(tmp_path):
    """Cleanup r1: floor read a nonexistent attr (permanently 'unset') and
    ok was hardcoded True — a lie the hash gate would freeze forever."""
    from claudlobby.claudron_compat import COMPAT_FLOOR
    from claudlobby.plane.registry_emit import vault_payload

    root = _fleet_root(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".gitignore").write_text("runtime/\n.env\n")
    text = (root / "fleet.yaml").read_text().replace(
        "  defaults:",
        "  defaults:\n    claudron_vault_path: " + str(vault))
    (root / "fleet.yaml").write_text(text)
    fleet, _ = load_fleet(root / "fleet.yaml")
    vp = vault_payload(Paths(root=root), fleet)
    assert vp is not None
    assert vp["compat"]["ok"] is None                # no probe ran = no verdict
    assert vp["compat"]["floor"] != "unset"
    assert vp["compat"]["floor"] in {
        c.default_order_release for c in COMPAT_FLOOR}


def test_declared_fleets_sees_the_nested_vault_layout(tmp_path):
    """Cleanup-measured [] on the live nested host: the walk is
    paths._iter_fleet_dirs, never a depth-1 glob."""
    from claudlobby.plane.registry_emit import host_payload

    root = _fleet_root(tmp_path)
    nested = root / "local" / "sys" / "deep-fleet"
    nested.mkdir(parents=True)
    (nested / "fleet.yaml").write_text("fleet:\n  name: deep-fleet\n")
    hp = host_payload(Paths(root=root))
    assert "deep-fleet" in hp["declared_fleets"]


def test_float_in_project_raw_does_not_vaporize_the_scan(tmp_path):
    """Cleanup-measured: CANON_V1 refuses floats and one float in raw YAML
    aborted the WHOLE scan through the non-blocking hook — 'no registry
    ever'. _safe_hash degrades deterministically instead."""
    from claudlobby.plane.registry_emit import project_payload

    root = _fleet_root(tmp_path)
    fleet, _ = load_fleet(root / "fleet.yaml")

    class _Proj:
        key = "p"
        title = "P"
        repos: list = []
        mission_file = None
        validation = None
        raw = {"validation": {"threshold": 0.8}}
    p = project_payload(Paths(root=root), fleet, _Proj(), None)
    assert p["validation_hash"].startswith("sha256:")


def test_unknown_metric_warns_but_commits(tmp_path, capsys):
    root = _fleet_root(tmp_path)
    out = emit_batch(root, [{
        "event_type": "metric_sample", "emitter": "t", "fleet": "test-fleet",
        "payload": {"subject_kind": "host", "subject": "h",
                    "metric": "made.up.metric", "value": 1}}])
    assert [o.status for o in out] == ["committed"]
    assert "unknown metric" in capsys.readouterr().err


def test_library_walk_discovers_integrations(tmp_path):
    """Cleanup r1: the hand-list missed integrations (never keyframed,
    never tombstonable) and scanned a nonexistent voices dir — categories
    are DISCOVERED from the dirs that exist."""
    root = _fleet_root(tmp_path)
    integ = root / "library" / "integrations"
    integ.mkdir(parents=True)
    (integ / "github-app.md").write_text("# integration\n")
    _scan(root)
    conn = _db(root)
    aliases = [r["entity_alias"] for r in conn.execute(
        "SELECT entity_alias FROM registry_snapshots"
        " WHERE entity_type='library_item'")]
    conn.close()
    assert "shared/integrations/github-app" in aliases
