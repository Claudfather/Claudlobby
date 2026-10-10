"""Local owner command effects against disposable authority only."""

from contextlib import contextmanager
import io
import json
import sqlite3
from uuid import uuid4

import pytest

from claudlobby.__main__ import main
from claudlobby.commands import host_owner
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.db import db_file
from claudlobby.plane.owner_access import AccessDenied, OwnerAccess, PrincipalRef


PRINCIPAL = PrincipalRef("test-verifier", "human-001")


@pytest.fixture
def owner(tmp_path, monkeypatch):
    for name in host_owner._GENERATED:
        monkeypatch.delenv(name, raising=False)
    ensure_host_uid(tmp_path / "state")
    return OwnerAccess.initialize(tmp_path)


def call(store, action, *args):
    return main(["--root", str(store.root), "host", "owner", action, *args])


def terminal(monkeypatch, answers):
    output = io.StringIO()
    output.readline = lambda size=-1: next(answers)

    @contextmanager
    def opened():
        yield output

    monkeypatch.setattr(host_owner, "_terminal", opened)
    return output


def test_status_does_not_initialize_or_mint_identity(tmp_path, capsys):
    assert call(OwnerAccess(tmp_path), "status", "--json") == 6
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "host.owner.status"
    assert not list(tmp_path.iterdir())


def test_status_reports_pairing_then_revocation_without_secret(owner, capsys):
    challenge = owner.begin_pairing(PRINCIPAL)
    grant = owner.confirm_pairing(challenge.token, expected_principal=PRINCIPAL)
    session = owner.open_session(PRINCIPAL)
    assert call(owner, "status", "--json") == 0
    output = capsys.readouterr().out
    data = json.loads(output)["data"]
    assert data["state"] == "paired"
    assert data["owner"]["revision"] == grant.revision
    assert challenge.token not in output and session.token not in output
    owner.revoke_owner(expected_revision=grant.revision)
    assert call(owner, "status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["data"]["state"] == "revoked"


@pytest.mark.parametrize("action", ["initialize", "confirm", "revoke", "bind-source"])
def test_changes_require_terminal_and_reject_json(owner, monkeypatch, action):
    monkeypatch.setattr(host_owner.sys.stdin, "isatty", lambda: False)
    assert call(owner, action) == 4
    assert call(owner, action, "--json") == 2
    assert owner.current_grant() is None


@pytest.mark.parametrize("marker", host_owner._GENERATED)
def test_generated_selectors_refuse_before_access(owner, monkeypatch, marker):
    monkeypatch.setenv(marker, "")
    monkeypatch.setattr(OwnerAccess, "current_grant", lambda *a: pytest.fail("read attempted"))
    assert call(owner, "status") == 4


def test_explicit_host_scope_required(owner, monkeypatch):
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(owner.root))
    assert main(["host", "owner", "status"]) == 2
    assert main(["--root", str(owner.root), "--fleet", "example", "host", "owner", "status"]) == 2
    assert main(["--root", str(owner.root), "--seed", "host", "owner", "status"]) == 2


def test_initialize_requires_exact_confirmation(tmp_path, monkeypatch):
    ensure_host_uid(tmp_path / "state")
    store = OwnerAccess(tmp_path)
    terminal(monkeypatch, iter(["no\n"]))
    assert call(store, "initialize") == 4
    assert not store.path.exists()
    terminal(monkeypatch, iter(["INITIALIZE\n"]))
    assert call(store, "initialize") == 0
    assert store.current_grant() is None


def test_confirmation_displays_exact_identity_without_printing_challenge(owner, monkeypatch, capsys):
    challenge = owner.begin_pairing(PRINCIPAL)
    output = terminal(monkeypatch, iter(["PAIR\n"]))
    monkeypatch.setattr(host_owner, "_challenge", lambda _: challenge.token)
    assert call(owner, "confirm") == 0
    assert owner.current_grant().principal == PRINCIPAL
    assert PRINCIPAL.subject in output.getvalue()
    captured = capsys.readouterr()
    assert challenge.token not in output.getvalue() + captured.out + captured.err
    assert call(owner, "confirm") == 4  # replay refuses before reading approval


def test_cancelled_pairing_remains_unapproved(owner, monkeypatch):
    challenge = owner.begin_pairing(PRINCIPAL)
    terminal(monkeypatch, iter(["no\n"]))
    monkeypatch.setattr(host_owner, "_challenge", lambda _: challenge.token)
    assert call(owner, "confirm") == 4
    assert owner.current_grant() is None
    assert owner.inspect_pairing(challenge.token).principal == PRINCIPAL


