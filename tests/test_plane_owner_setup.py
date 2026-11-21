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
              '\x1b(B\x0f\x1b[?2004h\x1b[1A\x1b[1C' + '*'*26 + '\r\r\n'
              '\x1b[2J\x1b[H\u2713 Long-lived authentication token created successfully!\r\n'
              'Your OAuth token (valid for 1 year):\r\n' + token + '\r\n'
              "Store this token securely. You won't be able to see it again.\r\n"
              '\x1b[?2031l\x1b[?1004l\x1b[?2004l').encode()
    for start in range(0, len(stream), 5): screen.feed(stream[start:start+5])
    assert screen.url == url
    assert screen.token() == token
    assert token not in repr(screen)


def test_provider_signin_parser_restores_saved_cursor():
    from claudlobby.plane.provider_signin import TokenScreen
    screen = TokenScreen()
    screen.feed(b'ab\x1b7\x1b[5Gx\x1b8c')
    assert ''.join(screen._rows[0]) == 'abc x'


@pytest.mark.parametrize('sequence', [b'\x1b[?2031;x', b'\x1b[>1q', b'\x1b[5c', b'\x1b[3;9r', b'\x1b=',
                                      b'\x1b[?123456h', b'\x1b[?3h', b'\x1b[?7l', b'\x1b[?25;6h', b'\x1b[?h',
                                      b'\x1b[>c', b'\x1b[?>1h', b'\x1b[?1u', b'\x1b[>0;1q', b'\x1b#8',
                                      b'\x1b[0r', b'\x1b[1>2A', b'\x1b[1?2A', b'\x1b(0', b'\x1b)B', b'\x0e'])
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
    screen.feed(('\x1b]8;id=a:b;' + url + '\x07open\x1b]8;;\x07').encode())
    screen.feed(b'\x1b]8;id=1;https://evil.example/cai/oauth/authorize\x07open\x1b]8;;\x07')
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
    # Each run's OAuth state is 'b'*32 unless a test writes another here (a fresh link).
    state_file=tmp_path/'synthetic-provider-state'
    # After the code each run prints the success screen, unless a test writes another reply here.
    reply_file=tmp_path/'synthetic-provider-reply'
    url=('https://claude.com/cai/oauth/authorize?code=true&client_id=test&response_type=code'
         '&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback'
         '&scope=user%3Ainference&code_challenge='+'a'*43+'&code_challenge_method=S256&state=')
    token='sk-ant-oat01-'+'s'*80
    body=f'''#!{sys.executable}
import os,sys,time
assert sys.argv[1:]==['setup-token']
assert os.environ['BROWSER']=='/usr/bin/true'
assert os.environ['DISABLE_AUTOUPDATER']=='1'
assert os.environ['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC']=='1'
assert os.environ['HTTPS_PROXY']=='http://proxy.example.test:8888'
assert not any(key in os.environ for key in ['ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN','SSH_AUTH_SOCK'])
import tty
tty.setraw(0)  # Like Claude's Ink TUI: raw before the prompt shows; only CR (Enter) submits, LF never does.
state=open({str(state_file)!r}).read() if os.path.exists({str(state_file)!r}) else 'b'*32
sys.stdout.write('\\x1b]8;;'+{url!r}+state+'\\x07Open Claude\\x1b]8;;\\x07\\r\\n');sys.stdout.flush()
typed=b''
while not typed.endswith(b'\\r'):
    ch=os.read(0,1)
    if not ch or ch==b'\\n':sys.exit(3)
    typed+=ch
code=typed[:-1].decode()
assert code=='accepted#'+state
if os.path.exists({str(reply_file)!r}):os.write(1,open({str(reply_file)!r},'rb').read());sys.exit(0)
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
        staged = engine.root/'state/provider-signin'/('claude-signin-'+attempt)/'claude'
        assert staged.stat().st_nlink == 1  # the live attempt runs its own pinned copy
        assert staged.read_bytes() == program.read_bytes()
        # The last moment a code is taken, in whole host-clock seconds, sent only beside the link.
        assert waiting['expires_at']==int(engine.store.access._now()+provider_signin.SECONDS-provider_signin.CODE_MARGIN)
        assert type(waiting['expires_at']) is int
        with pytest.raises(AccessDenied):engine.signin_begin(reader,grant.generation,str(uuid4()))
        with pytest.raises(AccessDenied):engine.preview(reader,grant.generation,'Do not start while signing in')
        with pytest.raises(AccessDenied):engine.signin_code(reader,grant.generation,attempt,'accepted#wrong-state')
        engine.signin_code(reader,grant.generation,attempt,'accepted#'+'b'*32)
        result=_signin_until(engine,grant,reader,attempt,'connected')
        assert not staged.parent.exists(), 'a finished attempt removes its copy'
        assert token not in json.dumps(result) and 'expires_at' not in result
        assert env.read_text()=='export ROUTE=private\n'+f"export CLAUDE_CODE_OAUTH_TOKEN='{token}'\n"
        with engine.store.access._connection() as conn:
            row=dict(conn.execute('SELECT * FROM setup_provider').fetchone())
            assert token not in json.dumps(row) and 'accepted#' not in json.dumps(row)
        assert engine.context(reader)['signin_attempt']['state']=='connected'
        assert SetupEngine(engine.store.access).signin_status(reader,grant.generation,attempt)['state']=='connected'
        assert engine.preview(reader,grant.generation,'Requested local work')['dispatch']=='prepared'
        with pytest.raises(AccessDenied):engine.signin_begin(reader,grant.generation,attempt)
    finally:engine.signin.shutdown()


def test_provider_signin_reported_deadline_never_outlasts_the_setup_approval(synthetic_signin,monkeypatch):
    from claudlobby.plane import provider_signin
    engine,grant,reader,_program,_token,_env=synthetic_signin
    monkeypatch.setattr(provider_signin,'SECONDS',10**6)  # longer than the approval
    attempt=str(uuid4())
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        waiting=_signin_until(engine,grant,reader,attempt,'waiting')
        assert waiting['expires_at']==int(grant.expires_at-provider_signin.CODE_MARGIN)
    finally:engine.signin.shutdown()
    assert 'expires_at' not in engine.signin_status(reader,grant.generation,attempt)


def test_provider_signin_late_code_is_refused_so_the_attempt_ends_as_a_clean_timeout(synthetic_signin):
    import time
    engine,grant,reader,_program,_token,env=synthetic_signin
    attempt=str(uuid4())
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        _signin_until(engine,grant,reader,attempt,'waiting')
        with engine.signin._lock:  # the worker cannot act meanwhile: the refusal is the cutoff's alone
            engine.signin._code_until=time.monotonic()  # the code window has just closed
            assert engine.signin_status(reader,grant.generation,attempt)['state']=='waiting'
            with pytest.raises(AccessDenied) as refused:
                engine.signin_code(reader,grant.generation,attempt,'accepted#'+'b'*32)
        assert refused.value.code=='signin_code_unavailable'
        engine.signin._thread.join(timeout=10)
        assert not engine.signin._thread.is_alive(),'ended at the cutoff, long before its deadline'
        ended=engine.signin_status(reader,grant.generation,attempt)
        assert (ended['state'],ended['reason'],ended.get('retryable'))==('needs_attention','timeout',True)
        assert _signin_row(engine,attempt)['code_delivered']==0,'nothing reached the CLI'
        assert env.read_text()=='export ROUTE=private\n'
    finally:engine.signin.shutdown()


def test_provider_signin_code_accepted_just_before_the_cutoff_is_still_delivered(synthetic_signin):
    import time
    engine,grant,reader,_program,token,env=synthetic_signin
    attempt=str(uuid4())
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        _signin_until(engine,grant,reader,attempt,'waiting')
        with engine.signin._lock:  # the worker cannot act meanwhile
            engine.signin._code_until=time.monotonic()+5
            engine.signin_code(reader,grant.generation,attempt,'accepted#'+'b'*32)  # taken inside the window
            engine.signin._code_until=time.monotonic()-1  # the window closes before the worker next looks
        assert _signin_until(engine,grant,reader,attempt,'connected')['state']=='connected','a taken code is never dropped'
        assert _signin_row(engine,attempt)['code_delivered']==1
        assert env.read_text()=='export ROUTE=private\n'+f"export CLAUDE_CODE_OAUTH_TOKEN='{token}'\n"
    finally:engine.signin.shutdown()


def test_provider_signin_ends_at_its_code_cutoff_so_try_again_is_offered_at_once(synthetic_signin,monkeypatch):
    import time
    from claudlobby.plane import provider_signin
    engine,grant,reader,*_=synthetic_signin
    monkeypatch.setattr(provider_signin,'CODE_MARGIN',provider_signin.SECONDS-1)  # a one-second code window
    attempt=str(uuid4())
    try:
        began=time.monotonic()
        engine.signin_begin(reader,grant.generation,attempt)
        engine.signin._thread.join(timeout=15)
        assert not engine.signin._thread.is_alive() and time.monotonic()-began<15
        assert engine.signin_status(reader,grant.generation,attempt)=={'attempt_id':attempt,'generation':grant.generation,
            'state':'needs_attention','reason':'timeout','retryable':True}
        assert _signin_row(engine,attempt)['code_delivered']==0
    finally:engine.signin.shutdown()


def test_provider_signin_begin_is_refused_when_the_approval_leaves_no_useful_code_window(synthetic_signin,monkeypatch):
    from claudlobby.plane import provider_signin
    engine,grant,reader,*_=synthetic_signin
    access=engine.store.access
    left=grant.expires_at-access._now()  # the approval's remaining time (the session stays valid)
    attempt=str(uuid4())
    try:
        monkeypatch.setattr(provider_signin,'MIN_CODE_WINDOW',left-provider_signin.CODE_MARGIN+1)  # one second short
        with pytest.raises(AccessDenied) as refused:engine.signin_begin(reader,grant.generation,attempt)
        assert refused.value.code=='signin_approval_ending'
        assert engine.signin._thread is None and not (engine.root/'state/provider-signin'/('claude-signin-'+attempt)).exists()
        with access._connection() as conn:assert conn.execute('SELECT COUNT(*) FROM setup_provider').fetchone()[0]==0
        monkeypatch.setattr(provider_signin,'MIN_CODE_WINDOW',left-provider_signin.CODE_MARGIN)  # exactly enough
        assert engine.signin_begin(reader,grant.generation,attempt)['attempt_id']==attempt
        assert engine.signin._thread is not None
    finally:engine.signin.shutdown()


def test_provider_signin_retryable_is_reported_exactly_when_the_approval_lets_begin_admit_one(synthetic_signin,monkeypatch):
    from claudlobby.plane import provider_signin
    engine,grant,reader,*_=synthetic_signin
    access=engine.store.access
    ended,successor=str(uuid4()),str(uuid4())
    with access._connection(write=True) as conn:
        conn.execute('INSERT INTO setup_provider VALUES(?,?,?,?,?,?,NULL)',(ended,grant.generation,'needs_attention','timeout',0,0))
    left=grant.expires_at-access._now()  # the host clock is frozen here
    try:
        monkeypatch.setattr(provider_signin,'MIN_CODE_WINDOW',left-provider_signin.CODE_MARGIN+1)  # one second short
        assert 'retryable' not in engine.signin_status(reader,grant.generation,ended),'no Try again the host would refuse'
        assert 'retryable' not in engine.context(reader)['signin_attempt']
        with pytest.raises(AccessDenied) as refused:engine.signin_begin(reader,grant.generation,successor)
        assert refused.value.code=='signin_approval_ending'
        assert _signin_row(engine,ended)['retired']==0 and engine.signin._thread is None
        monkeypatch.setattr(provider_signin,'MIN_CODE_WINDOW',left-provider_signin.CODE_MARGIN)  # exactly enough
        assert engine.signin_status(reader,grant.generation,ended)['retryable'] is True
        assert engine.context(reader)['signin_attempt']['retryable'] is True
        assert engine.signin_begin(reader,grant.generation,successor)['attempt_id']==successor
        assert _signin_row(engine,ended)['retired']==1
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
            later=SetupEngine(engine.store.access)
            result=later.signin_status(reader,grant.generation,attempt)
            # Stopped before any code reached the CLI: no token can exist and the child was reaped,
            # so the record admits a new attempt ID (the same proof as a timeout before its code).
            assert result=={'attempt_id':attempt,'generation':grant.generation,'state':'cancelled',
                            'reason':'effect_uncertain','retryable':True}
            if stop=='shutdown':
                with pytest.raises(AccessUnavailable):engine.signin_begin(reader,grant.generation,str(uuid4()))
            fresh=str(uuid4())
            try:
                owner=later if stop=='shutdown' else engine  # a closing process starts nothing new
                owner.signin_begin(reader,grant.generation,fresh)
                assert _signin_until(owner,grant,reader,fresh,'waiting')['url'].startswith('https://claude.com/')
                assert _signin_row(engine,attempt)['retired']==1
            finally:later.signin.shutdown()
    finally:engine.signin.shutdown()


def test_provider_signin_interrupted_reservation_is_unknown_never_restarted(synthetic_signin):
    engine,grant,reader,*_=synthetic_signin
    attempt=str(uuid4())
    with engine.store.access._connection(write=True) as conn:
        conn.execute('INSERT INTO setup_provider(attempt_id,generation,state) VALUES(?,?,?)',(attempt,grant.generation,'starting'))
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
        assert _staged_copies(engine.store.access.path.parent)==[], 'a refused copy is removed'
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
        if data in (('accepted#'+'b'*32).encode(),b'\r'):code_writes.append(data)
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
        if when=='ready-token':assert code_writes==[('accepted#'+'b'*32).encode(),b'\r']  # Enter is its own keypress
        assert env.read_text()=='export ROUTE=private\n'
        assert _staged_copies(provider_signin.staging(engine.root))==[], 'a cancelled or revoked attempt removes its copy'
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


def _signin_ended(engine,grant,reader,attempt):
    """Join the owned worker, then read the retained ending."""
    engine.signin._thread.join(timeout=10)
    assert not engine.signin._thread.is_alive()
    return engine.signin_status(reader,grant.generation,attempt)


def _signin_row(engine,attempt):
    with engine.store.access._connection() as conn:
        return dict(conn.execute('SELECT * FROM setup_provider WHERE attempt_id=?',(attempt,)).fetchone())


def _staged_copies(parent):
    """Attempt directories holding a staged copy under parent, by attempt ID."""
    from claudlobby.plane.provider_signin import ATTEMPT_DIRECTORY
    return sorted(p.name[len(ATTEMPT_DIRECTORY):] for p in parent.glob(ATTEMPT_DIRECTORY+'*'))


def _leave_staged_copy(parent,attempt):
    """A copy as if its worker's own removal had failed."""
    from claudlobby.plane.provider_signin import ATTEMPT_DIRECTORY
    directory=parent/(ATTEMPT_DIRECTORY+attempt); directory.mkdir(mode=0o700)
    (directory/'claude').write_bytes(b'staged'); (directory/'claude').chmod(0o500)
    return directory/'claude'


def test_provider_signin_timeout_before_code_admits_one_fresh_attempt(synthetic_signin,monkeypatch):
    import subprocess
    from claudlobby.plane import provider_signin
    engine,grant,reader,program,token,env=synthetic_signin
    seconds,margin=provider_signin.SECONDS,provider_signin.CODE_MARGIN
    monkeypatch.setattr(provider_signin,'SECONDS',2);monkeypatch.setattr(provider_signin,'CODE_MARGIN',0)  # a 2 s code window
    children=[];original_popen=subprocess.Popen
    def observed_popen(argv,*args,**kwargs):
        if argv[1:]==['setup-token']:children.append(argv[0])
        return original_popen(argv,*args,**kwargs)
    monkeypatch.setattr(provider_signin.subprocess,'Popen',observed_popen)
    first,second=str(uuid4()),str(uuid4())
    try:
        engine.signin_begin(reader,grant.generation,first)
        old_link=_signin_until(engine,grant,reader,first,'waiting')['url']
        assert _signin_ended(engine,grant,reader,first)=={'attempt_id':first,'generation':grant.generation,
            'state':'needs_attention','reason':'timeout','retryable':True}
        assert _signin_row(engine,first)['code_delivered']==0
        program.with_name('synthetic-provider-state').write_text('c'*32)  # the provider's next run: another OAuth state
        monkeypatch.setattr(provider_signin,'SECONDS',seconds);monkeypatch.setattr(provider_signin,'CODE_MARGIN',margin)
        started=engine.signin_begin(reader,grant.generation,second)
        assert started['attempt_id']==second and started['state'] in {'starting','waiting'} and 'retryable' not in started
        new_link=_signin_until(engine,grant,reader,second,'waiting')['url']
        assert new_link!=old_link and new_link.endswith('&state='+'c'*32)
        for retired in (lambda:engine.signin_status(reader,grant.generation,first),
                        lambda:engine.signin_code(reader,grant.generation,first,'accepted#'+'b'*32),
                        lambda:engine.signin_begin(reader,grant.generation,first)):
            with pytest.raises(AccessDenied):retired()  # the replaced attempt is retired, its ID never reused
        with pytest.raises(AccessDenied):engine.signin_code(reader,grant.generation,second,'accepted#'+'b'*32)  # the old link's code
        engine.signin_code(reader,grant.generation,second,'accepted#'+'c'*32)
        assert _signin_until(engine,grant,reader,second,'connected')['state']=='connected'
        assert len(children)==2 and children[0]!=children[1], 'two owned runs, each from its own staged copy'
        assert env.read_text()=='export ROUTE=private\n'+f"export CLAUDE_CODE_OAUTH_TOKEN='{token}'\n"
        assert engine.context(reader)['signin_attempt']['attempt_id']==second
        assert [(row['retired'],row['code_delivered']) for row in map(lambda a:_signin_row(engine,a),(first,second))]==[(1,0),(0,1)]
        with pytest.raises(AccessDenied):engine.signin_begin(reader,grant.generation,str(uuid4()))
        assert engine.preview(reader,grant.generation,'Requested local work')['dispatch']=='prepared'
    finally:engine.signin.shutdown()


def test_provider_signin_timeout_after_code_is_never_retried_even_after_restart(synthetic_signin,monkeypatch):
    from claudlobby.plane import provider_signin
    engine,grant,reader,program,token,env=synthetic_signin
    # The code reaches the CLI but Enter is not due before the deadline: a timeout after the code.
    # (CODE_MARGIN 0 models a code that slipped in at the last moment: the margin is what prevents it.)
    monkeypatch.setattr(provider_signin,'SECONDS',2)
    monkeypatch.setattr(provider_signin,'CODE_MARGIN',0)
    monkeypatch.setattr(provider_signin,'ENTER_DELAY',60)
    attempt=str(uuid4())
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        _signin_until(engine,grant,reader,attempt,'waiting')
        engine.signin_code(reader,grant.generation,attempt,'accepted#'+'b'*32)
        ended=_signin_ended(engine,grant,reader,attempt)
        assert (ended['state'],ended['reason'])==('needs_attention','timeout') and 'retryable' not in ended
        assert _signin_row(engine,attempt)['code_delivered']==1
        with pytest.raises(AccessDenied):engine.signin_begin(reader,grant.generation,str(uuid4()))
        restarted=SetupEngine(engine.store.access)  # a later process has only the record
        assert 'retryable' not in restarted.signin_status(reader,grant.generation,attempt)
        with pytest.raises(AccessDenied):restarted.signin_begin(reader,grant.generation,str(uuid4()))
        assert restarted.signin._thread is None
        assert env.read_text()=='export ROUTE=private\n'
    finally:engine.signin.shutdown()


def test_provider_signin_live_attempt_refuses_a_successor_from_any_process(synthetic_signin):
    engine,grant,reader,*_=synthetic_signin
    attempt=str(uuid4())
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        _signin_until(engine,grant,reader,attempt,'waiting')
        other=SetupEngine(engine.store.access)
        observed=other.signin_status(reader,grant.generation,attempt)
        assert (observed['state'],observed['reason'])==('unknown','interrupted') and 'retryable' not in observed
        for owner in (engine,other):
            with pytest.raises(AccessDenied):owner.signin_begin(reader,grant.generation,str(uuid4()))
        assert other.signin._thread is None and engine.signin._thread.is_alive()
        assert engine.signin_status(reader,grant.generation,attempt)['state']=='waiting'
    finally:engine.signin.shutdown()


@pytest.mark.parametrize('rows,retryable',[
    ([('needs_attention','timeout',0)],True),
    ([('needs_attention','timeout',0)]*2,True),
    ([('needs_attention','timeout',0)]*3,True),  # a slow sign-in keeps its Try again
    ([('needs_attention','timeout',0)]*9,True),
    ([('needs_attention','timeout',0)]*10,False),  # MAX_ATTEMPTS used
    ([('needs_attention','timeout',1)],False),  # the code reached the CLI
    ([('needs_attention','timeout',None)],False),  # a single-attempt-table row: delivery was never recorded
    ([('cancelled','effect_uncertain',0)],True),  # Cancel or a drained conversation before its code
    ([('cancelled','effect_uncertain',1)],False),  # cancelled after the code reached the CLI
    ([('cancelled','effect_uncertain',None)],False),
    ([('cancelled','effect_uncertain',0)]*10,False),
    ([('cancelled','provider_failed',0)],False),  # only the exact pairs
    ([('needs_attention','provider_failed',0)],False),
    ([('needs_attention','unsupported_output',0)],False),
    ([('needs_attention','storage_uncertain',1)],False),
    ([('needs_attention','environment_changed',0)],False),
    ([('needs_attention','scope_changed',0)],False),
    ([('needs_attention','credential_already_present',0)],False),
    ([('needs_attention','boundary_or_process_failure',0)],False),
    ([('unknown','child_quiet_unproven',0)],False),
    ([('starting',None,0)],False),  # read as unknown/interrupted: its child may still run
    ([('waiting',None,0)],False),
])
def test_provider_signin_only_a_recorded_timeout_before_code_admits_a_successor(synthetic_signin,rows,retryable):
    engine,grant,reader,*_=synthetic_signin
    ids=[str(uuid4()) for _ in rows]
    with engine.store.access._connection(write=True) as conn:
        for number,(attempt,(state,reason,delivered)) in enumerate(zip(ids,rows),1):
            conn.execute('INSERT INTO setup_provider VALUES(?,?,?,?,?,?,NULL)',
                         (attempt,grant.generation,state,reason,delivered,int(number<len(rows))))
    restarted=SetupEngine(engine.store.access)  # every check is read from the record, as after a restart
    status=restarted.signin_status(reader,grant.generation,ids[-1])
    assert ('retryable' in status)==retryable and status.get('retryable',True) is True
    successor=str(uuid4())
    try:
        if retryable:
            assert restarted.signin_begin(reader,grant.generation,successor)['attempt_id']==successor
            assert restarted.signin._thread is not None
            assert _signin_row(restarted,ids[-1])['retired']==1 and _signin_row(restarted,successor)['retired']==0
        else:
            with pytest.raises(AccessDenied):restarted.signin_begin(reader,grant.generation,successor)
            assert restarted.signin._thread is None
            assert restarted.signin_status(reader,grant.generation,ids[-1])['attempt_id']==ids[-1]
    finally:restarted.signin.shutdown()


def test_provider_signin_retry_still_refuses_a_saved_credential_and_a_closing_process(synthetic_signin,monkeypatch):
    from claudlobby.plane import provider_signin
    engine,grant,reader,program,token,env=synthetic_signin
    monkeypatch.setattr(provider_signin,'SECONDS',0.5)
    first,second=str(uuid4()),str(uuid4())
    try:
        engine.signin_begin(reader,grant.generation,first)
        assert _signin_ended(engine,grant,reader,first).get('retryable') is True
        staging=provider_signin.staging(engine.root)
        assert _staged_copies(staging)==[], 'the ended attempt removed its own copy'
        kept=_leave_staged_copy(staging,first)
        saved="export ROUTE=private\nexport CLAUDE_CODE_OAUTH_TOKEN='saved-elsewhere'\n"
        env.write_text(saved)
        with pytest.raises(provider_signin.SigninRefused,match='credential_already_present'):
            engine.signin_begin(reader,grant.generation,second)
        current=engine.signin_status(reader,grant.generation,first)
        assert (current['state'],current.get('retryable'))==('needs_attention',True), 'still the current attempt, still retryable'
        assert _signin_row(engine,first)['retired']==0
        with pytest.raises(AccessDenied):engine.signin_status(reader,grant.generation,second)
        assert not (engine.root/'state/provider-signin'/('claude-signin-'+second)).exists()
        assert kept.is_file(), 'a refused Try again leaves the current attempt alone'
        assert env.read_text()==saved
        env.write_text('export ROUTE=private\n')
        engine.signin.shutdown()
        with pytest.raises(AccessUnavailable):engine.signin_begin(reader,grant.generation,second)  # a closing process starts nothing
        with pytest.raises(AccessDenied):engine.signin_status(reader,grant.generation,second)
        assert _signin_row(engine,first)['retired']==0
        assert engine.signin_status(reader,grant.generation,first).get('retryable') is True
    finally:engine.signin.shutdown()


def test_provider_signin_an_ended_attempt_removes_its_copy_and_try_again_removes_one_left_behind(synthetic_signin,monkeypatch):
    from claudlobby.plane import provider_signin
    engine,grant,reader,*_=synthetic_signin
    monkeypatch.setattr(provider_signin,'SECONDS',0.5)
    staging=provider_signin.staging(engine.root)
    first,second,third,fourth,other=(str(uuid4()) for _ in range(5))
    def attempt(owner,new):
        assert owner.signin_begin(reader,grant.generation,new)['attempt_id']==new
        assert _signin_ended(owner,grant,reader,new).get('retryable') is True
    seen=[];original_discard=provider_signin.discard_binary
    def discard(parent,owner):
        seen.append((owner,_signin_row(engine,owner)['state']));original_discard(parent,owner)
    monkeypatch.setattr(provider_signin,'discard_binary',discard)
    try:
        attempt(engine,first)
        assert _staged_copies(staging)==[], 'a timed-out attempt removed its own copy'
        assert [owner for owner,_ in seen]==[first] and seen[0][1] in ('starting','waiting'), 'before the terminal row a Try again reads'
        _leave_staged_copy(staging,first); _leave_staged_copy(staging,other)
        attempt(engine,second)
        assert _staged_copies(staging)==[other], 'Try again removed only the replaced copy; the new attempt removed its own'
        attempt(engine,third)  # a copy already gone still lets Try again through
        assert _signin_row(engine,second)['retired']==1
        _leave_staged_copy(staging,third)
        engine.signin.shutdown()
        restarted=SetupEngine(engine.store.access)  # the next engine removes a copy the first one left
        try:
            attempt(restarted,fourth)
            assert _staged_copies(staging)==[other]
        finally:restarted.signin.shutdown()
    finally:engine.signin.shutdown()


def test_provider_signin_an_unproven_reap_keeps_its_copy(synthetic_signin,monkeypatch):
    import os,signal,subprocess
    from claudlobby.plane import provider_signin
    engine,grant,reader,*_=synthetic_signin
    monkeypatch.setattr(provider_signin,'SECONDS',0.5)
    children=[];original_popen=subprocess.Popen;original_getpgid=os.getpgid
    def observed_popen(argv,*args,**kwargs):
        child=original_popen(argv,*args,**kwargs)
        if argv[1:]==['setup-token']:children.append(child)
        return child
    def getpgid(pid):  # only the sign-in child's reap goes unproven
        if any(child.pid==pid for child in children): raise OSError('reap not proven')
        return original_getpgid(pid)
    monkeypatch.setattr(provider_signin.subprocess,'Popen',observed_popen)
    monkeypatch.setattr(provider_signin.os,'getpgid',getpgid)
    attempt=str(uuid4())
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        ended=_signin_ended(engine,grant,reader,attempt)
        assert (ended['state'],ended.get('reason'))==('unknown','child_quiet_unproven')
        copy=provider_signin.staging(engine.root)/(provider_signin.ATTEMPT_DIRECTORY+attempt)/'claude'
        assert copy.is_file(), 'a copy whose child may still run is left alone'
    finally:
        engine.signin.shutdown()
        for child in children:
            try: os.killpg(child.pid,signal.SIGKILL)
            except (ProcessLookupError,PermissionError): pass  # ESRCH; EPERM on macOS for an unreaped group
            child.wait(timeout=5)


def test_provider_signin_discard_binary_removes_only_a_plain_copy_and_follows_nothing(tmp_path):
    import os
    from claudlobby.plane.provider_signin import ATTEMPT_DIRECTORY,discard_binary
    staging=tmp_path/'provider-signin'; staging.mkdir(mode=0o700)
    outside=tmp_path/'outside'; outside.mkdir(); (outside/'claude').write_text('kept')
    def staged(make,attempt=None):
        attempt=attempt or str(uuid4())
        directory=staging/(ATTEMPT_DIRECTORY+attempt); directory.mkdir(mode=0o700); make(directory/'claude')
        return attempt,directory
    attempt,directory=staged(lambda p:p.write_text('copy'))
    discard_binary(staging,attempt)
    assert not directory.exists()
    linked=str(uuid4()); (staging/(ATTEMPT_DIRECTORY+linked)).symlink_to(outside)
    discard_binary(staging,linked)  # a directory link is not opened
    assert (staging/(ATTEMPT_DIRECTORY+linked)).is_symlink()
    for make in (lambda p:p.symlink_to(outside/'claude'),lambda p:os.link(outside/'claude',p)):
        attempt,directory=staged(make)
        discard_binary(staging,attempt)
        assert os.path.lexists(directory/'claude'), 'a link or second name is left alone'
    assert (outside/'claude').read_text()=='kept'
    attempt,directory=staged(lambda p:p.write_text('copy'),'not-an-attempt')
    discard_binary(staging,attempt)  # only an attempt ID's shape is ever removed
    assert (directory/'claude').exists()
    discard_binary(tmp_path/'missing',str(uuid4()))
    discard_binary(staging,None)  # never raises


def test_provider_signin_two_racing_successors_admit_exactly_one(synthetic_signin):
    import threading
    engine,grant,reader,*_=synthetic_signin
    ended=str(uuid4())
    with engine.store.access._connection(write=True) as conn:
        conn.execute('INSERT INTO setup_provider VALUES(?,?,?,?,?,?,NULL)',(ended,grant.generation,'needs_attention','timeout',0,0))
    owners=[SetupEngine(engine.store.access) for _ in range(2)]  # two processes, one record
    ids=[str(uuid4()) for _ in owners]
    gate,outcomes=threading.Barrier(2),[None,None]
    def race(number):
        gate.wait()
        try:outcomes[number]=owners[number].signin_begin(reader,grant.generation,ids[number])['attempt_id']
        except AccessDenied as exc:outcomes[number]=exc.code
    threads=[threading.Thread(target=race,args=(number,)) for number in range(2)]
    try:
        for thread in threads:thread.start()
        for thread in threads:thread.join(20)
        admitted=[number for number in range(2) if outcomes[number]==ids[number]]
        assert len(admitted)==1 and outcomes[1-admitted[0]]=='signin_original_attempt_required',outcomes
        assert [owner.signin._thread is not None for owner in owners].count(True)==1
        with engine.store.access._connection() as conn:
            assert conn.execute('SELECT attempt_id FROM setup_provider WHERE retired=0').fetchall()[0][0]==ids[admitted[0]]
            assert conn.execute('SELECT COUNT(*) FROM setup_provider').fetchone()[0]==2
    finally:
        for owner in owners:owner.signin.shutdown()


def test_provider_signin_single_attempt_table_is_read_then_upgraded_without_a_retry(synthetic_signin):
    from claudlobby.plane.provider_signin import ProviderSignin
    engine,grant,reader,*_=synthetic_signin
    attempt=str(uuid4())
    with engine.store.access._connection(write=True) as conn:
        conn.execute('DROP TABLE setup_provider')
        conn.execute('CREATE TABLE setup_provider(attempt_id TEXT PRIMARY KEY,generation TEXT NOT NULL,state TEXT NOT NULL,reason TEXT)')
        conn.execute('INSERT INTO setup_provider VALUES(?,?,?,?)',(attempt,grant.generation,'needs_attention','timeout'))
    restarted=SetupEngine(engine.store.access)
    ended={'attempt_id':attempt,'generation':grant.generation,'state':'needs_attention','reason':'timeout'}
    assert restarted.signin_status(reader,grant.generation,attempt)==ended
    with pytest.raises(AccessDenied):restarted.signin_begin(reader,grant.generation,str(uuid4()))
    with engine.store.access._connection() as conn:  # the refused begin's upgrade rolled back with it
        assert [row[1] for row in conn.execute('PRAGMA table_info(setup_provider)')]==['attempt_id','generation','state','reason']
    assert restarted.signin_begin(reader,grant.generation,attempt)==ended  # re-reading the original upgrades the table
    with engine.store.access._connection() as conn:
        assert [row[1] for row in conn.execute('PRAGMA table_info(setup_provider)')]==['attempt_id','generation','state','reason','code_delivered','retired','detail']
        assert tuple(conn.execute('SELECT code_delivered,retired FROM setup_provider').fetchone())==(None,0)
    with pytest.raises(AccessDenied):restarted.signin_begin(reader,grant.generation,str(uuid4()))
    assert restarted.signin._thread is None
    with engine.store.access._connection(write=True) as conn:
        conn.execute('DROP TABLE setup_provider')  # neither shape: refused, never repaired
        conn.execute('CREATE TABLE setup_provider(attempt_id TEXT PRIMARY KEY,generation TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,code_delivered INTEGER)')
        conn.execute('INSERT INTO setup_provider VALUES(?,?,?,?,0)',(attempt,grant.generation,'needs_attention','timeout'))
    with pytest.raises(AccessUnavailable):restarted.signin_status(reader,grant.generation,attempt)
    with pytest.raises(AccessUnavailable):
        with engine.store.access._connection(write=True) as conn:ProviderSignin.initialize(conn)


# --- Which rule refused Claude's reply: named from a closed vocabulary, stored, never sent ---

# Every detail the product makes must fit this. Written here on its own, not imported.
_DETAIL=(r'(?:utf8|control 0x[01][0-9a-f]|escape (?:[!-~]|other)|csi malformed(?: [A-Za-z~](?: [!-?_]{1,16})?)?'
         r'|csi private [?>][0-9;]{0,15} [A-Za-z]|csi range|csi [KJ] [0-9]{1,4}'
         r'|csi final [A-Za-z](?: [0-9;]{1,16})?|token shape|token changed|url changed'
         r'|bytes|escape length|exit 0 without token)')
_SIGNIN_URL=('https://claude.com/cai/oauth/authorize?code=true&client_id=test&response_type=code'
             '&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback'
             '&scope=user%3Ainference&code_challenge='+'a'*43+'&code_challenge_method=S256&state=')
_SIGNIN_TOKENS=['sk-ant-oat01-'+letter*80 for letter in 'ab']


def _success_screen(value,clear=True):
    return (('\r\x1b[2J\x1b[H' if clear else '')+'Long-lived authentication token created successfully!\r\n'
            'Your OAuth token (valid for 1 year):\r\n'+value+'\r\n'
            "Store this token securely. You won't be able to see it again.\r\n").encode()


def _refusal(*chunks):
    """What a fresh private screen refuses these chunks with: (reason, detail)."""
    import re
    from claudlobby.plane.provider_signin import DETAIL,TokenScreen,SigninRefused
    screen=TokenScreen()
    with pytest.raises(SigninRefused) as refused:
        for chunk in chunks:screen.feed(chunk)
    reason,detail=refused.value.code,refused.value.detail
    assert str(refused.value)==reason and refused.value.args==(reason,), 'the message stays the category alone'
    assert isinstance(detail,str) and re.fullmatch(_DETAIL,detail) and DETAIL.fullmatch(detail), detail
    assert len(detail)<=48 and detail.isascii()
    return reason,detail


@pytest.mark.parametrize('chunks,reason,detail',[
    pytest.param([b'ok\xff'],'unsupported_output','utf8',id='utf8'),
    pytest.param([b'\x0e'],'unsupported_output','control 0x0e',id='control'),
    pytest.param([b'\x00'],'unsupported_output','control 0x00',id='control-lowest'),
    pytest.param([b'\x1f'],'unsupported_output','control 0x1f',id='control-highest'),
    pytest.param([b'\x1b='],'unsupported_output','escape =',id='escape'),
    pytest.param([b'\x1bM'],'unsupported_output','escape M',id='escape-letter'),
    pytest.param([b'\x1b#8'],'unsupported_output','escape #',id='escape-names-one-character'),
    pytest.param([b'\x1b(0'],'unsupported_output','escape (',id='escape-unhandled-character-set'),
    pytest.param([b'\x1b\x01'],'unsupported_output','escape other',id='escape-other-control'),
    pytest.param([b'\x1b '],'unsupported_output','escape other',id='escape-other-space'),
    pytest.param(['\x1bé'.encode()],'unsupported_output','escape other',id='escape-other-not-ascii'),
    pytest.param(['\x1b[5é'.encode()],'unsupported_output','csi malformed',id='csi-malformed'),
    pytest.param([b'\x1b[1>2A'],'unsupported_output','csi malformed A 1>2',id='csi-malformed-letter'),
    pytest.param([b'\x1b[<u'],'unsupported_output','csi malformed u <',id='csi-malformed-other-prefix'),
    pytest.param([b'\x1b[0 q'],'unsupported_output','csi malformed q 0_',id='csi-malformed-intermediate'),
    pytest.param([b'\x1b['+b'9'*17+b'!p'],'unsupported_output','csi malformed p',id='csi-malformed-long-body'),
    pytest.param([b'\x1b[200~'],'unsupported_output','csi malformed ~ 200',id='csi-malformed-tilde'),
    pytest.param([b'\x1b[?2031;x'],'unsupported_output','csi private ?2031; x',id='csi-private'),
    pytest.param([b'\x1b[>1q'],'unsupported_output','csi private >1 q',id='csi-private-greater-than'),
    pytest.param([b'\x1b[?3h'],'unsupported_output','csi private ?3 h',id='csi-private-mode-not-allowed'),
    pytest.param([b'\x1b[?h'],'unsupported_output','csi private ? h',id='csi-private-no-number'),
    pytest.param([b'\x1b[999999999A'],'unsupported_output','csi range',id='csi-range'),
    pytest.param([b'\x1b[3K'],'unsupported_output','csi K 3',id='csi-K'),
    pytest.param([b'\x1b[1J'],'unsupported_output','csi J 1',id='csi-J'),
    pytest.param([b'\x1b[z'],'unsupported_output','csi final z',id='csi-final'),
    pytest.param([b'\x1b[1;2;3z'],'unsupported_output','csi final z 1;2;3',id='csi-final-parameters'),
    pytest.param([b'\x1b[5c'],'unsupported_output','csi final c 5',id='csi-final-query-not-ignored'),
    pytest.param([_success_screen('not-a-token')],'unsupported_output','token shape',id='token-shape'),
    pytest.param([_success_screen(value) for value in _SIGNIN_TOKENS],'ambiguous_output','token changed',id='token-changed'),
    pytest.param([b''.join(_success_screen(value,clear=False) for value in _SIGNIN_TOKENS)],
                 'ambiguous_output','token changed',id='token-changed-two-on-screen'),
    pytest.param([''.join('\x1b]8;;'+_SIGNIN_URL+state*32+'\x07open\x1b]8;;\x07' for state in 'bc').encode()],
                 'ambiguous_output','url changed',id='url-changed'),
    pytest.param([b'x'*131073],'output_limit','bytes',id='bytes'),
    pytest.param([b'\x1b]'+b'a'*8200],'output_limit','escape length',id='escape-length'),
])
def test_provider_signin_parser_names_the_rule_that_refused(chunks,reason,detail):
    assert _refusal(*chunks)==(reason,detail)


def test_provider_signin_refusal_detail_never_carries_reply_text_a_code_or_a_token():
    from claudlobby.plane.provider_signin import SigninRefused,refusal_detail
    # Upper case, so two neighbouring characters of either would show in a detail: its words are lower case.
    secret,code='SK-ANT-OAT01-'+'QZ'*40,'PASTED-CODE-XY#'+'B'*32
    def clean(detail):
        return not any(text[at:at+2] in detail for text in (secret,code) for at in range(len(text)-1))
    hostile=[  # the reply, and the whole of the detail it may leave
        ([b'\x1b'+secret.encode()],'escape S'),  # the one character after ESC, never the rest
        ([b'\x1b[ !"$%&'+secret.encode()],'csi malformed S _!"$%&'),  # a sequence stuffed with letters ends at its first
        ([b'\x1b['+secret.encode()],'csi final S'),
        ([b'\x1b[12;'+secret.encode()],'csi final S 12;'),
        ([b'\x1b[?'+code.encode()+b'h'],'csi private ? P'),
        ([b'\x1b]'+(secret+code).encode()*60],'escape length'),  # an unending sequence full of letters
        ([b'\x1b[?'+b'9'*300+b'h'],'csi private ?'+'9'*15+' h'),  # a long parameter string is cut to 16 characters
        ([b'\x1b[>'+b';'*5000+b'q'],'csi private >'+';'*15+' q'),
        ([b'\x1b['+b'7;'*200+b'z'],'csi final z'),  # too long to give beside the final letter: left out
        ([_success_screen(secret[:39])],'token shape'),  # nothing about the value, not even its length
        ([_success_screen(secret),_success_screen(secret[::-1])],'token changed'),
        ([code.encode()+b'\x0e'],'control 0x0e'),  # a code shown as text, then an unhandled control
        ([secret.encode()+b'\xff'],'utf8'),
    ]
    for chunks,expected in hostile:
        detail=_refusal(*chunks)[1]  # also held to the strict expression and the 48 characters
        assert detail==expected and clean(detail),chunks[0][:16]
    # The one builder leaves out whatever is outside a value's safe set, and a refusal carries only a built detail.
    assert refusal_detail(secret) is None and refusal_detail('escape',secret) is None
    assert refusal_detail('csi final',secret) is None and refusal_detail('csi final','z','1;'+secret)=='csi final z'
    assert refusal_detail('csi private','?1','h',code)=='csi private ?1 h'
    assert refusal_detail('token shape',secret)=='token shape' and refusal_detail('control','0x20') is None
    assert SigninRefused('unsupported_output',secret).detail is None
    assert SigninRefused('unsupported_output','csi range').detail=='csi range'
    assert SigninRefused('timeout').detail is None and str(SigninRefused('timeout','bytes'))=='timeout'


@pytest.mark.parametrize('reply,detail',[
    pytest.param(b'\x1b[5z','csi final z 5',id='unknown-sequence'),
    pytest.param(b'Signed in.\r\n','exit 0 without token',id='exit-0-without-token'),
])
def test_provider_signin_refused_reply_stores_its_rule_and_reports_only_the_reason(synthetic_signin,reply,detail):
    import re
    engine,grant,reader,program,_token,env=synthetic_signin
    program.with_name('synthetic-provider-reply').write_bytes(reply)  # after the code, in place of the success screen
    attempt=str(uuid4())
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        _signin_until(engine,grant,reader,attempt,'waiting')
        engine.signin_code(reader,grant.generation,attempt,'accepted#'+'b'*32)
        # Exactly the keys a refused attempt had before the detail: the browser's carrier checks them as a set.
        refused={'attempt_id':attempt,'generation':grant.generation,'state':'needs_attention','reason':'unsupported_output'}
        assert _signin_ended(engine,grant,reader,attempt)==refused
        row=_signin_row(engine,attempt)
        assert row=={**refused,'code_delivered':1,'retired':0,'detail':detail} and re.fullmatch(_DETAIL,row['detail'])
        restarted=SetupEngine(engine.store.access)  # a later process has only the record
        for owner in (engine,restarted):
            assert owner.signin_status(reader,grant.generation,attempt)==refused
            context=owner.context(reader)
            assert context['signin_attempt']==refused
            assert 'detail' not in json.dumps(context) and detail not in json.dumps(context)
        assert env.read_text()=='export ROUTE=private\n'
    finally:engine.signin.shutdown()


@pytest.mark.parametrize('ending',['connected','timeout'])
def test_provider_signin_success_and_timeout_record_no_refusal_detail(synthetic_signin,monkeypatch,ending):
    from claudlobby.plane import provider_signin
    engine,grant,reader,*_=synthetic_signin
    if ending=='timeout':monkeypatch.setattr(provider_signin,'SECONDS',0.5)
    attempt=str(uuid4())
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        if ending=='connected':
            _signin_until(engine,grant,reader,attempt,'waiting')
            engine.signin_code(reader,grant.generation,attempt,'accepted#'+'b'*32)
        ended=_signin_ended(engine,grant,reader,attempt)
        assert (ended['state'],ended['reason'])==(('connected',None) if ending=='connected' else ('needs_attention','timeout'))
        row=_signin_row(engine,attempt)
        assert 'detail' in row and row['detail'] is None
    finally:engine.signin.shutdown()


def test_provider_signin_table_from_before_the_refusal_detail_gains_the_column_and_still_works(synthetic_signin):
    from claudlobby.plane.provider_signin import ProviderSignin
    engine,grant,reader,program,_token,_env=synthetic_signin
    before=['attempt_id','generation','state','reason','code_delivered','retired']
    ended,successor=str(uuid4()),str(uuid4())
    def columns():
        with engine.store.access._connection() as conn:
            return [row[1] for row in conn.execute('PRAGMA table_info(setup_provider)')]
    def as_before():
        with engine.store.access._connection(write=True) as conn:
            conn.execute('DROP TABLE setup_provider')  # the table as the version before this column made it
            conn.execute('CREATE TABLE setup_provider(attempt_id TEXT PRIMARY KEY,generation TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,code_delivered INTEGER,retired INTEGER NOT NULL DEFAULT 0)')
            conn.execute('INSERT INTO setup_provider VALUES(?,?,?,?,?,?)',(ended,grant.generation,'needs_attention','timeout',0,0))
    timed_out={'attempt_id':ended,'generation':grant.generation,'state':'needs_attention','reason':'timeout','retryable':True}
    restarted=SetupEngine(engine.store.access)
    as_before()
    assert restarted.signin_status(reader,grant.generation,ended)==timed_out and columns()==before, 'a read upgrades nothing'
    with engine.store.access._connection(write=True) as conn:ProviderSignin.initialize(conn)  # approving setup upgrades it
    assert columns()==before+['detail']
    assert _signin_row(engine,ended)=={'attempt_id':ended,'generation':grant.generation,'state':'needs_attention',
        'reason':'timeout','code_delivered':0,'retired':0,'detail':None}, 'its row is kept, with no detail'
    as_before()
    program.with_name('synthetic-provider-reply').write_bytes(b'\x1b[5z')
    try:  # Try again on the older table: begin upgrades it, and the new attempt's refusal is recorded
        assert restarted.signin_begin(reader,grant.generation,successor)['attempt_id']==successor
        assert columns()==before+['detail']
        _signin_until(restarted,grant,reader,successor,'waiting')
        restarted.signin_code(reader,grant.generation,successor,'accepted#'+'b'*32)
        refused=_signin_ended(restarted,grant,reader,successor)
        assert (refused['state'],refused['reason'])==('needs_attention','unsupported_output')
        assert [(row['retired'],row['code_delivered'],row['detail']) for row in map(lambda a:_signin_row(engine,a),(ended,successor))]==[(1,0,None),(0,1,'csi final z 5')]
    finally:restarted.signin.shutdown()


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
