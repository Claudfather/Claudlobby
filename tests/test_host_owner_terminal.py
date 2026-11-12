"""Real child CLI / controlling terminal; all state and principals are synthetic."""

import errno
import os
import select
import signal
import sys
import time

import pytest

from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.owner_access import AccessDenied, OwnerAccess, PrincipalRef
from tests.conftest import constructed_env

# The activation fixture temporarily replaces sys.executable with a native stub.
_PYTHON = sys.executable


def _command(root, action, dialogue, *, args=(), bootstrap=None, on_prompt=None):
    import pty

    tail = ["--root", str(root), "host", "owner", action, *args]
    argv = [_PYTHON, "-c", bootstrap, *tail] if bootstrap else [_PYTHON, "-m", "claudlobby", *tail]
    env = constructed_env()
    if bootstrap == _RUNTIME_FIXTURE:
        # Carry only the explicit private installation, never ambient bot/auth state.
        env['CLAUDLOBBY_INSTALLATION'] = os.environ['CLAUDLOBBY_INSTALLATION']
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(_PYTHON, argv, env)
    output = bytearray()
    pending = list(dialogue)
    status = None
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if pending and pending[0][0].encode() in output:
                    prompt, answer = pending.pop(0)
                    if on_prompt is not None:
                        on_prompt(prompt)
                    os.write(fd, (answer + "\n").encode())
            done, status_value = os.waitpid(pid, os.WNOHANG)
            if done:
                status = status_value
                break
        while status is None and time.monotonic() < deadline:
            done, status_value = os.waitpid(pid, os.WNOHANG)
            if done:
                status = status_value
            else:
                time.sleep(0.01)
        assert status is not None, "owner terminal command did not finish: " + output.decode(errors="replace")
        assert not pending, "owner terminal command missed prompts: " + output.decode(errors="replace")
        return os.waitstatus_to_exitcode(status), output.decode(errors="replace")
    finally:
        os.close(fd)
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="owner local console requires a POSIX terminal")
def test_initialize_pair_revoke_through_actual_cli_terminal(active):
    root, _ = active
    store = OwnerAccess(root)
    code, output = _command(root, "initialize", [("Type INITIALIZE", "INITIALIZE")], bootstrap=_RUNTIME_FIXTURE)
    assert code == 0, output
    assert store.current_grant() is None
    principal = PrincipalRef("test-verifier", "human-001")
    challenge = store.begin_pairing(principal)
    code, output = _command(root, "confirm", [
        ("Paste the browser pairing code (hidden):", challenge.token),
        ("Type PAIR", "PAIR"),
    ], bootstrap=_RUNTIME_FIXTURE)
    assert code == 0, output
    assert principal.subject in output and challenge.token not in output
    assert store.current_grant().principal == principal
    session = store.open_session(principal)
    code, output = _command(root, "revoke", [("Type REVOKE", "REVOKE")], bootstrap=_RUNTIME_FIXTURE)
    assert code == 0, output
    assert not store.current_grant().active
    with pytest.raises(AccessDenied):
        store.authorize_read(session.token, principal, host_uid=session.grant.host_uid)


@pytest.mark.skipif(os.name != "posix", reason="owner local console requires a POSIX terminal")
def test_attest_existing_source_through_actual_cli_terminal(active):
    from claudlobby.plane.owner_source import inspect_source

    root, _ = active
    OwnerAccess.initialize(root)
    code, output = _command(root, "bind-source", [("Type BIND", "BIND")], bootstrap=_RUNTIME_FIXTURE)
    assert code == 0, output
    assert "including imported records" in output
    inspect_source(root)


# Only release/native-manager identity, which includes the operator shell's
# unit ancestry, is a fixture; terminal approval, CLI dispatch, active config,
# mutation admission, Plane registration and authority writes are real. This is
# not a sealed installed CLI or live bot canary.
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401
from tests.test_releases import installed  # noqa: F401
from tests.test_task_read_cli import active  # noqa: F401

_RUNTIME_FIXTURE = """
import sys
from pathlib import Path
from dataclasses import replace
from claudlobby import context
from claudlobby.installation import read_installation
from claudlobby.resources import get_resources
from claudlobby.runtime_admission import RuntimeIdentity
root = Path(sys.argv[sys.argv.index('--root') + 1])
installation = read_installation(Path(__import__('os').environ['CLAUDLOBBY_INSTALLATION']))
assert installation.data_root == root
package = get_resources(installation=installation)
context.get_resources = lambda: package
RuntimeIdentity.current = classmethod(lambda cls: RuntimeIdentity(
    installation.cli, installation.native, '', installation, package.source_revision))
from claudlobby.commands import operator_context
operator_context.require_operator_context = lambda root=None: None
from claudlobby.__main__ import main
raise SystemExit(main(sys.argv[1:]))
"""


