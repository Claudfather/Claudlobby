"""Real optional approval/render storage; synthetic coordinator/native boundary."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import json
import pytest

from claudlobby.command_result import CommandFailure
from claudlobby.plane.owner_access import OwnerAccess, AccessDenied, AccessUnavailable, VerifiedReader
from claudlobby.plane.owner_setup import OwnerSetup, SetupEngine
from claudlobby.plane.ids import ensure_host_uid
from tests.test_plane_owner_workspace import OWNER, proposed
from tests.test_plane_owner_connected import evidence
from tests.test_setup_commands import team_setup


@pytest.fixture
def setup_authority(team_setup, monkeypatch):
    from claudlobby import git_update, resources
    from claudlobby.config_plan import ConfigPlanBuilder
    args, root, effects = team_setup
    original = git_update._prepare_update_locked
    def prepare(store, **kwargs):
        record = original(store, **kwargs)
        manifest = root / "local/shop/fleet.yaml"
        projects = manifest.parent / "projects.yaml"
        builder = ConfigPlanBuilder(root, fleets=("shop",), effects={})
        from claudlobby.config import host_override_path
        builder.input(host_override_path())  # The actual renderer freezes this host-local input too.
        _, fleet_digest = builder.input_snapshot(manifest)
        _, projects_digest = builder.input_snapshot(projects)
        builder.effects.update(fleet_sources={"shop": {
            "fleet": {"path": str(manifest), "sha256": fleet_digest},
            "projects": {"path": str(projects), "sha256": projects_digest}}},
            fleet_manifests={"shop": str(manifest)})
        rendered = builder.freeze(record["target"])
        args.retained["record"].update(config_fingerprint=rendered.fingerprint,
            initialization_scope=kwargs["initialization"])
        return dict(args.retained["record"])
    monkeypatch.setattr(git_update, "_prepare_update_locked", prepare)
    package = resources.get_resources()
    package.installation.bin.mkdir(parents=True)
    package.installation.cli.write_text("#!/bin/sh\nexit 0\n")
    package.installation.cli.chmod(0o755)
    ensure_host_uid(root / "state")
    clock = [1800000000.0]
    access = OwnerAccess.initialize(root, clock=lambda:clock[0], before_first_fleet=True)
    pair = access.begin_pairing(OWNER)
    owner = access.confirm_pairing(pair.token,expected_principal=OWNER)
    workspace = access.workspace(); workspace.initialize(expected_owner=owner)
    workspace.confirm(expected_proposal=proposed((access,workspace,owner,clock)))
    connected = access.connected(); connected.initialize(expected_owner=owner)
    session = connected.issue(evidence(connected,clock),OWNER)
    units = root.parent / "units"; units.mkdir()
    store = OwnerSetup(access)
    grant = store.candidate(OWNER,template="revenue-web-app",fleet="shop",repository="example-org/shop",
                            install_directory=str(units))
    return store,access,grant,VerifiedReader(OWNER,session.token),clock,args


def allow(fixture):
    store,access,grant,reader,*_=fixture
    store.allow(grant)
    return SetupEngine(access),grant,reader


def binding(preview):
    return {key:preview[key] for key in ("generation","request_id","config_fingerprint","source_revision")}


def test_provider_signin_without_local_scope_never_starts(setup_authority):
    engine, grant, reader = allow(setup_authority)
    with pytest.raises(AccessDenied):
        engine.signin_begin(reader, grant.generation, str(uuid4()))


@pytest.mark.parametrize("digest,version,accepted", [
    pytest.param("84590fefe6338be9087f5814b61f9a54c5f21524163a4c127cb5a808e31009f2", "2.1.325", True, id="darwin-arm64"),
    pytest.param("7b6e484b1989a5a14a4b354709ccc8672518a3ec5b9ccd7981fc6254327fde67", "2.1.325", True, id="linux-arm64-glibc"),
    pytest.param("0dc4f4a9a165fc60746d7ae8489712e5fc352f1e7ad4eb22e7141bd0812eb0cf", "2.1.325", True, id="linux-x64-glibc"),
    pytest.param("0dc4f4a9a165fc60746d7ae8489712e5fc352f1e7ad4eb22e7141bd0812eb0c0", "2.1.325", False, id="changed-x64-bytes"),
    pytest.param("a5e3bd36dce97db0429705aa5c3573eea346a77938a89104b9c28fce409b20cd", "2.1.325", False, id="unreviewed-linux-x64-musl"),
    pytest.param("0" * 64, "2.1.325", False, id="unrecognized-same-version"),
    pytest.param("0dc4f4a9a165fc60746d7ae8489712e5fc352f1e7ad4eb22e7141bd0812eb0cf", "2.1.326", False, id="different-version"),
])
def test_provider_signin_scope_requires_reviewed_exact_artifact(digest, version, accepted):
    from claudlobby.plane.provider_signin import scope_shape
    # Public vendor artifact identities, independently inspected per platform;
    # none of these executables is invoked by this scope-admission test.
    scope = {"binary": "/synthetic/claude", "sha256": digest, "version": version,
             "home": "/synthetic/home", "account_directory": "/synthetic/profile",
             "environment": "/synthetic/root/.env"}
    assert scope_shape(scope) is accepted


def test_provider_signin_same_version_label_does_not_admit_unreviewed_bytes(tmp_path):
    from claudlobby.plane.provider_signin import approved_scope, SigninRefused
    root = tmp_path / "private"; root.mkdir(mode=0o700)
    profile = tmp_path / "synthetic-profile"; profile.mkdir(mode=0o700)
    program = tmp_path / "unreviewed-provider"
    program.write_text("#!/bin/sh\n# Claude Code 2.1.325 linux-x64 glibc\nexit 99\n")
    program.chmod(0o700)
    # Missing .env is intentional: reject the actual software bytes before
    # opening a credential destination, irrespective of the version label.
    with pytest.raises(SigninRefused, match="unsupported_provider"):
        approved_scope(program, root, str(profile))


def test_provider_signin_parser_private_redraw_chunks_and_wrapped_token():
    from claudlobby.plane.provider_signin import TokenScreen
    screen = TokenScreen()
    url = ('https://claude.com/cai/oauth/authorize?code=true&client_id=test&response_type=code'
           '&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback'
           '&scope=user%3Ainference&code_challenge=' + 'a'*43 + '&code_challenge_method=S256&state=' + 'b'*32)
    token = 'sk-ant-oat01-' + 's'*80
    stream = ('old waiting text\r\x1b[2K\x1b]8;;' + url + '\x07Open browser\x1b]8;;\x07\n'
              '✓ Long-lived authentication token created successfully!\n'
              'Your OAuth token (valid for 1 year):\n' + token[:40] + '\r\n' + token[40:] + '\n'
              "Store this token securely. You won't be able to see it again.\n"
              'Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>\n').encode()
    for start in range(0,len(stream),3): screen.feed(stream[start:start+3])
    assert screen.url == url
    assert screen.token() == token
    assert token not in repr(screen)


def test_provider_signin_parser_accepts_claude_2_1_325_ink_stream():
    """Claude 2.1.325 setup-token: DECSC/DECRC, DECSTBM reset, focus/theme
    private modes, unanswered terminal queries, an unknown OSC, and an OSC 8
    hyperlink carrying an id parameter (shape captured from the real binary)."""
    from claudlobby.plane.provider_signin import TokenScreen
    screen = TokenScreen()
    url = ('https://claude.com/cai/oauth/authorize?code=true&client_id=test&response_type=code'
           '&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback'
           '&scope=user%3Ainference&code_challenge=' + 'a'*43 + '&code_challenge_method=S256&state=' + 'b'*32)
    token = 'sk-ant-oat01-' + 's'*80
    stream = ('\x1b7\x1b[r\x1b8\x1b[?25h\x1b[?25l\x1b[?2004h\x1b[?2031h\x1b[?1004h'
              '\x1b[>0q\x1b[?u\x1b[c\x1b]7501;?\x07'
              'Welcome\x1b[9Gto\x1b[12GClaude\x1b[19GCode\x1b[24Gv2.1.325\r\r\n'
              '\x1b[1A\x1b[1C\x1b[2G\u00b7 Opening browser to sign in\u2026\r\r\n'
              "Browser didn't open? Use the url below to sign in (c to copy)\r\r\n\r\r\n"
              '\x1b]8;id=17zzcid;' + url + '\x07' + url + '\x1b]8;;\x07\r\r\n'
              'Paste code here if prompted >\r\r\n'
              '\x1b[2J\x1b[H\u2713 Long-lived authentication token created successfully!\r\n'
              'Your OAuth token (valid for 1 year):\r\n' + token + '\r\n'
              "Store this token securely. You won't be able to see it again.\r\n"
              '\x1b[?2031l\x1b[?1004l\x1b[?2004l').encode()
    for start in range(0, len(stream), 5): screen.feed(stream[start:start+5])
    assert screen.url == url
    assert screen.token() == token
    assert token not in repr(screen)


@pytest.mark.parametrize('sequence', [b'\x1b[?2031;x', b'\x1b[>1q', b'\x1b[5c', b'\x1b[3;9r', b'\x1b=',
                                      b'\x1b[?123456h'])
def test_provider_signin_parser_still_refuses_unmodelled_sequences(sequence):
    from claudlobby.plane.provider_signin import TokenScreen, SigninRefused
    with pytest.raises(SigninRefused):
        TokenScreen().feed(sequence + b'x')


def test_provider_signin_parser_ignores_osc8_with_unknown_params():
    from claudlobby.plane.provider_signin import TokenScreen
    screen = TokenScreen()
    url = ('https://claude.com/cai/oauth/authorize?code=true&client_id=test&response_type=code'
           '&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback'
           '&scope=user%3Ainference&code_challenge=' + 'a'*43 + '&code_challenge_method=S256&state=' + 'b'*32)
    screen.feed(('\x1b]8;foo=bar;' + url + '\x07open\x1b]8;;\x07').encode())
    assert screen.url is None


def test_provider_signin_rejects_foreign_url_echo_and_ambiguous_tokens():
    from claudlobby.plane.provider_signin import TokenScreen, SigninRefused
    screen = TokenScreen()
    screen.feed(b'\x1b]8;;https://evil.example/login\x07open\x1b]8;;\x07')
    assert screen.url is None
    screen.feed(b'sk-ant-oat01-example-echo-not-success\n')
    assert screen.token() is None
    with pytest.raises(SigninRefused):
        screen.feed(b'\x1b[999999999A')


def test_provider_signin_env_update_preserves_routing_and_refuses_link(tmp_path):
    import os
    from claudlobby.plane.provider_signin import store_token, SigninRefused
    root=tmp_path/'data'; root.mkdir(mode=0o700)
    env=root/'.env'; env.write_bytes(b"export CLAUDE_CONFIG_DIR='/approved/profile'\n# retained route\n")
    env.chmod(0o600)
    token='sk-ant-oat01-'+'x'*80
    store_token(root,token,lambda:None)
    assert env.read_text().startswith("export CLAUDE_CONFIG_DIR='/approved/profile'\n# retained route\n")
    assert env.read_text().count('CLAUDE_CODE_OAUTH_TOKEN=')==1
    with pytest.raises(SigninRefused): store_token(root,token,lambda:None)
    env.unlink(); env.write_text('export ROUTE=local\n'); env.chmod(0o600)
    os.link(env,root/'alias')
    with pytest.raises(SigninRefused): store_token(root,token,lambda:None)


def test_no_setup_grant_or_optional_schema_never_repairs(setup_authority):
    store,access,grant,reader,*_=setup_authority
    before=access.path.read_bytes()
    with pytest.raises(AccessUnavailable): store.authorize(reader)
    assert access.path.read_bytes()==before
    store.allow(grant)
    assert store.authorize(reader)==grant
    with pytest.raises(AccessDenied):store.authorize(VerifiedReader(OWNER,access.open_session(OWNER).token))


def test_preview_freezes_one_canonical_original_request_without_dispatch(setup_authority):
    from claudlobby.config_plan import read_rendered
    engine,grant,reader=allow(setup_authority)
    preview=engine.preview(reader,grant.generation,"Improve checkout tests")
    assert preview["version"]==2 and preview["fleets"]==["shop"]
    assert preview["dispatch"]=="prepared" and preview["phase"]=="quiesce"
    rendered=read_rendered(engine.root,preview["config_fingerprint"])
    rendered.check_inputs()
    assert rendered.revision==grant.source_revision and not setup_authority[-1].dispatches
    context=engine.context(reader)
    assert context["request"]==binding(preview) and not context["launch_reserved"]
    assert context["continuation"].endswith("host update continue")
    before=(engine.root/"local/shop/fleet.yaml").read_bytes()
    with pytest.raises(AccessDenied):engine.preview(reader,grant.generation,"A changed goal")
    assert (engine.root/"local/shop/fleet.yaml").read_bytes()==before


@pytest.mark.parametrize("other",["other-fleet","other-runtime","target-library","projects"])
def test_whole_host_plan_cannot_include_unapproved_inputs(setup_authority,other):
    engine,grant,reader=allow(setup_authority)
    path=engine.root/"local"/("other" if other.startswith("other") else "shop")
    path.mkdir(parents=True)
    if other=="other-fleet": (path/"fleet.yaml").write_text("fleet: {name: other}\n")
    elif other=="other-runtime": (path/"runtime").mkdir()
    elif other=="target-library": (path/"library").mkdir()
    else: (path/"projects.yaml").write_text("projects: {}\n")
    with pytest.raises((AccessDenied,AccessUnavailable)):engine.preview(reader,grant.generation,"Improve checkout tests")
    assert not setup_authority[-1].dispatches


def test_changed_frozen_inputs_and_expired_preview_refuse_before_reservation(setup_authority):
    engine,grant,reader=allow(setup_authority)
    preview=engine.preview(reader,grant.generation,"Improve checkout tests")
    request=binding(preview)
    path=engine.root/"local/shop/fleet.yaml"; original=path.read_bytes(); path.write_bytes(original+b"\n")
    with pytest.raises(ValueError):engine.launch(reader,**request)
    assert not engine.context(reader)["launch_reserved"]
    path.write_bytes(original)
    setup_authority[4][0]=preview["expires_at"]
    with pytest.raises(AccessDenied):engine.launch(reader,**request)
    assert not setup_authority[-1].dispatches


def test_launch_uses_original_request_once_receipts_never_fill_unknown(setup_authority,monkeypatch):
    from claudlobby import git_update
    engine,grant,reader=allow(setup_authority)
    request=binding(engine.preview(reader,grant.generation,"Improve checkout tests"))
    result=engine.launch(reader,**request)
    assert result["state"]=="quiesce" and result["launch_reserved"]
    assert len(setup_authority[-1].dispatches)==1
    assert engine.launch(reader,**request)==result
    assert engine.receipt(reader,**request)==result
    with pytest.raises(AccessDenied):engine.launch(reader,**{**request,"request_id":str(uuid4())})
    monkeypatch.setattr(git_update,"read_update",lambda _:None)
    assert engine.receipt(reader,**request)["state"]=="unknown"
    assert engine.launch(reader,**request)["state"]=="unknown"
    assert len(setup_authority[-1].dispatches)==1


def test_lost_launch_response_is_reserved_and_never_replayed(setup_authority,monkeypatch):
    from claudlobby import git_update
    engine,grant,reader=allow(setup_authority)
    request=binding(engine.preview(reader,grant.generation,"Improve checkout tests")); calls=[]
    def lost(*_,**kwargs):calls.append(kwargs["expected_request_id"]);raise RuntimeError("lost response")
    monkeypatch.setattr(git_update,"apply_or_continue",lost)
    with pytest.raises(RuntimeError):engine.launch(reader,**request)
    assert engine.launch(reader,**request)["state"]=="quiesce"
    assert calls==[request["request_id"]]


def test_lost_preview_response_recovers_frozen_original_without_new_work(setup_authority,monkeypatch):
    engine,grant,reader=allow(setup_authority)
    monkeypatch.setattr(engine.store,"retain",lambda *_: (_ for _ in ()).throw(RuntimeError("lost preview")))
    with pytest.raises(RuntimeError):engine.preview(reader,grant.generation,"Improve checkout tests")
    context=engine.context(reader)
    assert context["request"]["request_id"]==setup_authority[-1].retained["record"]["request_id"]
    assert not context["launch_reserved"] and context["continuation"]
    with pytest.raises(AccessDenied):engine.preview(reader,grant.generation,"Another goal")
    assert not setup_authority[-1].dispatches


@pytest.mark.parametrize("field,value",[("target","c"*40),("config_fingerprint","d"*64),
                                        ("initialization_scope",{"directory":"/different","manifests":[]})])
def test_original_request_mismatch_refuses_without_dispatch(setup_authority,field,value):
    engine,grant,reader=allow(setup_authority)
    request=binding(engine.preview(reader,grant.generation,"Improve checkout tests"))
    setup_authority[-1].retained["record"][field]=value
    with pytest.raises(AccessDenied):engine.receipt(reader,**request)
    assert not setup_authority[-1].dispatches


def test_partial_initialization_recovery_is_setup_only(setup_authority):
    from claudlobby.plane.owner_source import inspect_source
    from claudlobby.plane.db import db_file
    engine,grant,reader=allow(setup_authority)
    request=binding(engine.preview(reader,grant.generation,"Improve checkout tests"))
    engine.store.reserve(reader,grant,request)
    db_file(engine.root).write_bytes(b"not an admitted recorder")
    assert engine.store.recovery_host(OWNER)==setup_authority[1].current_grant().host_uid
    with pytest.raises((AccessDenied,AccessUnavailable)):inspect_source(engine.root)
    with pytest.raises(AccessDenied):engine.preview(reader,grant.generation,"Another goal")


def test_grant_revocation_and_workspace_rebinding_still_gate_receipts(setup_authority):
    engine,grant,reader=allow(setup_authority)
    request=binding(engine.preview(reader,grant.generation,"Improve checkout tests"))
    engine.store.revoke(grant)
    with pytest.raises(AccessDenied):engine.receipt(reader,**request)
    with pytest.raises(AccessDenied):engine.store.allow(replace(grant,generation=str(uuid4())))
    assert setup_authority[-1].retained["record"]["request_id"]==request["request_id"]


@pytest.mark.parametrize("malformed",["CREATE TABLE setup_metadata(singleton INTEGER,version INTEGER)",
    "CREATE VIEW setup_metadata AS SELECT 1 AS singleton,1 AS version", "CREATE TABLE setup_metadata(wrong INTEGER)"])
def test_optional_setup_absence_is_empty_but_partial_schema_refuses(setup_authority,malformed):
    store,access,*_=setup_authority
    before=access.path.read_bytes()
    assert store.local() is None and access.path.read_bytes()==before
    with access._connection(write=True) as conn:conn.execute(malformed)
    with pytest.raises(AccessUnavailable):store.local()


def test_historical_sealed_setup_is_retained_never_reinterpreted(setup_authority):
    store,access,grant,*_=setup_authority
    store.allow(grant)
    request=str(uuid4()); plan="p-"+"a"*64
    with access._connection(write=True) as conn:
        conn.execute("UPDATE setup_metadata SET version=1")
        conn.execute("UPDATE setup_grant SET launch_id=?,launch_plan=?",(request,plan))
    before=access.path.read_bytes()
    state=store.retained_state()
    assert state.state=="legacy_recovery_required" and state.grant is None
    assert (state.launch_id,state.launch_plan)==(request,plan)
    with pytest.raises(AccessUnavailable,match="historical sealed"):store.local()
    assert access.path.read_bytes()==before


@pytest.mark.parametrize("local", [False, True])
def test_local_approval_uses_managed_source_and_observed_directory(setup_authority,monkeypatch,tmp_path,local):
    from claudlobby.__main__ import main
    from claudlobby import supervision_inventory
    from tests.test_host_owner import terminal
    store,access,grant,*_=setup_authority
    if local:
        monkeypatch.setenv("HOME", str(tmp_path))
        project=tmp_path/"project"; project.mkdir()
        grant=store.candidate(OWNER,template="local-project",fleet="shop",repository=None,
            project_directory=str(project),install_directory=grant.install_directory)
    monkeypatch.setattr(supervision_inventory,"Adapter",lambda _:SimpleNamespace(read=lambda _:f"manager\tLinux\ndirectory\t{grant.install_directory}\n"))
    flags=["--root",str(access.root),"host","owner","allow-setup","--template",grant.template,
        "--target-fleet",grant.fleet,"--install-directory",grant.install_directory,
        *(["--project-directory",grant.project_directory] if local else ["--repository",grant.repository])]
    terminal(monkeypatch,iter(["NO"]))
    assert main(flags)!=0 and store.local() is None
    approval_output=terminal(monkeypatch,iter(["ALLOW-SETUP"]))
    assert main(flags)==0 and store.local().source_revision==grant.source_revision
    if local:
        assert "Approve read/write access to this exact local project: " + json.dumps(grant.project_directory) in approval_output.getvalue()
    else:
        assert "project_directory" not in approval_output.getvalue()
    terminal(monkeypatch,iter(["REVOKE-SETUP"]))
    assert main(["--root",str(access.root),"host","owner","revoke-setup"])==0
    assert store.local() is None


def test_guide_pending_original_returns_local_continuation_without_serving(setup_authority,monkeypatch):
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.command_result import CommandFailure
    engine,grant,reader=allow(setup_authority)
    request=binding(engine.preview(reader,grant.generation,"Improve checkout tests"))
    monkeypatch.setattr(guide,"_serving",lambda *_:pytest.fail("started a server"))
    with pytest.raises(CommandFailure) as caught:guide.run(SimpleNamespace(),SimpleNamespace(root=engine.root),setup_authority[1],None)
    assert caught.value.data["request_id"]==request["request_id"]
    assert "host update continue" in caught.value.error.hint


@pytest.mark.parametrize("local", [False, True])
def test_guide_reuses_matching_git_approval_and_stable_cli_without_renewal(setup_authority,monkeypatch,tmp_path,local):
    from contextlib import contextmanager
    import sys
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.context import resolve_paths
    from tests.test_host_owner import terminal
    from tests.test_plane_owner_workspace import ISSUER, ORIGIN
    store,access,grant,*_=setup_authority
    if local:
        monkeypatch.setenv("HOME", str(tmp_path))
        project=tmp_path/"project"; project.mkdir()
        grant=store.candidate(OWNER,template="local-project",fleet="shop",repository=None,
            project_directory=str(project),install_directory=grant.install_directory)
    store.allow(grant)
    monkeypatch.setattr(guide,"_native_directories",lambda _: (Path(grant.install_directory),))
    commands=[]
    @contextmanager
    def serving(command,*_):
        commands.append(command)
        class Child:
            def wait(self, timeout=None):raise KeyboardInterrupt
        yield Child()
    monkeypatch.setattr(guide,"_serving",serving)
    args=SimpleNamespace(origin="https://host.example.test",tailscale=Path(sys.executable),tailscale_socket="",
        socket=access.root/"state/owner/owner.sock",workspace_issuer=ISSUER,workspace_origin=ORIGIN,
        target_fleet=grant.fleet,repository=grant.repository,template=grant.template,project_directory=grant.project_directory,
        install_directory=Path(grant.install_directory))
    console=terminal(monkeypatch,iter(())); before=access.path.read_bytes()
    result=guide.run(args,resolve_paths(root=access.root),access,console)
    assert result.data["state"]=="serving_stopped" and len(commands)==1
    assert commands[0][0]==str(Path(grant.installation)/"bin/claudlobby")
    assert access.path.read_bytes()==before and store.local()==grant


@pytest.mark.parametrize("platform,xdg,observed,preferred,override", [
    ("darwin", None, ("mac-system", "mac-user"), "mac-user", None),
    ("linux", None, ("control", "transient", "linux-system", "linux-user"), "linux-user", None),
    ("linux", "absolute", ("linux-user", "xdg-user", "linux-system"), "xdg-user", None),
    ("linux", None, ("control", "transient", "linux-system"), None, None),
    ("linux", None, ("linux-system",), None, None),
    ("linux", "absolute", ("linux-user",), None, None),
    ("linux", "relative", ("linux-user", "linux-system"), None, None),
    ("linux", None, ("linux-user", "linux-system"), "linux-user", "linux-system"),
    ("linux", None, ("linux-user",), "linux-user", "linux-system"),
], ids=["mac-user", "linux-user", "linux-xdg", "no-user-match", "only-system",
        "xdg-not-observed", "relative-xdg", "explicit-observed", "explicit-unobserved"])
def test_guide_install_directory_uses_only_observed_persistent_user_default(
        tmp_path, monkeypatch, platform, xdg, observed, preferred, override):
    import io
    import json
    import sys
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.command_result import CommandFailure

    home = tmp_path / "home"
    root = tmp_path / "data"
    directories = {
        "mac-user": home / "Library/LaunchAgents",
        "mac-system": Path("/Library/LaunchAgents"),
        "linux-user": home / ".config/systemd/user",
        "xdg-user": tmp_path / "config/systemd/user",
        "linux-system": Path("/etc/systemd/user"),
        "control": home / ".config/systemd/user.control",
        "transient": tmp_path / "run/systemd/transient",
    }
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    if xdg:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config") if xdg == "absolute" else "relative-config")
    monkeypatch.setattr(guide.sys, "platform", platform)
    monkeypatch.setattr(guide, "_native_directories", lambda _: tuple(directories[key] for key in observed))
    args = SimpleNamespace(origin="https://host.example.test", tailscale=sys.executable,
        tailscale_socket="", socket=str(root / "state/owner/owner.sock"),
        workspace_issuer="https://accounts.example.test", workspace_origin="https://plane.example.test",
        target_fleet="shop", repository="example-org/shop",
        install_directory=directories[override] if override else None)
    terminal = io.StringIO()
    terminal.readline = lambda size=-1: "\n"
    approval = error = None
    try:
        _, approval = guide._configuration(args, SimpleNamespace(root=root), terminal,
            Path(sys.executable))
    except CommandFailure as exc:
        error = exc
    output = terminal.getvalue()
    if override:
        if override in observed:
            assert error is None
            assert approval.install_directory == directories[override]
        else:
            assert error is not None and "not an observed native search path" in str(error)
    elif preferred:
        assert "Native installation directory [" + json.dumps(str(directories[preferred])) + "]: " in output
        assert error is None
        assert approval.install_directory == directories[preferred]
        assert "Native installation choices:" not in output
    else:
        assert "Native installation directory: " in output
        assert "Native installation directory [" not in output
        assert error is not None and "explicit printable configuration" in str(error)
        assert "Native installation choices:" in output
    assert not (root / "state").exists()


def test_local_grant_preview_recovery_and_launch_keep_original(setup_authority, tmp_path, monkeypatch):
    store, access, old, reader, *rest = setup_authority
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"; project.mkdir()
    grant = store.candidate(OWNER, template="local-project", fleet="shop", repository=None,
                           project_directory=str(project), install_directory=old.install_directory)
    store.allow(grant)
    engine = SetupEngine(access)
    context = engine.context(reader)
    assert context["version"] == 3 and context["project_directory"] == str(project)
    assert "repository" not in context
    monkeypatch.setattr(engine.store, "retain", lambda *_: (_ for _ in ()).throw(RuntimeError("lost preview")))
    with pytest.raises(RuntimeError, match="lost preview"):
        engine.preview(reader, grant.generation, "Review local code")
    recovered = engine.context(reader)
    request = recovered["request"]
    assert request and not recovered["launch_reserved"]
    assert engine.launch(reader, **request)["version"] == 3
    assert engine.launch(reader, **request)["version"] == 3
    assert len(setup_authority[-1].dispatches) == 1


def test_local_grant_refuses_project_retarget_and_authority_overlap(setup_authority, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store, access, old, *_ = setup_authority
    for project in (access.root, access.root.parent, Path(old.install_directory)):
        with pytest.raises((AccessDenied, CommandFailure)):
            store.candidate(OWNER, template="local-project", fleet="shop", repository=None,
                            project_directory=str(project), install_directory=old.install_directory)


def test_repository_snapshot_stays_byte_compatible(setup_authority):
    from dataclasses import asdict
    from claudlobby.plane.owner_setup import _snapshot
    store, _, grant, *_ = setup_authority
    old = asdict(grant); old.pop("project_directory"); old.pop("account_directory"); old.pop("managed_owner"); old.pop("provider_signin")
    expected = json.dumps(old, sort_keys=True, separators=(",", ":"))
    assert _snapshot(grant) == expected
    store.allow(grant)
    assert store.local() == grant


def test_local_account_approval_survives_lost_preview_and_exact_launch(setup_authority, tmp_path, monkeypatch):
    import yaml
    store, access, old, reader, *_ = setup_authority
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"; project.mkdir()
    account = tmp_path / "private account"; account.mkdir()
    grant = store.candidate(OWNER, template="local-project", fleet="shop", repository=None,
        project_directory=str(project), account_directory=str(account), install_directory=old.install_directory)
    store.allow(grant)
    assert store.local().account_directory == str(account)
    engine = SetupEngine(access)
    monkeypatch.setattr(engine.store, "retain", lambda *_: (_ for _ in ()).throw(RuntimeError("lost preview")))
    with pytest.raises(RuntimeError, match="lost preview"):
        engine.preview(reader, grant.generation, "Review local work")
    context = engine.context(reader)
    assert "account_directory" not in context  # No new browser-controlled path.
    manifest = access.root / "local/shop/fleet.yaml"
    assert yaml.safe_load(manifest.read_bytes())["fleet"]["accounts"]["default"] == str(account)
    request = context["request"]
    assert engine.launch(reader, **request)["launch_reserved"]
    assert engine.launch(reader, **request)["launch_reserved"]
    assert len(setup_authority[-1].dispatches) == 1


@pytest.mark.parametrize("explicit", [False, True])
def test_guide_local_account_choice_is_displayed_and_explicit_choice_wins(tmp_path, monkeypatch, explicit):
    import io, sys
    from claudlobby.commands import host_owner_setup as guide
    monkeypatch.setenv("HOME", str(tmp_path))
    project=tmp_path/"project"; project.mkdir()
    ambient=tmp_path/"ambient profile"; ambient.mkdir()
    selected=tmp_path/"selected profile"; selected.mkdir()
    units=tmp_path/"units"; units.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ambient))
    monkeypatch.setattr(guide, "_native_directories", lambda _: (units,))
    args=SimpleNamespace(origin="https://host.example.test",tailscale=Path(sys.executable),tailscale_socket="",
        socket=tmp_path/"owner.sock",workspace_issuer="https://accounts.example.test",workspace_origin="https://plane.example.test",
        target_fleet="local-team",repository=None,template="local-project",project_directory=str(project),
        account_directory=str(selected) if explicit else None,install_directory=units)
    terminal=io.StringIO()
    _, approval=guide._configuration(args,SimpleNamespace(root=tmp_path/"data"),terminal,tmp_path/"install/bin/claudlobby")
    expected=str(selected if explicit else ambient)
    assert approval.account_directory==expected
    assert expected in terminal.getvalue()


def test_local_account_redirect_after_preview_refuses_without_dispatch(setup_authority, tmp_path, monkeypatch):
    store, access, old, reader, *_=setup_authority
    monkeypatch.setenv("HOME",str(tmp_path))
    project=tmp_path/"project"; project.mkdir()
    account=tmp_path/"account"; account.mkdir()
    other=tmp_path/"other account"; other.mkdir()
    grant=store.candidate(OWNER,template="local-project",fleet="shop",repository=None,
        project_directory=str(project),account_directory=str(account),install_directory=old.install_directory)
    store.allow(grant);engine=SetupEngine(access)
    preview=engine.preview(reader,grant.generation,"Review local work")
    account.rmdir(); account.symlink_to(other,target_is_directory=True)
    with pytest.raises(CommandFailure):engine.launch(reader,**binding(preview))
    assert not setup_authority[-1].dispatches


def test_guide_hands_off_original_launch_instead_of_waiting_for_updater(setup_authority, monkeypatch):
    from contextlib import contextmanager
    import subprocess, sys
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.context import resolve_paths
    from tests.test_host_owner import terminal
    from tests.test_plane_owner_workspace import ISSUER, ORIGIN
    store, access, grant, reader, *_ = setup_authority
    store.allow(grant)
    engine = SetupEngine(access)
    monkeypatch.setattr(guide, "_native_directories", lambda _: (Path(grant.install_directory),))
    original = {}
    drained = []
    @contextmanager
    def serving(*_):
        class Child:
            def wait(self, timeout=None):
                assert timeout is not None, "guide retains its reader indefinitely after Launch"
                assert not original, "guide waited again after the original Launch was reserved"
                preview = engine.preview(reader, grant.generation, "Review checkout")
                original.update(binding(preview))
                engine.launch(reader, **original)
                raise subprocess.TimeoutExpired("owner", timeout)
        try:
            yield Child()
        finally:
            drained.append(True)
    monkeypatch.setattr(guide, "_serving", serving)
    args = SimpleNamespace(origin="https://host.example.test", tailscale=Path(sys.executable), tailscale_socket="",
        socket=access.root/"state/owner/owner.sock", workspace_issuer=ISSUER, workspace_origin=ORIGIN,
        target_fleet=grant.fleet, repository=grant.repository, template=grant.template,
        project_directory=None, install_directory=Path(grant.install_directory))
    result = guide.run(args, resolve_paths(root=access.root), access, terminal(monkeypatch, iter(())))
    assert drained == [True]
    assert result.data["state"] == "launch_handoff"
    assert result.data["request_id"] == original["request_id"]
    assert result.data["config_fingerprint"] == original["config_fingerprint"]
    assert len(setup_authority[-1].dispatches) == 1
    assert any("host update status" in line for line in result.lines)
    assert any("host owner serve" in line for line in result.lines)


def test_typed_guide_preserves_original_launch_and_exits_without_terminal_handoff(setup_authority, monkeypatch):
    from contextlib import contextmanager
    import subprocess, sys
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.context import resolve_paths
    store, access, grant, reader, *_ = setup_authority
    store.allow(grant)
    engine = SetupEngine(access)
    config = {"origin": "https://host.example.test", "socket": str(access.root / "state/owner/owner.sock"),
        "tailscale": sys.executable, "tailscale_socket": None,
        "workspace_issuer": "https://accounts.example.test", "workspace_origin": "https://plane.example.test"}
    approval = SimpleNamespace(**{name: getattr(grant, name) for name in ("template", "repository", "project_directory",
        "account_directory", "managed_owner", "provider_signin")}, target_fleet=grant.fleet,
        install_directory=Path(grant.install_directory))
    monkeypatch.setattr(guide, "_configuration", lambda *_: (config, approval))
    states, original, drained = [], {}, []
    class Consent:
        def state(self, state, **data): states.append(state)
        def check_cancel(self): pass
        def approve(self, *_args, **_kwargs): pytest.fail("matching grant was renewed")
    consent = Consent()
    @contextmanager
    def serving(*args, **kwargs):
        assert kwargs["consent"] is consent
        class Child:
            def wait(self, timeout):
                original.update(binding(engine.preview(reader, grant.generation, "Review checkout")))
                engine.launch(reader, **original)
                raise subprocess.TimeoutExpired("owner", timeout)
        try: yield Child()
        finally: drained.append(True)
    monkeypatch.setattr(guide, "_serving", serving)
    profile = SimpleNamespace(prepare_directories=lambda *_: None, prepare_provider=lambda *_: None, origin=lambda: config["origin"],
        project=Path("/fixed/project"), account=Path("/fixed/profile"), units=Path(grant.install_directory),
        provider=Path("/fixed/provider"))
    result = guide.run(SimpleNamespace(), resolve_paths(root=access.root), access, None, consent=consent, profile=profile)
    assert result.data == {"state": "launch_handoff", **original}
    assert drained == [True] and states == ["preview_launch_ready"]
    assert len(setup_authority[-1].dispatches) == 1


def test_typed_completion_reopen_only_offers_optional_messages_for_original_done(setup_authority, monkeypatch):
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby.context import resolve_paths
    from claudlobby.plane.owner_access import OwnerAccess
    engine, grant, request, _ = completed_setup(setup_authority, monkeypatch)
    monkeypatch.setattr(guide, "_configuration", lambda *_: pytest.fail("reopened configuration"))
    monkeypatch.setattr(guide, "_serving", lambda *_: pytest.fail("reopened competing listener"))
    seen = []
    class Consent:
        def approve(self, action, scope, **kwargs):
            seen.append(action)
            raise StdioRefused("declined")
    result = guide.run(SimpleNamespace(finish_setup=list(request.values()), target_fleet=None),
        resolve_paths(root=engine.root), OwnerAccess(engine.root), None, consent=Consent())
    assert result.data["state"] == "setup_complete" and result.data["messages"] == {"state": "skipped"}
    assert seen == ["actor_alias"] and len(setup_authority[-1].dispatches) == 1


def test_typed_fresh_guide_keeps_pairing_before_workspace_storage(team_setup, monkeypatch):
    from contextlib import contextmanager
    import sys
    from claudlobby import resources
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.commands.owner_setup_consent import StdioGuideConsent
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby.context import resolve_paths
    from tests.test_plane_owner_workspace import proposed
    args, root, _ = team_setup
    package = resources.get_resources()
    package.installation.bin.mkdir(parents=True)
    package.installation.cli.write_text("#!/bin/sh\nexit 0\n")
    package.installation.cli.chmod(0o755)
    access = OwnerAccess(root)
    config = {"origin": "https://host.example.test", "socket": str(root / "state/owner/owner.sock"),
        "tailscale": sys.executable, "tailscale_socket": None,
        "workspace_issuer": "https://accounts.example.test", "workspace_origin": "https://plane.example.test"}
    monkeypatch.setattr(guide, "_configuration", lambda *_: (config, SimpleNamespace(managed_owner=None)))
    codes, seen, servers = {}, [], []
    class Channel:
        def approve(self, action, scope, **kwargs):
            seen.append(action)
            if action == "bind_workspace":
                raise StdioRefused("declined")
            return kwargs.get("original", True)
        def token(self, kind): return codes[kind]
        def emit(self, *_args, **_kwargs): pass
        def check_cancel(self): pass
    @contextmanager
    def serving(command, *_args, **_kwargs):
        servers.append(command)
        owner = access.current_grant()
        if len(servers) == 1:
            assert owner is None and "--bootstrap-pairing-origin" in command
            with access._connection() as conn:
                assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'workspace_%'").fetchone()
            codes["pairing"] = access.begin_pairing(OWNER).token
        else:
            assert owner.active and "--bootstrap-pairing-origin" not in command
            assert access.connected().preparation_state() == "ready"
            codes["workspace"] = proposed((access, access.workspace(), owner, [access._now()])).nonce
        yield SimpleNamespace()
    monkeypatch.setattr(guide, "_serving", serving)
    profile = SimpleNamespace(prepare_directories=lambda *_: None, prepare_provider=lambda *_: None, origin=lambda: config["origin"],
        project=Path("/fixed/project"), account=Path("/fixed/profile"), units=root.parent / "units", provider=Path("/fixed/provider"))
    with pytest.raises(StdioRefused, match="declined"):
        guide.run(SimpleNamespace(), resolve_paths(root=root), access, None,
            consent=StdioGuideConsent(Channel()), profile=profile)
    assert len(servers) == 2
    assert seen == ["initialize_owner", "pair_owner", "initialize_workspace", "initialize_connected", "bind_workspace"]
    assert access.current_grant().active and access.workspace().status() == ("absent", None)
    with access._connection() as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_guide_hands_off_launch_retained_as_soon_as_listener_is_ready(setup_authority, monkeypatch):
    """The browser can launch before the guide's second retained-state read."""
    from contextlib import contextmanager
    import sys
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.context import resolve_paths
    from tests.test_host_owner import terminal
    from tests.test_plane_owner_workspace import ISSUER, ORIGIN
    store, access, grant, reader, *_ = setup_authority
    store.allow(grant)
    engine = SetupEngine(access); original = {}; drained = []
    monkeypatch.setattr(guide, "_native_directories", lambda _: (Path(grant.install_directory),))
    @contextmanager
    def serving(*_):
        original.update(binding(engine.preview(reader, grant.generation, "Review checkout")))
        engine.launch(reader, **original)
        class Child:
            def wait(self, timeout=None): pytest.fail("retained Launch must hand off before waiting")
        try: yield Child()
        finally: drained.append(True)
    monkeypatch.setattr(guide, "_serving", serving)
    args = SimpleNamespace(origin="https://host.example.test", tailscale=Path(sys.executable), tailscale_socket="",
        socket=access.root/"state/owner/owner.sock", workspace_issuer=ISSUER, workspace_origin=ORIGIN,
        target_fleet=grant.fleet, repository=grant.repository, template=grant.template,
        project_directory=None, install_directory=Path(grant.install_directory))
    result = guide.run(args, resolve_paths(root=access.root), access, terminal(monkeypatch, iter(())))
    assert result.data["state"] == "launch_handoff" and drained == [True]
    assert result.data["request_id"] == original["request_id"]
    assert len(setup_authority[-1].dispatches) == 1


