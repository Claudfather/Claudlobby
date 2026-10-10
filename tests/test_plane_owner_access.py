"""Policy/state tests with synthetic identities, not Tailscale authentication."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading

import pytest

from claudlobby.plane.ids import ensure_host_uid, read_host_uid
from claudlobby.plane.owner_access import (
    AccessDenied, AccessUnavailable, MAX_PENDING, MAX_SESSIONS, OwnerAccess,
    OwnerMessageGrant,
    PAIRING_SECONDS, PrincipalRef, SESSION_SECONDS,
)
from tests.conftest import constructed_env

OWNER = PrincipalRef("test-verifier", "human-001")
OTHER = PrincipalRef("test-verifier", "human-002")
FLEET = "fleet_" + "1" * 32
OTHER_FLEET = "fleet_" + "2" * 32
ACTOR = "actor_" + "1" * 32
OTHER_ACTOR = "actor_" + "2" * 32
ALIAS = "human:owner"


@pytest.fixture
def access(tmp_path):
    ensure_host_uid(tmp_path / "state")
    clock = [1_800_000_000.0]
    store = OwnerAccess.initialize(tmp_path, clock=lambda: clock[0])
    return store, clock


def pair(store, principal=OWNER):
    challenge = store.begin_pairing(principal)
    return store.confirm_pairing(challenge.token, expected_principal=principal)


def admitted(store, session, principal=OWNER):
    return store.authorize_read(session.token, principal, host_uid=session.grant.host_uid)


def allow_messages(store, owner):
    return store.allow_messages(expected_owner=owner, fleet_uid=FLEET,
                                actor_uid=ACTOR, actor_alias=ALIAS)


def test_unprepared_reads_and_initialization_do_not_mint_host_identity(tmp_path):
    store = OwnerAccess(tmp_path)
    with pytest.raises(AccessUnavailable):
        store.current_grant()
    with pytest.raises(AccessUnavailable):
        OwnerAccess.initialize(tmp_path)
    assert not list(tmp_path.iterdir())
    ensure_host_uid(tmp_path / "state")
    with pytest.raises(AccessUnavailable):
        store.current_grant()
    assert not (tmp_path / "state/plane").exists()


def test_local_confirmation_is_required_and_state_is_separate(access):
    store, _ = access
    challenge = store.begin_pairing(OWNER)
    assert store.current_grant() is None
    with pytest.raises(AccessDenied, match="owner_not_paired"):
        store.open_session(OWNER)
    grant = store.confirm_pairing(challenge.token, expected_principal=OWNER)
    session = store.open_session(OWNER)
    assert grant.active and grant.revision == 1
    assert admitted(store, session) == grant
    assert not (store.path.parent / "plane.db").exists()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_initialization_failure_cannot_publish_partial_authority(tmp_path, monkeypatch):
    ensure_host_uid(tmp_path / "state")
    connect = sqlite3.connect

    class Interrupted(sqlite3.Connection):
        def executescript(self, sql):
            super().executescript(sql)
            raise sqlite3.OperationalError("simulated setup interruption")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=Interrupted))
        with pytest.raises(AccessUnavailable):
            OwnerAccess.initialize(tmp_path)
    assert not (tmp_path / "state/plane/owner-access.db").exists()
    assert OwnerAccess.initialize(tmp_path).current_grant() is None


@pytest.mark.parametrize("directory_exists", [False, True])
def test_initialization_syncs_authority_directory_and_its_parent(tmp_path, monkeypatch,
                                                               directory_exists):
    state = tmp_path / "state"
    ensure_host_uid(state)
    plane = state / "plane"
    if directory_exists:
        plane.mkdir(mode=0o700)
    synced = []
    fsync = os.fsync

    def record(fd):
        synced.append(os.fstat(fd).st_ino)
        fsync(fd)

    monkeypatch.setattr(os, "fsync", record)
    OwnerAccess.initialize(tmp_path)
    assert state.stat().st_ino in synced, "the new plane entry must be durable in state"
    assert plane.stat().st_ino in synced, "the published authority entry must be durable"


def test_concurrent_initializers_publish_one_complete_store(tmp_path, monkeypatch):
    ensure_host_uid(tmp_path / "state")
    barrier = threading.Barrier(2)
    parent_inode = (tmp_path / "state").stat().st_ino
    synced_by = set()
    fsync = os.fsync

    def record(fd):
        if os.fstat(fd).st_ino == parent_inode:
            synced_by.add(threading.get_ident())
        fsync(fd)

    monkeypatch.setattr(os, "fsync", record)

    def initialize(_):
        barrier.wait(timeout=5)
        return OwnerAccess.initialize(tmp_path)

    with ThreadPoolExecutor(2) as pool:
        one, two = pool.map(initialize, range(2))
    assert one.current_grant() is None and two.current_grant() is None
    assert len(synced_by) == 2, "both initializers must sync the authority directory's parent"
    grant = pair(one)
    assert two.current_grant() == grant
    assert [p.name for p in one.path.parent.iterdir()] == ["owner-access.db"]


def test_pairing_requires_exact_principal_and_is_single_use(access):
    store, _ = access
    challenge = store.begin_pairing(OWNER)
    with pytest.raises(AccessDenied, match="pairing_unavailable"):
        store.confirm_pairing(challenge.token, expected_principal=OTHER)
    grant = store.confirm_pairing(challenge.token, expected_principal=OWNER)
    with pytest.raises(AccessDenied, match="pairing_unavailable"):
        store.confirm_pairing(challenge.token, expected_principal=OWNER)
    with pytest.raises(AccessDenied, match="owner_already_paired"):
        store.begin_pairing(OTHER)
    assert store.current_grant() == grant


@pytest.mark.parametrize("offset", [PAIRING_SECONDS, PAIRING_SECONDS + 1, -1])
def test_expired_or_future_pairing_is_refused(access, offset):
    store, clock = access
    challenge = store.begin_pairing(OWNER)
    clock[0] += offset
    with pytest.raises(AccessDenied, match="pairing_unavailable"):
        store.confirm_pairing(challenge.token, expected_principal=OWNER)
    assert store.current_grant() is None


def test_pairing_does_not_cross_hosts(access, tmp_path):
    store, _ = access
    challenge = store.begin_pairing(OWNER)
    other_root = tmp_path / "other"
    ensure_host_uid(other_root / "state")
    other = OwnerAccess.initialize(other_root)
    with pytest.raises(AccessDenied, match="pairing_unavailable"):
        other.confirm_pairing(challenge.token, expected_principal=OWNER)


def test_competing_confirmations_have_one_winner(access):
    store, _ = access
    candidates = [(store.begin_pairing(principal), principal) for principal in (OWNER, OTHER)]
    barrier = threading.Barrier(2)

    def confirm(item):
        challenge, principal = item
        barrier.wait(timeout=5)
        try:
            return store.confirm_pairing(challenge.token, expected_principal=principal)
        except AccessDenied:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(confirm, candidates))
    winners = [grant for grant in results if grant is not None]
    assert len(winners) == 1 and winners[0].revision == 1
    assert store.current_grant() == winners[0]


def test_read_requires_token_current_principal_and_target_deployment(access, tmp_path):
    store, _ = access
    pair(store)
    session = store.open_session(OWNER)
    for principal in (OTHER, PrincipalRef("other-verifier", OWNER.subject)):
        with pytest.raises(AccessDenied):
            admitted(store, session, principal)
        with pytest.raises(AccessDenied):
            store.renew_session(session.token, principal)
        with pytest.raises(AccessDenied):
            store.open_session(principal)
    with pytest.raises(AccessDenied, match="wrong_deployment"):
        store.authorize_read(session.token, OWNER, host_uid="host_" + "0" * 32)
    other_root = tmp_path / "other"
    ensure_host_uid(other_root / "state")
    other = OwnerAccess.initialize(other_root)
    other_grant = pair(other)
    with pytest.raises(AccessDenied, match="session_unavailable"):
        other.authorize_read(session.token, OWNER, host_uid=other_grant.host_uid)


@pytest.mark.parametrize("offset", [SESSION_SECONDS, SESSION_SECONDS + 1, -1])
def test_expired_or_future_session_cannot_read_or_renew(access, offset):
    store, clock = access
    pair(store)
    session = store.open_session(OWNER)
    clock[0] += offset
    with pytest.raises(AccessDenied, match="session_unavailable"):
        admitted(store, session)
    with pytest.raises(AccessDenied, match="session_unavailable"):
        store.renew_session(session.token, OWNER)


def test_expired_session_does_not_revoke_pairing(access):
    store, clock = access
    grant = pair(store)
    old = store.open_session(OWNER)
    clock[0] += SESSION_SECONDS
    fresh = store.open_session(OWNER)
    assert admitted(store, fresh) == grant
    with pytest.raises(AccessDenied):
        admitted(store, old)


def test_renewal_rotates_and_old_token_cannot_be_replayed(access):
    store, clock = access
    pair(store)
    old = store.open_session(OWNER)
    clock[0] += 30
    new = store.renew_session(old.token, OWNER)
    assert new.token != old.token and new.expires_at > old.expires_at
    assert admitted(store, new).active
    with pytest.raises(AccessDenied):
        admitted(store, old)
    with pytest.raises(AccessDenied):
        store.renew_session(old.token, OWNER)


def test_concurrent_renewal_cannot_duplicate_session(access):
    store, _ = access
    pair(store)
    session = store.open_session(OWNER)
    barrier = threading.Barrier(2)

    def renew(_):
        barrier.wait(timeout=5)
        try:
            return store.renew_session(session.token, OWNER)
        except AccessDenied:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(renew, range(2)))
    assert len([result for result in results if result is not None]) == 1
    with pytest.raises(AccessDenied):
        admitted(store, session)


def test_signout_ends_only_its_session_and_not_pairing(access):
    store, _ = access
    grant = pair(store)
    one, two = store.open_session(OWNER), store.open_session(OWNER)
    with pytest.raises(AccessDenied):
        store.end_session(two.token, OTHER)
    store.end_session(one.token, OWNER)
    with pytest.raises(AccessDenied):
        admitted(store, one)
    assert admitted(store, two) == grant
    assert admitted(store, store.open_session(OWNER)) == grant


def test_applied_revoke_blocks_existing_sessions_renewal_and_fresh_login(access):
    store, clock = access
    grant = pair(store)
    session = store.open_session(OWNER)
    reader = OwnerAccess(store.root, clock=lambda: clock[0])
    assert admitted(reader, session) == grant
    removed = store.revoke_owner(expected_revision=grant.revision)
    assert not removed.active and removed.revision == grant.revision + 1
    with pytest.raises(AccessDenied):
        admitted(reader, session)
    with pytest.raises(AccessDenied):
        reader.renew_session(session.token, OWNER)
    with pytest.raises(AccessDenied):
        reader.open_session(OWNER)


def test_repairing_never_revives_old_credentials_or_stale_revoke(access):
    store, _ = access
    old_challenge = store.begin_pairing(OWNER)
    first = store.confirm_pairing(old_challenge.token, expected_principal=OWNER)
    old_session = store.open_session(OWNER)
    store.revoke_owner(expected_revision=first.revision)
    third = pair(store, OTHER)
    assert third.revision == 3
    with pytest.raises(AccessDenied, match="grant_changed"):
        store.revoke_owner(expected_revision=first.revision)
    with pytest.raises(AccessDenied):
        store.confirm_pairing(old_challenge.token, expected_principal=OWNER)
    with pytest.raises(AccessDenied):
        admitted(store, old_session, OTHER)
    assert admitted(store, store.open_session(OTHER), OTHER) == third
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT revision, active FROM grants ORDER BY revision").fetchall() == [
            (1, 1), (2, 0), (3, 1),
        ]


def test_tokens_are_hashed_and_not_in_reprs_or_errors(access):
    store, _ = access
    challenge = store.begin_pairing(OWNER)
    store.confirm_pairing(challenge.token, expected_principal=OWNER)
    session = store.open_session(OWNER)
    assert challenge.token not in repr(challenge)
    assert session.token not in repr(session)
    for path in store.path.parent.iterdir():
        if path.is_file():
            data = path.read_bytes()
            assert challenge.token.encode() not in data and session.token.encode() not in data
    with pytest.raises(AccessDenied) as refused:
        store.confirm_pairing(session.token, expected_principal=OWNER)
    assert session.token not in str(refused.value)
    with pytest.raises(AccessDenied):
        store.authorize_read(challenge.token, OWNER, host_uid=session.grant.host_uid)


def test_admission_reads_do_not_change_authority(access):
    store, _ = access
    grant = pair(store)
    session = store.open_session(OWNER)
    before = store.path.read_bytes()
    assert admitted(store, session) == grant
    with pytest.raises(AccessDenied):
        admitted(store, session, OTHER)
    assert store.path.read_bytes() == before


def test_message_grants_are_absent_until_explicit_local_allow(access):
    store, _ = access
    owner = pair(store)
    session = store.open_session(OWNER)
    before = store.path.read_bytes()
    with pytest.raises(AccessDenied, match="messages_not_allowed"):
        store.authorize_message(session.token, OWNER, host_uid=owner.host_uid,
                                fleet_uid=FLEET)
    assert store.path.read_bytes() == before
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'message_grants'").fetchone() is None
    assert admitted(store, session) == owner


def test_explicit_message_allow_is_durable_fleet_scoped_and_revocable(access):
    store, clock = access
    owner = pair(store)
    session = store.open_session(OWNER)
    approved = allow_messages(store, owner)
    assert approved == OwnerMessageGrant(owner, FLEET, ACTOR, ALIAS, approved.generation)
    assert len(approved.generation) == 64
    assert allow_messages(store, owner) == approved
    reopened = OwnerAccess(store.root, clock=lambda: clock[0])
    assert reopened.authorize_message(session.token, OWNER, host_uid=owner.host_uid,
                                      fleet_uid=FLEET) == approved
    with pytest.raises(AccessDenied, match="messages_not_allowed"):
        reopened.authorize_message(session.token, OWNER, host_uid=owner.host_uid,
                                   fleet_uid=OTHER_FLEET)
    before = store.path.read_bytes()
    assert reopened.authorize_message(session.token, OWNER, host_uid=owner.host_uid,
                                      fleet_uid=FLEET) == approved
    assert store.path.read_bytes() == before
    store.revoke_messages(expected_owner=owner, fleet_uid=FLEET)
    with pytest.raises(AccessDenied, match="messages_not_allowed"):
        reopened.authorize_message(session.token, OWNER, host_uid=owner.host_uid,
                                   fleet_uid=FLEET)
    replacement = allow_messages(store, owner)
    assert replacement.generation != approved.generation
    with pytest.raises(AccessDenied, match="message_binding_changed"):
        store.revoke_messages(expected_owner=owner, fleet_uid=FLEET, expected_grant=approved)
    assert store.current_message_grant(expected_owner=owner, fleet_uid=FLEET) == replacement


def legacy_message_grant(store, owner):
    with sqlite3.connect(store.path) as conn:
        conn.execute("CREATE TABLE message_grants (owner_revision INTEGER NOT NULL REFERENCES grants(revision), "
                     "fleet_uid TEXT NOT NULL, actor_uid TEXT NOT NULL, actor_alias TEXT NOT NULL, "
                     "PRIMARY KEY(owner_revision, fleet_uid))")
        conn.execute("INSERT INTO message_grants VALUES (?, ?, ?, ?)",
                     (owner.revision, FLEET, ACTOR, ALIAS))


def test_legacy_generation_is_read_only_then_migrated_without_rebinding_other_fleets(access):
    store, _ = access
    owner = pair(store)
    session = store.open_session(OWNER)
    legacy_message_grant(store, owner)
    before = store.path.read_bytes()
    old = store.authorize_message(session.token, OWNER, host_uid=owner.host_uid, fleet_uid=FLEET)
    assert old.generation.startswith("legacy_")
    assert store.local_status() == (owner, (old,))
    assert store.path.read_bytes() == before
    new = store.allow_messages(expected_owner=owner, fleet_uid=OTHER_FLEET,
                               actor_uid=OTHER_ACTOR, actor_alias="human:other")
    assert not new.generation.startswith("legacy_")
    assert store.current_message_grant(expected_owner=owner, fleet_uid=FLEET) == old
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT generation FROM message_grants WHERE fleet_uid = ?", (FLEET,)).fetchone()[0] == old.generation
    store.revoke_messages(expected_owner=owner, fleet_uid=FLEET, expected_grant=old)
    assert allow_messages(store, owner).generation != old.generation


def test_legacy_generation_migration_failure_rolls_back_without_partial_schema(access, monkeypatch):
    store, _ = access
    owner = pair(store)
    legacy_message_grant(store, owner)
    before = store.path.read_bytes()
    connect = sqlite3.connect

    class Interrupted(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("UPDATE message_grants SET generation"):
                raise sqlite3.OperationalError("simulated migration interruption")
            return super().execute(sql, *args)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=Interrupted))
        with pytest.raises(AccessUnavailable):
            allow_messages(store, owner)
    assert store.path.read_bytes() == before
    with sqlite3.connect(store.path) as conn:
        assert "generation" not in {row[1] for row in conn.execute("PRAGMA table_info(message_grants)")}
    assert allow_messages(store, owner).generation.startswith("legacy_")


@pytest.mark.parametrize("generation", [None, "", "invalid", "0" * 63])
def test_damaged_generation_is_not_repaired_or_admitted(access, generation):
    store, _ = access
    owner = pair(store)
    legacy_message_grant(store, owner)
    allow_messages(store, owner)  # legacy migration permits SQLite NULL; admission never does
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE message_grants SET generation = ?", (generation,))
    before = store.path.read_bytes()
    with pytest.raises(AccessUnavailable, match="generation"):
        store.current_message_grant(expected_owner=owner, fleet_uid=FLEET)
    with pytest.raises(AccessUnavailable, match="generation"):
        allow_messages(store, owner)
    assert store.path.read_bytes() == before


def test_message_binding_is_immutable_until_explicit_revoke(access):
    store, _ = access
    owner = pair(store)
    approved = allow_messages(store, owner)
    with pytest.raises(AccessDenied, match="message_binding_changed"):
        store.allow_messages(expected_owner=owner, fleet_uid=FLEET,
                             actor_uid=OTHER_ACTOR, actor_alias=ALIAS)
    with pytest.raises(AccessDenied, match="message_binding_changed"):
        store.allow_messages(expected_owner=owner, fleet_uid=FLEET,
                             actor_uid=ACTOR, actor_alias="human:other")
    assert store.authorize_message(store.open_session(OWNER).token, OWNER,
                                   host_uid=owner.host_uid, fleet_uid=FLEET) == approved
    store.revoke_messages(expected_owner=owner, fleet_uid=FLEET)
    changed = store.allow_messages(expected_owner=owner, fleet_uid=FLEET,
                                   actor_uid=OTHER_ACTOR, actor_alias="human:other")
    assert changed.actor_uid == OTHER_ACTOR and changed.actor_alias == "human:other"


def test_competing_message_approvals_cannot_replace_first_binding(access):
    store, _ = access
    owner = pair(store)
    barrier = threading.Barrier(2)

    def approve(actor_uid):
        barrier.wait(timeout=5)
        try:
            return store.allow_messages(expected_owner=owner, fleet_uid=FLEET,
                                        actor_uid=actor_uid, actor_alias=ALIAS)
        except AccessDenied as exc:
            assert exc.code == "message_binding_changed"
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(approve, (ACTOR, OTHER_ACTOR)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    session = store.open_session(OWNER)
    assert store.authorize_message(session.token, OWNER, host_uid=owner.host_uid,
                                   fleet_uid=FLEET) == winners[0]


def test_message_admission_requires_current_session_principal_host_and_fleet(access):
    store, clock = access
    owner = pair(store)
    allow_messages(store, owner)
    session = store.open_session(OWNER)
    for principal in (OTHER, PrincipalRef("other-verifier", OWNER.subject)):
        with pytest.raises(AccessDenied):
            store.authorize_message(session.token, principal, host_uid=owner.host_uid,
                                    fleet_uid=FLEET)
    with pytest.raises(AccessDenied, match="wrong_deployment"):
        store.authorize_message(session.token, OWNER, host_uid="host_" + "0" * 32,
                                fleet_uid=FLEET)
    with pytest.raises(AccessDenied, match="messages_not_allowed"):
        store.authorize_message(session.token, OWNER, host_uid=owner.host_uid,
                                fleet_uid=OTHER_FLEET)
    clock[0] += SESSION_SECONDS
    with pytest.raises(AccessDenied, match="session_unavailable"):
        store.authorize_message(session.token, OWNER, host_uid=owner.host_uid,
                                fleet_uid=FLEET)


def test_stale_message_approval_and_owner_repairing_do_not_restore_grant(access):
    store, _ = access
    first = pair(store)
    allow_messages(store, first)
    old_session = store.open_session(OWNER)
    store.revoke_owner(expected_revision=first.revision)
    with pytest.raises(AccessDenied, match="grant_changed"):
        allow_messages(store, first)
    with pytest.raises(AccessDenied, match="grant_changed"):
        store.revoke_messages(expected_owner=first, fleet_uid=FLEET)
    current = pair(store, OTHER)
    current_session = store.open_session(OTHER)
    with pytest.raises(AccessDenied):
        store.authorize_message(old_session.token, OWNER, host_uid=first.host_uid,
                                fleet_uid=FLEET)
    with pytest.raises(AccessDenied, match="messages_not_allowed"):
        store.authorize_message(current_session.token, OTHER, host_uid=current.host_uid,
                                fleet_uid=FLEET)
    with pytest.raises(AccessDenied, match="grant_changed"):
        allow_messages(store, first)


@pytest.mark.parametrize("fleet_uid,actor_uid,actor_alias", [
    ("fleet_short", ACTOR, ALIAS),
    ("FLEET_" + "1" * 32, ACTOR, ALIAS),
    (FLEET, "actor_short", ALIAS),
    (FLEET, "ACTOR_" + "1" * 32, ALIAS),
    (FLEET, ACTOR, "bot:owner"),
    (FLEET, ACTOR, "human:two words"),
    (FLEET, ACTOR, "human:two/slashes"),
    (FLEET, ACTOR, "human:"),
    (None, ACTOR, ALIAS),
])
def test_message_approval_rejects_noncanonical_bindings_without_creating_table(
        access, fleet_uid, actor_uid, actor_alias):
    store, _ = access
    owner = pair(store)
    with pytest.raises(AccessDenied, match="invalid_message_binding"):
        store.allow_messages(expected_owner=owner, fleet_uid=fleet_uid,
                             actor_uid=actor_uid, actor_alias=actor_alias)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'message_grants'").fetchone() is None


@pytest.mark.parametrize("bad", ["", "short", " " * 43, "é" * 43, None])
def test_malformed_credentials_are_refused(access, bad):
    store, _ = access
    grant = pair(store)
    with pytest.raises(AccessDenied, match="invalid_credential"):
        store.authorize_read(bad, OWNER, host_uid=grant.host_uid)


def test_ephemeral_records_are_bounded_and_expire(access):
    store, clock = access
    for _ in range(MAX_PENDING):
        store.begin_pairing(OWNER)
    with pytest.raises(AccessDenied, match="pairing_capacity"):
        store.begin_pairing(OWNER)
    clock[0] += PAIRING_SECONDS
    pair(store)
    sessions = [store.open_session(OWNER) for _ in range(MAX_SESSIONS)]
    with pytest.raises(AccessDenied, match="session_capacity"):
        store.open_session(OWNER)
    assert admitted(store, store.renew_session(sessions[0].token, OWNER)).active
    clock[0] += SESSION_SECONDS
    assert admitted(store, store.open_session(OWNER)).active
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM challenges").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_initialization_is_idempotent_but_cannot_rebind_copied_state(access, tmp_path):
    store, clock = access
    grant = pair(store)
    session = store.open_session(OWNER)
    again = OwnerAccess.initialize(store.root, clock=lambda: clock[0])
    assert admitted(again, session) == grant
    other_root = tmp_path / "other"
    ensure_host_uid(other_root / "state")
    other = OwnerAccess.initialize(other_root)
    shutil.copyfile(store.path, other.path)
    with pytest.raises(AccessUnavailable):
        other.current_grant()
    with pytest.raises(AccessUnavailable):
        OwnerAccess.initialize(other_root)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "version", "loose", "symlink", "identity"])
def test_unavailable_authority_fails_closed_without_repair(access, damage):
    store, _ = access
    pair(store)
    session = store.open_session(OWNER)
    if damage == "missing":
        store.path.unlink()
    elif damage == "corrupt":
        store.path.write_bytes(b"not a database")
    elif damage == "version":
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE metadata SET version = 2")
    elif damage == "loose":
        store.path.chmod(0o644)
    elif damage == "symlink":
        target = store.path.with_suffix(".moved")
        store.path.rename(target)
        store.path.symlink_to(target)
    else:
        (store.root / "state/host-uid").write_text("host_" + "0" * 32)
    with pytest.raises(AccessUnavailable):
        admitted(store, session)
    if damage == "missing":
        assert not store.path.exists()
    if damage == "loose":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o644


def test_session_survives_separate_processes_and_revoke_is_seen(access):
    store, clock = access
    grant = pair(store)
    session = store.open_session(OWNER)
    program = """