@pytest.mark.skipif(os.name != "posix", reason="requires a controlling terminal")
def test_ordinary_message_registration_grant_revoke_through_actual_terminal(active):
    import sqlite3
    from claudlobby.plane.db import db_file
    root, host = active
    store = OwnerAccess.initialize(root)
    principal = PrincipalRef("test-verifier", "human-001")
    challenge = store.begin_pairing(principal)
    owner = store.confirm_pairing(challenge.token, expected_principal=principal)
    observed = []
    def inspect(prompt):
        with sqlite3.connect(db_file(root)) as conn:
            row = conn.execute("SELECT uid FROM identity_registry WHERE kind='actor' AND alias='human:terminal-owner'").fetchone()
        if prompt == "Type REGISTER":
            assert row is None
        else:
            assert row is not None
            observed.append(row[0])
    code, output = _command(root, "allow-messages", [("Type REGISTER", "REGISTER"), ("Type ALLOW", "ALLOW")],
        args=("--target-fleet", "example", "--actor", "human:terminal-owner", "--register-actor"),
        bootstrap=_RUNTIME_FIXTURE, on_prompt=inspect)
    assert code == 0, output
    assert "ordinary messages only" in output and observed[0] in output
    from claudlobby.activation_identity import read_current_identity_bindings
    fleet = read_current_identity_bindings(root, "example", package=host.package)["fleet_uid"]
    grant = store.current_message_grant(expected_owner=owner, fleet_uid=fleet)
    assert grant.actor_uid == observed[0]
    assert not (root / "state/selected-release.json").exists()
    code, output = _command(root, "revoke-messages", [("Type REVOKE-MESSAGES", "REVOKE-MESSAGES")], args=("--fleet-uid", fleet), bootstrap=_RUNTIME_FIXTURE)
    assert code == 0, output
    assert fleet in output and observed[0] in output
    assert store.current_grant() == owner
    with pytest.raises(AccessDenied):
        store.current_message_grant(expected_owner=owner, fleet_uid=fleet)


@pytest.mark.skipif(os.name != "posix", reason="requires a controlling terminal")
def test_selected_actions_register_and_approve_once_through_actual_terminal(active):
    root, _ = active
    store = OwnerAccess.initialize(root)
    principal = PrincipalRef("test-verifier", "human-001")
    challenge = store.begin_pairing(principal)
    store.confirm_pairing(challenge.token, expected_principal=principal)
    code, output = _command(root, "allow-actions", [("Type REGISTER", "REGISTER"),
        ("Type ALLOW-ACTIONS", "ALLOW-ACTIONS")],
        args=("--target-fleet", "example", "--actor", "human:terminal-actions", "--register-actor",
              "--capabilities", "messages", "nudges", "feedback"), bootstrap=_RUNTIME_FIXTURE)
    assert code == 0, output
    status = store.local_action_status()
    assert len(status.message_grants) == len(status.nudge_grants) == len(status.feedback_grants) == 1
    assert status.permission_grants == ()
    assert all(grant.actor_alias == "human:terminal-actions" for grant in
               (*status.message_grants, *status.nudge_grants, *status.feedback_grants))
    assert output.count("Type REGISTER") == output.count("Type ALLOW-ACTIONS") == 1


@pytest.mark.skipif(os.name != "posix", reason="requires a controlling terminal")
def test_terminal_owner_revision_change_before_allow_refuses(active):
    import sqlite3
    from claudlobby.plane.db import db_file
    root, _ = active
    store = OwnerAccess.initialize(root)
    principal = PrincipalRef("test-verifier", "human-001")
    challenge = store.begin_pairing(principal)
    owner = store.confirm_pairing(challenge.token, expected_principal=principal)
    def race(prompt):
        if prompt == "Type ALLOW":
            store.revoke_owner(expected_revision=owner.revision)
            fresh = store.begin_pairing(principal)
            store.confirm_pairing(fresh.token, expected_principal=principal)
    code, output = _command(root, "allow-messages", [("Type REGISTER", "REGISTER"), ("Type ALLOW", "ALLOW")],
        args=("--target-fleet", "example", "--actor", "human:terminal-race", "--register-actor"),
        bootstrap=_RUNTIME_FIXTURE, on_prompt=race)
    assert code == 4, output
    with sqlite3.connect(store.path) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='message_grants'").fetchone()
    assert store.current_grant().revision != owner.revision


