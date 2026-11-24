"""Finite owner guide framing and consent, using private OS pipes."""

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def wire(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


@pytest.fixture
def pipes():
    incoming, client = os.pipe()
    observer, outgoing = os.pipe()
    yield incoming, outgoing, client, observer
    for fd in (incoming, outgoing, client, observer):
        try:
            os.close(fd)
        except OSError:
            pass


def start(session, **changes):
    value = {"version": 1, "type": "start", "seq": 1, "session_id": session,
             "profile": "owner-setup-v1", "expected_uid": 501,
             "source_revision": "a" * 40, "binding_sha256": "b" * 64,
             "finish_setup": None}
    return {**value, **changes}


def read_frame(fd):
    raw = bytearray()
    while not raw.endswith(b"\n"):
        part = os.read(fd, 1)
        assert part, "guide output ended without a frame"
        raw.extend(part)
    value = json.loads(raw)
    assert raw == wire(value)
    return value


def admitted(pipes):
    from claudlobby.commands.owner_stdio import OwnerStdio
    incoming, outgoing, client, observer = pipes
    console = OwnerStdio(incoming, outgoing)
    console.hello()
    hello = read_frame(observer)
    assert hello["type"] == "hello" and hello["seq"] == 1
    os.write(client, wire(start(hello["session_id"])))
    assert console.start()["expected_uid"] == 501
    return console, hello["session_id"]


@pytest.mark.parametrize("action", ["initialize_workspace", "install_provider"])
def test_stdio_approves_the_retained_original_object_only(pipes, action):
    console, session = admitted(pipes)
    if action == "install_provider":
        from claudlobby.commands.owner_setup_consent import StdioGuideConsent
        StdioGuideConsent(console).state("provider_installing", provider_version="2.1.325", platform="linux-x64")
        progress = read_frame(pipes[3])
        assert progress["version"] == 1 and progress["provider_version"] == "2.1.325"
    original = object()
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(console.approve(
        action, {"host_uid": "example-host", "effects": ["displayed core effect"]},
        original=original)))
    thread.start()
    proposal = read_frame(pipes[3])
    snapshot = proposal["proposal"]
    assert proposal["type"] == "proposal"
    assert snapshot["action"] == action
    assert proposal["digest"] == hashlib.sha256(wire(snapshot).rstrip(b"\n")).hexdigest()
    os.write(pipes[2], wire({"version": 1, "type": "decide", "seq": 2,
        "session_id": session, "proposal_id": proposal["proposal_id"],
        "digest": proposal["digest"], "approve": True}))
    thread.join(2)
    assert not thread.is_alive() and outcome == [original]


@pytest.mark.parametrize("mutation", [
    {"seq": 2}, {"session_id": "different"}, {"profile": "other"},
    {"expected_uid": True}, {"source_revision": "A" * 40},
    {"binding_sha256": "b" * 63}, {"env": {}}, {"path": "/tmp/arbitrary"},
])
def test_stdio_start_rejects_substitution_and_unknown_fields(pipes, mutation):
    from claudlobby.commands.owner_stdio import OwnerStdio, StdioRefused
    console = OwnerStdio(pipes[0], pipes[1])
    console.hello()
    hello = read_frame(pipes[3])
    os.write(pipes[2], wire(start(hello["session_id"], **mutation)))
    with pytest.raises(StdioRefused):
        console.start()


@pytest.mark.parametrize("raw", [b"{}\n", b'{"version":1,"version":1}\n',
    b'{"value":"\xff"}\n', b' {"version":1}\n', b"[]\n", b"x" * 16385])
def test_stdio_rejects_noncanonical_or_oversized_frames(pipes, raw):
    from claudlobby.commands.owner_stdio import OwnerStdio, StdioRefused
    console = OwnerStdio(pipes[0], pipes[1])
    console.hello()
    read_frame(pipes[3])
    writer = threading.Thread(target=lambda: os.write(pipes[2], raw), daemon=True)
    writer.start()
    with pytest.raises(StdioRefused):
        console.start()
    writer.join(2)


def test_stdio_eof_refuses_without_a_decision(pipes):
    from claudlobby.commands.owner_stdio import StdioRefused
    console, _ = admitted(pipes)
    os.close(pipes[2])
    with pytest.raises(StdioRefused):
        console.approve("initialize_owner", {"effects": ["initialize authority"]})


@pytest.mark.parametrize("change", [
    {"approve": False}, {"approve": "true"}, {"seq": 3},
    {"session_id": "different"}, {"proposal_id": "substituted"},
    {"digest": "0" * 64}, {"scope": {"host_uid": "replacement"}},
])
def test_stdio_changed_or_declined_decision_never_returns_approval(pipes, change):
    from claudlobby.commands.owner_stdio import StdioRefused
    console, session = admitted(pipes)
    failures, returned = [], []
    def proposal():
        try:
            returned.append(console.approve("pair_owner", {"host_uid": "example-host"}))
        except StdioRefused as error:
            failures.append(error)
    thread = threading.Thread(target=proposal)
    thread.start()
    pending = read_frame(pipes[3])
    decision = {"version": 1, "type": "decide", "seq": 2,
        "session_id": session, "proposal_id": pending["proposal_id"],
        "digest": pending["digest"], "approve": True}
    os.write(pipes[2], wire({**decision, **change}))
    thread.join(2)
    assert not thread.is_alive() and len(failures) == 1 and not returned


def test_stdio_expired_proposal_cannot_be_approved(pipes):
    from claudlobby.commands.owner_stdio import StdioRefused
    console, _ = admitted(pipes)
    with pytest.raises(StdioRefused):
        console.approve("bind_workspace", {"host_uid": "example-host"}, expires_at=time.time() - 1)


def test_stdio_old_decision_cannot_approve_a_new_proposal(pipes):
    from claudlobby.commands.owner_stdio import StdioRefused
    console, session = admitted(pipes)
    results, failures = [], []
    def proposals():
        try:
            results.append(console.approve("initialize_workspace", {}))
            console.approve("initialize_connected", {})
        except StdioRefused as error:
            failures.append(error)
    thread = threading.Thread(target=proposals)
    thread.start()
    first = read_frame(pipes[3])
    decision = {"version": 1, "type": "decide", "seq": 2, "session_id": session,
                "proposal_id": first["proposal_id"], "digest": first["digest"], "approve": True}
    os.write(pipes[2], wire(decision))
    second = read_frame(pipes[3])
    assert second["proposal_id"] != first["proposal_id"]
    os.write(pipes[2], wire({**decision, "seq": 3}))
    thread.join(2)
    assert not thread.is_alive() and len(results) == 1 and len(failures) == 1


def test_typed_workspace_presenter_keeps_exact_signed_proposal(tmp_path):
    from claudlobby.commands.owner_setup_consent import StdioGuideConsent
    from claudlobby.plane.owner_consent import apply_workspace_binding
    from tests.test_owner_consent import installation, proposed
    access, owner, clock = installation(tmp_path / "authority")
    proposal = proposed((access, owner, clock))
    seen = []
    class Channel:
        def approve(self, action, scope, *, original, expires_at):
            seen.append((action, scope, original, expires_at))
            return original
    result = apply_workspace_binding(access, proposal, presenter=StdioGuideConsent(Channel()))
    assert seen[0][0] == "bind_workspace" and seen[0][2] is proposal
    assert seen[0][1]["attestation"]["workspace_id"] == proposal.attestation.workspace_id
    assert seen[0][3] == proposal.expires_at and result.workspace_id == proposal.attestation.workspace_id


def test_typed_workspace_presenter_decline_cannot_change_authority(tmp_path):
    from claudlobby.commands.owner_setup_consent import StdioGuideConsent
    from claudlobby.plane.owner_consent import apply_workspace_binding
    from claudlobby.commands.owner_stdio import StdioRefused
    from tests.test_owner_consent import installation, proposed, effects
    access, owner, clock = installation(tmp_path / "authority")
    proposal = proposed((access, owner, clock))
    before = effects(access)
    class Channel:
        def approve(self, *args, **kwargs):
            raise StdioRefused("declined")
    with pytest.raises(StdioRefused):
        apply_workspace_binding(access, proposal, presenter=StdioGuideConsent(Channel()))
    assert effects(access) == before


def test_typed_storage_requires_both_distinct_confirmations(tmp_path):
    from claudlobby.commands import host_owner
    from claudlobby.commands.owner_stdio import StdioRefused
    from tests.test_owner_consent import installation
    access, owner, _ = installation(tmp_path / "authority", workspace=False)
    seen = []
    class Consent:
        def approve(self, action, scope, **kwargs):
            seen.append(action)
            if action == "initialize_connected":
                raise StdioRefused("declined")
    with pytest.raises(StdioRefused):
        host_owner._initialize_connected(access, None, prepare_workspace=True, consent=Consent())
    assert seen == ["initialize_workspace", "initialize_connected"]
    import sqlite3
    with sqlite3.connect(access.path) as conn:
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('workspace_metadata','connected_metadata')").fetchone()[0] == 0


