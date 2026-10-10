"""Synthetic native/local authority only; no browser or live issuer proof."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
import sqlite3

import pytest

from claudlobby.plane.owner_access import AccessDenied, AccessUnavailable, OwnerAccess, _digest
from claudlobby.plane.session_assertion import SessionAttestation
from tests.test_plane_owner_workspace import local, proposed, OWNER, OTHER, USER, ORIGIN, call
from tests.test_host_owner import terminal


@pytest.fixture
def connected(local):
    access, workspace, owner, clock = local
    binding = workspace.confirm(expected_proposal=proposed(local))
    store = access.connected()
    store.initialize(expected_owner=owner)
    return store, access, workspace, binding, clock


def evidence(store, clock, challenge=None):
    c = challenge or store.begin(OWNER)
    return SessionAttestation(c.expected, int(clock[0]), int(clock[0]) + 120, USER)


def admit(store, token, binding, principal=OWNER):
    return store.authorize(token, principal, host_uid=binding.owner.host_uid, operational_origin=ORIGIN)


def test_optional_read_never_initializes_and_direct_unchanged(local):
    access, _, owner, _ = local
    direct = access.open_session(OWNER)
    before = access.path.read_bytes()
    with pytest.raises(AccessUnavailable):
        access.connected().begin(OWNER)
    assert before == access.path.read_bytes()
    assert access.authorize_read(direct.token, OWNER, host_uid=owner.host_uid) == owner


def test_no_binding_no_session_and_partial_schema_never_repairs(local):
    access, _, owner, _ = local
    store = access.connected(); store.initialize(expected_owner=owner)
    with pytest.raises(AccessDenied):
        store.begin(OWNER)
    with sqlite3.connect(access.path) as conn:
        conn.execute("DROP TABLE connected_sessions")
    with pytest.raises(AccessUnavailable):
        store.initialize(expected_owner=owner)


def test_one_use_hash_only_read_credential_no_cookie_or_action_authority(connected):
    store, access, _, binding, clock = connected
    attestation = evidence(store, clock)
    session = store.issue(attestation, OWNER)
    assert admit(store, session.token, binding) == session.admission
    with pytest.raises(AccessDenied):
        store.issue(attestation, OWNER)
    with pytest.raises(AccessDenied):
        access.authorize_read(session.token, OWNER, host_uid=binding.owner.host_uid)
    with pytest.raises(AccessDenied):
        access.authorize_message(session.token, OWNER, host_uid=binding.owner.host_uid, fleet_uid="fleet_" + "a"*32)
    with pytest.raises(AccessDenied):
        admit(store, access.open_session(OWNER).token, binding)
    with sqlite3.connect(access.path) as conn:
        row = conn.execute("SELECT digest FROM connected_sessions").fetchone()
        assert row == (_digest(session.token),)
        assert conn.execute("SELECT count(*) FROM connected_challenges").fetchone()[0] == 0
    assert session.token.encode() not in access.path.read_bytes()
    assert attestation.expected.nonce.encode() not in access.path.read_bytes()
    assert "token=" not in repr(session)


def test_competing_issue_consumes_once(connected):
    store, _, _, binding, clock = connected
    e = evidence(store, clock)
    def attempt(_):
        try:
            return store.issue(e, OWNER)
        except AccessDenied:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(r is not None for r in results) == 1
    admit(store, next(r.token for r in results if r), binding)


@pytest.mark.parametrize("change", ["principal", "origin", "host"])
def test_bound_reader_refusals(connected, change):
    store, _, _, binding, clock = connected
    session = store.issue(evidence(store, clock), OWNER)
    with pytest.raises(AccessDenied):
        store.authorize(session.token, OTHER if change == "principal" else OWNER,
            host_uid="host_" + "f"*32 if change == "host" else binding.owner.host_uid,
            operational_origin="https://wrong.example" if change == "origin" else ORIGIN)


def test_host_local_rotation_no_assertion_and_logout_only_one_session(connected):
    store, access, _, binding, clock = connected
    first = store.issue(evidence(store, clock), OWNER)
    second = store.issue(evidence(store, clock), OWNER)
    direct = access.open_session(OWNER)
    clock[0] += 2
    renewed = store.renew(first.token, OWNER, host_uid=binding.owner.host_uid, operational_origin=ORIGIN)
    with pytest.raises(AccessDenied):
        admit(store, first.token, binding)
    admit(store, renewed.token, binding)
    store.end(renewed.token, OWNER, host_uid=binding.owner.host_uid, operational_origin=ORIGIN)
    with pytest.raises(AccessDenied):
        admit(store, renewed.token, binding)
    admit(store, second.token, binding)
    access.authorize_read(direct.token, OWNER, host_uid=binding.owner.host_uid)


def test_revoked_generation_and_repaired_owner_cannot_revive(connected):
    store, access, workspace, binding, clock = connected
    session = store.issue(evidence(store, clock), OWNER)
    workspace.revoke(expected_binding=binding, expected_owner=binding.owner)
    with pytest.raises(AccessDenied):
        admit(store, session.token, binding)
    new_binding = workspace.confirm(expected_proposal=proposed((access, workspace, binding.owner, clock)))
    assert new_binding.generation != binding.generation
    with pytest.raises(AccessDenied):
        admit(store, session.token, new_binding)
    current = store.issue(evidence(store, clock), OWNER)
    access.revoke_owner(expected_revision=binding.owner.revision)
    with pytest.raises(AccessDenied):
        admit(store, current.token, new_binding)
    p = access.begin_pairing(OWNER)
    access.confirm_pairing(p.token, expected_principal=OWNER)
    with pytest.raises(AccessDenied):
        admit(store, current.token, new_binding)


@pytest.mark.parametrize("final", [1800000120.0, 1799999999.0])
def test_final_issue_time_refusal_rolls_back_nonce_and_session(connected, final):
    store, access, _, _, clock = connected
    e = evidence(store, clock)
    samples = iter([clock[0], clock[0], final])
    access._clock = lambda: next(samples)
    with pytest.raises(AccessDenied):
        store.issue(e, OWNER)
    access._clock = lambda: clock[0]
    assert store.expected(e.expected.nonce, OWNER) == e.expected
    with sqlite3.connect(access.path) as conn:
        assert conn.execute("SELECT count(*) FROM connected_sessions").fetchone()[0] == 0


def test_final_renew_time_failure_keeps_original(connected):
    store, access, _, binding, clock = connected
    s = store.issue(evidence(store, clock), OWNER)
    samples = iter([clock[0], clock[0], s.admission.expires_at])
    access._clock = lambda: next(samples)
    with pytest.raises(AccessDenied):
        store.renew(s.token, OWNER, host_uid=binding.owner.host_uid, operational_origin=ORIGIN)
    access._clock = lambda: clock[0]
    admit(store, s.token, binding)


def test_init_cli_preview_refusal_then_explicit_initialize(local, monkeypatch):
    access, _, _, _ = local
    t = terminal(monkeypatch, iter(["NO\n"]))
    assert call(access, "initialize-connected") == 1
    assert "No binding, session or action grant" in t.getvalue()
    t = terminal(monkeypatch, iter(["INITIALIZE-CONNECTED\n"]))
    assert call(access, "initialize-connected") == 0
    assert "optional connected read-session storage" in t.getvalue()


@pytest.mark.skipif(os.name != "posix", reason="controlling terminal requires POSIX")
def test_real_private_terminal_initialization_and_refusal(local):
    from tests.test_host_owner_terminal import _command
    access, _, _, _ = local
    rc, text = _command(access.root, "initialize-connected", [("Type INITIALIZE-CONNECTED", "NO")])
    assert rc == 1 and "No binding, session or action grant" in text
    rc, text = _command(access.root, "initialize-connected", [("Type INITIALIZE-CONNECTED", "INITIALIZE-CONNECTED")])
    assert rc == 0 and "storage initialized" in text
    with sqlite3.connect(access.path) as conn:
        assert conn.execute("SELECT count(*) FROM connected_sessions").fetchone()[0] == 0