def managed_grant(fixture, tmp_path, monkeypatch):
    import sys
    from claudlobby.commands.host_owner_setup import _owner_override_plan, _commit_owner_override
    store, access, old, reader, *_ = fixture
    override = tmp_path/"private-host.yaml"
    override.write_text("host:\n  unit_prefix: ownedtest\n  jobs:\n    plane-owner:\n      enroll: false\n")
    monkeypatch.setenv("CLAUDLOBBY_HOST_SYSTEM_YAML", str(override))
    config = dict(origin="https://host.example.test", tailscale=sys.executable, tailscale_socket=None,
        socket=str(access.root/"state/owner/owner.sock"), workspace_issuer="https://accounts.example.test",
        workspace_origin="https://plane.example.test")
    plan = _owner_override_plan(access.root, config)
    grant = store.candidate(OWNER, template=old.template, fleet=old.fleet, repository=old.repository,
        install_directory=old.install_directory, managed_owner=plan["binding"])
    return grant, plan, override, _commit_owner_override


def test_managed_owner_requires_consented_override_and_preserves_original_preview(setup_authority, tmp_path, monkeypatch):
    grant, plan, override, commit = managed_grant(setup_authority, tmp_path, monkeypatch)
    store, access, _, reader, *_ = setup_authority
    with pytest.raises(AccessDenied): store.allow(grant)
    assert "enroll: false" in override.read_text()
    commit(plan); store.allow(grant)
    assert store.local() == grant
    engine = SetupEngine(access)
    monkeypatch.setattr(engine.store, "retain", lambda *_: (_ for _ in ()).throw(RuntimeError("lost preview")))
    with pytest.raises(RuntimeError): engine.preview(reader, grant.generation, "Review local work")
    context = engine.context(reader)
    assert context["managed_owner"] is True
    assert "override" not in context and "config" not in context
    original = context["request"]
    assert engine.launch(reader, **original)["managed_owner"] is True
    assert engine.launch(reader, **original)["launch_reserved"] is True
    assert len(setup_authority[-1].dispatches) == 1