@pytest.fixture
def fixed_profile(tmp_path, monkeypatch):
    import platform
    import pwd
    import sys
    import sysconfig
    # Emulate only the profile's platform predicate, after CPython has grounded
    # its own build metadata in the actual test interpreter's platform.
    sysconfig.get_config_vars()
    from claudlobby.installation import Installation
    home = tmp_path / "account"
    home.mkdir(mode=0o700)
    family = home / ".local/share/claudlobby-browser"
    for name in ("install", "data", "staging"):
        (family / name).mkdir(parents=True, mode=0o700)
    interpreter = tmp_path / "independent-python"
    interpreter.write_bytes(b"synthetic interpreter; never executed")
    interpreter.chmod(0o755)
    binding = {"schema": 1, "purpose": "installation-preparation-only", "installation": str(family / "install"),
        "data_root": str(family / "data"), "remote": "https://github.com/Claudfather/Claudlobby.git", "ref": "a" * 40,
        "interpreter": str(interpreter), "interpreter_sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest()}
    receipt = {**binding, "revision": "a" * 40, "lock_sha256": "c" * 64, "environment_owned": True, "complete": True}
    path = family / "install/preparation.json"
    path.write_bytes(json.dumps(receipt, sort_keys=True).encode())
    path.chmod(0o600)
    request = start("unused", expected_uid=os.getuid(), binding_sha256=hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest())
    package = SimpleNamespace(installation=Installation(family / "install", family / "data"), source_revision="a" * 40)
    monkeypatch.setattr(pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(home), pw_name="example-account", pw_uid=os.getuid()))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(Installation, "preflight", lambda self, **kwargs: "a" * 40)
    # The running Linux interpreter, trusted from this directory and owner as
    # /usr/bin and root are on a real computer.
    from claudlobby import bootstrap
    monkeypatch.setattr(bootstrap, "_SYSTEM_PYTHON_DIRECTORY", tmp_path)
    monkeypatch.setattr(bootstrap, "_SYSTEM_PYTHON_OWNER", os.getuid())
    monkeypatch.setattr(sys, "executable", str(interpreter))
    return SimpleNamespace(root=family / "data", package=package), request, home, path


