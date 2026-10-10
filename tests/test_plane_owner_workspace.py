"""Optional local authority, synthetic verified evidence; no ingress/network calls."""
from dataclasses import asdict, replace
import io
import json
import os
import select
import signal
import sqlite3
import time
from uuid import UUID

import pytest

from claudlobby.__main__ import main
from claudlobby.commands import host_owner
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.owner_access import AccessDenied, AccessUnavailable, OwnerAccess, PrincipalRef
from claudlobby.plane.workspace_assertion import WorkspaceAttestation
from tests.test_host_owner import terminal

OWNER = PrincipalRef("tailscale:controlplane.tailscale.com", "1001")
OTHER = PrincipalRef("tailscale:controlplane.tailscale.com", "1002")
ISSUER = "https://accounts.example.test"
ORIGIN = "https://plane.example.test"
USER = "00000000-0000-4000-8000-000000000001"
WORKSPACE = "00000000-0000-4000-8000-000000000002"
JTI = "00000000-0000-4000-8000-000000000003"


@pytest.fixture
def local(tmp_path, monkeypatch):
    for name in host_owner._GENERATED:
        monkeypatch.delenv(name, raising=False)
    ensure_host_uid(tmp_path / "state")
    clock = [1800000000.0]
    access = OwnerAccess.initialize(tmp_path, clock=lambda: clock[0])
    challenge = access.begin_pairing(OWNER)
    grant = access.confirm_pairing(challenge.token, expected_principal=OWNER)
    workspace = access.workspace()
    workspace.initialize(expected_owner=grant)
    return access, workspace, grant, clock


def proposed(local):
    access, workspace, owner, clock = local
    challenge = workspace.begin(OWNER, issuer=ISSUER, operational_origin=ORIGIN)
    attestation = WorkspaceAttestation(ISSUER, USER, WORKSPACE, owner.host_uid,
                                      challenge.nonce, ORIGIN, int(clock[0]), int(clock[0]) + 120, JTI)
    return workspace.stage(attestation, OWNER)


def call(access, action, *extra):
    return main(["--root", str(access.root), "host", "owner", action, *extra])


def test_optional_schema_absent_is_unavailable_but_direct_session_works(tmp_path):
    ensure_host_uid(tmp_path / "state")
    access = OwnerAccess.initialize(tmp_path)
    pair = access.begin_pairing(OWNER)
    owner = access.confirm_pairing(pair.token, expected_principal=OWNER)
    session = access.open_session(OWNER)
    with pytest.raises(AccessUnavailable):
        access.workspace().status()
    assert access.authorize_read(session.token, OWNER, host_uid=owner.host_uid) == owner
    assert "workspace_metadata" not in {r[0] for r in sqlite3.connect(access.path).execute("SELECT name FROM sqlite_master")}


def test_initialization_requires_current_native_pairing(local):
    access, workspace, owner, _ = local
    with pytest.raises(AccessDenied):
        workspace.initialize(expected_owner=replace(owner, revision=0))
    access.revoke_owner(expected_revision=owner.revision)
    p = access.begin_pairing(PrincipalRef("other-verifier", "human-001"))
    new = access.confirm_pairing(p.token, expected_principal=p.principal)
    with pytest.raises(AccessDenied, match="workspace_native_owner_required"):
        workspace.initialize(expected_owner=new)
    assert access.open_session(new.principal).grant == new  # legacy direct pairing unchanged


def test_expected_uses_current_challenge_not_untrusted_token_values(local):
    _, workspace, owner, _ = local
    c = workspace.begin(OWNER, issuer=ISSUER, operational_origin=ORIGIN)
    assert asdict(workspace.expected(c.nonce, OWNER)) == {
        "issuer": ISSUER, "host_uid": owner.host_uid, "nonce": c.nonce,
        "operational_origin": ORIGIN, "allow_loopback_http": False}
    with pytest.raises(AccessDenied):
        workspace.expected(c.nonce, OTHER)


@pytest.mark.parametrize("field,value", [("issuer", "https://wrong.example.test"),
    ("operational_origin", "https://wrong.example.test"), ("host_uid", "host_" + "f"*32),
    ("subject", "not-uuid"), ("issued_at", True), ("expires_at", 1800000121)])