def test_expiry_between_preview_and_confirmation_refuses(owner, monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr(OwnerAccess, "_now", lambda _: now[0])
    challenge = owner.begin_pairing(PRINCIPAL)
    terminal(monkeypatch, iter([]))
    monkeypatch.setattr(host_owner, "_challenge", lambda _: challenge.token)
    monkeypatch.setattr(host_owner, "_approve", lambda *a: now.__setitem__(0, challenge.expires_at))
    assert call(owner, "confirm") == 4
    assert owner.current_grant() is None


def test_revoke_invalidates_sessions_and_does_not_revoke_replacement(owner, monkeypatch):
    challenge = owner.begin_pairing(PRINCIPAL)
    original = owner.confirm_pairing(challenge.token, expected_principal=PRINCIPAL)
    session = owner.open_session(PRINCIPAL)
    terminal(monkeypatch, iter(["REVOKE\n"]))
    assert call(owner, "revoke") == 0
    with pytest.raises(AccessDenied):
        owner.authorize_read(session.token, PRINCIPAL, host_uid=original.host_uid)
    challenge = owner.begin_pairing(PRINCIPAL)
    owner.confirm_pairing(challenge.token, expected_principal=PRINCIPAL)

    def concurrent_repair(*args):
        owner.revoke_owner(expected_revision=owner.current_grant().revision)
        fresh = owner.begin_pairing(PRINCIPAL)
        owner.confirm_pairing(fresh.token, expected_principal=PRINCIPAL)

    monkeypatch.setattr(host_owner, "_approve", concurrent_repair)
    assert call(owner, "revoke") == 4
    assert owner.current_grant().active


def test_credential_arguments_are_never_accepted_or_echoed(owner, capsys):
    with pytest.raises(SystemExit) as exc:
        call(owner, "confirm", "--json", "--token", "private-pairing-value")
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert "private-pairing-value" not in output.out + output.err
    assert json.loads(output.out)["command"] == "host.owner.confirm"


def test_hidden_input_failure_is_not_allowed_to_echo(owner, monkeypatch):
    terminal(monkeypatch, iter([]))

    def insecure_input(*a, **k):
        import warnings
        warnings.warn("hidden input unavailable", host_owner.getpass.GetPassWarning)
        pytest.fail("fallback continued")

    monkeypatch.setattr(host_owner.getpass, "getpass", insecure_input)
    assert call(owner, "confirm") == 4
    assert owner.current_grant() is None


# Independent activation fixtures use the real frozen config/Plane and runtime
# admission, with an explicitly synthetic release identity/native supervisor.
from tests.test_activation import cold, tmp_path  # noqa: F401
from tests.test_releases import installed  # noqa: F401
from tests.test_task_read_cli import active  # noqa: F401
from tests.test_message_write_cli import _human
from claudlobby.operation_context import resolve_task_mutation_context


@pytest.fixture
def message_owner(active, monkeypatch):
    root, host = active
    _human(monkeypatch, host.release)
    for name in host_owner._GENERATED:
        monkeypatch.delenv(name, raising=False)
    store = OwnerAccess.initialize(root)
    challenge = store.begin_pairing(PRINCIPAL)
    grant = store.confirm_pairing(challenge.token, expected_principal=PRINCIPAL)
    ctx = resolve_task_mutation_context(root=root, fleet="example", operator_alias="human:local-owner", package=host.package)
    return store, host, grant, ctx


def allow(store, *extra):
    return call(store, "allow-messages", "--target-fleet", "example", "--actor", "human:local-owner", *extra)


def test_allow_and_revoke_display_exact_binding_and_revoke_without_active_config(message_owner, monkeypatch):
    store, host, owner, ctx = message_owner
    output = terminal(monkeypatch, iter(["ALLOW\n"]))
    assert allow(store) == 0
    grant = store.current_message_grant(expected_owner=owner, fleet_uid=ctx.fleet_uid)
    assert grant.actor_uid == ctx.caller.uid and grant.actor_alias == ctx.caller.alias
    assert all(value in output.getvalue() for value in (owner.principal.subject, owner.host_uid, ctx.fleet_uid, ctx.caller.uid, ctx.caller.alias))
    assert "ordinary messages only" in output.getvalue()
    # Revocation reads only retained authority; no active config or Plane needed.
    (store.root / "state/selected-release.json").unlink()
    db_file(store.root).rename(store.root / "retained-plane")
    output = terminal(monkeypatch, iter(["REVOKE-MESSAGES\n"]))
    assert call(store, "revoke-messages", "--fleet-uid", ctx.fleet_uid) == 0
    assert ctx.caller.uid in output.getvalue()
    with pytest.raises(AccessDenied):
        store.current_message_grant(expected_owner=owner, fleet_uid=ctx.fleet_uid)
    assert store.current_grant() == owner


@pytest.mark.parametrize("action", ["allow-messages", "revoke-messages"])
def test_message_grants_require_terminal_json_refusal_and_explicit_scope(message_owner, monkeypatch, action):
    store, _, _, ctx = message_owner
    extra = ["--target-fleet", "example", "--actor", "human:local-owner"] if action == "allow-messages" else ["--fleet-uid", ctx.fleet_uid]
    monkeypatch.setattr(host_owner.sys.stdin, "isatty", lambda: False)
    assert call(store, action, *extra) == 4
    assert call(store, action, *extra, "--json") == 2
    assert main(["--root", str(store.root), "--fleet", "example", "host", "owner", action, *extra]) == 2
    for marker in host_owner._GENERATED:
        with monkeypatch.context() as patch:
            patch.setenv(marker, "")
            assert call(store, action, *extra) == 4


@pytest.mark.parametrize("actor", ["bot:example/worker", "human:", "human:two words", "human:x/y"])
def test_allow_actor_must_be_explicit_canonical_human(message_owner, monkeypatch, actor):
    store, *_ = message_owner
    terminal(monkeypatch, iter([]))
    assert call(store, "allow-messages", "--target-fleet", "example", "--actor", actor) == 2


def test_cold_actor_requires_separate_registration_approval_before_any_write(message_owner, monkeypatch):
    store, host, owner, ctx = message_owner
    actor = "human:new-local-owner"
    with sqlite3.connect(db_file(store.root)) as conn:
        before = conn.execute("SELECT count(*) FROM ingest_ledger").fetchone()[0]
    argv = ("--target-fleet", "example", "--actor", actor)
    terminal(monkeypatch, iter([]))
    assert call(store, "allow-messages", *argv) == 4
    terminal(monkeypatch, iter(["no\n"]))
    assert call(store, "allow-messages", *argv, "--register-actor") == 4
    with sqlite3.connect(db_file(store.root)) as conn:
        assert not conn.execute("SELECT 1 FROM identity_registry WHERE alias=?", (actor,)).fetchone()
        assert conn.execute("SELECT count(*) FROM ingest_ledger").fetchone()[0] == before
    output = terminal(monkeypatch, iter(["REGISTER\n", "no\n"]))
    assert call(store, "allow-messages", *argv, "--register-actor") == 4
    with sqlite3.connect(db_file(store.root)) as conn:
        uid = conn.execute("SELECT uid FROM identity_registry WHERE alias=?", (actor,)).fetchone()[0]
        assert conn.execute("SELECT count(*) FROM events WHERE event='operator_first_seen' AND subject_alias=?", (actor,)).fetchone()[0] == 1
    assert uid in output.getvalue()
    with pytest.raises(AccessDenied):
        store.current_message_grant(expected_owner=owner, fleet_uid=ctx.fleet_uid)
    terminal(monkeypatch, iter(["ALLOW\n"]))
    assert call(store, "allow-messages", *argv) == 0
    assert store.current_message_grant(expected_owner=owner, fleet_uid=ctx.fleet_uid).actor_uid == uid


@pytest.mark.parametrize("action", ["allow", "register", "revoke"])
def test_changed_owner_revision_after_display_refuses_message_changes(message_owner, monkeypatch, action):
    store, _, owner, ctx = message_owner
    if action == "revoke":
        store.allow_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid, actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    terminal(monkeypatch, iter([]))
    def replace_owner(*args):
        store.revoke_owner(expected_revision=owner.revision)
        challenge = store.begin_pairing(PRINCIPAL)
        store.confirm_pairing(challenge.token, expected_principal=PRINCIPAL)
    monkeypatch.setattr(host_owner, "_approve", replace_owner)
    if action == "revoke":
        assert call(store, "revoke-messages", "--fleet-uid", ctx.fleet_uid) == 4
    elif action == "register":
        assert call(store, "allow-messages", "--target-fleet", "example", "--actor", "human:cold-race", "--register-actor") == 4
        with sqlite3.connect(db_file(store.root)) as conn:
            assert not conn.execute("SELECT 1 FROM identity_registry WHERE alias='human:cold-race'").fetchone()
    else:
        assert allow(store) == 4
    with pytest.raises(AccessDenied):
        store.current_message_grant(expected_owner=store.current_grant(), fleet_uid=ctx.fleet_uid)


def test_changed_actor_uid_after_display_refuses_grant(message_owner, monkeypatch):
    store, _, owner, ctx = message_owner
    terminal(monkeypatch, iter([]))
    def change(*args):
        with sqlite3.connect(db_file(store.root)) as conn:
            conn.execute("UPDATE identity_registry SET uid=? WHERE kind='actor' AND alias=?", ("actor_" + uuid4().hex, ctx.caller.alias))
    monkeypatch.setattr(host_owner, "_approve", change)
    assert allow(store) == 4
    with pytest.raises(AccessDenied):
        store.current_message_grant(expected_owner=owner, fleet_uid=ctx.fleet_uid)


def test_revoke_cannot_remove_a_replacement_binding(message_owner, monkeypatch):
    store, _, owner, ctx = message_owner
    original = store.allow_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid, actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    terminal(monkeypatch, iter([]))
    replacement = []
    def change(*args):
        store.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
        replacement.append(store.allow_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid,
                          actor_uid="actor_" + uuid4().hex, actor_alias="human:replacement"))
    monkeypatch.setattr(host_owner, "_approve", change)
    assert call(store, "revoke-messages", "--fleet-uid", ctx.fleet_uid) == 4
    assert store.current_message_grant(expected_owner=owner, fleet_uid=ctx.fleet_uid) == replacement[0]



def test_final_allow_never_registers_existing_actor(message_owner, monkeypatch):
    store, *_ = message_owner
    terminal(monkeypatch, iter(["ALLOW\n"]))
    monkeypatch.setattr("claudlobby.operation_context.resolve_task_mutation_context",
                        lambda *a, **k: pytest.fail("ALLOW must not register an actor"))
    assert allow(store) == 0