@pytest.mark.skipif(os.name != "posix", reason="requires a controlling terminal")
def test_nudge_registration_and_independent_grant_through_actual_terminal(active):
    from claudlobby.activation_identity import read_current_identity_bindings
    root, host = active
    store = OwnerAccess.initialize(root)
    principal = PrincipalRef("test-verifier", "human-001")
    challenge = store.begin_pairing(principal)
    owner = store.confirm_pairing(challenge.token, expected_principal=principal)
    code, output = _command(root, "allow-nudges", [("Type REGISTER", "REGISTER"),
        ("Type ALLOW-NUDGES", "ALLOW-NUDGES")],
        args=("--target-fleet", "example", "--actor", "human:terminal-nudge", "--register-actor"),
        bootstrap=_RUNTIME_FIXTURE)
    assert code == 0, output
    assert "selected-task nudges only" in output
    fleet = read_current_identity_bindings(root, "example", package=host.package)["fleet_uid"]
    grant = store.current_nudge_grant(expected_owner=owner, fleet_uid=fleet)
    assert grant.actor_alias == "human:terminal-nudge"
    with pytest.raises(AccessDenied, match="messages_not_allowed"):
        store.current_message_grant(expected_owner=owner, fleet_uid=fleet)
    assert not (root / "state/selected-release.json").exists()
    code, output = _command(root, "revoke-nudges", [("Type REVOKE-NUDGES", "REVOKE-NUDGES")],
                            args=("--fleet-uid", fleet), bootstrap=_RUNTIME_FIXTURE)
    assert code == 0, output
    with pytest.raises(AccessDenied, match="nudges_not_allowed"):
        store.current_nudge_grant(expected_owner=owner, fleet_uid=fleet)


@pytest.mark.skipif(os.name != "posix", reason="requires a controlling terminal")
@pytest.mark.parametrize("capability,kind", [("feedback", "feedback"), ("permissions", "permission")])
def test_action_registration_and_independent_grant_through_actual_terminal(active, capability, kind):
    from claudlobby.activation_identity import read_current_identity_bindings
    root, host = active
    store = OwnerAccess.initialize(root)
    principal = PrincipalRef("test-verifier", "human-001")
    challenge = store.begin_pairing(principal)
    owner = store.confirm_pairing(challenge.token, expected_principal=principal)
    code, output = _command(root, "allow-" + capability, [("Type REGISTER", "REGISTER"),
        ("Type ALLOW-" + capability.upper(), "ALLOW-" + capability.upper())],
        args=("--target-fleet", "example", "--actor", "human:terminal-feedback", "--register-actor"),
        bootstrap=_RUNTIME_FIXTURE)
    assert code == 0, output
    assert ("task-linked comments" if capability == "feedback" else "approve, decline and revoke") in output
    fleet = read_current_identity_bindings(root, "example", package=host.package)["fleet_uid"]
    grant = getattr(store, "current_" + kind + "_grant")(expected_owner=owner, fleet_uid=fleet)
    assert grant.actor_alias == "human:terminal-feedback"
    with pytest.raises(AccessDenied, match="messages_not_allowed"):
        store.current_message_grant(expected_owner=owner, fleet_uid=fleet)
    assert not (root / "state/selected-release.json").exists()
    code, output = _command(root, "revoke-" + capability, [("Type REVOKE-" + capability.upper(), "REVOKE-" + capability.upper())],
                            args=("--fleet-uid", fleet), bootstrap=_RUNTIME_FIXTURE)
    assert code == 0, output
    with pytest.raises(AccessDenied, match=capability + "_not_allowed"):
        getattr(store, "current_" + kind + "_grant")(expected_owner=owner, fleet_uid=fleet)


@pytest.mark.skipif(os.name != "posix", reason="owner local console requires POSIX")
def test_guided_setup_initial_consent_cancellation_over_real_terminal(tmp_path):
    root = tmp_path / "data"; root.mkdir()
    directory = tmp_path / "units"; directory.mkdir()
    bootstrap = """import sys
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from claudlobby.commands import host_owner_setup
from claudlobby import resources
from claudlobby.installation import Installation
from claudlobby.__main__ import main
root=Path(sys.argv[sys.argv.index('--root')+1])
package=replace(resources.get_resources(), installation=Installation(root.parent/'installation',root),source_revision='a'*40)
resources.get_resources=lambda:package
host_owner_setup._native_directories=lambda _: (Path(sys.argv[sys.argv.index('--install-directory')+1]),)
raise SystemExit(main(sys.argv[1:]))
"""
    args = ("--origin", "https://host.example.test", "--tailscale", sys.executable,
        "--workspace-issuer", "https://accounts.example.test", "--workspace-origin", "https://plane.example.test",
        "--target-fleet", "shop", "--repository", "example-org/shop", "--install-directory", str(directory),
        "--socket", str(root/"state/owner/owner.sock"))
    rc, output = _command(root, "setup", [("Native Tailscale control socket", ""), ("Type INITIALIZE", "NO")], args=args, bootstrap=bootstrap)
    assert rc == 4 and "owner change cancelled" in output
    assert not (root / "state").exists()