def test_staging_refuses_mismatch_without_consuming(local, field, value):
    p = proposed(local)
    with pytest.raises(AccessDenied):
        local[1].stage(replace(p.attestation, **{field: value}), OWNER)
    assert local[1].inspect(p.nonce) == p


def test_metadata_immutable_and_digest_only_no_sessions_or_grants(local):
    access, workspace, owner, _ = local
    session = access.open_session(OWNER)
    p = proposed(local)
    assert workspace.stage(p.attestation, OWNER) == p
    with pytest.raises(AccessDenied, match="workspace_proposal_changed"):
        workspace.stage(replace(p.attestation, jti=USER), OWNER)
    with sqlite3.connect(access.path) as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        row = conn.execute("SELECT digest,proposal FROM workspace_challenges").fetchone()
        assert p.nonce not in ''.join(row)
        assert 'nonce' not in json.loads(row[1])
    binding = workspace.confirm(expected_proposal=p)
    UUID(binding.generation)
    assert binding.revision == 1 and binding.workspace_id == WORKSPACE
    assert workspace.status() == ("active", binding)
    assert access.authorize_read(session.token, OWNER, host_uid=owner.host_uid) == owner
    status = access.local_action_status()
    assert status.message_grants == status.nudge_grants == status.feedback_grants == ()
    with sqlite3.connect(access.path) as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM workspace_challenges").fetchone()[0] == 0


def test_one_use_clears_competitors_and_fresh_store_cannot_replay(local):
    access, workspace, _, clock = local
    p = proposed(local)
    competitor = proposed(local)
    binding = workspace.confirm(expected_proposal=p)
    fresh = OwnerAccess(access.root, clock=lambda: clock[0]).workspace()
    for candidate in (p, competitor):
        with pytest.raises(AccessDenied):
            fresh.confirm(expected_proposal=candidate)
    assert fresh.status()[1] == binding
    with pytest.raises(AccessDenied, match="workspace_already_bound"):
        fresh.begin(OWNER, issuer=ISSUER, operational_origin=ORIGIN)


@pytest.mark.parametrize("delta", [-1, 120])
def test_expiry_and_clock_rollback_refuse_without_binding(local, delta):
    p = proposed(local)
    local[3][0] += delta
    with pytest.raises(AccessDenied):
        local[1].confirm(expected_proposal=p)
    local[3][0] = 1800000000
    assert local[1].status() == ("absent", None)


def test_owner_revision_change_invalidates_staged_proposal_and_bound_workspace(local):
    access, workspace, owner, _ = local
    p = proposed(local)
    binding = workspace.confirm(expected_proposal=p)
    access.revoke_owner(expected_revision=owner.revision)
    pair = access.begin_pairing(OWNER)
    new_owner = access.confirm_pairing(pair.token, expected_principal=OWNER)
    assert workspace.status() == ("stale-owner", binding)
    with pytest.raises(AccessDenied):
        workspace.confirm(expected_proposal=p)
    with pytest.raises(AccessDenied):
        workspace.begin(OWNER, issuer=ISSUER, operational_origin=ORIGIN)
    workspace.revoke(expected_binding=binding, expected_owner=new_owner)
    new = workspace.confirm(expected_proposal=proposed(local))
    assert new.owner == new_owner and new.generation != binding.generation


def test_revoke_reallow_generation_fences_old_preview_without_touching_session(local):
    access, workspace, owner, _ = local
    session = access.open_session(OWNER)
    first = workspace.confirm(expected_proposal=proposed(local))
    revoked = workspace.revoke(expected_binding=first, expected_owner=owner)
    assert not revoked.active and revoked.revision == 2
    current = workspace.confirm(expected_proposal=proposed(local))
    assert current.revision == 3 and current.generation != first.generation
    with pytest.raises(AccessDenied):
        workspace.revoke(expected_binding=first, expected_owner=owner)
    assert workspace.status() == ("active", current)
    assert access.authorize_read(session.token, OWNER, host_uid=owner.host_uid) == owner


