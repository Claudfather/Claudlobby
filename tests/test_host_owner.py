"""Local owner command effects against disposable authority only."""

from contextlib import contextmanager
import io
import json

import pytest

from claudlobby.__main__ import main
from claudlobby.commands import host_owner
from claudlobby.plane.ids import ensure_host_uid
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


@pytest.mark.parametrize("problem,expected,code", [("unbound", "host owner bind-source", 6),
    ("index", "host owner bind-source", 6), ("foreign", "Do not rebind", 4),
    ("corrupt", "database and schema locally", 6)])
def test_serve_source_failures_have_local_source_remediation(tmp_path, capsys, problem, expected, code):
    import sqlite3
    from claudlobby.plane.db import db_file
    from claudlobby.plane.owner_source import bind_source
    from tests.test_plane_two_fleets import _seed
    _seed(tmp_path)
    store = OwnerAccess.initialize(tmp_path)
    if problem != "unbound": bind_source(tmp_path)
    if problem == "index":
        with sqlite3.connect(db_file(tmp_path)) as conn:
            conn.execute("DROP INDEX owner_source_events_host")
    if problem == "foreign":
        with sqlite3.connect(db_file(tmp_path)) as conn:
            conn.execute("UPDATE owner_source_binding SET host_uid='foreign-host'")
    if problem == "corrupt": db_file(tmp_path).write_bytes(b"not SQLite")
    assert call(store, "serve", "--origin", "https://plane.example.test", "--tailscale", "/unused/binary") == code
    error = capsys.readouterr().err
    assert expected in error and "request pairing again" not in error
    assert str(tmp_path) not in error and "foreign-host" not in error
    if problem in {"foreign", "corrupt"}:
        assert "re-run host owner bind-source" not in error


def test_bind_foreign_source_never_suggests_pairing_or_rebinding(tmp_path, monkeypatch, capsys):
    import sqlite3
    from claudlobby.plane.db import db_file
    from claudlobby.plane.owner_source import bind_source
    from tests.test_plane_two_fleets import _seed
    _seed(tmp_path)
    store = OwnerAccess.initialize(tmp_path)
    bind_source(tmp_path)
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute("UPDATE communications SET host_uid='foreign-host'")
        conn.execute("UPDATE work_items SET host_uid='foreign-host'")
    terminal(monkeypatch, iter(["BIND\n"]))
    assert call(store, "bind-source") == 4
    error = capsys.readouterr().err
    assert "Do not rebind" in error and "request pairing again" not in error
    assert "re-run host owner bind-source" not in error and "foreign-host" not in error