import json, sys
from pathlib import Path
from claudlobby.plane.owner_access import OwnerAccess, PrincipalRef, AccessDenied
data = json.load(sys.stdin)
store = OwnerAccess(Path(data['root']), clock=lambda: data['now'])
try:
    store.authorize_read(data['token'], PrincipalRef('test-verifier', 'human-001'),
                         host_uid=data['host'])
except AccessDenied:
    print('denied')
else:
    print('admitted')
"""

    def fresh_process():
        result = subprocess.run([sys.executable, "-c", program], input=json.dumps({
            "root": str(store.root), "now": clock[0], "host": grant.host_uid, "token": session.token,
        }), capture_output=True, text=True, timeout=10, env=constructed_env())
        assert result.returncode == 0, result.stderr
        assert session.token not in result.stdout + result.stderr
        return result.stdout.strip()

    assert fresh_process() == "admitted"
    assert fresh_process() == "admitted"
    store.revoke_owner(expected_revision=grant.revision)
    assert fresh_process() == "denied"


@pytest.mark.parametrize("damage", ["missing", "malformed", "loose", "symlink", "directory", "fifo"])
def test_read_host_uid_never_mints_or_repairs(tmp_path, damage):
    state = tmp_path / "state"
    ensure_host_uid(state)
    path = state / "host-uid"
    if damage == "malformed":
        path.write_text("invalid")
    elif damage == "loose":
        path.chmod(0o644)
    else:
        path.unlink()
        if damage == "symlink":
            target = tmp_path / "identity"
            target.write_text("host_" + "0" * 32)
            target.chmod(0o600)
            path.symlink_to(target)
        elif damage == "directory":
            path.mkdir()
        elif damage == "fifo":
            os.mkfifo(path, 0o600)
    with pytest.raises(ValueError, match="existing host identity"):
        read_host_uid(state)
    if damage == "missing":
        assert not path.exists()
    if damage == "loose":
        assert stat.S_IMODE(path.stat().st_mode) == 0o644



def test_local_message_inspection_is_read_only_without_session_or_repair(access):
    store, _ = access
    owner = pair(store)
    before = store.path.read_bytes()
    with pytest.raises(AccessDenied, match="messages_not_allowed"):
        store.current_message_grant(expected_owner=owner, fleet_uid=FLEET)
    assert store.path.read_bytes() == before
    grant = allow_messages(store, owner)
    before = store.path.read_bytes()
    assert store.current_message_grant(expected_owner=owner, fleet_uid=FLEET) == grant
    assert store.path.read_bytes() == before
    store.revoke_owner(expected_revision=owner.revision)
    with pytest.raises(AccessDenied):
        store.current_message_grant(expected_owner=owner, fleet_uid=FLEET)


def test_expected_message_grant_atomically_refuses_replacement(access):
    store, _ = access
    owner = pair(store)
    original = allow_messages(store, owner)
    store.revoke_messages(expected_owner=owner, fleet_uid=FLEET)
    replacement = store.allow_messages(expected_owner=owner, fleet_uid=FLEET,
                                       actor_uid=OTHER_ACTOR, actor_alias="human:replacement")
    with pytest.raises(AccessDenied, match="message_binding_changed"):
        store.revoke_messages(expected_owner=owner, fleet_uid=FLEET, expected_grant=original)
    assert store.current_message_grant(expected_owner=owner, fleet_uid=FLEET) == replacement
    store.revoke_messages(expected_owner=owner, fleet_uid=FLEET, expected_grant=replacement)
    with pytest.raises(AccessDenied):
        store.current_message_grant(expected_owner=owner, fleet_uid=FLEET)


def test_nudge_grants_are_independent_durable_generations_without_message_migration(access):
    from claudlobby.plane.owner_access import OwnerNudgeGrant
    store, clock = access
    owner = pair(store)
    session = store.open_session(OWNER)
    legacy_message_grant(store, owner)
    before = store.path.read_bytes()
    with pytest.raises(AccessDenied, match="nudges_not_allowed"):
        store.authorize_nudge(session.token, OWNER, host_uid=owner.host_uid, fleet_uid=FLEET)
    assert store.local_action_status().nudge_grants == ()
    assert store.path.read_bytes() == before
    options = dict(expected_owner=owner, fleet_uid=FLEET, actor_uid=ACTOR, actor_alias=ALIAS)
    first = store.allow_nudges(**options)
    assert type(first) is OwnerNudgeGrant and len(first.generation) == 64
    assert store.allow_nudges(**options) == first
    with sqlite3.connect(store.path) as conn:
        assert "generation" not in [row[1] for row in conn.execute("PRAGMA table_info(message_grants)")]
    reopened = OwnerAccess(store.root, clock=lambda: clock[0])
    assert reopened.authorize_nudge(session.token, OWNER, host_uid=owner.host_uid, fleet_uid=FLEET) == first
    assert reopened.local_action_status().nudge_grants == (first,)
    with pytest.raises(AccessDenied, match="nudges_not_allowed"):
        store.authorize_nudge(session.token, OWNER, host_uid=owner.host_uid, fleet_uid=OTHER_FLEET)
    with pytest.raises(AccessDenied, match="nudge_binding_changed"):
        store.allow_nudges(**{**options, "actor_uid": OTHER_ACTOR})
    store.revoke_nudges(expected_owner=owner, fleet_uid=FLEET, expected_grant=first)
    assert store.authorize_message(session.token, OWNER, host_uid=owner.host_uid, fleet_uid=FLEET)
    replacement = store.allow_nudges(**options)
    assert replacement.generation != first.generation
    with pytest.raises(AccessDenied, match="nudge_binding_changed"):
        store.revoke_nudges(expected_owner=owner, fleet_uid=FLEET, expected_grant=first)
    assert store.current_nudge_grant(expected_owner=owner, fleet_uid=FLEET) == replacement
    store.revoke_messages(expected_owner=owner, fleet_uid=FLEET)
    assert store.authorize_nudge(session.token, OWNER, host_uid=owner.host_uid, fleet_uid=FLEET) == replacement
    store.revoke_owner(expected_revision=owner.revision)
    with pytest.raises(AccessDenied):
        store.authorize_nudge(session.token, OWNER, host_uid=owner.host_uid, fleet_uid=FLEET)
    current = pair(store)
    with pytest.raises(AccessDenied, match="nudges_not_allowed"):
        store.current_nudge_grant(expected_owner=current, fleet_uid=FLEET)


@pytest.mark.parametrize("change", ["host", "principal", "expired", "rotated"])
def test_nudge_requires_current_reader_and_host(access, change):
    store, clock = access
    owner = pair(store)
    session = store.open_session(OWNER)
    store.allow_nudges(expected_owner=owner, fleet_uid=FLEET, actor_uid=ACTOR, actor_alias=ALIAS)
    before = store.path.read_bytes()
    if change == "expired":
        clock[0] += SESSION_SECONDS + 1
    elif change == "rotated":
        store.renew_session(session.token, OWNER)
        before = store.path.read_bytes()
    with pytest.raises(AccessDenied):
        store.authorize_nudge(session.token, OTHER if change == "principal" else OWNER,
            host_uid="foreign-host" if change == "host" else owner.host_uid, fleet_uid=FLEET)
    assert store.path.read_bytes() == before