def test_staged_preview_race_and_transaction_failure_roll_back(local):
    access, workspace, _, _ = local
    p = proposed(local)
    with pytest.raises(AccessDenied):
        workspace.confirm(expected_proposal=replace(p, expires_at=p.expires_at-1))
    with sqlite3.connect(access.path) as conn:
        conn.execute("CREATE TRIGGER fail_consume BEFORE DELETE ON workspace_challenges BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(AccessUnavailable):
        workspace.confirm(expected_proposal=p)
    assert workspace.status() == ("absent", None)
    assert workspace.inspect(p.nonce) == p


def test_bounded_challenges_and_expiry_clean_up(local):
    _, workspace, _, clock = local
    for _ in range(32):
        workspace.begin(OWNER, issuer=ISSUER, operational_origin=ORIGIN)
    with pytest.raises(AccessDenied, match="too_many_workspace_requests"):
        workspace.begin(OWNER, issuer=ISSUER, operational_origin=ORIGIN)
    clock[0] += 300
    workspace.begin(OWNER, issuer=ISSUER, operational_origin=ORIGIN)


def test_partial_schema_never_repaired_and_direct_owner_unchanged(local):
    access, workspace, owner, _ = local
    with sqlite3.connect(access.path) as conn:
        conn.execute("DROP TABLE workspace_bindings")
    with pytest.raises(AccessUnavailable):
        workspace.initialize(expected_owner=owner)
    assert access.current_grant() == owner


def test_cli_preview_refusal_and_confirmation_no_secret_echo(local, monkeypatch, capsys):
    access, workspace, _, _ = local
    # CLI's independent clock is wall time; use wall-time synthetic proposal.
    local[3][0] = time.time()
    p = proposed(local)
    monkeypatch.setattr(host_owner, "_challenge", lambda *a, **k: p.nonce)
    output = terminal(monkeypatch, iter(["NO\n"]))
    assert call(access, "confirm-workspace") == 4
    assert workspace.status()[0] == "absent"
    output = terminal(monkeypatch, iter(["BIND-WORKSPACE\n"]))
    assert call(access, "confirm-workspace") == 0
    local[3][0] = time.time()
    assert all(v in output.getvalue() for v in (USER, WORKSPACE, ISSUER, ORIGIN, OWNER.subject))
    assert p.nonce not in output.getvalue() + capsys.readouterr().out
    assert call(access, "workspace-status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["data"]["state"] == "active"


def test_cli_nonterminal_and_generated_presence_refuse(local, monkeypatch):
    access = local[0]
    monkeypatch.setattr(host_owner.sys.stdin, "isatty", lambda: False)
    for verb in ("initialize-workspace", "confirm-workspace", "revoke-workspace"):
        assert call(access, verb) == 4
        assert call(access, verb, "--json") == 2
    monkeypatch.setenv("BOT_SERVICE", "")
    assert call(access, "workspace-status", "--json") == 4


def test_real_terminal_confirms_and_refuses_without_issuing_authority(local):
    """Actual private PTY drives shipped console/getpass, synthetic DB only."""
    import pty
    import sys
    from tests.conftest import constructed_env
    access, workspace, _, clock = local
    clock[0] = time.time()
    p = proposed(local)
    for answer, expected_rc in (("NO", 4), ("BIND-WORKSPACE", 0)):
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, "-m", "claudlobby", "--root", str(access.root),
                                      "host", "owner", "confirm-workspace"], constructed_env())
        output = b""
        sent_code = sent_answer = False
        status = None
        done = 0
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                readable, _, _ = select.select([fd], [], [], .1)
                if readable:
                    try:
                        output += os.read(fd, 8192)
                    except OSError:
                        break
                if not sent_code and b"code (hidden):" in output:
                    os.write(fd, (p.nonce + "\n").encode()); sent_code = True
                if not sent_answer and b"Type BIND-WORKSPACE" in output:
                    os.write(fd, (answer + "\n").encode()); sent_answer = True
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
            if status is None or not done:
                done, status = os.waitpid(pid, os.WNOHANG)
            assert done == pid, output.decode(errors="replace")
            assert os.waitstatus_to_exitcode(status) == expected_rc, output.decode(errors="replace")
            assert sent_code and sent_answer and p.nonce.encode() not in output
            clock[0] = time.time()
            assert workspace.status()[0] == ("active" if expected_rc == 0 else "absent")
        finally:
            os.close(fd)
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except ProcessLookupError:
                pass
        if expected_rc == 4:
            assert workspace.inspect(p.nonce) == p
    with sqlite3.connect(access.path) as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    status = access.local_action_status()
    assert status.message_grants == status.nudge_grants == status.feedback_grants == ()