def test_managed_owner_change_after_preview_refuses_dispatch(setup_authority, tmp_path, monkeypatch):
    grant, plan, override, commit = managed_grant(setup_authority, tmp_path, monkeypatch)
    store, access, _, reader, *_ = setup_authority
    commit(plan); store.allow(grant)
    engine = SetupEngine(access)
    preview = engine.preview(reader, grant.generation, "Review local work")
    override.write_text(override.read_text().replace("https://host.example.test", "https://other.example.test"))
    with pytest.raises(AccessDenied): engine.launch(reader, **binding(preview))
    assert not setup_authority[-1].dispatches


def test_guide_handoff_drains_real_child_and_releases_both_runtime_readers(setup_authority, monkeypatch, tmp_path):
    import fcntl, os, subprocess, sys, threading, time
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.context import resolve_paths
    from claudlobby.installation import Installation, _runtime_lock
    from tests.conftest import constructed_env
    from tests.test_host_owner import terminal
    from tests.test_plane_owner_workspace import ISSUER, ORIGIN
    store, access, grant, reader, *_ = setup_authority
    store.allow(grant)
    engine = SetupEngine(access)
    installation = Installation(Path(grant.installation), access.root)
    lock = access.root / "state/runtime-use.lock"
    lock.touch(mode=0o600)
    parent_fd = _runtime_lock(installation)
    ready, drained = tmp_path/"ready", tmp_path/"drained"
    child_code = """import fcntl,os,signal,time
from pathlib import Path
fd=os.open(os.environ['LOCK_FILE'],os.O_RDONLY)
fcntl.flock(fd,fcntl.LOCK_SH)
def stop(*_):
    time.sleep(.05)
    Path(os.environ['DRAINED_FILE']).touch()
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
Path(os.environ['READY_FILE']).touch()
while True: time.sleep(.05)
"""
    real_popen = subprocess.Popen
    children, failures, forced = [], [], []
    def spawn(command, **kwargs):
        if command[0] == str(installation.cli):
            kwargs['env'] = constructed_env(LOCK_FILE=str(lock), READY_FILE=str(ready), DRAINED_FILE=str(drained))
            child = real_popen([sys.executable, '-I', '-B', '-c', child_code], **kwargs)
            children.append(child)
            return child
        return real_popen(command, **kwargs)
    monkeypatch.setattr(guide.subprocess, 'Popen', spawn)
    monkeypatch.setattr(guide, '_ready', lambda *_: ready.exists())
    monkeypatch.setattr(guide, '_native_directories', lambda _: (Path(grant.install_directory),))
    def launch_from_browser():
        try:
            deadline = time.monotonic()+5
            while not ready.exists() and time.monotonic()<deadline: time.sleep(.01)
            assert ready.exists()
            probe = os.open(lock, os.O_RDONLY)
            try:
                with pytest.raises(BlockingIOError): fcntl.flock(probe, fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally: os.close(probe)
            preview = engine.preview(reader, grant.generation, 'Review checkout')
            engine.launch(reader, **binding(preview))
            deadline = time.monotonic()+3
            while not drained.exists() and time.monotonic()<deadline: time.sleep(.01)
            if not drained.exists():
                forced.append(True)
                children[0].terminate()  # Test teardown: old guide otherwise waits forever.
        except BaseException as exc:
            failures.append(exc)
            if children and children[0].poll() is None: children[0].terminate()
    thread = threading.Thread(target=launch_from_browser)
    args = SimpleNamespace(origin='https://host.example.test', tailscale=Path(sys.executable), tailscale_socket='',
        socket=access.root/'state/owner/owner.sock', workspace_issuer=ISSUER, workspace_origin=ORIGIN,
        target_fleet=grant.fleet, repository=grant.repository, template=grant.template,
        project_directory=None, install_directory=Path(grant.install_directory))
    try:
        thread.start()
        result = guide.run(args, resolve_paths(root=access.root), access, terminal(monkeypatch, iter(())))
        thread.join(5)
        assert not thread.is_alive() and not failures and not forced
        assert result.data['state']=='launch_handoff' and drained.exists()
        assert children[0].poll()==0
        probe = os.open(lock, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError): fcntl.flock(probe, fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally: os.close(probe)
    finally:
        os.close(parent_fd)  # The stable launcher closes its lease when run returns.
        if children and children[0].poll() is None:
            children[0].terminate(); children[0].wait(timeout=5)
        thread.join(5)
    probe = os.open(lock, os.O_RDONLY)
    try: fcntl.flock(probe, fcntl.LOCK_EX|fcntl.LOCK_NB)
    finally: os.close(probe)


def test_local_preview_and_launch_refuse_redirected_project(setup_authority, tmp_path, monkeypatch):
    store, access, old, reader, *_ = setup_authority
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"; project.mkdir()
    other = tmp_path / "other"; other.mkdir()
    grant = store.candidate(OWNER, template="local-project", fleet="shop", repository=None,
                           project_directory=str(project), install_directory=old.install_directory)
    store.allow(grant); engine = SetupEngine(access)
    plan = engine.preview(reader, grant.generation, "Review local code")
    project.rmdir(); project.symlink_to(other, target_is_directory=True)
    with pytest.raises(CommandFailure): engine.launch(reader, **binding(plan))
    assert not setup_authority[-1].dispatches
    assert engine.context(reader)["request"] == binding(plan)


@pytest.mark.parametrize("change", ["expired", "revoked", "mixed"])
def test_local_authority_changes_refuse_effects(setup_authority, tmp_path, monkeypatch, change):
    store, access, old, reader, clock, args = setup_authority
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"; project.mkdir()
    grant = store.candidate(OWNER, template="local-project", fleet="shop", repository=None,
                           project_directory=str(project), install_directory=old.install_directory)
    store.allow(grant); engine = SetupEngine(access)
    if change == "expired": clock[0] = grant.expires_at
    elif change == "revoked": store.revoke(grant)
    else:
        with access._connection(write=True) as conn:
            value = json.loads(conn.execute("SELECT snapshot FROM setup_grant").fetchone()[0])
            value["repository"] = "example-org/shop"
            conn.execute("UPDATE setup_grant SET snapshot=?", (json.dumps(value),))
    with pytest.raises((AccessDenied, AccessUnavailable)):
        engine.preview(reader, grant.generation, "Review local code")
    assert not args.dispatches and args.retained["record"] is None


@pytest.fixture
def synthetic_signin(setup_authority,monkeypatch,tmp_path):
    """Only the external provider is synthetic; real PTY, DB, admission and files."""
    import hashlib,sys
    from dataclasses import replace
    from claudlobby.plane import provider_signin
    store,access,grant,reader,*_=setup_authority
    access.root.chmod(0o700)
    env=access.root/'.env'; env.write_text('export ROUTE=private\n');env.chmod(0o600)
    lock=access.root/'state/runtime-use.lock';lock.touch(mode=0o600)
    profile=Path.home()/'.claude';profile.mkdir(mode=0o700)
    monkeypatch.setenv('HTTPS_PROXY','http://proxy.example.test:8888')
    program=tmp_path/'synthetic-provider'
    url=('https://claude.com/cai/oauth/authorize?code=true&client_id=test&response_type=code'
         '&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback'
         '&scope=user%3Ainference&code_challenge='+'a'*43+'&code_challenge_method=S256&state='+'b'*32)
    token='sk-ant-oat01-'+'s'*80
    body=f'''#!{sys.executable}
import os,sys,time
assert sys.argv[1:]==['setup-token']
assert os.environ['BROWSER']=='/usr/bin/true'
assert os.environ['DISABLE_AUTOUPDATER']=='1'
assert os.environ['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC']=='1'
assert os.environ['HTTPS_PROXY']=='http://proxy.example.test:8888'
assert not any(key in os.environ for key in ['ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN','SSH_AUTH_SOCK'])
sys.stdout.write('\\x1b]8;;'+{url!r}+'\\x07Open Claude\\x1b]8;;\\x07\\r\\n');sys.stdout.flush()
code=sys.stdin.readline().strip()
assert code=='accepted#'+ 'b'*32
sys.stdout.write('\\x1b[2K\\r✓ Long-lived authentication token created successfully!\\r\\nYour OAuth token (valid for 1 year):\\r\\n'+{token!r}+'\\r\\nStore this token securely. You won\\\'t be able to see it again.\\r\\nUse this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>\\r\\n');sys.stdout.flush()
'''
    program.write_text(body);program.chmod(0o700)
    digest=hashlib.sha256(program.read_bytes()).hexdigest()
    monkeypatch.setattr(provider_signin,'SUPPORTED',{digest:'2.1.325'})
    scope=provider_signin.approved_scope(program,access.root,str(profile))
    grant=replace(grant,provider_signin=scope)
    store.allow(grant)
    return SetupEngine(access),grant,reader,program,token,env


def _signin_until(engine,grant,reader,attempt,state):
    import time
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        result=engine.signin_status(reader,grant.generation,attempt)
        if result['state']==state:return result
        if result['state'] in {'needs_attention','unknown','cancelled'}:pytest.fail(str(result))
        time.sleep(0.025)
    pytest.fail('synthetic sign-in did not reach expected finite state')


@pytest.mark.parametrize('source_links', [1, 2])
def test_provider_signin_real_pty_original_attempt_code_and_host_only_store(synthetic_signin, source_links):
    import os, time
    from claudlobby.plane import provider_signin
    engine,grant,reader,program,token,env=synthetic_signin
    if source_links == 2:
        os.link(program, program.with_name('package-manager-link'))
    assert provider_signin.approved_scope(program, engine.root, grant.account_directory) == grant.provider_signin
    attempt=str(uuid4())
    try:
        first=engine.signin_begin(reader,grant.generation,attempt)
        assert first['attempt_id']==attempt
        waiting=_signin_until(engine,grant,reader,attempt,'waiting')
        assert waiting['url'].startswith('https://claude.com/cai/oauth/authorize?')
        with pytest.raises(AccessDenied):engine.signin_begin(reader,grant.generation,str(uuid4()))
        with pytest.raises(AccessDenied):engine.preview(reader,grant.generation,'Do not start while signing in')
        with pytest.raises(AccessDenied):engine.signin_code(reader,grant.generation,attempt,'accepted#wrong-state')
        engine.signin_code(reader,grant.generation,attempt,'accepted#'+'b'*32)
        result=_signin_until(engine,grant,reader,attempt,'connected')
        staged = engine.root/'state/provider-signin'/('claude-signin-'+attempt)/'claude'
        assert staged.stat().st_nlink == 1
        assert staged.read_bytes() == program.read_bytes()
        assert token not in json.dumps(result)
        assert env.read_text()=='export ROUTE=private\n'+f"export CLAUDE_CODE_OAUTH_TOKEN='{token}'\n"
        with engine.store.access._connection() as conn:
            row=dict(conn.execute('SELECT * FROM setup_provider').fetchone())
            assert token not in json.dumps(row) and 'accepted#' not in json.dumps(row)
        assert engine.context(reader)['signin_attempt']['state']=='connected'
        assert SetupEngine(engine.store.access).signin_status(reader,grant.generation,attempt)['state']=='connected'
        assert engine.preview(reader,grant.generation,'Requested local work')['dispatch']=='prepared'
        with pytest.raises(AccessDenied):engine.signin_begin(reader,grant.generation,attempt)
    finally:engine.signin.shutdown()


@pytest.mark.parametrize('stop',['cancel','shutdown','revoke'])
def test_provider_signin_owned_child_drained_no_replay_and_no_credential(synthetic_signin,stop):
    import os
    engine,grant,reader,program,token,env=synthetic_signin
    attempt=str(uuid4())
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        _signin_until(engine,grant,reader,attempt,'waiting')
        if stop=='cancel':engine.signin_cancel(reader,grant.generation,attempt)
        elif stop=='shutdown':engine.signin.shutdown()
        else:
            engine.store.revoke(grant)
            engine.signin._thread.join(timeout=8)
        assert not engine.signin._thread.is_alive()
        assert env.read_text()=='export ROUTE=private\n'
        if stop!='revoke':
            result=SetupEngine(engine.store.access).signin_status(reader,grant.generation,attempt)
            assert result['state'] in {'cancelled','needs_attention','unknown'}
            assert 'url' not in result
    finally:engine.signin.shutdown()


def test_provider_signin_interrupted_reservation_is_unknown_never_restarted(synthetic_signin):
    engine,grant,reader,*_=synthetic_signin
    attempt=str(uuid4())
    with engine.store.access._connection(write=True) as conn:
        conn.execute('INSERT INTO setup_provider VALUES(?,?,?,NULL)',(attempt,grant.generation,'starting'))
    assert engine.signin_begin(reader,grant.generation,attempt)['state']=='unknown'
    assert engine.signin._thread is None
    with pytest.raises(AccessDenied):engine.signin_begin(reader,grant.generation,str(uuid4()))


@pytest.mark.parametrize('content',["export CLAUDE_CODE_OAUTH_TOKEN='existing'\n",'export ROUTE=$(run-command)\n','source /other/root\n'])
def test_provider_signin_unsafe_or_existing_environment_never_changes(tmp_path,content):
    from claudlobby.plane.provider_signin import store_token,SigninRefused
    root=tmp_path/'private';root.mkdir(mode=0o700)
    env=root/'.env';env.write_text(content);env.chmod(0o600)
    with pytest.raises(SigninRefused):store_token(root,'sk-ant-oat01-'+'x'*80,lambda:None)
    assert env.read_text()==content


@pytest.mark.parametrize('source_links', [1, 2])
def test_provider_signin_unknown_or_changed_binary_refuses_before_process(synthetic_signin, source_links):
    import os
    engine,grant,reader,program,*_=synthetic_signin
    changed=program
    if source_links == 2:
        changed=program.with_name('package-manager-link');os.link(program,changed)
    changed.write_text('#!/bin/sh\nexit 0\n')
    with pytest.raises(ValueError):engine.signin_begin(reader,grant.generation,str(uuid4()))
    assert engine.signin._thread is None


@pytest.mark.parametrize('case',['ambiguous','limit','unsafe-control'])
def test_provider_signin_parser_refuses_ambiguous_or_unbounded_stream(case):
    from claudlobby.plane.provider_signin import TokenScreen,SigninRefused
    screen=TokenScreen()
    with pytest.raises(SigninRefused):
        if case=='limit':screen.feed(b'x'*131073)
        elif case=='unsafe-control':screen.feed(b'\x1b[1;2;3z')
        else:
            for token in ['sk-ant-oat01-'+'a'*80,'sk-ant-oat01-'+'b'*80]:
                screen.feed(('\r\x1b[2J\x1b[HLong-lived authentication token created successfully!\r\n'
                    'Your OAuth token (valid for 1 year):\r\n'+token+'\r\n'
                    "Store this token securely. You won't be able to see it again.\r\n").encode())


def test_provider_signin_existing_token_refuses_before_any_child(synthetic_signin):
    engine,grant,reader,_,_,env=synthetic_signin
    env.write_text("export CLAUDE_CODE_OAUTH_TOKEN='existing-private-token'\n")
    with pytest.raises(ValueError):engine.signin_begin(reader,grant.generation,str(uuid4()))
    assert engine.signin._thread is None
    assert env.read_text()=="export CLAUDE_CODE_OAUTH_TOKEN='existing-private-token'\n"


def test_provider_signin_cutover_fence_refuses_without_starting(synthetic_signin):
    engine,grant,reader,*_=synthetic_signin
    (engine.root/'state/update.json').write_text(json.dumps({'schema':1,'phase':'quiesce','maintenance':True}))
    from claudlobby.installation import InstallationError
    with pytest.raises(InstallationError):engine.signin_begin(reader,grant.generation,str(uuid4()))
    assert engine.signin._thread is None


def test_provider_signin_staged_bytes_survive_original_path_replacement(synthetic_signin):
    import hashlib,os
    from claudlobby.plane import provider_signin
    engine,grant,reader,program,*_=synthetic_signin
    fd,before=provider_signin._open_file(program,executable=True)
    try:
        copied,pinned,info=provider_signin.stage_binary(engine.store.access.path.parent,grant.provider_signin,str(uuid4()),fd,before)
        replacement=program.with_name('new-version');replacement.write_bytes(b'#!/bin/sh\nexit 99\n');replacement.chmod(0o700)
        os.replace(replacement,program)
        assert hashlib.sha256(copied.read_bytes()).hexdigest()==grant.provider_signin['sha256']
        assert provider_signin._node(os.fstat(pinned))==provider_signin._node(info)
        os.close(pinned)
    finally:os.close(fd)


def test_provider_signin_linked_source_alias_write_during_copy_refuses(synthetic_signin, monkeypatch):
    import os
    from claudlobby.plane import provider_signin
    engine,grant,_,program,*_=synthetic_signin
    alias=program.with_name('package-manager-link');os.link(program,alias)
    # Approval accepts the reviewed bytes; the alias can still change that inode
    # before the owned copy completes, so the final source binding must refuse.
    provider_signin.approved_scope(program,engine.root,grant.account_directory)
    fd=os.open(program,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    before=os.fstat(fd);original_read=os.read;changed=[]
    def read(number, count):
        data=original_read(number,count)
        if number==fd and data and not changed:
            changed.append(True);alias.write_bytes(b'#!/bin/sh\nexit 99\n')
        return data
    monkeypatch.setattr(provider_signin.os,'read',read)
    try:
        with pytest.raises(provider_signin.SigninRefused,match='unsupported_provider'):
            provider_signin.stage_binary(engine.store.access.path.parent,grant.provider_signin,str(uuid4()),fd,before)
        assert changed == [True]
    finally:os.close(fd)


def test_provider_signin_private_staged_executable_still_refuses_hardlinks(synthetic_signin):
    import os
    from claudlobby.plane import provider_signin
    engine,grant,_,program,*_=synthetic_signin
    fd,before=provider_signin._open_file(program,executable=True)
    try:
        copied,pinned,_=provider_signin.stage_binary(engine.store.access.path.parent,grant.provider_signin,str(uuid4()),fd,before)
        os.close(pinned)
        os.link(copied,copied.with_name('unexpected-link'))
        with pytest.raises(provider_signin.SigninRefused,match='file_binding_changed'):
            provider_signin._open_file(copied,executable=True)
    finally:os.close(fd)


def test_provider_signin_local_approval_is_separate_and_inert(synthetic_signin,monkeypatch):
    from claudlobby.commands import host_owner
    from claudlobby.context import resolve_paths
    from claudlobby import supervision_inventory
    from tests.test_host_owner import terminal
    engine,old,_,program,*_=synthetic_signin
    engine.store.revoke(old)
    monkeypatch.setattr(supervision_inventory,'Adapter',lambda _:SimpleNamespace(read=lambda _:f'manager\tLinux\ndirectory\t{old.install_directory}\n'))
    args=SimpleNamespace(template=old.template,target_fleet=old.fleet,repository=old.repository,
        project_directory=old.project_directory,account_directory=old.account_directory,
        install_directory=Path(old.install_directory),provider_signin=old.provider_signin)
    terminal_io=terminal(monkeypatch,iter(['NO']))
    with pytest.raises(CommandFailure):host_owner._allow_setup(args,resolve_paths(root=engine.root),engine.store.access,terminal_io)
    assert engine.store.local() is None
    terminal_io=terminal(monkeypatch,iter(['ALLOW-CLAUDE-SIGNIN','ALLOW-SETUP']))
    host_owner._allow_setup(args,resolve_paths(root=engine.root),engine.store.access,terminal_io)
    assert engine.store.local().provider_signin==old.provider_signin
    assert not list(engine.store.access.path.parent.glob('claude-signin-*'))


@pytest.mark.parametrize('race',['replacement','hardlink','admission'])
def test_provider_signin_environment_commit_race_never_appends(tmp_path,race):
    import os
    from claudlobby.plane.provider_signin import store_token,SigninRefused
    root=tmp_path/'data';root.mkdir(mode=0o700)
    env=root/'.env';env.write_text('export ROUTE=original\n');env.chmod(0o600)
    def admission():
        if race=='replacement':
            env.rename(root/'retained');env.write_text('export ROUTE=replacement\n');env.chmod(0o600)
        elif race=='hardlink':os.link(env,root/'alias')
        else:raise AccessDenied('revoked')
    with pytest.raises((SigninRefused,AccessDenied)):store_token(root,'sk-ant-oat01-'+'x'*80,admission)
    assert 'CLAUDE_CODE_OAUTH_TOKEN' not in env.read_text()
    if race=='replacement':assert (root/'retained').read_text()=='export ROUTE=original\n'


@pytest.mark.parametrize('when',['before-thread','staging','ready-token','queued-code','queued-code-revoke'])
def test_provider_signin_cancel_wins_before_effect_admission(synthetic_signin,monkeypatch,when):
    import inspect,subprocess,threading
    from claudlobby.plane import provider_signin
    engine,grant,reader,_,_,env=synthetic_signin
    attempt=str(uuid4());entered=threading.Event();release=threading.Event();calls=[]
    queue_gate=threading.Event();code_writes=[]
    original_write=provider_signin.os.write
    def observed_write(fd,data):
        if data==('accepted#'+'b'*32+'\n').encode():code_writes.append(True)
        return original_write(fd,data)
    monkeypatch.setattr(provider_signin.os,'write',observed_write)
    original_popen=subprocess.Popen
    def observed_popen(argv,*args,**kwargs):
        if argv[1:]==['setup-token']:calls.append(argv)
        return original_popen(argv,*args,**kwargs)
    monkeypatch.setattr(provider_signin.subprocess,'Popen',observed_popen)
    if when=='before-thread':
        original_start=threading.Thread.start
        def start(thread):
            if getattr(thread,'_target',None)==engine.signin._run:engine.signin.shutdown()
            return original_start(thread)
        monkeypatch.setattr(threading.Thread,'start',start)
    elif when=='staging':
        original_stage=provider_signin.stage_binary
        def stage(*args):
            result=original_stage(*args);entered.set()
            assert release.wait(8)
            return result
        monkeypatch.setattr(provider_signin,'stage_binary',stage)
    elif when.startswith('queued-code'):
        original_select=provider_signin.select.select
        def selected(*args):
            if queue_gate.is_set() and not entered.is_set():
                entered.set();assert release.wait(8)
            return original_select(*args)
        monkeypatch.setattr(provider_signin.select,'select',selected)
    else:
        original_token=provider_signin.TokenScreen.token
        def token(screen):
            result=original_token(screen)
            if result and inspect.currentframe().f_back.f_code.co_name=='_run':
                entered.set();assert release.wait(8)
            return result
        monkeypatch.setattr(provider_signin.TokenScreen,'token',token)
    cancel=None
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        if when=='ready-token' or when.startswith('queued-code'):
            _signin_until(engine,grant,reader,attempt,'waiting')
            if when.startswith('queued-code'):
                queue_gate.set();assert entered.wait(8)
            engine.signin_code(reader,grant.generation,attempt,'accepted#'+'b'*32)
        if when!='before-thread':
            assert entered.wait(8)
            if when=='queued-code-revoke':
                engine.store.revoke(grant);release.set();engine.signin._thread.join(timeout=8)
            else:
                cancel=threading.Thread(target=lambda:engine.signin_cancel(reader,grant.generation,attempt))
                cancel.start();assert engine.signin._cancel.wait(3)
                release.set();cancel.join(timeout=8);assert not cancel.is_alive()
        engine.signin.shutdown()
        assert not engine.signin._thread.is_alive()
        assert len(calls)==(1 if when=='ready-token' or when.startswith('queued-code') else 0)
        if when.startswith('queued-code'):assert not code_writes
        assert env.read_text()=='export ROUTE=private\n'
    finally:
        release.set();engine.signin.shutdown()
        if cancel is not None:cancel.join(timeout=8)


def test_provider_signin_admitted_store_serializes_cancel_and_keeps_commit_truth(synthetic_signin,monkeypatch):
    import threading
    from claudlobby.plane import provider_signin
    engine,grant,reader,_,token,env=synthetic_signin
    attempt=str(uuid4());entered=threading.Event();release=threading.Event()
    original_store=provider_signin.store_token
    def store(*args):
        entered.set();assert release.wait(8)
        return original_store(*args)
    monkeypatch.setattr(provider_signin,'store_token',store)
    cancel=None;cancel_results=[];cancel_errors=[]
    def request_cancel():
        try:
            cancel_results.append(engine.signin_cancel(reader,grant.generation,attempt))
        except BaseException as exc:
            cancel_errors.append(exc)
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        _signin_until(engine,grant,reader,attempt,'waiting')
        engine.signin_code(reader,grant.generation,attempt,'accepted#'+'b'*32)
        assert entered.wait(8)
        acquired=engine.signin._lock.acquire(blocking=False)
        if acquired:engine.signin._lock.release()
        assert not acquired,'credential commit was not admitted under the cancellation lock'
        cancel=threading.Thread(target=request_cancel)
        cancel.start();release.set();cancel.join(timeout=8);assert not cancel.is_alive()
        assert not cancel_errors,repr(cancel_errors)
        assert len(cancel_results)==1
        assert cancel_results[0]['attempt_id']==attempt
        assert cancel_results[0]['generation']==grant.generation
        assert cancel_results[0]['state']=='connected'
        assert engine.signin_status(reader,grant.generation,attempt)['state']=='connected'
        assert env.read_text().endswith(f"export CLAUDE_CODE_OAUTH_TOKEN='{token}'\n")
    finally:
        release.set();engine.signin.shutdown()
        if cancel is not None:cancel.join(timeout=8)


def completed_setup(fixture,monkeypatch):
    from claudlobby import active_config
    engine,grant,reader=allow(fixture)
    request=binding(engine.preview(reader,grant.generation,'Review requested local code'))
    engine.launch(reader,**request)
    record=fixture[-1].retained['record']
    record.update(phase='done',maintenance=False,config_committed=True,health_errors={},candidate_config={'original':'committed'})
    (engine.root/'state/config.json').write_text(json.dumps(record['candidate_config']))
    monkeypatch.setattr(active_config,'read_current_config',lambda *a,**k:dict(record['candidate_config']))
    return engine,grant,request,record


def test_completed_guide_skips_configuration_and_serving_even_after_grant_expiry(setup_authority,monkeypatch):
    from claudlobby.commands import host_owner_setup as guide,host_owner
    from claudlobby.context import resolve_paths
    from tests.test_host_owner import terminal
    engine,grant,request,record=completed_setup(setup_authority,monkeypatch)
    setup_authority[4][0]=grant.expires_at+1
    monkeypatch.setattr(guide,'_configuration',lambda *_:pytest.fail('completed guide tried to configure again'))
    monkeypatch.setattr(guide,'_serving',lambda *_:pytest.fail('completed guide competed with owner socket'))
    offered=[]
    monkeypatch.setattr(host_owner,'_setup_messages',lambda paths,store,tty,fleet:offered.append(fleet) or {'state':'skipped'},raising=False)
    result=guide.run(SimpleNamespace(finish_setup=list(request.values()),target_fleet=None),resolve_paths(root=engine.root),setup_authority[1],terminal(monkeypatch,iter(())))
    assert result.data['state']=='setup_complete' and offered==[grant.fleet]
    assert len(setup_authority[-1].dispatches)==1 and record['phase']=='done'


@pytest.mark.parametrize('change',['request','fingerprint','source','health','maintenance','committed','expected-generation','config'])
def test_completed_guide_drift_never_offers_message_consent(setup_authority,monkeypatch,change):
    from claudlobby.commands import host_owner_setup as guide,host_owner
    from claudlobby.context import resolve_paths
    from claudlobby import active_config
    from tests.test_host_owner import terminal
    engine,grant,request,record=completed_setup(setup_authority,monkeypatch)
    expected=list(request.values())
    if change=='request':record['request_id']=str(uuid4())
    elif change=='fingerprint':record['config_fingerprint']='f'*64
    elif change=='source':record['target']='f'*40
    elif change=='health':record['health_errors']={'lead':'not ready'}
    elif change=='maintenance':record['maintenance']=True
    elif change=='committed':record['config_committed']=False
    elif change=='expected-generation':expected[0]=str(uuid4())
    else:monkeypatch.setattr(active_config,'read_current_config',lambda *a,**k:{'other':'configuration'})
    monkeypatch.setattr(host_owner,'_setup_messages',lambda *_:pytest.fail('drift granted consent'),raising=False)
    monkeypatch.setattr(guide,'_configuration',lambda *_:pytest.fail('drift caused reconfiguration'))
    with pytest.raises((CommandFailure,ValueError,RuntimeError)):
        guide.run(SimpleNamespace(finish_setup=expected,target_fleet=None),resolve_paths(root=engine.root),setup_authority[1],terminal(monkeypatch,iter(())))


def test_guide_keeps_serving_for_explicit_workspace_recovery(setup_authority, monkeypatch):
    from contextlib import contextmanager
    import sqlite3
    import sys
    from claudlobby.commands import host_owner_setup as guide, host_owner
    from claudlobby.context import resolve_paths
    from tests.test_host_owner import terminal
    from tests.test_plane_owner_workspace import ISSUER, ORIGIN
    store, access, grant, _, clock, _ = setup_authority
    owner = access.current_grant()
    # Synthetic fresh-workspace fixture; the existing connected storage stays prepared.
    with sqlite3.connect(access.path) as conn:
        conn.execute("DELETE FROM workspace_bindings")
    workspace = access.workspace()
    old = proposed((access, workspace, owner, clock))
    clock[0] = old.expires_at
    monkeypatch.setattr(guide, "_native_directories", lambda _: (Path(grant.install_directory),))
    serving = []
    @contextmanager
    def serve(*args):
        serving.append(True)
        try:
            yield object()
        finally:
            serving.pop()
    monkeypatch.setattr(guide, "_serving", serve)
    codes = [old.nonce]
    def read_code(*args, **kwargs):
        assert serving == [True]
        if codes:
            return codes.pop()
        return proposed((access, workspace, owner, clock)).nonce
    monkeypatch.setattr(host_owner, "_challenge", read_code)
    args = SimpleNamespace(origin="https://host.example.test", tailscale=Path(sys.executable), tailscale_socket="",
        socket=access.root/"state/owner/owner.sock", workspace_issuer=ISSUER, workspace_origin=ORIGIN,
        target_fleet=grant.fleet, repository=grant.repository, template=grant.template,
        project_directory=grant.project_directory, install_directory=Path(grant.install_directory))
    tty = terminal(monkeypatch, iter(["RETRY\n", "BIND-WORKSPACE\n", "CANCEL\n"]))
    with pytest.raises(CommandFailure, match="setup directory is not an observed native search path"):
        guide.run(args, resolve_paths(root=access.root), access, tty)
    assert not serving
    assert workspace.status()[0] == "active"
    assert access.current_grant() == owner
    assert store.local() is None
    assert not setup_authority[-1].dispatches


@pytest.mark.parametrize('finish', [None, ['wrong']])
def test_typed_completion_requires_original_tuple_before_any_message_consent(setup_authority, monkeypatch, finish):
    from claudlobby.commands import host_owner_setup as guide, host_owner
    from claudlobby.context import resolve_paths
    from claudlobby.command_result import CommandFailure
    from claudlobby.plane.owner_access import OwnerAccess
    engine, _, _, _ = completed_setup(setup_authority, monkeypatch)
    monkeypatch.setattr(host_owner, '_setup_messages', lambda *_a, **_k: pytest.fail('unbound reopen offered messages'))
    with pytest.raises((CommandFailure, ValueError)):
        guide.run(SimpleNamespace(finish_setup=finish, target_fleet=None),
                  resolve_paths(root=engine.root), OwnerAccess(engine.root), None, consent=object())


@pytest.mark.parametrize('retained_launch', [False, True])
def test_typed_serving_interrupt_drains_without_success_replay_or_regrant(setup_authority, monkeypatch, retained_launch):
    from contextlib import contextmanager
    import sys
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby.context import resolve_paths
    store, access, grant, reader, *_ = setup_authority
    store.allow(grant)
    engine = SetupEngine(access)
    config = {'origin': 'https://host.example.test', 'socket': str(access.root / 'state/owner/owner.sock'),
        'tailscale': sys.executable, 'tailscale_socket': None,
        'workspace_issuer': 'https://accounts.example.test', 'workspace_origin': 'https://plane.example.test'}
    approval = SimpleNamespace(**{name: getattr(grant, name) for name in ('template', 'repository', 'project_directory',
        'account_directory', 'managed_owner', 'provider_signin')}, target_fleet=grant.fleet,
        install_directory=Path(grant.install_directory))
    monkeypatch.setattr(guide, '_configuration', lambda *_: (config, approval))
    original, drained = {}, []
    class Consent:
        def state(self, *_a, **_k): pass
        def check_cancel(self): pass
        def approve(self, *_a, **_k): pytest.fail('interrupt renewed consent')
    @contextmanager
    def serving(*_a, **_k):
        class Child:
            def wait(self, timeout):
                if retained_launch:
                    original.update(binding(engine.preview(reader, grant.generation, 'Review checkout')))
                    engine.launch(reader, **original)
                raise KeyboardInterrupt
        try: yield Child()
        finally: drained.append(True)
    monkeypatch.setattr(guide, '_serving', serving)
    profile = SimpleNamespace(prepare_directories=lambda *_: None, prepare_provider=lambda *_: None,
        origin=lambda: config['origin'], project=Path('/fixed/project'), account=Path('/fixed/profile'),
        units=Path(grant.install_directory), provider=Path('/fixed/provider'))
    with pytest.raises(StdioRefused, match='interrupted_unknown'):
        guide.run(SimpleNamespace(), resolve_paths(root=access.root), access, None, consent=Consent(), profile=profile)
    assert drained == [True]
    retained = store.retained_state()
    assert retained.grant == grant
    assert len(setup_authority[-1].dispatches) == int(retained_launch)
    if retained_launch:
        assert retained.launch_id == original['request_id'] and retained.launch_plan == original['config_fingerprint']