def test_fixed_profile_ignores_caller_home_and_uses_same_private_policy(fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    paths, request, home, _ = fixed_profile
    monkeypatch.setenv("HOME", "/untrusted-home")
    monkeypatch.setenv("CLAUDLOBBY_HOST_SYSTEM_YAML", "/untrusted-policy")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/personal-provider")
    profile = FixedOwnerProfile(paths, request)
    with profile.environment():
        assert os.environ["HOME"] == str(home)
        assert os.environ["CLAUDLOBBY_HOST_SYSTEM_YAML"] == str(home / ".local/share/claudlobby-browser/host-policy/system.yaml")
        assert os.environ["CLAUDE_CONFIG_DIR"] == str(home / ".local/share/claudlobby-browser/provider-profile")
        assert profile.provider == home / ".local/share/claudlobby-browser/provider-bin/claude"
    assert os.environ["HOME"] == "/untrusted-home"


def test_linux_fixed_owner_environment_lets_first_team_setup_record(fixed_profile, monkeypatch):
    # A browser Preview refused at prepare on a real computer: the guide's own
    # fixed environment carried the harness silencer, the listener it starts
    # inherited it, and first-fleet setup refuses under it by design (#2611).
    import subprocess
    import sys
    from claudlobby import env_tiers as tiers
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.plane import registry_emit as registry
    paths, request, _home, _ = fixed_profile
    fleet = SimpleNamespace(name="first-team")
    monkeypatch.setattr(tiers, "resolve", lambda *a, **k: {})
    silenced = "PLANE_EMIT_DISABLED=1 in this command's environment silences"
    # An operator who exports the switch is still refused, here and after the guide.
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    assert registry.activation_scan_refusal(paths, fleet).startswith(silenced)
    # A shell child, not sys.executable: this fixture stands a file in for the interpreter.
    child = ["/bin/sh", "-c", 'printf %s "${PLANE_EMIT_DISABLED-None}"']
    profile = FixedOwnerProfile(paths, request)
    with profile.environment():
        assert registry.activation_scan_refusal(paths, fleet) is None
        assert "PLANE_EMIT_DISABLED" not in os.environ
        # The listener and the update's stages are children that inherit this.
        assert subprocess.run(child, capture_output=True, text=True, check=True).stdout.strip() == "None"
    assert registry.activation_scan_refusal(paths, fleet).startswith(silenced)


@pytest.mark.parametrize("change", ["uid", "binding", "source", "incomplete", "receipt_link"])
def test_fixed_profile_refuses_changed_installation_without_writes(fixed_profile, change):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    paths, request, home, path = fixed_profile
    if change == "uid":
        request["expected_uid"] += 1
    elif change == "binding":
        request["binding_sha256"] = "f" * 64
    elif change == "source":
        request["source_revision"] = "f" * 40
    elif change == "incomplete":
        value = json.loads(path.read_bytes()); value["complete"] = False; path.write_text(json.dumps(value))
    else:
        target = path.with_suffix(".saved"); path.rename(target); path.symlink_to(target)
    before = sorted(str(p) for p in home.rglob("*"))
    with pytest.raises(StdioRefused):
        FixedOwnerProfile(paths, request)
    assert sorted(str(p) for p in home.rglob("*")) == before


def test_stdio_generated_selector_refusal_is_framed_before_environment_scrub(tmp_path):
    import subprocess
    import sys
    from tests.conftest import constructed_env
    root = tmp_path / "never-created"
    process = subprocess.run([sys.executable, "-m", "claudlobby", "--root", str(root),
        "host", "owner", "setup", "--stdio"], input=b"", stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=constructed_env(BOT_NAME="synthetic-generated"), timeout=10)
    frames = [json.loads(line) for line in process.stdout.splitlines()]
    assert process.returncode != 0 and frames[-1]["type"] == "error"
    assert frames[-1]["code"] == "operator_required" and not root.exists()
    assert all(wire(frame).strip() == line for frame, line in zip(frames, process.stdout.splitlines()))
    assert not process.stderr


def test_serving_poll_has_no_admission_timer_and_partial_cancel_cannot_block(pipes):
    console, _ = admitted(pipes)
    console._began -= 1900
    console.check_cancel()
    os.write(pipes[2], b'{"type":')
    finished = threading.Event()
    def poll():
        from claudlobby.commands.owner_stdio import StdioRefused
        try:
            console.check_cancel()
        except StdioRefused:
            pass
        finally:
            finished.set()
    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    try:
        assert finished.wait(.25), "partial input blocked grant/liveness polling"
    finally:
        os.close(pipes[2])
        thread.join(2)


def test_unknown_provider_refuses_before_install_effects(fixed_profile, monkeypatch):
    # The accepted helper owns byte recognition; the guide must never offer a
    # replacement for its foreign/blocked observation.
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    paths, request, home, _ = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    profile.provider.parent.mkdir(mode=0o700)
    profile.provider.write_bytes(b"not a supported provider")
    profile.provider.chmod(0o700)
    before = sorted(str(p) for p in home.rglob("*"))
    consent = SimpleNamespace(approve=lambda *_args, **_kwargs: pytest.fail("unusable provider was proposed"))
    monkeypatch.setattr("claudlobby.provider_install.status", lambda *_: provider_result(profile, "blocked"))
    with pytest.raises(StdioRefused, match="provider_install_blocked"):
        profile.prepare_provider(consent, paths)
    assert sorted(str(p) for p in home.rglob("*")) == before


@pytest.mark.parametrize("decision", ["approve", "decline", "substitute"])
def test_fixed_directory_effects_are_explicit_create_only_and_revalidated(fixed_profile, monkeypatch, decision):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby.commands import host_owner_setup
    paths, request, home, _ = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    monkeypatch.setattr(host_owner_setup, "_native_directories", lambda _: (profile.units,))
    proposals = []
    class Consent:
        def approve(self, action, scope):
            assert action == "prepare_directories"
            proposals.append(scope)
            assert scope["empty_private_environment"] == str(profile.data / ".env")
            assert not profile.project.exists() and not (profile.data / ".env").exists()
            if decision == "decline":
                raise StdioRefused("declined")
            if decision == "substitute":
                profile.project.parent.symlink_to(home)
    if decision == "approve":
        profile.prepare_directories(Consent(), paths)
        assert profile.project.is_dir() and profile.account.is_dir() and profile.units.is_dir()
        assert (profile.data / ".env").read_bytes() == b""
        assert (profile.data / ".env").stat().st_mode & 0o777 == 0o600
        assert profile.policy.parent.stat().st_mode & 0o777 == 0o700
    else:
        with pytest.raises(StdioRefused):
            profile.prepare_directories(Consent(), paths)
        assert not (profile.data / ".env").exists()
    assert len(proposals) == 1


def test_stdio_refuses_nonprofile_root_before_context_or_authority_reads(fixed_profile, monkeypatch):
    from claudlobby.commands.owner_stdio import _admit, StdioRefused
    from claudlobby.commands import operator_context
    from claudlobby import context
    paths, request, home, _ = fixed_profile
    args = SimpleNamespace(root=str(home / "other-data"), fleet=None, seed=False, json=False,
                           template="revenue-web-app")
    monkeypatch.setattr(context, "resolve_paths", lambda **_: pytest.fail("resolved an unselected root"))
    monkeypatch.setattr(operator_context, "require_operator_context", lambda *_: pytest.fail("observed an unselected root"))
    with pytest.raises(StdioRefused, match="installation_changed"):
        _admit(args, request)


def test_stdio_status_is_read_only_with_absent_authority(fixed_profile):
    from claudlobby.commands.owner_stdio import _status
    from claudlobby.plane.owner_access import OwnerAccess
    paths, _, home, _ = fixed_profile
    before = sorted(str(p) for p in home.rglob("*"))
    assert _status(paths, OwnerAccess(paths.root)) == {"state": "status", "owner": None,
        "setup": {"state": "absent"}, "operation": None}
    assert sorted(str(p) for p in home.rglob("*")) == before


@pytest.mark.parametrize("native,expected", [
    ({"BackendState": "Running", "Self": {"Online": True, "DNSName": "example-host.example-tailnet.ts.net."}},
        "https://example-host.example-tailnet.ts.net"),
    ({"BackendState": "NeedsLogin", "Self": None}, None),
    ({"BackendState": "Running", "Self": []}, None),
    ({"BackendState": "Running", "Self": {"Online": False, "DNSName": "example-host.example-tailnet.ts.net."}}, None),
    ({"BackendState": "Running", "Self": {"Online": True, "Expired": True, "DNSName": "example-host.example-tailnet.ts.net."}}, None),
    ({"BackendState": "Running", "Self": {"Online": True, "DNSName": "example-host.example-tailnet.ts.net./other"}}, None),
], ids=["official-self", "not-signed-in", "malformed-self", "offline", "expired", "arbitrary-url"])
def test_fixed_origin_uses_only_official_native_self_identity(fixed_profile, monkeypatch, native, expected):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby.plane import owner_ingress
    paths, request, _, _ = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    class Native:
        def __init__(self, *, tailscale_binary):
            assert tailscale_binary == Path("/usr/bin/tailscale")
        async def _realm(self): pass
        async def _command(self, *args):
            assert args == ("status", "--json")
            return native
    monkeypatch.setattr(owner_ingress, "ServePrincipalVerifier", Native)
    if expected is None:
        with pytest.raises(StdioRefused, match="native_ingress_prerequisite_missing"):
            profile.origin()
    else:
        assert profile.origin() == expected


def provider_result(profile, state="missing"):
    from claudlobby.provider_install import ProviderResult
    return ProviderResult(state, "synthetic_fixture", str(profile.provider), "0dc4f4a9a165fc60746d7ae8489712e5fc352f1e7ad4eb22e7141bd0812eb0cf",
        "2.1.325", "linux-x64")


def test_install_provider_ready_is_readonly_without_a_proposal(fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby import provider_install
    paths, request, home, _ = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    ready = provider_result(profile, "ready")
    monkeypatch.setattr(provider_install, "status", lambda account, uid: ready)
    monkeypatch.setattr(provider_install, "prepare", lambda *_a, **_k: pytest.fail("reinstalled canonical bytes"))
    consent = SimpleNamespace(approve=lambda *_a, **_k: pytest.fail("proposed a no-op"))
    before = sorted(str(p) for p in home.rglob("*"))
    assert profile.prepare_provider(consent, paths) == ready
    assert sorted(str(p) for p in home.rglob("*")) == before


def test_install_provider_missing_requires_one_explicit_separate_consent(fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby import provider_install
    paths, request, _, _ = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    profile.provider.parent.mkdir(mode=0o700)
    approved = []
    def status(account, uid):
        assert account == "example-account" and uid == os.getuid()
        return provider_result(profile)
    def prepare(account, uid, *, cancel):
        assert approved == ["install_provider"] and not cancel.is_set()
        profile.provider.write_bytes(b"synthetic public executable fixture; never executed")
        return provider_result(profile, "ready")
    monkeypatch.setattr(provider_install, "status", status)
    monkeypatch.setattr(provider_install, "prepare", prepare)
    class Consent:
        def approve(self, action, scope):
            assert not profile.provider.exists()
            assert scope["version"] == "2.1.325" and scope["download_host"] == "downloads.claude.ai"
            assert scope["binary"] == str(profile.provider)
            approved.append(action)
        def check_cancel(self): pass
        def state(self, state, **details):
            assert state == "provider_installing"
            assert details == {"provider_version": "2.1.325", "platform": "linux-x64"}
        def install_provider(self, operation): return operation(threading.Event())
    assert profile.prepare_provider(Consent(), paths).state == "ready"
    assert profile.provider.read_bytes() == b"synthetic public executable fixture; never executed"


@pytest.mark.parametrize("code", ["declined", "disconnected", "cancelled"])
def test_install_provider_unapproved_or_cancelled_admission_creates_no_attempt(fixed_profile, monkeypatch, code):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby import provider_install
    paths, request, home, _ = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    monkeypatch.setattr(provider_install, "status", lambda *_: provider_result(profile))
    monkeypatch.setattr(provider_install, "prepare", lambda *_a, **_k: pytest.fail("unapproved helper effect"))
    class Consent:
        def approve(self, *_a, **_k):
            if code == "declined": raise StdioRefused(code)
        def check_cancel(self): raise StdioRefused(code)
    before = sorted(str(p) for p in home.rglob("*"))
    with pytest.raises(StdioRefused, match=code):
        profile.prepare_provider(Consent(), paths)
    assert sorted(str(p) for p in home.rglob("*")) == before


@pytest.mark.parametrize("state", ["unknown", "blocked", "cancelled"])
def test_install_provider_uncertain_result_never_repeats_in_this_invocation(fixed_profile, monkeypatch, state):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby import provider_install
    paths, request, _, _ = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    monkeypatch.setattr(provider_install, "status", lambda *_: provider_result(profile))
    effects = []
    def prepare(*_a, **_k):
        effects.append(True)
        return provider_result(profile, state)
    monkeypatch.setattr(provider_install, "prepare", prepare)
    class Consent:
        def approve(self, *_a, **_k): pass
        def check_cancel(self): pass
        def state(self, *_a, **_k): pass
        def install_provider(self, operation): return operation(threading.Event())
    for _ in range(2):
        with pytest.raises(StdioRefused):
            profile.prepare_provider(Consent(), paths)
    assert effects == [True]


def test_install_provider_foreign_status_refuses_without_proposing_an_effect(fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby import provider_install
    paths, request, _, _ = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    monkeypatch.setattr(provider_install, "status", lambda *_: provider_result(profile, "blocked"))
    consent = SimpleNamespace(approve=lambda *_a, **_k: pytest.fail("foreign provider was proposed"))
    with pytest.raises(StdioRefused, match="provider_install_blocked"):
        profile.prepare_provider(consent, paths)


@pytest.mark.parametrize("interruption", ["eof", "cancel"])
def test_install_provider_pipe_cancel_drains_and_joins_admitted_helper(pipes, interruption):
    from claudlobby.commands.owner_stdio import StdioRefused
    console, session = admitted(pipes)
    began, drained = threading.Event(), threading.Event()
    def prepare(cancel):
        began.set()
        assert cancel.wait(2), "guide did not pass EOF to its owned helper"
        drained.set()
        return "cancelled fixture"
    failures = []
    def invoke():
        try: console.install_provider(prepare)
        except StdioRefused as exc: failures.append(exc.code)
    thread = threading.Thread(target=invoke)
    thread.start()
    assert began.wait(2)
    if interruption == "eof":
        os.close(pipes[2])
    else:
        os.write(pipes[2], wire({"version": 1, "type": "cancel", "seq": 2, "session_id": session}))
    thread.join(3)
    assert not thread.is_alive() and drained.is_set()
    assert failures == ["provider_install_unknown"]


def test_clean_missing_provider_after_unknown_requires_new_invocation_and_consent(fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby import provider_install
    paths, request, _, _ = fixed_profile
    first = FixedOwnerProfile(paths, request)
    attempts, approvals = [], []
    monkeypatch.setattr(provider_install, "status", lambda *_: provider_result(first))
    def prepare(*_a, **_k):
        attempts.append(True)
        return provider_result(first, "unknown" if len(attempts) == 1 else "ready")
    monkeypatch.setattr(provider_install, "prepare", prepare)
    class Consent:
        def approve(self, action, scope): approvals.append(action)
        def check_cancel(self): pass
        def state(self, *_a, **_k): pass
        def install_provider(self, operation): return operation(threading.Event())
    with pytest.raises(StdioRefused, match="provider_install_unknown"):
        first.prepare_provider(Consent(), paths)
    assert attempts == [True] and approvals == ["install_provider"]
    fresh = FixedOwnerProfile(paths, request)
    assert fresh.prepare_provider(Consent(), paths).state == "ready"
    assert attempts == [True, True] and approvals == ["install_provider", "install_provider"]


def test_provider_install_revalidates_source_after_consent_before_helper_effect(fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby import provider_install
    paths, request, _, receipt = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    monkeypatch.setattr(provider_install, "status", lambda *_: provider_result(profile))
    monkeypatch.setattr(provider_install, "prepare", lambda *_a, **_k: pytest.fail("changed source reached a provider write"))
    class Consent:
        def approve(self, *_a, **_k):
            value = json.loads(receipt.read_bytes()); value["complete"] = False; receipt.write_text(json.dumps(value))
        def check_cancel(self): pass
    with pytest.raises(StdioRefused, match="installation_changed"):
        profile.prepare_provider(Consent(), paths)


def test_provider_install_unknown_cleanup_is_not_hidden_by_pipe_eof(pipes):
    from claudlobby.commands.owner_stdio import StdioRefused
    console, _ = admitted(pipes)
    began = threading.Event()
    def prepare(cancel):
        began.set()
        assert cancel.wait(2)
        return SimpleNamespace(state="unknown")
    failures = []
    def invoke():
        try: console.install_provider(prepare)
        except StdioRefused as exc: failures.append(exc.code)
    thread = threading.Thread(target=invoke)
    thread.start()
    assert began.wait(2)
    os.close(pipes[2])
    thread.join(3)
    assert not thread.is_alive() and failures == ["provider_install_unknown"]


def test_concurrent_canonical_provider_completion_needs_no_second_write(fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby import provider_install
    paths, request, _, _ = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    observations = iter((provider_result(profile), provider_result(profile, "ready")))
    monkeypatch.setattr(provider_install, "status", lambda *_: next(observations))
    monkeypatch.setattr(provider_install, "prepare", lambda *_a, **_k: pytest.fail("replaced concurrent canonical bytes"))
    approvals = []
    consent = SimpleNamespace(approve=lambda action, scope: approvals.append(action), check_cancel=lambda: None)
    assert profile.prepare_provider(consent, paths).state == "ready"
    assert approvals == ["install_provider"]


def test_stdio_provider_status_only_observes_public_bytes(fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import _status
    from claudlobby import provider_install
    from claudlobby.plane.owner_access import OwnerAccess
    paths, request, home, _ = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    observed = provider_result(profile)
    monkeypatch.setattr(provider_install, "status", lambda *_: observed)
    monkeypatch.setattr(provider_install, "prepare", lambda *_a, **_k: pytest.fail("status wrote provider bytes"))
    before = sorted(str(p) for p in home.rglob("*"))
    result = _status(paths, OwnerAccess(paths.root), profile=profile)
    assert result["provider"]["state"] == "missing"
    assert result["provider"]["version"] == "2.1.325" and result["provider"]["binary"] == str(profile.provider)
    assert sorted(str(p) for p in home.rglob("*")) == before



def test_fixed_profile_environment_resolves_actual_git_resources_and_managed_setup(fixed_profile, monkeypatch):
    import subprocess
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.installation import write_installation
    from claudlobby.resources import get_resources
    from claudlobby.plane.owner_setup import managed_setup
    paths, request, _, receipt_path = fixed_profile
    installation = paths.package.installation
    source = installation.source
    for name in ('library/voices', 'templates', 'claudlobby/_runtime_scripts'):
        (source / name).mkdir(parents=True)
    for name in ('claudlobby/system.yaml', 'fleet.yaml.seed', 'projects.yaml.seed', '.env.seed.example'):
        (source / name).write_text('synthetic resource\n')
    def git(*args):
        return subprocess.run(['/usr/bin/git', '-C', str(source), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '--quiet'); git('add', '--all')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '--quiet', '-m', 'fixture')
    revision = git('rev-parse', 'HEAD')
    receipt = json.loads(receipt_path.read_bytes())
    receipt['ref'] = receipt['revision'] = revision
    receipt_path.write_text(json.dumps(receipt, sort_keys=True))
    binding = {key: value for key, value in receipt.items() if key not in ('revision', 'lock_sha256', 'environment_owned', 'complete')}
    request['source_revision'] = paths.package.source_revision = revision
    request['binding_sha256'] = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    monkeypatch.setattr(type(installation), 'preflight', lambda self, **_: revision)
    write_installation(installation)
    monkeypatch.setenv('CLAUDLOBBY_INSTALLATION', '/unadmitted/install')
    monkeypatch.setenv('CLAUDLOBBY_ROOT', '/unadmitted/root')
    profile = FixedOwnerProfile(paths, request)
    with profile.environment():
        package = get_resources()  # No patched resource resolver or prebuilt wheel path.
        assert package.installation == installation
        assert package.source_revision == revision and package.library == source / 'library'
        assert managed_setup(paths.root).installation == installation
        assert os.environ['CLAUDLOBBY_ROOT'] == str(paths.root)
    assert os.environ['CLAUDLOBBY_INSTALLATION'] == '/unadmitted/install'


@pytest.mark.parametrize('change', ['activity', 'replace', 'mode'])
def test_directory_consent_retains_identity_but_tolerates_unrelated_parent_activity(fixed_profile, monkeypatch, change):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby.commands import host_owner_setup
    paths, request, _, _ = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    parent = profile.units.parent.parent
    parent.mkdir(mode=0o700)
    monkeypatch.setattr(host_owner_setup, '_native_directories', lambda _: (profile.units,))
    class Consent:
        def approve(self, *_):
            if change == 'activity':
                (parent / 'unrelated-app').mkdir()
            elif change == 'replace':
                parent.rename(parent.with_name('saved-config'))
                parent.mkdir(mode=0o700)
            else:
                parent.chmod(0o777)
    if change == 'activity':
        profile.prepare_directories(Consent(), paths)
        assert profile.units.is_dir() and (parent / 'unrelated-app').is_dir()
    else:
        with pytest.raises(StdioRefused):
            profile.prepare_directories(Consent(), paths)
        assert not profile.units.exists() and not (profile.data / '.env').exists()


def test_serving_partial_overread_has_same_five_second_timeout(pipes, monkeypatch):
    from claudlobby.commands import owner_stdio
    console = owner_stdio.OwnerStdio(pipes[0], pipes[1])
    os.write(pipes[2], wire(start(console.session_id)) + b'{"type":')
    console.start()
    assert console._buffer == b'{"type":'
    now = time.monotonic()
    monkeypatch.setattr(owner_stdio.time, 'monotonic', lambda: now)
    console.check_cancel()
    monkeypatch.setattr(owner_stdio.time, 'monotonic', lambda: now + 6)
    with pytest.raises(owner_stdio.StdioRefused):
        console.check_cancel()


@pytest.mark.parametrize('window', ['started', 'cleanup'])
@pytest.mark.parametrize('stop', ['keyboard', 'SIGTERM', 'SIGHUP', 'SIGINT'])
def test_admitted_provider_keyboard_interrupt_joins_and_requires_inspection(pipes, monkeypatch, window, stop):
    import signal
    from claudlobby.commands.host_owner_setup import interrupted
    from claudlobby.commands.owner_stdio import StdioRefused
    console, _ = admitted(pipes)
    began, drained, release = threading.Event(), threading.Event(), threading.Event()
    cancelled = []
    def prepare(cancel):
        began.set()
        while not cancel.wait(.01) and not release.is_set():
            pass
        cancelled.append(cancel.is_set())
        drained.set()
        return SimpleNamespace(state='ready')  # Cancellation is not rollback.
    original_start, original_join = threading.Thread.start, threading.Thread.join
    injected = []
    def interrupt():
        if stop == 'keyboard': raise KeyboardInterrupt
        signal.raise_signal(getattr(signal, stop))
    def start(thread):
        original_start(thread)
        assert began.wait(2)
        if window == 'started':
            injected.append(True)
            interrupt()
    def join(thread, timeout=None):
        if window == 'cleanup' and not injected:
            injected.append(True)
            interrupt()
        return original_join(thread, timeout)
    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(threading.Thread, 'join', join)
    try:
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        with interrupted(), pytest.raises(BaseException) as stopped:
            console.install_provider(prepare)
        assert {sig: signal.getsignal(sig) for sig in previous} == previous
        joined = not any(t.name == 'owner-provider-install' for t in threading.enumerate())
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == 'owner-provider-install': original_join(thread, 3)
    assert isinstance(stopped.value, StdioRefused) and stopped.value.code == 'provider_install_unknown'
    assert injected and drained.is_set() and cancelled == [True] and joined


@pytest.mark.parametrize('stop', ['SIGTERM', 'SIGHUP', 'SIGINT'])
def test_typed_real_child_drain_records_repeated_signals_before_success(tmp_path, monkeypatch, stop):
    import signal
    import sys
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.commands.owner_stdio import StdioRefused
    ready, draining, sent, drained = (tmp_path / name for name in ('ready', 'draining', 'sent', 'drained'))
    # The child drains until the sender says both signals are out (ten seconds at
    # most), so both arrive during the owned cleanup however late a busy runner
    # schedules the sender. With a fixed drain time, a stalled sender's signal
    # arrived after the handlers were restored and ended the whole pytest process.
    child = '''import signal,time,sys
from pathlib import Path
ready,draining,sent,drained=map(Path,sys.argv[1:5])
def stop(*_):
    draining.touch()
    until=time.monotonic()+10
    while not sent.exists() and time.monotonic()<until: time.sleep(.005)
    time.sleep(.05)
    drained.touch()
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
ready.touch()
while True: time.sleep(.01)
'''
    monkeypatch.setattr(guide, '_ready', lambda *_: ready.exists())
    failures, late, under_test = [], [], [True]
    def send_stops():
        try:
            until = time.monotonic() + 10
            while not draining.exists() and time.monotonic() < until: time.sleep(.005)
            assert draining.exists()
            os.kill(os.getpid(), getattr(signal, stop))
            time.sleep(.02)
            os.kill(os.getpid(), getattr(signal, stop))
        except BaseException as exc: failures.append(exc)
        finally: sent.touch()
    sender = threading.Thread(target=send_stops)
    # A signal that still arrives after the code under test has returned is
    # recorded here and fails this test. Left to pytest's own handlers it would
    # end the session. Inside the code under test SIGINT keeps Python's meaning.
    def stray(number, frame):
        if number == signal.SIGINT and under_test[0]: signal.default_int_handler(number, frame)
        late.append(number)
    outer = {sig: signal.signal(sig, stray) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        sender.start()
        try:
            try:
                with guide.interrupted(), pytest.raises(StdioRefused, match='interrupted_unknown'):
                    with guide._serving([sys.executable, '-I', '-B', '-c', child, str(ready), str(draining), str(sent), str(drained)],
                            tmp_path / 'owner.sock', 'https://host.example.test',
                            consent=SimpleNamespace(check_cancel=lambda: None)):
                        pass  # Interrupt during owned cleanup, after a possible Launch return.
            finally:
                under_test[0] = False
        except KeyboardInterrupt:
            under_test[0] = False
            late.append('KeyboardInterrupt left the owned cleanup')
        assert drained.exists()
    finally:
        under_test[0] = False
        sender.join(15)
        # Only once the sender has finished. A sender still running has a signal
        # left to send, which must find these handlers and not pytest's own; the
        # test then fails below and the handlers stay for the rest of the session.
        if not sender.is_alive():
            for sig, handler in outer.items(): signal.signal(sig, handler)
    assert not sender.is_alive() and not failures and not late, (sender.is_alive(), failures, late)


@pytest.fixture
def darwin_fixed_profile(fixed_profile, monkeypatch):
    from claudlobby import installation as locations
    paths, request, home, receipt_path = fixed_profile
    runtime_root = home / ".local/share/claudlobby-browser/runtime/profile"
    python = runtime_root / "python/bin/python3.12"
    python.parent.mkdir(parents=True, mode=0o700)
    python.write_bytes(b"exact synthetic managed interpreter; never executed"); python.chmod(0o755)
    runtime = SimpleNamespace(uid=os.getuid(), account="example-account", home=home,
        root=runtime_root, python=python, bin=runtime_root / "bin", ca_bundle=runtime_root / "ca-bundle.pem")
    environment = {"HOME":str(home), "PATH":str(runtime.bin) + ":/usr/bin:/bin:/usr/sbin:/sbin",
        "CLAUDLOBBY_NATIVE_PYTHON":str(python), "SSL_CERT_FILE":str(runtime.ca_bundle),
        "GIT_EXEC_PATH":str(runtime_root / "git/libexec/git-core"), "GIT_ASKPASS":"/usr/bin/false"}
    monkeypatch.setattr(locations, "managed_browser_runtime", lambda actual: runtime
                        if actual == paths.package.installation else None)
    monkeypatch.setattr(locations, "managed_browser_environment", lambda actual: dict(environment)
                        if actual == paths.package.installation else None)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(__import__("platform"), "system", lambda: "Darwin")
    monkeypatch.setattr(sys, "executable", str(python))
    receipt = json.loads(receipt_path.read_bytes())
    receipt.update(interpreter=str(python), interpreter_sha256=hashlib.sha256(python.read_bytes()).hexdigest())
    receipt_path.write_text(json.dumps(receipt, sort_keys=True))
    binding = {key:value for key,value in receipt.items()
               if key not in {"revision", "lock_sha256", "environment_owned", "complete"}}
    request["binding_sha256"] = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    return paths, request, home, receipt_path, runtime, environment


@pytest.mark.parametrize("plane", [None, "0", "1"])
def test_darwin_fixed_owner_preserves_canonical_runtime_environment_and_launchd_target(darwin_fixed_profile, monkeypatch, plane):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    paths, request, home, _receipt, runtime, environment = darwin_fixed_profile
    if plane is not None: environment["PLANE_EMIT_DISABLED"] = plane
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "foreign-bus")
    monkeypatch.setenv("SSL_CERT_FILE", "foreign-ca")
    previous = dict(os.environ)
    profile = FixedOwnerProfile(paths, request)
    assert profile.units == home / "Library/LaunchAgents" and profile.runtime is runtime
    with profile.environment():
        assert os.environ["PATH"] == environment["PATH"]
        assert os.environ["CLAUDLOBBY_NATIVE_PYTHON"] == str(runtime.python)
        assert os.environ["SSL_CERT_FILE"] == str(runtime.ca_bundle)
        assert os.environ.get("PLANE_EMIT_DISABLED") == plane
        assert not {"XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"} & os.environ.keys()
        assert os.environ["CLAUDE_CONFIG_DIR"] == str(profile.account)
    assert dict(os.environ) == previous


@pytest.mark.parametrize("defect", ["interpreter", "running-python", "runtime-unavailable", "runtime-home", "environment"])
def test_darwin_owner_runtime_or_python_drift_refuses_without_effects(darwin_fixed_profile, monkeypatch, defect):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby import installation as locations
    paths, request, home, receipt_path, runtime, _env = darwin_fixed_profile
    if defect == "interpreter":
        receipt = json.loads(receipt_path.read_bytes())
        other = home / "other-python"; other.write_bytes(b"other");other.chmod(0o755)
        receipt.update(interpreter=str(other), interpreter_sha256=hashlib.sha256(other.read_bytes()).hexdigest())
        receipt_path.write_text(json.dumps(receipt, sort_keys=True))
        binding = {key:value for key,value in receipt.items()
                   if key not in {"revision", "lock_sha256", "environment_owned", "complete"}}
        request["binding_sha256"] = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    elif defect == "running-python": monkeypatch.setattr(sys, "executable", str(home / "unselected-python"))
    elif defect == "runtime-unavailable": monkeypatch.setattr(locations, "managed_browser_runtime", lambda _actual: None)
    elif defect == "environment":
        def changed(_actual): raise locations.InstallationError("changed runtime")
        monkeypatch.setattr(locations, "managed_browser_environment", changed)
    else: runtime.home = home / "other-home"
    before = dict(os.environ), sorted(str(p) for p in home.rglob("*"))
    with pytest.raises(StdioRefused): FixedOwnerProfile(paths, request)
    assert before == (dict(os.environ), sorted(str(p) for p in home.rglob("*")))


def test_darwin_launchd_directory_requires_existing_manager_admission(darwin_fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby.commands import host_owner_setup
    paths, request, home, *_ = darwin_fixed_profile
    profile = FixedOwnerProfile(paths, request)
    monkeypatch.setattr(host_owner_setup, "_native_directories", lambda _paths: ())
    refused = SimpleNamespace(approve=lambda *_a, **_kw: pytest.fail("unadmitted launchd directory proposed"))
    with pytest.raises(StdioRefused, match="native_manager_prerequisite_missing"):
        profile.prepare_directories(refused, paths)
    assert not profile.project.exists() and not (profile.data / ".env").exists()
    monkeypatch.setattr(host_owner_setup, "_native_directories", lambda _paths: (profile.units,))
    proposals = []
    consent = SimpleNamespace(approve=lambda action, scope: proposals.append((action,scope)))
    profile.prepare_directories(consent, paths)
    assert proposals[0][0] == "prepare_directories" and profile.units.is_dir()
    assert not (home / ".config/systemd").exists()


def test_darwin_stdio_admits_exact_root_only_after_native_operator_context(darwin_fixed_profile, monkeypatch):
    from claudlobby.commands.owner_stdio import _admit
    from claudlobby.commands import operator_context
    from claudlobby import context
    from claudlobby.plane import owner_source
    paths, request, _home, *_ = darwin_fixed_profile
    observed = []
    monkeypatch.setattr(context, "resolve_paths", lambda **_kw: paths)
    monkeypatch.setattr(owner_source, "fresh_owner_setup", lambda *_a, **_kw: observed.append("fresh"))
    monkeypatch.setattr(operator_context, "require_operator_context", lambda *_a: observed.append("native"))
    monkeypatch.setattr(os, "isatty", lambda _fd: False)
    args = SimpleNamespace(root=str(paths.root), fleet=None, seed=False, json=False, template="revenue-web-app")
    assert _admit(args, request) is paths and observed == ["fresh", "native"]


def test_darwin_owner_ingress_remains_explicit_native_gate(darwin_fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby.plane import owner_ingress
    paths, request, *_ = darwin_fixed_profile
    profile = FixedOwnerProfile(paths, request)
    monkeypatch.setattr(owner_ingress, "ServePrincipalVerifier", lambda **_kw: pytest.fail("executed Linux native path"))
    with pytest.raises(StdioRefused, match="native_ingress_prerequisite_missing"): profile.origin()


def test_linux_system_python_update_keeps_the_recorded_binding(fixed_profile):
    """Ubuntu replaces /usr/bin/python3.x on a security update; setup goes on."""
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    paths, request, home, path = fixed_profile
    receipt, binding = path.read_bytes(), request["binding_sha256"]
    interpreter = Path(json.loads(receipt)["interpreter"])
    interpreter.write_bytes(b"synthetic interpreter after a security update; never executed")
    assert hashlib.sha256(interpreter.read_bytes()).hexdigest() != json.loads(receipt)["interpreter_sha256"]
    before = sorted(str(p) for p in home.rglob("*"))
    FixedOwnerProfile(paths, request)
    assert path.read_bytes() == receipt and request["binding_sha256"] == binding
    assert sorted(str(p) for p in home.rglob("*")) == before


@pytest.mark.parametrize("defect", ["owner", "group-writable", "other-writable", "not-executable",
                                    "outside", "running-python", "folder-owner", "folder-group-writable", "folder-other-writable", "folder-symlink"])
def test_linux_system_python_must_stay_trusted(fixed_profile, monkeypatch, tmp_path, defect):
    from claudlobby import bootstrap  # holds the shared trust check
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    paths, request, home, path = fixed_profile
    interpreter = Path(json.loads(path.read_bytes())["interpreter"])
    if defect == "owner":
        monkeypatch.setattr(bootstrap, "_SYSTEM_PYTHON_OWNER", os.getuid() + 1)
    elif defect == "group-writable":
        interpreter.chmod(0o775)
    elif defect == "other-writable":
        interpreter.chmod(0o757)
    elif defect == "not-executable":
        interpreter.chmod(0o644)
    elif defect == "outside":
        monkeypatch.setattr(bootstrap, "_SYSTEM_PYTHON_DIRECTORY", tmp_path / "elsewhere")
    elif defect == "running-python":
        other = tmp_path / "other-python"; other.write_bytes(b"other"); other.chmod(0o755)
        monkeypatch.setattr(sys, "executable", str(other))
    else:
        monkeypatch.setattr(bootstrap, "_SYSTEM_PYTHON_DIRECTORY", _fake_folder(tmp_path, defect))
    receipt, before = path.read_bytes(), sorted(str(p) for p in home.rglob("*"))
    with pytest.raises(StdioRefused, match="installation_changed"):
        FixedOwnerProfile(paths, request)
    assert path.read_bytes() == receipt and sorted(str(p) for p in home.rglob("*")) == before


def test_darwin_managed_python_bytes_stay_pinned(darwin_fixed_profile):
    """Claudlobby's own Mac runtime is its own file: a changed byte still refuses."""
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    paths, request, home, receipt_path, runtime, _env = darwin_fixed_profile
    FixedOwnerProfile(paths, request)
    runtime.python.write_bytes(b"changed managed interpreter; never executed")
    receipt = receipt_path.read_bytes()
    with pytest.raises(StdioRefused, match="installation_changed"):
        FixedOwnerProfile(paths, request)
    assert receipt_path.read_bytes() == receipt


def _fake_folder(path, defect):
    """The trusted folder as lstat reports it, with at most one property faked."""
    if not (defect or "").startswith("folder-"):
        return path
    import stat as stat_module
    class Folder(type(Path("/"))):
        def lstat(self):
            info = super().lstat()
            mode = stat_module.S_IFLNK | 0o777 if defect == "folder-symlink" else info.st_mode
            mode |= {"folder-group-writable": 0o020, "folder-other-writable": 0o002}.get(defect, 0)
            return SimpleNamespace(st_mode=mode, st_uid=info.st_uid + (defect == "folder-owner"))
    return Folder(path)


def test_linux_recorded_python_digest_is_never_compared(fixed_profile):
    """Only the recorded digest changes, and the binding the website holds agrees."""
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    paths, request, _home, path = fixed_profile
    receipt = json.loads(path.read_bytes())
    receipt["interpreter_sha256"] = "e" * 64
    path.write_text(json.dumps(receipt, sort_keys=True))
    binding = {key: value for key, value in receipt.items()
               if key not in {"revision", "lock_sha256", "environment_owned", "complete"}}
    request["binding_sha256"] = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    FixedOwnerProfile(paths, request)


@pytest.mark.parametrize("action", ["start_workspace", "initialize_link_storage"])
def test_stdio_proposes_the_computer_runs_cards(pipes, action):
    """The computer run's two new card actions pass the channel's closed list."""
    console, session = admitted(pipes)
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(console.approve(
        action, {"effects": ["displayed core effect"]}, original="approved")), daemon=True)
    thread.start()
    import select
    assert select.select([pipes[3]], [], [], 5)[0], "the channel refused the action"
    proposal = read_frame(pipes[3])
    assert proposal["type"] == "proposal" and proposal["proposal"]["action"] == action
    os.write(pipes[2], wire({"version": 1, "type": "decide", "seq": 2,
        "session_id": session, "proposal_id": proposal["proposal_id"],
        "digest": proposal["digest"], "approve": True}))
    thread.join(2)
    assert not thread.is_alive() and outcome == ["approved"]


DONE = {"request_id": "33333333-3333-4333-8333-333333333333", "target": "a" * 40, "phase": "done",
        "health_errors": {}, "maintenance": False, "config_committed": True}


@pytest.fixture
def moved_profile(fixed_profile, monkeypatch):
    """Receipt at X ("e"), installed and requested at Y ("a"); the record on disk and Git are stand-ins.

    tests/test_owner_computer_run.py checks admitted_update against the updater's own records.
    """
    from claudlobby import git_update
    paths, request, home, path = fixed_profile
    receipt = json.loads(path.read_bytes())
    receipt["ref"] = receipt["revision"] = "e" * 40
    path.write_bytes(json.dumps(receipt, sort_keys=True).encode())
    fields = ("schema", "purpose", "installation", "data_root", "remote", "ref", "interpreter", "interpreter_sha256")
    request["binding_sha256"] = hashlib.sha256(json.dumps({key: receipt[key] for key in fields},
                                                          sort_keys=True).encode()).hexdigest()
    world = SimpleNamespace(record=dict(DONE), git=[], ancestor=True)
    monkeypatch.setattr(git_update, "read_update", lambda installation: world.record and dict(world.record))
    def git(installation, *args, check=True):
        world.git.append(args)
        return SimpleNamespace(returncode=0 if world.ancestor and args == (
            "merge-base", "--is-ancestor", "e" * 40, "a" * 40) else 1)
    monkeypatch.setattr(git_update, "_git", git)
    return paths, request, home, world


def test_the_computer_run_is_admitted_at_a_later_revision_naming_its_finished_update(moved_profile):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    paths, request, home, world = moved_profile
    before = sorted(str(p) for p in home.rglob("*"))
    computer = dict(request, update_record=[DONE["request_id"], "a" * 40])
    profile = FixedOwnerProfile(paths, computer, computer=True)
    assert profile.receipt_revision == "e" * 40
    assert world.git == [("merge-base", "--is-ancestor", "e" * 40, "a" * 40)]
    # Today's guide keeps its rule: the same computer, without a finish tuple, is refused.
    with pytest.raises(StdioRefused, match="installation_changed"):
        FixedOwnerProfile(paths, request)
    assert sorted(str(p) for p in home.rglob("*")) == before


@pytest.mark.parametrize("change", ["health_errors", "another_id", "nothing_named", "no_record", "not_done",
                                    "another_target", "not_an_ancestor", "binding", "finish_tuple"])
def test_the_computer_run_at_a_later_revision_refuses_anything_else(moved_profile, change):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    paths, request, home, world = moved_profile
    computer = dict(request, update_record=[DONE["request_id"], "a" * 40])
    if change == "health_errors":
        world.record["health_errors"] = {"claudlobby-plane-owner.service": "unit failed to start"}
    elif change == "another_id":
        computer["update_record"] = ["44444444-4444-4444-8444-444444444444", "a" * 40]
    elif change == "nothing_named":
        computer["update_record"] = None
    elif change == "no_record":  # a checkout moved by hand
        world.record = None
    elif change == "not_done":
        world.record["phase"] = "restore"
    elif change == "another_target":
        world.record["target"] = computer["update_record"][1] = "b" * 40
    elif change == "not_an_ancestor":
        world.ancestor = False
    elif change == "binding":
        computer["binding_sha256"] = "f" * 64
    else:
        computer["finish_setup"] = ["11111111-1111-4111-8111-111111111111", DONE["request_id"], "b" * 64, "a" * 40]
    before = sorted(str(p) for p in home.rglob("*"))
    with pytest.raises(StdioRefused, match="installation_changed"):
        FixedOwnerProfile(paths, computer, computer=True)
    assert sorted(str(p) for p in home.rglob("*")) == before


def test_the_computer_run_at_the_receipts_revision_needs_no_record(fixed_profile, monkeypatch):
    from claudlobby import git_update
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    paths, request, _home, _ = fixed_profile
    monkeypatch.setattr(git_update, "read_update", lambda *_: pytest.fail("read a record at the receipt's revision"))
    assert FixedOwnerProfile(paths, dict(request, update_record=None), computer=True).receipt_revision == "a" * 40


@pytest.mark.parametrize("mutation", [
    {}, {"update_record": ["33333333-3333-4333-8333-333333333333"]},
    {"update_record": ["not-an-id", "a" * 40]}, {"update_record": ["33333333-3333-4333-8333-333333333333", "A" * 40]},
    {"update_record": "33333333-3333-4333-8333-333333333333"},
    {"update_record": "x"}, {"update_record": 7}, {"update_record": []}, {"update_record": {}},
    {"update_record": [None, None]}, {"update_record": [1, 2]},
    {"update_record": None, "finish_setup": ["11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222", "b" * 64, "c" * 40]},
])
def test_stdio_computer_start_takes_one_more_key_and_nothing_else(pipes, mutation):
    from claudlobby.commands.owner_stdio import OwnerStdio, StdioRefused
    console = OwnerStdio(pipes[0], pipes[1])
    console.hello()
    hello = read_frame(pipes[3])
    value = start(hello["session_id"], **mutation)
    os.write(pipes[2], wire(value))
    with pytest.raises(StdioRefused) as refused:
        console.start(computer=True)
    assert refused.value.code == "invalid_request" and refused.value.data == {}


@pytest.mark.parametrize("known", [None, ["33333333-3333-4333-8333-333333333333", "a" * 40]])
def test_stdio_computer_start_returns_the_named_record(pipes, known):
    from claudlobby.commands.owner_stdio import OwnerStdio
    console = OwnerStdio(pipes[0], pipes[1])
    console.hello()
    hello = read_frame(pipes[3])
    os.write(pipes[2], wire(start(hello["session_id"], update_record=known)))
    assert console.start(computer=True)["update_record"] == known


@pytest.mark.parametrize("outcome", ["ready", "start_failed"])
def test_dispatch_runs_the_computer_run_for_its_flag(fixed_profile, monkeypatch, outcome):
    """`--stdio-computer` reaches run_computer with the computer admission, never today's run."""
    from claudlobby.command_result import CommandOutput
    from claudlobby.commands import host_owner_setup, owner_stdio
    from claudlobby.commands import owner_setup_consent
    paths, request, *_ = fixed_profile
    seen = []
    real = owner_setup_consent.FixedOwnerProfile

    def profile(admitted, value, **kwargs):
        seen.append(("profile", kwargs))
        return real(admitted, value, **kwargs)

    def run_computer(args, admitted, store, *, consent, profile):
        seen.append(("run", args.update_record))
        if outcome == "start_failed":
            raise owner_stdio.StdioRefused("start_failed", data={"line": "core's line", "request_id": "id"})
        return CommandOutput({"state": "computer_ready", "origin": "https://host.example.test"})

    monkeypatch.setattr(owner_setup_consent, "FixedOwnerProfile", profile)
    monkeypatch.setattr(host_owner_setup, "run", lambda *a, **k: pytest.fail("ran today's guide"))
    monkeypatch.setattr(host_owner_setup, "run_computer", run_computer)
    monkeypatch.setattr(owner_stdio, "_selectors", lambda args: None)
    monkeypatch.setattr(owner_stdio, "_admit", lambda args, value: paths)
    incoming, client = os.pipe()
    observer, outgoing = os.pipe()

    class Channel(owner_stdio.OwnerStdio):
        def __init__(self, _in, _out):
            super().__init__(incoming, outgoing)

        def start(self, *, computer=False):
            assert computer is True
            return dict(request, update_record=None)

    monkeypatch.setattr(owner_stdio, "OwnerStdio", Channel)
    try:
        code = owner_stdio.dispatch(SimpleNamespace(stdio_status=False, stdio_computer=True, root=str(paths.root)))
        os.close(outgoing)
        outgoing = None
        frames = [json.loads(line) for line in os.read(observer, 65536).splitlines()]
    finally:
        for fd in (incoming, client, observer, outgoing):
            if fd is not None:
                os.close(fd)
    assert seen == [("profile", {"computer": True}), ("run", None)]
    if outcome == "ready":
        assert code == 0 and frames[-1]["type"] == "result" and frames[-1]["state"] == "computer_ready"
    else:
        assert code == 4 and {key: frames[-1][key] for key in ("type", "code", "line", "request_id")} == {
            "type": "error", "code": "start_failed", "line": "core's line", "request_id": "id"}


@pytest.mark.parametrize("refusal, extra", [
    ("declined", []), ("source_mismatch", []), ("provider_install_failed", ["next"]),
    ("interrupted_unknown", ["next"]), ("conflict", []),
])
def test_todays_error_frame_keeps_its_keys(fixed_profile, monkeypatch, refusal, extra):
    """Today's `--stdio` refusal on the wire: the error frame's keys, exactly, for each kind of refusal."""
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import host_owner_setup, owner_stdio
    paths, request, *_ = fixed_profile

    def run(*_args, **_kwargs):
        if refusal == "conflict":
            raise CommandFailure("conflict", "the original setup is not current healthy DONE",
                                 data={"request_id": "id"}, hint="host update status")
        raise owner_stdio.StdioRefused(refusal)

    monkeypatch.setattr(host_owner_setup, "run", run)
    monkeypatch.setattr(host_owner_setup, "run_computer", lambda *a, **k: pytest.fail("ran the computer run"))
    monkeypatch.setattr(owner_stdio, "_selectors", lambda args: None)
    monkeypatch.setattr(owner_stdio, "_admit", lambda args, value: paths)
    incoming, client = os.pipe()
    observer, outgoing = os.pipe()

    class Channel(owner_stdio.OwnerStdio):
        def __init__(self, _in, _out):
            super().__init__(incoming, outgoing)

        def start(self, *, computer=False):
            assert computer is False
            return dict(request)

    monkeypatch.setattr(owner_stdio, "OwnerStdio", Channel)
    try:
        code = owner_stdio.dispatch(SimpleNamespace(stdio_status=False, stdio_computer=False, root=str(paths.root)))
        os.close(outgoing)
        outgoing = None
        frames = [json.loads(line) for line in os.read(observer, 65536).splitlines()]
    finally:
        for fd in (incoming, client, observer, outgoing):
            if fd is not None:
                os.close(fd)
    assert code == 4 and frames[-1]["type"] == "error" and frames[-1]["code"] == refusal
    assert sorted(frames[-1]) == sorted(["code", "retry", "seq", "session_id", "state", "type", "version", *extra])


def test_stdio_computer_is_one_more_hidden_mode_of_the_same_exclusive_group(monkeypatch):
    import argparse
    from claudlobby.commands import _host_owner_parsers as parsers
    parser = argparse.ArgumentParser(exit_on_error=False)
    parsers.register_host_owner(parser.add_subparsers(dest="host_command"))
    args = parser.parse_args(["owner", "setup", "--stdio-computer"])
    assert args.stdio_computer is True and args.stdio is False and args.stdio_status is False
    setup = parser._subparsers._group_actions[0].choices["owner"]._subparsers._group_actions[0].choices["setup"]
    (group,) = [group for group in setup._mutually_exclusive_groups
                if "--stdio" in [flag for action in group._group_actions for flag in action.option_strings]]
    modes = {action.option_strings[0]: action.help for action in group._group_actions}
    assert modes == {"--stdio": argparse.SUPPRESS, "--stdio-status": argparse.SUPPRESS,
                     "--stdio-computer": argparse.SUPPRESS}
    routed = []
    monkeypatch.setattr(parsers, "import_module", lambda name, package: SimpleNamespace(
        dispatch=lambda value: routed.append(name) or 0))
    assert parsers._dispatch(args) == 0 and routed == [".owner_stdio"]


def _team_card(pipes, *, marked, answer):
    """One allow_setup proposal, with or without the messages mark, answered by ``answer``'s extra keys."""
    from claudlobby.commands.owner_stdio import StdioRefused
    console, session = admitted(pipes)
    grant = {"fleet": "example", **({"messages": {"sender": "card"}} if marked else {})}
    outcome = []
    def proposal():
        try:
            outcome.append(console.approve("allow_setup", {"grant": grant, "effects": ["displayed core effect"]}))
        except StdioRefused as error:
            outcome.append(error)
    thread = threading.Thread(target=proposal)
    thread.start()
    pending = read_frame(pipes[3])
    assert pending["proposal"]["scope"]["grant"] == grant
    os.write(pipes[2], wire({"version": 1, "type": "decide", "seq": 2, "session_id": session,
        "proposal_id": pending["proposal_id"], "digest": pending["digest"], "approve": True, **answer}))
    thread.join(2)
    assert not thread.is_alive() and len(outcome) == 1
    return outcome[0], console


def test_marked_team_card_answer_carries_the_name(pipes):
    returned, console = _team_card(pipes, marked=True, answer={"actor_alias": "human:ada-lovelace"})
    assert returned == "human:ada-lovelace"
    assert console._named is False and console._pending is None


def test_unmarked_team_card_refuses_a_name(pipes):
    from claudlobby.commands.owner_stdio import StdioRefused
    returned, _ = _team_card(pipes, marked=False, answer={"actor_alias": "human:ada-lovelace"})
    assert isinstance(returned, StdioRefused) and returned.code == "invalid_request"


@pytest.mark.parametrize("approve", [True, False])
def test_todays_website_frame_on_a_marked_team_card_is_refused(pipes, approve):
    """Today's page answers the team card with no `actor_alias`, approving or declining: refused as an
    invalid request. The website's core pin moves only together with its name-card change."""
    from claudlobby.commands.owner_stdio import StdioRefused
    returned, _ = _team_card(pipes, marked=True, answer={"approve": approve})
    assert isinstance(returned, StdioRefused) and returned.code == "invalid_request"


@pytest.mark.parametrize("name", ["human:owner", "human:Ada", "human:a", "human:ada--b", 42, None])
def test_marked_team_card_refuses_a_name_failing_the_rule(pipes, name):
    from claudlobby.commands.owner_stdio import StdioRefused
    returned, _ = _team_card(pipes, marked=True, answer={"actor_alias": name})
    assert isinstance(returned, StdioRefused) and returned.code == "sender_name_refused"


def test_declined_marked_team_card_is_declined(pipes):
    from claudlobby.commands.owner_stdio import StdioRefused
    returned, _ = _team_card(pipes, marked=True, answer={"approve": False, "actor_alias": "human:ada-lovelace"})
    assert isinstance(returned, StdioRefused) and returned.code == "declined"


# The computer run's link intent (C2): its start key, its card and its refusals on the wire.
@pytest.mark.parametrize("value, admitted", [(True, True), (False, False), (None, False), (1, False),
                                             ("true", False), ([], False)])
def test_stdio_computer_start_takes_link_claude_only_as_true(pipes, value, admitted):
    from claudlobby.commands.owner_stdio import OwnerStdio, StdioRefused
    console = OwnerStdio(pipes[0], pipes[1])
    console.hello()
    hello = read_frame(pipes[3])
    os.write(pipes[2], wire(start(hello["session_id"], update_record=None, link_claude=value)))
    if admitted:
        assert console.start(computer=True) == start(hello["session_id"], update_record=None, link_claude=True)
        return
    with pytest.raises(StdioRefused) as refused:
        console.start(computer=True)
    assert refused.value.code == "invalid_request" and refused.value.data == {}


def test_stdio_todays_start_refuses_the_link_key(pipes):
    """Only the computer run takes the link intent; today's guide refuses it, alone or with update_record."""
    from claudlobby.commands.owner_stdio import OwnerStdio, StdioRefused
    for extra in ({"link_claude": True}, {"link_claude": True, "update_record": None}):
        incoming, outgoing = os.pipe(), os.pipe()
        console = OwnerStdio(incoming[0], outgoing[1])
        try:
            console.hello()
            hello = read_frame(outgoing[0])
            os.write(incoming[1], wire(start(hello["session_id"], **extra)))
            with pytest.raises(StdioRefused) as refused:
                console.start()
            assert refused.value.code == "invalid_request"
        finally:
            for fd in (*incoming, *outgoing):
                os.close(fd)


def test_stdio_proposes_the_link_card(pipes):
    """``link_provider`` passes the channel's closed list."""
    console, session = admitted(pipes)
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(console.approve(
        "link_provider", {"provider_signin": {"binary": "/synthetic"}}, original="approved")), daemon=True)
    thread.start()
    import select
    assert select.select([pipes[3]], [], [], 5)[0], "the channel refused the action"
    proposal = read_frame(pipes[3])
    assert proposal["type"] == "proposal" and proposal["proposal"]["action"] == "link_provider"
    os.write(pipes[2], wire({"version": 1, "type": "decide", "seq": 2,
        "session_id": session, "proposal_id": proposal["proposal_id"],
        "digest": proposal["digest"], "approve": True}))
    thread.join(2)
    assert not thread.is_alive() and outcome == ["approved"]


def _dispatch_computer(fixed_profile, monkeypatch, request, run_computer):
    from claudlobby.commands import host_owner_setup, owner_stdio
    paths = fixed_profile[0]
    monkeypatch.setattr(host_owner_setup, "run", lambda *a, **k: pytest.fail("ran today's guide"))
    monkeypatch.setattr(host_owner_setup, "run_computer", run_computer)
    monkeypatch.setattr(owner_stdio, "_selectors", lambda args: None)
    monkeypatch.setattr(owner_stdio, "_admit", lambda args, value: paths)
    incoming, client = os.pipe()
    observer, outgoing = os.pipe()

    class Channel(owner_stdio.OwnerStdio):
        def __init__(self, _in, _out):
            super().__init__(incoming, outgoing)

        def start(self, *, computer=False):
            assert computer is True
            return dict(request)

    monkeypatch.setattr(owner_stdio, "OwnerStdio", Channel)
    try:
        code = owner_stdio.dispatch(SimpleNamespace(stdio_status=False, stdio_computer=True, root=str(paths.root)))
        os.close(outgoing)
        outgoing = None
        frames = [json.loads(line) for line in os.read(observer, 65536).splitlines()]
    finally:
        for fd in (incoming, client, observer, outgoing):
            if fd is not None:
                os.close(fd)
    return code, frames


@pytest.mark.parametrize("sent", [True, False])
def test_dispatch_carries_the_link_intent_only_when_sent(fixed_profile, monkeypatch, sent):
    from uuid import uuid4
    from claudlobby.command_result import CommandOutput
    request = dict(fixed_profile[1], update_record=None, **({"link_claude": True} if sent else {}))
    seen = []

    def run_computer(args, admitted, store, *, consent, profile):
        seen.append(args.link_claude)
        return CommandOutput({"state": "link_ready", "generation": str(uuid4())}
                             if sent else {"state": "computer_ready", "origin": "https://host.example.test"})
    code, frames = _dispatch_computer(fixed_profile, monkeypatch, request, run_computer)
    assert code == 0 and seen == [sent]
    assert sorted(frames[-1]) == sorted(["seq", "session_id", "type", "version", "state",
                                         "generation" if sent else "origin"])


@pytest.mark.parametrize("refusal", ["not_here", "not_started", "already_linked", "signin_live", "signin_unconfirmed",
                                     "busy", "credential_present", "environment_format", "declined", "expired",
                                     "installed_declined", "installed_expired", "scope_changed", "setup_denied"])
def test_link_refusals_on_the_wire_keep_todays_error_keys(fixed_profile, monkeypatch, refusal):
    """A link run's refusal is an error frame with exactly code, state and retry: no line, no next."""
    from claudlobby.commands import owner_stdio
    request = dict(fixed_profile[1], update_record=None, link_claude=True)

    def run_computer(*_args, **_kwargs):
        raise owner_stdio.StdioRefused(refusal)
    code, frames = _dispatch_computer(fixed_profile, monkeypatch, request, run_computer)
    assert code == 4 and frames[-1]["type"] == "error" and frames[-1]["code"] == refusal
    assert sorted(frames[-1]) == ["code", "retry", "seq", "session_id", "state", "type", "version"]
    assert frames[-1]["state"] == "stopped" and frames[-1]["retry"] is False


@pytest.mark.parametrize("computer", [False, True])
def test_the_install_card_rechecks_the_profile_as_it_was_admitted(fixed_profile, monkeypatch, computer):
    """After the install card the profile is checked again as its run admitted it: the computer run (and so
    its link intent, at a later installed revision) as the computer run; today's guide with today's call."""
    from claudlobby.commands import owner_setup_consent
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby import provider_install
    paths, request, _home, _ = fixed_profile
    profile = FixedOwnerProfile(paths, dict(request, update_record=None) if computer else request,
                                **({"computer": True} if computer else {}))
    answers = iter([provider_result(profile), provider_result(profile, "ready")])
    monkeypatch.setattr(provider_install, "status", lambda *_: next(answers))
    checks = []
    monkeypatch.setattr(owner_setup_consent, "FixedOwnerProfile", lambda *args, **kwargs: checks.append(kwargs))
    consent = SimpleNamespace(approve=lambda *_a, **_k: None, check_cancel=lambda: None)
    assert profile.prepare_provider(consent, paths).state == "ready"
    assert checks == [{"computer": True} if computer else {}]
