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


def test_context_carries_the_retained_preview_so_a_page_that_lost_the_response_can_launch(setup_authority,monkeypatch):
    engine,grant,reader=allow(setup_authority)
    preview=engine.preview(reader,grant.generation,"Improve checkout tests")
    assert engine.context(reader)["preview"]=={**preview,"goal":"Improve checkout tests"}


def test_context_carries_the_preview_recovered_from_a_response_lost_before_it_was_kept(setup_authority,monkeypatch):
    engine,grant,reader=allow(setup_authority)
    monkeypatch.setattr(engine.store,"retain",lambda *_: (_ for _ in ()).throw(RuntimeError("lost preview")))
    with pytest.raises(RuntimeError):engine.preview(reader,grant.generation,"Improve checkout tests")
    preview=engine.context(reader)["preview"]
    assert preview["request_id"]==setup_authority[-1].retained["record"]["request_id"]
    assert preview["goal"]=="Improve checkout tests" and preview["phase"]=="quiesce" and preview["dispatch"]=="prepared"


def test_context_has_no_preview_when_preparation_did_not_finish_or_launch_is_reserved(setup_authority):
    engine,grant,reader=allow(setup_authority)
    engine.preview(reader,grant.generation,"Improve checkout tests")
    record=setup_authority[-1].retained["record"]
    record["phase"]="prepare"  # a preparation the computer holds as not finished
    context=engine.context(reader)
    assert context["request"] is not None and "preview" not in context and context["continuation"]
    record["phase"]="quiesce"
    assert "preview" in engine.context(reader)
    setup_authority[0].reserve(reader,grant,engine.context(reader)["request"])
    assert "preview" not in engine.context(reader)


def test_context_offers_no_preview_past_its_window(setup_authority):
    engine,grant,reader=allow(setup_authority)
    preview=engine.preview(reader,grant.generation,"Improve checkout tests")
    setup_authority[4][0]=preview["expires_at"]  # the window is [created, expires)
    context=engine.context(reader)
    assert "preview" not in context and context["request"] is not None
    # Expired, not "did not finish": only the computer can go on with it, and the context says how.
    assert context["preview_state"]=="expired" and context["continuation"].endswith("host update continue")


# What became of this approval's Preview, as the context read reports it (preview_state).
def _scope(engine,grant):
    return {"directory":grant.install_directory,"manifests":[str(engine.root/"local"/grant.fleet/"fleet.yaml")]}


def _preparing(engine,grant,**changes):
    """This approval's own first-team record while Preview's prepare stages run: no fingerprint bound yet."""
    record={"target":grant.source_revision,"config_fingerprint":None,"finalized":False,"phase":"prepare",
            "request_id":str(uuid4()),"initialization_scope":_scope(engine,grant)}
    record.update(changes)
    return {key:value for key,value in record.items() if value is not ...}


def _begun(fixture,record,*,lease=True):
    """A Preview row begun as Preview begins it; lease=True also leaves the lease file a Preview creates."""
    engine,grant,reader=allow(fixture)
    if lease:
        with engine.store.preview_lease():pass
    engine.store.begin_preview(reader,grant,"Improve checkout tests")
    fixture[-1].retained["record"]=record(engine,grant) if callable(record) else record
    return engine,grant,reader


def test_context_says_none_before_any_preview(setup_authority):
    engine,grant,reader=allow(setup_authority)
    context=engine.context(reader)
    assert context["preview_state"]=="none" and context["request"] is None
    assert "preview" not in context and "continuation" not in context


@pytest.mark.parametrize("record",[
    pytest.param(None,id="before-any-update-record"),
    pytest.param(_preparing,id="no-config-fingerprint-yet"),
    pytest.param(lambda engine,grant:_preparing(engine,grant,finalized=True,config_fingerprint="b"*64),
                 id="fingerprint-bound-prepare-stage-still-running"),
])
def test_context_answers_preparing_while_this_approvals_own_preview_runs(setup_authority,record):
    engine,grant,reader=_begun(setup_authority,record)
    with engine.store.preview_lease():  # Preview holds it for as long as it runs
        context=engine.context(reader)
        assert context["preview_state"]=="preparing" and context["request"] is None
        assert not context["launch_reserved"] and "preview" not in context and "continuation" not in context
        # Only the read answers: Launch's own checks still refuse an unfinished preparation.
        with pytest.raises((AccessDenied,AccessUnavailable)):engine.store.retained_request(reader)
        with pytest.raises(AccessDenied):engine.store.begin_preview(reader,grant,"Another goal")
    assert not setup_authority[-1].dispatches


def test_context_says_failed_on_a_stage_error_and_names_the_local_command(setup_authority):
    engine,grant,reader=_begun(setup_authority,lambda engine,grant:_preparing(engine,grant,
        stage_error={"stage":"render","detail":"refused"}))
    with engine.store.preview_lease():context=engine.context(reader)
    assert context["preview_state"]=="failed" and context["request"] is None and "preview" not in context
    assert context["continuation"].endswith("host update continue")


def test_context_says_failed_once_no_preview_runs_here_and_offers_a_command_only_for_a_recorded_operation(setup_authority):
    engine,grant,reader=_begun(setup_authority,_preparing)
    context=engine.context(reader)  # Nothing holds the preview lease: no Preview runs on this computer.
    assert context["preview_state"]=="failed" and context["continuation"].endswith("host update continue")
    setup_authority[-1].retained["record"]=None  # it ended before any update record was written
    context=engine.context(reader)
    assert context["preview_state"]=="failed" and "continuation" not in context and context["request"] is None


def test_an_unreadable_lease_reads_preparing_up_to_the_bound_and_a_held_one_past_it(setup_authority,monkeypatch):
    from claudlobby import git_update
    from claudlobby.plane import owner_setup
    assert owner_setup.preparing_seconds()==3*git_update._STAGE_TIMEOUT+300  # authorize, render, prepare, plus Git and file steps
    # The connected session lasts less than that bound, so the clock walks a shortened one.
    monkeypatch.setattr(owner_setup,"preparing_seconds",lambda:120)
    engine,grant,reader=_begun(setup_authority,_preparing,lease=False)  # no lease file: the signal cannot be read
    assert engine.store.preview_running() is None
    started=setup_authority[4][0]
    setup_authority[4][0]=started+119
    assert engine.context(reader)["preview_state"]=="preparing","unknown is never failed inside the bound"
    setup_authority[4][0]=started+120
    assert engine.context(reader)["preview_state"]=="failed"
    with engine.store.preview_lease():
        assert engine.context(reader)["preview_state"]=="preparing","a Preview shown alive is never failed"


def test_a_second_engine_sees_a_preview_another_engine_runs_and_then_what_its_record_says(setup_authority,monkeypatch):
    """Each SSH call is a fresh process and engine; the listener is one engine. Both read the same lease."""
    from claudlobby import git_update
    engine,grant,reader=allow(setup_authority)
    other=SetupEngine(setup_authority[1])
    seen=[];prepared=git_update._prepare_update_locked
    def slow(store,**kwargs):
        setup_authority[-1].retained["record"]=_preparing(engine,grant)
        seen.append(other.context(reader)["preview_state"])
        return prepared(store,**kwargs)
    monkeypatch.setattr(git_update,"_prepare_update_locked",slow)
    engine.preview(reader,grant.generation,"Improve checkout tests")
    assert seen==["preparing"]
    assert other.context(reader)["preview_state"]=="ready"


def test_a_preview_that_fails_or_whose_process_dies_reads_failed_from_another_engine(setup_authority,monkeypatch):
    import subprocess,sys
    from claudlobby import git_update
    engine,grant,reader=allow(setup_authority)
    other=SetupEngine(setup_authority[1])
    def refused(store,**kwargs):
        setup_authority[-1].retained["record"]=_preparing(engine,grant,stage_error={"stage":"render","detail":"refused"})
        raise git_update.UpdateError("application stage render refused")
    monkeypatch.setattr(git_update,"_prepare_update_locked",refused)
    with pytest.raises(Exception):engine.preview(reader,grant.generation,"Improve checkout tests")
    assert other.context(reader)["preview_state"]=="failed"
    # A Preview process that dies holding the lease: the operating system releases it, and no marker stays.
    setup_authority[-1].retained["record"]=_preparing(engine,grant)
    holder=subprocess.Popen([sys.executable,"-c","import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_RDWR);"
        "fcntl.flock(fd,fcntl.LOCK_EX);print('held',flush=True);time.sleep(60)",str(other.store._lease_path)],
        stdout=subprocess.PIPE,text=True)
    try:
        assert holder.stdout.readline().strip()=="held"
        assert other.context(reader)["preview_state"]=="preparing"
    finally:holder.kill();holder.wait()
    assert other.context(reader)["preview_state"]=="failed"


def test_a_preview_finishing_between_the_status_read_and_the_lease_test_reads_ready(setup_authority,monkeypatch):
    engine,grant,reader=allow(setup_authority)
    engine.preview(reader,grant.generation,"Improve checkout tests")  # finished: request kept, lease released
    real=engine.store.preview_status;calls=[]
    def interleaved(reader):
        calls.append(1)
        if len(calls)==1:  # the view read just before that Preview kept its request
            return "preparing",None,False,{"goal":"Improve checkout tests","created_at":setup_authority[4][0],
                "expires_at":setup_authority[4][0]+600,"record":_preparing(engine,grant)}
        return real(reader)
    monkeypatch.setattr(engine.store,"preview_status",interleaved)
    assert engine.store.preview_running() is False
    context=engine.context(reader)
    assert context["preview_state"]=="ready" and context["preview"]["goal"]=="Improve checkout tests"
    assert len(calls)==2


def test_expiry_after_a_local_launch_reads_as_before_not_as_an_expired_preview(setup_authority):
    engine,grant,reader=allow(setup_authority)
    preview=engine.preview(reader,grant.generation,"Improve checkout tests")
    setup_authority[-1].retained["record"]["phase"]="git"  # the continue command on the computer moved it on
    setup_authority[4][0]=preview["expires_at"]
    context=engine.context(reader)
    assert "preview_state" not in context and "preview" not in context
    assert context["request"]==binding(preview) and not context["launch_reserved"] and context["continuation"]


def test_context_says_ready_with_the_plan_and_nothing_once_launch_is_reserved(setup_authority):
    engine,grant,reader=allow(setup_authority)
    preview=engine.preview(reader,grant.generation,"Improve checkout tests")
    context=engine.context(reader)
    assert context["preview_state"]=="ready" and context["preview"]=={**preview,"goal":"Improve checkout tests"}
    engine.store.reserve(reader,grant,context["request"])
    context=engine.context(reader)
    assert context["launch_reserved"] and "preview_state" not in context and "preview" not in context


@pytest.mark.parametrize("changes",[{"target":"c"*40},{"initialization_scope":{"directory":"/different","manifests":[]}},
    {"request_id":"not-a-request"},{"initialize":False},{"phase":"quiesce"},
    {"finalized":True},{"finalized":False,"config_fingerprint":"b"*64}],ids=str)
def test_a_foreign_or_changed_record_mid_preparation_is_still_refused(setup_authority,changes):
    engine,grant,reader=_begun(setup_authority,lambda engine,grant:_preparing(engine,grant,**changes))
    with engine.store.preview_lease():
        with pytest.raises(AccessDenied):engine.context(reader)


def test_another_approval_never_sees_this_approvals_preview_state(setup_authority):
    engine,grant,reader=_begun(setup_authority,_preparing)
    engine.store.revoke(grant)
    with pytest.raises(AccessDenied):engine.context(reader)


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


def test_local_preview_check_keeps_the_original_and_its_folder_project(setup_authority, tmp_path, monkeypatch):
    """The frozen operation is still exactly fleet.yaml with no projects.yaml, and
    it composes the approved folder as the lead's project; a changed one refuses."""
    from claudlobby.config import load_fleet_snapshot
    from claudlobby.config_plan import read_rendered
    store, access, old, reader, *_ = setup_authority
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "claudfather-projects" / "first-team"; project.mkdir(parents=True)
    other = tmp_path / "claudfather-projects" / "second-team"; other.mkdir()
    grant = store.candidate(OWNER, template="local-project", fleet="shop", repository=None,
                            project_directory=str(project), install_directory=old.install_directory)
    store.allow(grant)
    engine = SetupEngine(access)
    goal = "Build a one-page hello website"
    preview = engine.preview(reader, grant.generation, goal)
    record = engine.store.operation(grant)
    engine.store.check_frozen(grant, record, goal)
    rendered = read_rendered(engine.root, preview["config_fingerprint"])
    item = rendered.effects["fleet_sources"]["shop"]
    manifest, content = rendered.frozen_input(item["fleet"], required=True)
    _, project_content = rendered.frozen_input(item["projects"])
    assert project_content is None
    fleet, _ = load_fleet_snapshot(manifest, content, project_content)
    assert set(fleet.projects) == {"first-team"} and fleet.projects["first-team"].directory == str(project)
    for changed, text in ((grant, "A changed goal"), (replace(grant, project_directory=str(other)), goal)):
        with pytest.raises(AccessDenied, match="setup_original_operation_changed"):
            engine.store.check_frozen(changed, record, text)
    assert not setup_authority[-1].dispatches


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
    old.pop("messages")  # The messages mark, like the fields above, is absent from an unmarked snapshot.
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


def _typed_reopen(fixture, monkeypatch, wait, *, ssh=None, on_serve=None, **request):
    """The typed guide started again on this computer, as the embedding page starts it; wait stands in for the pane.

    ssh carries the guide over SSH instead of the browser; on_serve runs as serving starts."""
    from contextlib import contextmanager
    import sys
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.context import resolve_paths
    store, access, grant, *_ = fixture
    config = {"origin": "https://host.example.test", "socket": str(access.root / "state/owner/owner.sock"),
        "tailscale": sys.executable, "tailscale_socket": None,
        "workspace_issuer": "https://accounts.example.test", "workspace_origin": "https://plane.example.test"}
    approval = SimpleNamespace(**{name: getattr(grant, name) for name in ("template", "repository", "project_directory",
        "account_directory", "managed_owner", "provider_signin")}, target_fleet=grant.fleet,
        install_directory=Path(grant.install_directory))
    monkeypatch.setattr(guide, "_configuration", lambda *_: (config, approval))
    frames, served = [], []
    class Consent:
        def state(self, state, **data): frames.append((state, data))
        def check_cancel(self): pass
        def approve(self, *_args, **_kwargs): pytest.fail("a card was asked again")
    @contextmanager
    def serving(*_args, **_kwargs):
        served.append(True)
        if on_serve is not None: on_serve()
        class Child:
            def wait(self, timeout): return wait(timeout)
        yield Child()
    monkeypatch.setattr(guide, "_serving", serving)
    profile = SimpleNamespace(prepare_directories=lambda *_: None, prepare_provider=lambda *_: None, origin=lambda: config["origin"],
        project=Path("/fixed/project"), account=Path("/fixed/profile"), units=Path(grant.install_directory),
        provider=Path("/fixed/provider"))
    def run():
        return guide.run(SimpleNamespace(**request), resolve_paths(root=access.root), access, None,
                         consent=Consent(), profile=profile, **({"ssh": ssh} if ssh is not None else {}))
    return run, frames, served


def test_typed_guide_started_again_after_preview_serves_the_kept_preview_and_its_launch(setup_authority, monkeypatch):
    """The workspace was closed after Preview: the guide run ended, the Preview was kept and nothing was launched."""
    import subprocess
    store, access, grant, reader, *_ = setup_authority
    engine, grant, reader = allow(setup_authority)
    preview = engine.preview(reader, grant.generation, "Review checkout")
    seen = {}
    def wait(timeout):
        seen["context"] = engine.context(reader)
        engine.launch(reader, **seen["context"]["request"])
        raise subprocess.TimeoutExpired("owner", timeout)
    run, frames, served = _typed_reopen(setup_authority, monkeypatch, wait)
    result = run()
    assert served == [True]
    assert frames == [("preview_launch_ready", {"origin": "https://host.example.test", "generation": grant.generation})]
    assert seen["context"]["preview_state"] == "ready" and not seen["context"]["launch_reserved"]
    assert seen["context"]["preview"] == {**preview, "goal": "Review checkout"}
    assert result.data == {"state": "launch_handoff", **binding(preview)}
    assert store.retained_state().grant == grant and len(setup_authority[-1].dispatches) == 1


def test_typed_guide_started_again_after_the_preview_window_says_expired(setup_authority, monkeypatch):
    from claudlobby.commands.owner_stdio import StdioRefused
    store, access, grant, reader, clock, _ = setup_authority
    engine, grant, reader = allow(setup_authority)
    preview = engine.preview(reader, grant.generation, "Review checkout")
    clock[0] = preview["expires_at"]  # the window is [created, expires); the approval itself still holds
    seen = {}
    def wait(_timeout):
        seen["context"] = engine.context(reader)
        raise KeyboardInterrupt
    run, frames, served = _typed_reopen(setup_authority, monkeypatch, wait)
    with pytest.raises(StdioRefused, match="interrupted_unknown"):
        run()
    assert served == [True]
    assert frames == [("preview_launch_ready", {"origin": "https://host.example.test", "generation": grant.generation})]
    assert seen["context"]["preview_state"] == "expired" and "preview" not in seen["context"]
    assert store.local() == grant and not setup_authority[-1].dispatches


@pytest.mark.parametrize("case", ["reserved", "launched", "dispatched", "quiet_support", "notice_support", "phase",
    "unfinalized", "stage_error", "committed", "request_id", "fingerprint", "target", "scope", "not_initialize",
    "no_preview_row", "expired", "revoked", "replaced", "no_approval", "finish_setup", "ssh"])
def test_typed_guide_started_again_refuses_anything_but_a_kept_unlaunched_preview(setup_authority, monkeypatch, case):
    import sqlite3
    store, access, grant, reader, clock, args = setup_authority
    engine, grant, reader = allow(setup_authority)
    preview = engine.preview(reader, grant.generation, "Review checkout")
    record = args.retained["record"]
    if case == "reserved": store.reserve(reader, grant, binding(preview))
    elif case == "launched": engine.launch(reader, **binding(preview))
    elif case == "dispatched": record["job_dispatch"] = {"state": "pending", "attempt": 1}  # e.g. a local continue
    # A local continue saves quiet (and notice) support before job support: one that failed between those saves.
    elif case == "quiet_support": record["quiet_support"] = {"supervisor.sh": "0" * 64}
    elif case == "notice_support": record["notice_support"] = {"path": "/synthetic/notice"}
    # Defence only: the updater's own validation refuses an unfinalized record past prepare.
    elif case == "unfinalized": record["finalized"] = False
    elif case == "target": record["target"] = "c" * 40
    elif case == "scope": record["initialization_scope"] = {"directory": "/different", "manifests": []}
    elif case == "not_initialize": record["initialize"] = False
    elif case == "replaced":
        # The approval is replaced between the entry read and the guide's second read of it.
        from claudlobby.plane.owner_setup import _snapshot
        operation = OwnerSetup.operation
        def replacing(self, observed):
            found = operation(self, observed)
            with sqlite3.connect(access.path) as conn:
                conn.execute("UPDATE setup_grant SET snapshot=?", (_snapshot(replace(grant, generation=str(uuid4()))),))
            return found
        monkeypatch.setattr(OwnerSetup, "operation", replacing)
    elif case == "phase": record["phase"] = "commit"
    elif case == "stage_error": record["stage_error"] = {"stage": "prepare", "detail": "synthetic"}
    elif case == "committed": record["config_committed"] = True
    elif case == "request_id": record["request_id"] = str(uuid4())
    elif case == "fingerprint": record["config_fingerprint"] = "d" * 64
    elif case == "expired": clock[0] = grant.expires_at
    elif case == "revoked": store.revoke(grant)
    elif case in ("no_preview_row", "no_approval"):
        with sqlite3.connect(access.path) as conn:
            conn.execute("DELETE FROM " + ("setup_previews" if case == "no_preview_row" else "setup_grant"))
    request = {"finish_setup": list(binding(preview).values())} if case == "finish_setup" else {}
    carried = {"ssh": object()} if case == "ssh" else {}  # the SSH guide never needed this run alive
    run, frames, served = _typed_reopen(setup_authority, monkeypatch, lambda _timeout: pytest.fail("waited"),
                                        **carried, **request)
    with pytest.raises(CommandFailure) as refused:
        run()
    assert refused.value.error.code == "conflict"
    assert str(refused.value.error.message) == (
        "a first-team operation is retained; inspect its original receipt and continue locally")
    assert refused.value.data == {"request_id": record["request_id"]}
    assert frames == [] and served == []
    assert len(setup_authority[-1].dispatches) == int(case == "launched")


def test_typed_guide_started_again_refuses_when_the_approval_lapses_before_it_is_read_again(setup_authority, monkeypatch):
    """Lapsing after the entry check never reaches the approval cards or the settings file."""
    from claudlobby.commands import host_owner
    store, access, grant, reader, clock, args = setup_authority
    engine, grant, reader = allow(setup_authority)
    engine.preview(reader, grant.generation, "Review checkout")
    monkeypatch.setattr(host_owner, "_allow_setup", lambda *_a, **_k: pytest.fail("asked to allow setup again"))
    def lapse(): clock[0] = grant.expires_at
    run, frames, served = _typed_reopen(setup_authority, monkeypatch, lambda _timeout: pytest.fail("waited"), on_serve=lapse)
    with pytest.raises(CommandFailure) as refused:
        run()
    assert served == [True] and frames == []
    assert refused.value.error.code == "conflict"
    assert str(refused.value.error.message) == (
        "a first-team operation is retained; inspect its original receipt and continue locally")
    assert refused.value.data == {"request_id": args.retained["record"]["request_id"]}
    assert "host update continue" in refused.value.error.hint
    assert store.retained_state().grant == grant and not setup_authority[-1].dispatches


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
            codes["workspace"] = proposed((access, access.workspace(), owner, None)).nonce
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


def _prepared_record(engine,args,**changes):
    """The update record a prepared first-team Preview leaves on disk: at quiesce, so native startup is fenced.

    The fixture's update store keeps the record in memory only; the real one writes it here."""
    record={**args.retained['record'],'schema':1,'maintenance':False,'finalized':True,**changes}
    (engine.root/'state/update.json').write_text(json.dumps(record))


def _signed_in_preview(engine,grant,reader,args):
    """Signed in through the real PTY and the synthetic provider, then a prepared Preview with its record on disk."""
    from claudlobby import resources
    from claudlobby.installation import _check_maintenance,FencedError
    attempt=str(uuid4())
    engine.signin_begin(reader,grant.generation,attempt)
    _signin_until(engine,grant,reader,attempt,'waiting')
    engine.signin_code(reader,grant.generation,attempt,'accepted#'+'b'*32)
    _signin_until(engine,grant,reader,attempt,'connected')
    preview=engine.preview(reader,grant.generation,'Requested local work')
    assert preview['dispatch']=='prepared' and preview['phase']=='quiesce'
    _prepared_record(engine,args)
    installation=resources.get_resources().installation
    _check_maintenance(installation)  # A read passes ...
    with pytest.raises(FencedError):_check_maintenance(installation,native=True)  # ... a native start does not.
    return attempt,binding(preview)


def _nothing_launched(engine,args):
    assert not args.dispatches
    with engine.store.access._connection() as conn:
        assert conn.execute('SELECT launch_id FROM setup_grant').fetchone()[0] is None


def test_signed_in_first_team_launches_its_own_prepared_preview(synthetic_signin,setup_authority):
    """Found live: Launch asked the native-start fence, which this approval's own Preview had raised."""
    engine,grant,reader,*_=synthetic_signin
    args=setup_authority[-1]
    try:
        _,request=_signed_in_preview(engine,grant,reader,args)
        result=engine.launch(reader,**request)
        assert result['state']=='quiesce' and result['launch_reserved']
        assert [record['request_id'] for record in args.dispatches]==[request['request_id']]
        assert engine.launch(reader,**request)==result and engine.receipt(reader,**request)==result
        assert engine.context(reader)['launch_reserved'] and len(args.dispatches)==1
    finally:engine.signin.shutdown()


def test_signed_in_first_team_launch_still_refuses_a_signin_that_is_not_connected(synthetic_signin,setup_authority):
    engine,grant,reader,*_=synthetic_signin
    args=setup_authority[-1]
    try:
        attempt,request=_signed_in_preview(engine,grant,reader,args)
        for ending in ('needs_attention','cancelled','unknown','missing'):  # missing last: it deletes the row
            with engine.store.access._connection(write=True) as conn:
                conn.execute("UPDATE setup_provider SET state='connected' WHERE attempt_id=?",(attempt,))
            engine.signin.ready(reader,grant)  # each ending starts from a connected sign-in
            with engine.store.access._connection(write=True) as conn:
                if ending=='missing':conn.execute('DELETE FROM setup_provider')
                else:conn.execute('UPDATE setup_provider SET state=? WHERE attempt_id=?',(ending,attempt))
            with pytest.raises(AccessDenied,match='signin_not_complete'):engine.launch(reader,**request)
            assert not engine.context(reader)['launch_reserved']
            _nothing_launched(engine,args)
    finally:engine.signin.shutdown()


def test_signed_in_first_team_launch_still_refuses_during_maintenance(synthetic_signin,setup_authority):
    """ready() skips only the native-start fence: active maintenance still refuses before anything is reserved."""
    from claudlobby.installation import InstallationError
    engine,grant,reader,*_=synthetic_signin
    args=setup_authority[-1]
    try:
        _,request=_signed_in_preview(engine,grant,reader,args)
        _prepared_record(engine,args,maintenance=True)
        with pytest.raises(InstallationError,match='maintenance is active'):engine.launch(reader,**request)
        _nothing_launched(engine,args)
    finally:engine.signin.shutdown()


def test_signin_calls_stay_fenced_while_the_prepared_preview_quiesces(synthetic_signin,setup_authority):
    """Only ready() reads past the native-start fence; every call the browser or worker makes still asks it."""
    from claudlobby.installation import FencedError
    engine,grant,reader,*_=synthetic_signin
    try:
        attempt,_=_signed_in_preview(engine,grant,reader,setup_authority[-1])
        engine.signin.ready(reader,grant)
        signin=engine.signin
        # The browser's own calls end earlier, at the retained Preview; under them the fence still holds.
        with pytest.raises(AccessDenied,match='signin_preview_retained'):engine.signin_status(reader,grant.generation,attempt)
        for call in (lambda:signin.status(reader,grant,attempt),lambda:signin.begin(reader,grant,attempt),
                     lambda:signin.begin(reader,grant,str(uuid4())),lambda:signin.code(reader,grant,attempt,'accepted#'+'b'*32),
                     lambda:signin.cancel(reader,grant,attempt),lambda:signin.summary(reader,grant),
                     lambda:signin._admit(reader,grant)):
            with pytest.raises(FencedError):call()
        with pytest.raises(FencedError):
            with engine.signin._runtime(reader,grant):pytest.fail('a fenced runtime was entered')
        assert engine.signin._thread is not None and not engine.signin._thread.is_alive()
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
         r'|csi private [?><=][0-9;]{0,15} [A-Za-z]|csi range|csi [KJ] [0-9]{1,4}'
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
    pytest.param([b'\x1b[!p'],'unsupported_output','csi malformed p !',id='csi-malformed-other-prefix'),
    pytest.param([b'\x1b[0 q'],'unsupported_output','csi malformed q 0_',id='csi-malformed-intermediate'),
    pytest.param([b'\x1b['+b'9'*17+b'!p'],'unsupported_output','csi malformed p',id='csi-malformed-long-body'),
    pytest.param([b'\x1b[200~'],'unsupported_output','csi malformed ~ 200',id='csi-malformed-tilde'),
    pytest.param([b'\x1b[?2031;x'],'unsupported_output','csi private ?2031; x',id='csi-private'),
    pytest.param([b'\x1b[>1q'],'unsupported_output','csi private >1 q',id='csi-private-greater-than'),
    pytest.param([b'\x1b[>4;3m'],'unsupported_output','csi private >4;3 m',id='csi-private-modkeys-out-of-range'),
    pytest.param([b'\x1b[>5m'],'unsupported_output','csi private >5 m',id='csi-private-other-xterm-resource'),
    pytest.param([b'\x1b[<5;5M'],'unsupported_output','csi private <5;5 M',id='csi-private-less-than'),
    pytest.param([b'\x1b[=1;9u'],'unsupported_output','csi private =1;9 u',id='csi-private-equals'),
    pytest.param([b'\x1b[>123u'],'unsupported_output','csi private >123 u',id='csi-private-kitty-push'),
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


@pytest.mark.parametrize("launch", ["none", "unmarked", "marked"])
@pytest.mark.parametrize("interruption", ["disconnect", "keyboard", "drain"])
def test_typed_serving_interrupt_drains_without_success_replay_or_regrant(
    setup_authority, monkeypatch, launch, interruption,
):
    from contextlib import contextmanager
    import subprocess, sys
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby.context import resolve_paths
    store, access, grant, reader, *_ = setup_authority
    if launch != "unmarked":
        grant = replace(grant, messages={"alias": ALIAS})
    store.allow(grant)
    engine = SetupEngine(access)
    config = {'origin': 'https://host.example.test', 'socket': str(access.root / 'state/owner/owner.sock'),
        'tailscale': sys.executable, 'tailscale_socket': None,
        'workspace_issuer': 'https://accounts.example.test', 'workspace_origin': 'https://plane.example.test'}
    approval = SimpleNamespace(**{name: getattr(grant, name) for name in ('template', 'repository', 'project_directory',
        'account_directory', 'managed_owner', 'provider_signin')}, target_fleet=grant.fleet,
        install_directory=Path(grant.install_directory))
    monkeypatch.setattr(guide, '_configuration', lambda *_: (config, approval))
    original, drained, started = {}, [], []
    waited = False
    monkeypatch.setattr(guide, "computer_can_finish", lambda _root: True)

    def start_waiter(_paths, data):
        assert drained == [True], "the guide starts Finish only after its server drains"
        started.append(data)
        return True

    monkeypatch.setattr(guide, "_start_waiter", start_waiter)
    class Consent:
        def state(self, *_a, **_k): pass
        def check_cancel(self):
            if interruption == "disconnect" and waited:
                raise StdioRefused("disconnected")
        def approve(self, *_a, **_k): pytest.fail('interrupt renewed consent')
    @contextmanager
    def serving(*_a, **_k):
        class Child:
            def wait(self, timeout):
                nonlocal waited
                assert not waited
                waited = True
                if launch != "none":
                    original.update(binding(engine.preview(reader, grant.generation, 'Review checkout')))
                    engine.launch(reader, **original)
                if interruption == "keyboard":
                    raise KeyboardInterrupt
                if interruption == "drain" and not original:
                    return 0
                raise subprocess.TimeoutExpired("owner", timeout)
        try:
            yield Child()
        finally:
            drained.append(True)
            if interruption == "drain":
                raise StdioRefused("interrupted_unknown")
    monkeypatch.setattr(guide, '_serving', serving)
    profile = SimpleNamespace(prepare_directories=lambda *_: None, prepare_provider=lambda *_: None,
        origin=lambda: config['origin'], project=Path('/fixed/project'), account=Path('/fixed/profile'),
        units=Path(grant.install_directory), provider=Path('/fixed/provider'))
    code = "disconnected" if interruption == "disconnect" else "interrupted_unknown"
    with pytest.raises(StdioRefused, match=code):
        guide.run(SimpleNamespace(), resolve_paths(root=access.root), access, None, consent=Consent(), profile=profile)
    assert drained == [True]
    retained = store.retained_state()
    assert retained.grant == grant
    assert len(setup_authority[-1].dispatches) == int(bool(original))
    if original:
        assert retained.launch_id == original['request_id'] and retained.launch_plan == original['config_fingerprint']
    assert started == ([{"state": "launch_handoff", **original}] if launch == "marked" else [])
    if original:
        assert preview_payload(access, grant).get("finish") == ({"starter": "computer"} if started else None)


# --- Keyboard-mode switches are ignored: they never change the screen ---
@pytest.mark.parametrize('sequence',[b'\x1b[>4m',b'\x1b[>4;0m',b'\x1b[<u',b'\x1b[<1u',b'\x1b[<12u'])
def test_provider_signin_parser_ignores_keyboard_mode_switch_offs(sequence):
    from claudlobby.plane.provider_signin import TokenScreen
    screen=TokenScreen()
    screen.feed(b'ab'+sequence+b'c')
    assert ''.join(screen._rows[0])=='abc'


# Turning a key-reporting mode ON stays refused, by name, and so does every near miss.
@pytest.mark.parametrize('sequence,detail',[
    (b'\x1b[>4;1m','csi private >4;1 m'),(b'\x1b[>4;2m','csi private >4;2 m'),
    (b'\x1b[>u','csi private > u'),(b'\x1b[>1u','csi private >1 u'),
    (b'\x1b[=1u','csi private =1 u'),(b'\x1b[=1;1u','csi private =1;1 u'),(b'\x1b[=u','csi private = u'),
    (b'\x1b[>4;2u','csi private >4;2 u'),(b'\x1b[<1;1u','csi private <1;1 u'),(b'\x1b[<1m','csi private <1 m'),
    (b'\x1b[>44m','csi private >44 m'),(b'\x1b[>4;m','csi private >4; m'),(b'\x1b[<123u','csi private <123 u'),
])
def test_provider_signin_parser_still_refuses_switching_a_keyboard_mode_on(sequence,detail):
    import re
    from claudlobby.plane.provider_signin import TokenScreen, SigninRefused
    with pytest.raises(SigninRefused) as refused:
        TokenScreen().feed(b'ab'+sequence+b'c')
    assert str(refused.value)=='unsupported_output' and refused.value.detail==detail and re.fullmatch(_DETAIL,detail)


@pytest.mark.parametrize('sequence',[b'\x1b[?1016l',b'\x1b[?1006l',b'\x1b[?1003l',b'\x1b[?1002l',b'\x1b[?1000l',
    b'\x1b[?9l',b'\x1b[?1001l',b'\x1b[?1005l',b'\x1b[?1015l',b'\x1b[?1000;1006l',b'\x1b[?7h'])
def test_provider_signin_parser_ignores_mouse_reporting_switch_offs_and_line_wrap_on(sequence):
    # Claude's exit: mouse reporting off (1016, 1006, 1003, 1002, 1000 seen), then line wrap on.
    from claudlobby.plane.provider_signin import TokenScreen
    plain,switched=TokenScreen(),TokenScreen()
    plain.feed(b'ab\r\ncd'); switched.feed(b'ab'+sequence+b'\r\n'+sequence+b'cd')
    assert switched._rows==plain._rows and (switched._row,switched._col)==(plain._row,plain._col)


@pytest.mark.parametrize('sequence,detail',[
    (b'\x1b[?1000h','csi private ?1000 h'),(b'\x1b[?1006h','csi private ?1006 h'),(b'\x1b[?1016h','csi private ?1016 h'),
    (b'\x1b[?7l','csi private ?7 l'),(b'\x1b[?7;25h','csi private ?7;25 h'),(b'\x1b[?25;1000l','csi private ?25;1000 l'),
    (b'\x1b[?1007l','csi private ?1007 l'),(b'\x1b[?6h','csi private ?6 h'),(b'\x1b[?3l','csi private ?3 l'),
    (b'\x1b[?47l','csi private ?47 l'),(b'\x1b[?10160l','csi private ?10160 l')])
def test_provider_signin_parser_still_refuses_other_private_mode_switches(sequence,detail):
    import re
    from claudlobby.plane.provider_signin import TokenScreen, SigninRefused
    with pytest.raises(SigninRefused) as refused:
        TokenScreen().feed(b'ab'+sequence+b'c')
    assert str(refused.value)=='unsupported_output' and refused.value.detail==detail and re.fullmatch(_DETAIL,detail)


# The control sequences a real Claude sends on its way out after a sign-in, in its own order
# (captured from the program's exit; the first two unknown ones were also named by real
# sign-in attempts): focus reports off, key reporting off, kitty pop, theme reports off,
# bracketed paste off, US-ASCII, mouse reporting off five ways, line wrap on, cursor shown,
# the scroll region reset between a cursor save and restore, and an empty window title.
_CLAUDE_EXIT=(b'\x1b[?1004l\x1b[>4m\x1b[<u\x1b[?2031l\x1b[?2004l\x1b(B\x0f\x1b[?1016l\x1b[?1006l\x1b[?1003l'
              b'\x1b[?1002l\x1b[?1000l\x1b[?7h\x1b[?25h\x1b7\x1b[r\x1b8\x1b]0;\x07')


def test_provider_signin_parser_reads_claudes_whole_exit_without_changing_the_screen():
    from claudlobby.plane.provider_signin import TokenScreen
    plain,exited=TokenScreen(),TokenScreen()
    plain.feed(b'ab\r\ncd'); exited.feed(b'ab\r\ncd'+_CLAUDE_EXIT)
    assert exited._rows==plain._rows and (exited._row,exited._col)==(plain._row,plain._col)


def test_provider_signin_reads_the_token_after_the_keyboard_mode_switch_claude_sends(synthetic_signin):
    # What a real Claude 2.1.325 did after the code: "CSI > 4 m", then its success screen.
    engine,grant,reader,program,token,env=synthetic_signin
    success=('\x1b[2K\r\u2713 Long-lived authentication token created successfully!\r\nYour OAuth token (valid for 1 year):\r\n'
             +token+"\r\nStore this token securely. You won't be able to see it again.\r\n"
             'Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>\r\n')
    program.with_name('synthetic-provider-reply').write_bytes(b'\x1b[>4m'+success.encode()+_CLAUDE_EXIT)
    attempt=str(uuid4())
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        _signin_until(engine,grant,reader,attempt,'waiting')
        engine.signin_code(reader,grant.generation,attempt,'accepted#'+'b'*32)
        ended=_signin_ended(engine,grant,reader,attempt)
        assert (ended['state'],ended['reason'])==('connected',None)
        row=_signin_row(engine,attempt)
        assert row['code_delivered']==1 and row['detail'] is None
        assert env.read_text()=="export ROUTE=private\nexport CLAUDE_CODE_OAUTH_TOKEN='"+token+"'\n"
    finally:engine.signin.shutdown()


# Today's guide on the wire. Written before the computer run was added and
# kept at the head of every later core phase: the ordered frames, each frame's
# keys, and each proposal's keys and action, of a typed Linux guide driven
# through the real channel from a fresh computer to its Launch hand-off.
_PROPOSAL = ["digest", "proposal", "proposal_id", "seq", "session_id", "type", "version"]
TODAYS_GUIDE_FRAMES = [
    ("hello", None, ["profile", "seq", "session_id", "type", "version"]),
    ("proposal", "prepare_directories", _PROPOSAL),
    ("proposal", "install_provider", _PROPOSAL),
    ("state", "provider_installing", ["platform", "provider_version", "seq", "session_id", "state", "type", "version"]),
    ("proposal", "initialize_owner", _PROPOSAL),
    ("state", "pairing_ready", ["origin", "seq", "session_id", "state", "type", "version"]),
    ("state", "awaiting_pairing", ["seq", "session_id", "state", "type", "version"]),
    ("proposal", "pair_owner", _PROPOSAL),
    ("proposal", "initialize_workspace", _PROPOSAL),
    ("proposal", "initialize_connected", _PROPOSAL),
    ("state", "workspace_ready", ["origin", "seq", "session_id", "state", "type", "version"]),
    ("state", "awaiting_workspace", ["seq", "session_id", "state", "type", "version"]),
    ("proposal", "bind_workspace", _PROPOSAL),
    ("proposal", "provider_signin", _PROPOSAL),
    ("proposal", "allow_setup", _PROPOSAL),
    ("state", "preview_launch_ready", ["generation", "origin", "seq", "session_id", "state", "type", "version"]),
    ("result", "launch_handoff", ["config_fingerprint", "generation", "request_id", "seq", "session_id",
                                  "source_revision", "state", "type", "version"]),
]
TODAYS_PROPOSAL_KEYS = ["action", "expires_at", "scope"]
TODAYS_START_KEYS = ["binding_sha256", "expected_uid", "finish_setup", "profile", "seq", "session_id",
                     "source_revision", "type", "version"]


def test_the_guide_wire_keeps_todays_frames_and_the_marked_team_card_answer_names_the_sender(team_setup, monkeypatch, tmp_path):
    """Today's typed Linux run with its own configuration: the local project, the
    Claude sign-in scope and the managed owner's settings plan are built by
    `_configuration` itself. Stand-ins only for what this fixture has no real one
    of: the native service manager and its directory, the Tailscale executable,
    the Claude download and its reviewed digest, the operator gate, and the
    browser on the other side of the guide's listener."""
    from contextlib import contextmanager
    import hashlib, os, subprocess, sys, threading
    from claudlobby import config as host_config, git_update, provider_install, resources, supervision_inventory
    from claudlobby.commands import host_owner_setup as guide, operator_context, owner_setup_consent
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile, StdioGuideConsent
    from claudlobby.commands.owner_stdio import OwnerStdio
    from claudlobby.config_plan import ConfigPlanBuilder
    from claudlobby.context import resolve_paths
    from claudlobby.plane import provider_signin
    from tests.test_plane_owner_workspace import ISSUER, ORIGIN
    args, root, _effects = team_setup
    original = git_update._prepare_update_locked
    def prepare(store, **kwargs):  # As setup_authority freezes the one team's inputs.
        record = original(store, **kwargs)
        manifest = root / "local/web-app/fleet.yaml"
        builder = ConfigPlanBuilder(root, fleets=("web-app",), effects={})
        from claudlobby.config import host_override_path
        builder.input(host_override_path())
        _, fleet_digest = builder.input_snapshot(manifest)
        _, projects_digest = builder.input_snapshot(manifest.parent / "projects.yaml")
        builder.effects.update(fleet_sources={"web-app": {
            "fleet": {"path": str(manifest), "sha256": fleet_digest},
            "projects": {"path": str(manifest.parent / "projects.yaml"), "sha256": projects_digest}}},
            fleet_manifests={"web-app": str(manifest)})
        args.retained["record"].update(config_fingerprint=builder.freeze(record["target"]).fingerprint,
                                       initialization_scope=kwargs["initialization"])
        return dict(args.retained["record"])
    monkeypatch.setattr(git_update, "_prepare_update_locked", prepare)
    package = resources.get_resources()
    package.installation.bin.mkdir(parents=True)
    package.installation.cli.write_text("#!/bin/sh\nexit 0\n")
    package.installation.cli.chmod(0o755)
    root.chmod(0o700)  # A data root is private, as an installation makes it,
    (root / "state").mkdir(mode=0o700, exist_ok=True)
    (root / "state/runtime-use.lock").touch(mode=0o600)  # with the launcher's reader-lease file.
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    units = home / ".config/systemd/user"
    monkeypatch.setattr(guide, "_native_directories", lambda _paths: (units,))
    monkeypatch.setattr(supervision_inventory, "Adapter", lambda _package: SimpleNamespace(
        read=lambda _function: f"manager\tLinux\ndirectory\t{units}\n"))
    monkeypatch.setattr(operator_context, "require_operator_context", lambda root=None: None)
    monkeypatch.setattr(guide, "LINUX_TAILSCALE", Path(sys.executable))
    monkeypatch.setattr(guide, "_WORKSPACE_ORIGINS", (ISSUER, ORIGIN))
    # The fixed profile's own two cards, run on this fixture's directories.
    family = home / ".local/share/claudlobby-browser"
    family.mkdir(parents=True, mode=0o700)
    profile = SimpleNamespace(project=home / "projects/first-team", account=family / "provider-profile",
        policy=family / "host-policy/system.yaml", provider=family / "provider-bin/claude", units=units,
        data=root, account_name="example-account", uid=os.getuid(),
        request={"source_revision": "a" * 40, "binding_sha256": "b" * 64},
        origin=lambda: "https://host.example.test")
    monkeypatch.setenv(host_config.HOST_OVERRIDE_ENV, str(profile.policy))
    profile.prepare_directories = lambda consent, paths, **kw: FixedOwnerProfile.prepare_directories(profile, consent, paths, **kw)
    profile.prepare_provider = lambda consent, paths: FixedOwnerProfile.prepare_provider(profile, consent, paths)
    observed = SimpleNamespace(state="missing", version="2.1.0", platform="linux-x64", sha256="c" * 64)
    monkeypatch.setattr(provider_install, "status", lambda *_: observed)
    def install(*_args, **_kwargs):  # The download's one effect: Claude's executable at its fixed path.
        # Claude's sign-in as the browser sees it: a link, then the pasted code, then a token.
        link = ("https://claude.com/cai/oauth/authorize?code=true&client_id=test&response_type=code"
                "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=user%3Ainference"
                "&code_challenge=" + "a" * 43 + "&code_challenge_method=S256&state=" + "b" * 32)
        screen = ("\x1b[2K\r\u2713 Long-lived authentication token created successfully!\r\n"
                  "Your OAuth token (valid for 1 year):\r\nsk-ant-oat01-" + "s" * 80 + "\r\n"
                  "Store this token securely. You won't be able to see it again.\r\n"
                  "Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>\r\n")
        profile.provider.write_text("\n".join([
            "#!" + sys.executable, "import os, sys, tty", "tty.setraw(0)",
            "sys.stdout.write(" + repr("\x1b]8;;" + link + "\x07Open Claude\x1b]8;;\x07\r\n") + ")",
            "sys.stdout.flush()", "typed = b''",
            "while not typed.endswith(b'\\r'):", "    typed += os.read(0, 1)",
            "sys.stdout.write(" + repr(screen) + ")", "sys.stdout.flush()", ""]))
        profile.provider.chmod(0o755)
        monkeypatch.setattr(provider_signin, "SUPPORTED",
                            {hashlib.sha256(profile.provider.read_bytes()).hexdigest(): "2.1.325"})
        return SimpleNamespace(state="ready")
    monkeypatch.setattr(provider_install, "prepare", install)
    monkeypatch.setattr(owner_setup_consent, "FixedOwnerProfile", lambda *_: None)
    access = OwnerAccess(root)
    codes, servers = {}, []
    @contextmanager
    def serving(command, *_args, **_kwargs):
        servers.append(command)
        owner = access.current_grant()
        if owner is None:
            codes["pairing"] = access.begin_pairing(OWNER).token
            yield SimpleNamespace()
            return
        codes["workspace"] = proposed((access, access.workspace(), owner, None)).nonce
        class Child:
            def wait(self, timeout):
                connected = access.connected()
                session = connected.issue(evidence(connected, [access._now()]), OWNER)
                reader = VerifiedReader(OWNER, session.token)
                engine, grant = SetupEngine(access), OwnerSetup(access).local()
                attempt = str(uuid4())
                engine.signin_begin(reader, grant.generation, attempt)
                _signin_until(engine, grant, reader, attempt, "waiting")
                engine.signin_code(reader, grant.generation, attempt, "accepted#" + "b" * 32)
                _signin_until(engine, grant, reader, attempt, "connected")
                engine.launch(reader, **binding(engine.preview(reader, grant.generation, "Review checkout")))
                raise subprocess.TimeoutExpired("owner", timeout)
        yield Child()
    monkeypatch.setattr(guide, "_serving", serving)
    incoming, client = os.pipe()
    observer, outgoing = os.pipe()
    channel = OwnerStdio(incoming, outgoing)
    frames, starts = [], []
    def drive():
        seq = 0
        def send(kind, **body):
            nonlocal seq
            seq += 1
            os.write(client, (json.dumps({**body, "version": 1, "type": kind, "seq": seq, "session_id": channel.session_id},
                sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii"))
        with os.fdopen(observer, "rb", buffering=0) as stream:
            while True:
                line = b""
                while not line.endswith(b"\n"):
                    part = stream.read(1)
                    if not part:
                        return
                    line += part
                frame = json.loads(line)
                frames.append(frame)
                if frame["type"] == "hello":
                    start = {"profile": "owner-setup-v1", "expected_uid": 501, "source_revision": "a" * 40,
                             "binding_sha256": "b" * 64, "finish_setup": None}
                    starts.append(start)
                    send("start", **start)
                elif frame["type"] == "proposal" and frame["proposal"]["action"] == "allow_setup":
                    # The team card carries the messages mark; its answer carries the name (04a).
                    assert frame["proposal"]["scope"]["grant"]["messages"] == {"sender": "card"}
                    send("decide", proposal_id=frame["proposal_id"], digest=frame["digest"], approve=True,
                         actor_alias="human:ada-lovelace")
                elif frame["type"] == "proposal":
                    send("decide", proposal_id=frame["proposal_id"], digest=frame["digest"], approve=True)
                elif frame.get("state") == "awaiting_pairing":
                    send("submit_pairing", challenge=codes["pairing"])
                elif frame.get("state") == "awaiting_workspace":
                    send("submit_workspace", nonce=codes["workspace"])
                elif frame["type"] in {"result", "error"}:
                    return
    thread = threading.Thread(target=drive)
    thread.start()
    try:
        channel.hello()
        request = channel.start()
        assert sorted(["seq", "session_id", "type", "version", *starts[0]]) == TODAYS_START_KEYS
        assert request["finish_setup"] is None
        result = guide.run(SimpleNamespace(), resolve_paths(root=root), access, None,
                           consent=StdioGuideConsent(channel), profile=profile)
        channel.emit("result", **result.data)  # As owner_stdio.dispatch sends it.
    finally:
        thread.join(30)
        for fd in (incoming, outgoing, client):
            os.close(fd)
    assert not thread.is_alive()
    seen = [(frame["type"], frame["proposal"]["action"] if frame["type"] == "proposal"
             else frame.get("state"), sorted(frame)) for frame in frames]
    assert seen == TODAYS_GUIDE_FRAMES
    assert all(sorted(frame["proposal"]) == TODAYS_PROPOSAL_KEYS for frame in frames if frame["type"] == "proposal")
    assert [frame["seq"] for frame in frames] == list(range(1, len(frames) + 1))
    assert result.data["state"] == "launch_handoff" and len(args.dispatches) == 1 and len(servers) == 2
    # The approval is the real configuration's: the local project, Claude's sign-in, the managed owner.
    grant = OwnerSetup(access).local()
    assert (grant.template, grant.fleet, grant.project_directory) == ("local-project", "web-app", str(profile.project))
    assert grant.provider_signin["binary"] == str(profile.provider) and grant.managed_owner is not None
    assert grant.messages == {"alias": "human:ada-lovelace"}


from tests.test_owner_guide_preinstall import installed  # noqa: E402,F401
from tests.test_owner_stdio import fixed_profile, pipes  # noqa: E402,F401


def test_todays_guide_is_unchanged_on_the_wire_at_the_script(installed, tmp_path):
    """A request at a revision other than the receipt's, with no finish tuple, is refused as today."""
    from tests.test_owner_guide_preinstall import advance_source, run_prelude
    module, home, install, value, python = installed
    current = advance_source(module, install, python, home)
    operations = {"setup": "owner-guide-v1", "status": "owner-guide-status-v1", "api": "owner-api-v1"}
    for action in ("setup", "status"):
        refused = run_prelude(module, tmp_path, home, python, action, dict(value, source_revision=current))
        assert refused.stdout == module.wire({"schema_version": 1, "operation": operations[action],
            "state": "refused", "code": "installation_changed", "retry": False})
    # Their request's keys are fixed: one more key is refused before anything is read.
    record = ["22222222-2222-4222-8222-222222222222", current]
    for action, operation in operations.items():
        refused = run_prelude(module, tmp_path, home, python, action, dict(value, update_record=record))
        assert refused.returncode == 2 and refused.stdout == module.wire({"schema_version": 1,
            "operation": operation, "state": "refused", "code": "invalid_request", "retry": False})


def test_todays_guide_is_unchanged_on_the_wire_at_the_profile(fixed_profile, pipes):
    """The typed start keeps its keys, and the profile refuses a receipt at another revision."""
    import hashlib, os
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import OwnerStdio, StdioRefused
    from tests.test_owner_stdio import read_frame, start, wire
    paths, request, _home, path = fixed_profile
    receipt = json.loads(path.read_bytes())
    receipt["ref"] = receipt["revision"] = "e" * 40
    path.write_bytes(json.dumps(receipt, sort_keys=True).encode())
    fields = ("schema", "purpose", "installation", "data_root", "remote", "ref", "interpreter", "interpreter_sha256")
    request["binding_sha256"] = hashlib.sha256(json.dumps({key: receipt[key] for key in fields},
                                                          sort_keys=True).encode()).hexdigest()
    with pytest.raises(StdioRefused, match="installation_changed"):
        FixedOwnerProfile(paths, request)
    console = OwnerStdio(pipes[0], pipes[1])
    console.hello()
    hello = read_frame(pipes[3])
    os.write(pipes[2], wire(start(hello["session_id"], update_record=None)))
    with pytest.raises(StdioRefused):
        console.start()


def _real_sized_plan():
    """A plan the size a real first team renders: 76 generated files and 70 units, as measured on a
    fresh Ubuntu computer at the first browser Preview (changes about 38 KB, units about 115 KB)."""
    changes=[{"target":f"/home/example/claudlobby/data/runtime/bots/builder/file-{index:03}.conf",
              "before":None,"after":{"kind":"file","mode":420,"sha256":"a"*64},"padding":"p"*330}
             for index in range(76)]
    unit={"bot":"builder","enroll":True,"fleet":"web-app","phase":"start","platform":"linux","scope":"bot",
          "service":None,"sha256":"b"*64,"source":"/home/example/claudlobby/data/runtime/units/claudlobby-builder.service",
          "mode":420,"working_directory":"/home/example/projects/first-team",
          "environment":{f"CLAUDLOBBY_SETTING_{index:02}":"v"*40 for index in range(12)},
          "admission":{"release":"r"*64,"paths":["/home/example/claudlobby/install"]*6}}
    effects={"units":[dict(unit) for _ in range(70)],"restart_bots":["builder","reviewer","tester"],
             "reload_supervision":True,"coverage":{"fleets":["web-app"]}}
    return changes,effects


def test_a_preview_answer_fits_what_a_browser_accepts():
    """A real first team's plan, answered whole, is larger than any browser accepts as a setup answer,
    so its Preview could never be shown. Answered without each unit's working data it fits with room."""
    from claudlobby.plane.owner_setup import SETUP_ANSWER_BYTES, SHOWN_UNIT, shown_effects
    changes,effects=_real_sized_plan()
    size=lambda shown:len(json.dumps({"changes":changes,"effects":shown}).encode())
    assert size(effects)>SETUP_ANSWER_BYTES
    shown=shown_effects(effects)
    assert size(shown)<SETUP_ANSWER_BYTES//2
    assert all(set(unit)<=set(SHOWN_UNIT) and "environment" not in unit and "admission" not in unit
               for unit in shown["units"])
    assert [unit["sha256"] for unit in shown["units"]]==[unit["sha256"] for unit in effects["units"]]
    assert {key:value for key,value in shown.items() if key!="units"}=={
        key:value for key,value in effects.items() if key!="units"}
    assert "environment" in effects["units"][0]  # the rendered plan itself is left as it was
    assert shown_effects({"restart_bots":[]})=={"restart_bots":[]}


def test_preview_and_the_context_read_answer_units_without_their_working_data(setup_authority, monkeypatch):
    """Both answers a page can read the plan from are built by one function; the computer's own
    rendered plan stays whole."""
    from claudlobby import config_plan
    from claudlobby.plane.owner_setup import SETUP_ANSWER_BYTES, SHOWN_UNIT
    engine,grant,reader=allow(setup_authority)
    preview=engine.preview(reader,grant.generation,"Improve checkout tests")
    rendered=config_plan.read_rendered(engine.root,preview["config_fingerprint"])
    changes,effects=_real_sized_plan()
    whole=SimpleNamespace(fleets=rendered.fleets,changes=rendered.changes,effects={**rendered.effects,**effects})
    monkeypatch.setattr(config_plan,"read_rendered",lambda root,fingerprint:whole)
    request=binding(preview)
    answers=[engine._plan(grant,request,preview["expires_at"],"quiesce"),engine.context(reader)["preview"]]
    for answer in answers:
        assert len(answer["effects"]["units"])==70
        assert all(set(unit)<=set(SHOWN_UNIT) for unit in answer["effects"]["units"])
        assert len(json.dumps(answer).encode())<SETUP_ANSWER_BYTES
        assert answer["effects"]["restart_bots"]==effects["restart_bots"] and answer["dispatch"]=="prepared"
    assert "environment" in whole.effects["units"][0]



# The team card with the messages mark (04a) and its finish after a healthy Launch (04b1).
# The finish's reach beyond the authority database is a stand-in here; tests/test_host_owner.py
# runs the real registration and send.
ALIAS = "human:ada-lovelace"
FLEET_UID, ACTOR_UID, LEAD_UID = "fleet_" + "1" * 32, "actor_" + "2" * 32, "actor_" + "3" * 32


def _receipt(caller, recipient, text, status="submitted"):
    from claudlobby.message_payload import MessageBody
    from claudlobby.request_receipts import semantic_digest
    digest = semantic_digest({"body": MessageBody.from_input(text).text.encode("utf-8"), "kind": "chat"})
    return SimpleNamespace(operation="message.send", caller_uid=caller, recipient_uid=recipient, semantic_sha256=digest,
                           transmissions=(SimpleNamespace(transport=SimpleNamespace(status=status)),),
                           provenance={"carrier": "setup_finish"})


@pytest.mark.parametrize("by,provenance,expected", [
    ("computer", None, {"state": "unknown", "reason": "request_id_taken"}),
    ("computer", {"carrier": "connected"}, {"state": "unknown", "reason": "request_id_taken"}),
    ("computer", {"carrier": "setup_finish"}, {"state": "sent"}),
    ("page", None, {"state": "sent"}),
])
def test_computer_goal_requires_setup_finish_receipt_provenance(by, provenance, expected):
    from claudlobby.commands.host_owner_setup import _sent

    text = "Review this checkout"
    view = _receipt(ACTOR_UID, LEAD_UID, text)
    view.provenance = provenance
    value = {"by": by, "actor_uid": ACTOR_UID, "lead_uid": LEAD_UID}
    assert _sent(view, value, text) == expected


class FakeCard:
    def __init__(self, access, *, taken=(), fail_send=False):
        self.access, self.names, self.fail_send = access, set(taken), fail_send
        self.receipts, self.sent, self.registered = {}, [], []

    def taken(self):
        return set(self.names)

    def register(self, alias, check):
        check()  # As inside the registration's admission, just before the name is written.
        self.registered.append(alias)
        return {"owner": self.access.current_grant(), "fleet_uid": FLEET_UID, "actor_uid": ACTOR_UID, "lead_uid": LEAD_UID}

    def receipt(self, fleet_uid, request_id):
        assert fleet_uid == FLEET_UID
        return self.receipts.get(request_id)

    def send(self, alias, request_id, text, check):
        check(SimpleNamespace(caller=SimpleNamespace(uid=ACTOR_UID, alias=alias)),
              SimpleNamespace(peer=SimpleNamespace(uid=LEAD_UID)))
        if self.fail_send:
            raise OSError("synthetic native send failure before any receipt")
        self.sent.append((alias, request_id, text))
        self.receipts[request_id] = _receipt(ACTOR_UID, LEAD_UID, text)


def marked_setup(fixture, monkeypatch, alias=ALIAS):
    fixture = (fixture[0], fixture[1], replace(fixture[2], messages={"alias": alias}), *fixture[3:])
    return completed_setup(fixture, monkeypatch)


def finish(fixture, request, card, monkeypatch, *, consent=True):
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.context import resolve_paths
    monkeypatch.setattr(guide, "_CardEffects", lambda *_: card)
    access = fixture[1]
    return guide.run(SimpleNamespace(finish_setup=list(request.values()), target_fleet=None),
                     resolve_paths(root=access.root), access, None,
                     **({"consent": object()} if consent else {})).data


def preview_payload(access, grant):
    import sqlite3
    with sqlite3.connect(access.path) as conn:
        return json.loads(conn.execute("SELECT payload FROM setup_previews WHERE generation=?", (grant.generation,)).fetchone()[0])


def message_grants(access):
    return [(g.actor_uid, g.actor_alias) for g in access.local_action_status().message_grants]


def test_marked_approval_stores_the_name_and_a_malformed_mark_refuses_to_load(setup_authority):
    import sqlite3
    from claudlobby.plane.owner_setup import _snapshot
    store, access, grant, *_ = setup_authority
    marked = replace(grant, messages={"alias": ALIAS})
    assert json.loads(_snapshot(marked))["messages"] == {"alias": ALIAS}
    with pytest.raises(AccessDenied):
        store.allow(replace(grant, messages={"alias": "human:Ada"}))
    store.allow(marked)
    assert store.local() == marked
    for bad in ({"alias": "human:Ada"}, {"alias": ALIAS, "sender": "card"}, {"sender": "card"}, "human:ada"):
        value = json.loads(_snapshot(marked)); value["messages"] = bad
        with sqlite3.connect(access.path) as conn:
            conn.execute("UPDATE setup_grant SET snapshot=?", (json.dumps(value, sort_keys=True, separators=(",", ":")),))
        with pytest.raises(AccessUnavailable):
            store.local()


@pytest.mark.parametrize("marked", [True, False])
def test_preview_says_the_goal_is_sent_only_for_a_marked_approval(setup_authority, marked):
    store, access, grant, reader, *_ = setup_authority
    if marked:
        grant = replace(grant, messages={"alias": ALIAS})
    store.allow(grant)
    engine = SetupEngine(access)
    plan = engine.preview(reader, grant.generation, "Review requested local code")
    context = engine.context(reader)
    if marked:
        assert (plan["sends_goal"], plan["finish"], plan["goal"]) == (True, "page", "Review requested local code")
        assert context["sends_goal"] is True and context["preview"]["sends_goal"] is True
    else:
        assert not {"sends_goal", "finish", "goal"} & set(plan) and "sends_goal" not in context
        assert not {"sends_goal", "finish"} & set(context["preview"])


def test_page_finish_turns_messages_on_and_sends_the_goal_once(setup_authority, monkeypatch):
    from claudlobby.commands.host_owner_setup import card_status, goal_request_id
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    card = FakeCard(access, taken={"lead", "engineer"})
    first = finish(setup_authority, request, card, monkeypatch)
    expected = {"state": "allowed", "by": "page", "alias": ALIAS, "renamed": False, "goal": {"state": "sent"}}
    assert first == {"state": "setup_complete", "request_id": record["request_id"], "messages": expected}
    goal_id = goal_request_id(record["request_id"])
    assert card.sent == [(ALIAS, goal_id, "Review requested local code")] and goal_id != record["request_id"]
    assert message_grants(access) == [(ACTOR_UID, ALIAS)]
    # Again, as a reopened page would: the record answers, nothing is granted or sent again.
    assert finish(setup_authority, request, card, monkeypatch)["messages"] == expected
    assert len(card.sent) == 1 and card.registered == [ALIAS]
    status = card_status(access, OwnerSetup(access).retained_state())
    assert status == {"messages": expected, "goal": "Review requested local code", "finish": None}


def test_a_held_finish_lock_answers_in_progress_and_writes_nothing(setup_authority, monkeypatch):
    import fcntl, os
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    card = FakeCard(access)
    path = access.root / "state" / f"setup-finish-{record['request_id']}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # Another finish, blocked for as long as it likes.
        assert finish(setup_authority, request, card, monkeypatch)["messages"] == {"state": "in_progress", "by": "page"}
        assert "messages" not in preview_payload(access, grant) and not card.registered and not card.sent
    finally:
        os.close(fd)
    assert finish(setup_authority, request, card, monkeypatch)["messages"]["state"] == "allowed"
    assert len(card.sent) == 1


def test_finish_can_claim_lock_after_transient_status_probe(tmp_path, monkeypatch):
    import fcntl
    import os

    from claudlobby.commands import host_owner_setup as guide

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    request_id = str(uuid4())
    probe = os.open(state / f"setup-finish-{request_id}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        waits = []

        def end_probe(delay):
            waits.append(delay)
            fcntl.flock(probe, fcntl.LOCK_UN)

        monkeypatch.setattr(guide.time, "sleep", end_probe)
        with guide._finish_lock(tmp_path, request_id) as held:
            assert held
        assert len(waits) == 1 and 0 < waits[0] <= 0.02
    finally:
        os.close(probe)


def test_a_finish_that_died_after_choosing_its_label_resumes_with_it(setup_authority, monkeypatch):
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    card = FakeCard(access)
    card.register = lambda alias, check: (_ for _ in ()).throw(KeyboardInterrupt())  # Dies while registering.
    with pytest.raises(KeyboardInterrupt):
        finish(setup_authority, request, card, monkeypatch)
    assert preview_payload(access, grant)["messages"]["state"] == "in_progress"
    resumed = FakeCard(access, taken={"ada-lovelace"})  # Its own registration now reads as taken.
    answer = finish(setup_authority, request, resumed, monkeypatch)["messages"]
    assert (answer["state"], answer["alias"], answer["renamed"]) == ("allowed", ALIAS, False)
    assert resumed.registered == [ALIAS] and message_grants(access) == [(ACTOR_UID, ALIAS)]


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("change", ["revoke_setup", "re_pair"])
def test_a_withdrawn_or_re_paired_approval_registers_grants_and_sends_nothing(setup_authority, monkeypatch, change, resumed):
    from tests.test_plane_owner_workspace import OWNER as PRINCIPAL
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    if resumed:  # A finish died after choosing its label, before registering it.
        dying = FakeCard(access)
        dying.register = lambda alias, check: (_ for _ in ()).throw(KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt):
            finish(setup_authority, request, dying, monkeypatch)
    if change == "revoke_setup":
        OwnerSetup(access).revoke(grant)
    else:
        access.revoke_owner(expected_revision=access.current_grant().revision)
        access.confirm_pairing(access.begin_pairing(PRINCIPAL).token, expected_principal=PRINCIPAL)
    card = FakeCard(access)
    card.taken = lambda: pytest.fail("names were read for a withdrawn or re-paired approval")
    answer = finish(setup_authority, request, card, monkeypatch)["messages"]
    expected = ("revoked", "setup_withdrawn") if change == "revoke_setup" else ("stale", "stale_binding")
    assert (answer["state"], answer["reason"], answer["alias"]) == (*expected, ALIAS)
    assert answer["goal"] == {"state": "not_sent", "reason": expected[0]}
    assert not card.registered and not card.sent and message_grants(access) == []
    assert finish(setup_authority, request, card, monkeypatch)["messages"] == answer and not card.registered


@pytest.mark.parametrize("binding", ["as_approved", "rebound_before_finish"])
def test_finish_takes_no_website_account_and_stops_only_on_a_changed_binding(setup_authority, monkeypatch, binding):
    """The open question for the build (04a): core cannot tell which website account drives the guide.

    The finish takes no account input: whoever drives the guide over the computer's Tailscale user (a
    bare consent object here) gets exactly the card's effect under the card's name, for the binding the
    approval pinned. It refuses only when that binding changed (a re-pair or a re-bind). The website's
    name-card change adds the page-side rule (the page fills the name only for its own bound account)."""
    import inspect
    from claudlobby.commands.host_owner_setup import _card_messages
    assert set(inspect.signature(_card_messages).parameters) == {"paths", "store", "grant", "record", "by", "effects"}
    import sqlite3
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]

    def rebind():  # Another website account bound to this computer, as the binding row would read.
        with sqlite3.connect(access.path) as conn:
            conn.execute("UPDATE workspace_bindings SET subject=?", ("00000000-0000-4000-8000-00000000000b",))
    if binding == "rebound_before_finish":
        rebind()
        card = FakeCard(access)
        answer = finish(setup_authority, request, card, monkeypatch)["messages"]
        assert (answer["state"], answer["goal"]["state"]) == ("stale", "not_sent")
        assert not card.sent and message_grants(access) == []
        return
    card = FakeCard(access)
    answer = finish(setup_authority, request, card, monkeypatch)["messages"]
    assert (answer["state"], answer["alias"]) == ("allowed", ALIAS) and len(card.sent) == 1
    rebind()
    other = FakeCard(access)
    later = finish(setup_authority, request, other, monkeypatch)["messages"]
    assert later["state"] == "allowed", "a final record is never re-granted or re-sent"
    assert not other.sent and not other.registered


def test_messages_revoked_while_the_goal_is_pending_stops_the_send(setup_authority, monkeypatch):
    from claudlobby.commands.host_owner_setup import card_status
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    failing = FakeCard(access, fail_send=True)
    answer = finish(setup_authority, request, failing, monkeypatch)["messages"]
    assert answer["state"] == "allowed" and answer["goal"] == {"state": "pending", "reason": "send_unavailable"}
    access.revoke_messages(expected_owner=access.current_grant(), fleet_uid=FLEET_UID)
    card = FakeCard(access)
    later = finish(setup_authority, request, card, monkeypatch)["messages"]
    assert not card.sent and later["goal"] == {"state": "not_sent", "reason": "revoked"}
    assert (later["state"], later["reason"]) == ("revoked", "messages_revoked")
    assert card_status(access, OwnerSetup(access).retained_state())["messages"]["state"] == "revoked"


def test_a_goal_changed_after_launch_is_not_sent(setup_authority, monkeypatch):
    import sqlite3
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    payload = preview_payload(access, grant)
    payload["goal"] = "Delete the production database"
    with sqlite3.connect(access.path) as conn:
        conn.execute("UPDATE setup_previews SET payload=?", (json.dumps(payload),))
    card = FakeCard(access)
    answer = finish(setup_authority, request, card, monkeypatch)["messages"]
    assert answer["goal"] == {"state": "not_sent", "reason": "goal_changed"} and not card.sent


def test_a_receipt_under_the_goal_id_that_does_not_match_is_never_resent(setup_authority, monkeypatch):
    from claudlobby.commands.host_owner_setup import goal_request_id
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    card = FakeCard(access)
    card.receipts[goal_request_id(record["request_id"])] = _receipt("actor_" + "9" * 32, LEAD_UID, "Another body")
    for _ in range(2):
        answer = finish(setup_authority, request, card, monkeypatch)["messages"]
        assert answer["goal"] == {"state": "unknown", "reason": "request_id_taken"} and not card.sent


@pytest.mark.parametrize("alias,taken,expected", [
    ("human:ada-lovelace", {"ada-lovelace"}, "human:ada-lovelace-2"),
    ("human:default-user", {"default-user"}, "human:default-user-2"),
    ("human:" + "c" * 40, {"c" * 40}, "human:" + "c" * 38 + "-2"),
    ("human:ada-lovelace", {"ada-lovelace", *(f"ada-lovelace-{n}" for n in range(2, 10))}, None),
])
def test_a_taken_name_gets_a_number_or_name_taken(setup_authority, monkeypatch, alias, taken, expected):
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch, alias=alias)
    access = setup_authority[1]
    card = FakeCard(access, taken=taken)
    answer = finish(setup_authority, request, card, monkeypatch)["messages"]
    if expected is None:
        assert answer["state"] == "name_taken" and not card.registered and not card.sent and message_grants(access) == []
    else:
        assert (answer["state"], answer["alias"], answer["renamed"]) == ("allowed", expected, True)
        assert card.sent[0][0] == expected and message_grants(access) == [(ACTOR_UID, expected)]


def test_a_terminal_grant_for_this_team_is_kept_and_its_actor_never_reused(setup_authority, monkeypatch):
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    other = ("actor_" + "4" * 32, "human:someone-else")
    access.allow_messages(expected_owner=access.current_grant(), fleet_uid=FLEET_UID, actor_uid=other[0], actor_alias=other[1])
    card = FakeCard(access)
    answer = finish(setup_authority, request, card, monkeypatch)["messages"]
    assert (answer["state"], answer["alias"], answer["goal"]) == ("kept", ALIAS, {"state": "not_sent", "reason": "other_grant"})
    assert message_grants(access) == [other] and not card.sent and card.registered == [ALIAS]


def test_an_update_after_done_refuses_the_marked_finish(setup_authority, monkeypatch):
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    record["target"] = "f" * 40
    from claudlobby.commands import host_owner_setup as guide
    monkeypatch.setattr(guide, "_CardEffects", lambda *_: pytest.fail("a stale Launch reached the finish"))
    with pytest.raises((CommandFailure, ValueError, RuntimeError)):
        finish(setup_authority, request, None, monkeypatch)


@pytest.mark.parametrize("change", ["second_update", "update_started", "other_revision"])
def test_an_update_between_the_check_and_the_send_stops_the_send(setup_authority, monkeypatch, change):
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    retained = setup_authority[-1].retained
    card = FakeCard(access)
    send = card.send

    def later(alias, request_id, text, check):  # After the finish's first check, before its send.
        if change == "second_update":  # Another update finished healthy at the same revision.
            retained["record"] = {**retained["record"], "request_id": str(uuid4())}
        elif change == "update_started":
            retained["record"] = {**retained["record"], "phase": "quiesce"}
        else:  # The installed package moved to another revision; the update record did not.
            from claudlobby import resources
            monkeypatch.setattr(resources.get_resources(), "source_revision", "c" * 40)
        send(alias, request_id, text, check)
    card.send = later
    answer = finish(setup_authority, request, card, monkeypatch)["messages"]
    assert answer["state"] == "allowed" and answer["goal"] == {"state": "not_sent", "reason": "stale"}
    assert not card.sent and message_grants(access) == [(ACTOR_UID, ALIAS)]


def test_a_revoke_between_the_first_check_and_the_registration_registers_nothing(setup_authority, monkeypatch):
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    card = FakeCard(access)
    register = card.register

    def late(alias, check):  # The setup approval is revoked after the finish's first check.
        OwnerSetup(access).revoke(grant)
        return register(alias, check)
    card.register = late
    answer = finish(setup_authority, request, card, monkeypatch)["messages"]
    assert (answer["state"], answer["reason"], answer["alias"]) == ("revoked", "setup_withdrawn", ALIAS)
    assert not card.registered and not card.sent and message_grants(access) == []


def test_an_uncertain_send_is_reported_from_its_receipt_and_never_resent(setup_authority, monkeypatch):
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    card = FakeCard(access)
    send = card.send

    def uncertain(alias, request_id, text, check):  # The request is recorded, its transport unconfirmed.
        send(alias, request_id, text, check)
        card.receipts[request_id] = _receipt(ACTOR_UID, LEAD_UID, text, status="unknown")
        raise OSError("synthetic error after the effect")
    card.send = uncertain
    first = finish(setup_authority, request, card, monkeypatch)["messages"]
    assert first["goal"] == {"state": "unknown", "reason": "send_uncertain"} and len(card.sent) == 1
    again = finish(setup_authority, request, card, monkeypatch)["messages"]
    assert again["goal"] == {"state": "unknown", "reason": "send_uncertain"}
    assert len(card.sent) == 1, "the receipt answers the rerun; nothing is resent"


@pytest.mark.parametrize("when", ["before_the_send", "after_the_send"])
def test_an_unavailable_receipt_is_reported_and_never_counted_sent(setup_authority, monkeypatch, when):
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    card = FakeCard(access)
    lookups = []

    def receipt(fleet_uid, request_id):
        lookups.append(request_id)
        if when == "before_the_send" or len(lookups) > 1:
            raise OSError("synthetic receipt store failure")
        return None
    card.receipt = receipt
    answer = finish(setup_authority, request, card, monkeypatch)["messages"]
    if when == "before_the_send":
        assert answer["goal"] == {"state": "pending", "reason": "receipt_unavailable"} and not card.sent
    else:
        assert answer["goal"] == {"state": "unknown", "reason": "receipt_unavailable"} and len(card.sent) == 1


def test_a_team_without_a_lead_gets_messages_but_no_goal(setup_authority, monkeypatch):
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    card = FakeCard(access)
    register = card.register
    card.register = lambda alias, check: {**register(alias, check), "lead_uid": None}
    answer = finish(setup_authority, request, card, monkeypatch)["messages"]
    assert (answer["state"], answer["goal"]) == ("allowed", {"state": "not_sent", "reason": "no_lead"})
    assert not card.sent and message_grants(access) == [(ACTOR_UID, ALIAS)]


def test_a_finish_typed_at_the_terminal_is_recorded_by_terminal(setup_authority, monkeypatch):
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    card = FakeCard(access)
    answer = finish(setup_authority, request, card, monkeypatch, consent=False)["messages"]
    assert answer == {"state": "allowed", "by": "terminal", "alias": ALIAS, "renamed": False, "goal": {"state": "sent"}}
    assert len(card.sent) == 1 and message_grants(access) == [(ACTOR_UID, ALIAS)]


def test_the_card_carries_the_mark_only_while_its_finish_exists(monkeypatch):
    from claudlobby.commands import host_owner, host_owner_setup
    assert host_owner._card_finish_ready() is True
    monkeypatch.delattr(host_owner_setup, "_card_messages")
    assert host_owner._card_finish_ready() is False


# The link approval (setup_link): one yes on a started computer from the newer setup, which has no
# team tables, to Claude sign-ins for a short time. Only the provider is synthetic, as in
# synthetic_signin. A second OwnerAccess on the same root stands for a second process (the guide's):
# a flock belongs to the open file, so a second open in this process meets the lease as another
# process would; the tests that need a real second process start one.

def _link_provider(access,monkeypatch,tmp_path):
    """synthetic_signin's provider, for a computer the team approval never touched."""
    import hashlib,sys
    from claudlobby.plane import provider_signin
    access.root.chmod(0o700)
    env=access.root/'.env'; env.write_text('export ROUTE=private\n'); env.chmod(0o600)
    (access.root/'state/runtime-use.lock').touch(mode=0o600)
    profile=Path.home()/'.claude'; profile.mkdir(mode=0o700,exist_ok=True)
    program=tmp_path/'synthetic-provider'
    url=('https://claude.com/cai/oauth/authorize?code=true&client_id=test&response_type=code'
         '&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback'
         '&scope=user%3Ainference&code_challenge='+'a'*43+'&code_challenge_method=S256&state='+'b'*32)
    token='sk-ant-oat01-'+'s'*80
    program.write_text(f'''#!{sys.executable}
import os,sys,tty
tty.setraw(0)
sys.stdout.write('\\x1b]8;;'+{url!r}+'\\x07Open Claude\\x1b]8;;\\x07\\r\\n');sys.stdout.flush()
typed=b''
while not typed.endswith(b'\\r'):
    ch=os.read(0,1)
    if not ch or ch==b'\\n':sys.exit(3)
    typed+=ch
assert typed[:-1].decode()=='accepted#'+'b'*32
sys.stdout.write('\\x1b[2K\\r✓ Long-lived authentication token created successfully!\\r\\nYour OAuth token (valid for 1 year):\\r\\n'+{token!r}+'\\r\\nStore this token securely. You won\\\'t be able to see it again.\\r\\n');sys.stdout.flush()
''')
    program.chmod(0o700)
    monkeypatch.setattr(provider_signin,'SUPPORTED',{hashlib.sha256(program.read_bytes()).hexdigest():'2.1.325'})
    return provider_signin.approved_scope(program,access.root,str(profile)),program,token,env


@pytest.fixture
def link_signin(setup_authority,monkeypatch,tmp_path):
    """A started computer with no team tables and one link approval, written as the computer run writes it."""
    from claudlobby.plane import owner_source
    store,access,grant,reader,clock,args=setup_authority
    monkeypatch.setattr(owner_source,'start_state',lambda root,installation:'started')
    scope,program,token,env=_link_provider(access,monkeypatch,tmp_path)
    engine=SetupEngine(access)
    approval=engine.signin.link.write(scope)
    try:
        yield SimpleNamespace(engine=engine,approval=approval,reader=reader,program=program,token=token,env=env,
                              scope=scope,clock=clock,access=access,team=grant,args=args)
    finally:
        engine.signin.shutdown()


def _second_process(link):
    """Another process's store on the same computer, as the guide's run opens it."""
    from claudlobby.plane.provider_signin import LinkStore
    return LinkStore(OwnerAccess(link.access.root,clock=link.access._clock))


def _link_row(access):
    with access._connection() as conn:
        names={row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        return conn.execute('SELECT snapshot FROM setup_link').fetchone()[0] if 'setup_link' in names else None


def _attempts(access):
    with access._connection() as conn:
        names={row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        return [dict(row) for row in conn.execute('SELECT * FROM setup_provider')] if 'setup_provider' in names else []


def _lease(access):
    from claudlobby.plane.provider_signin import signin_running
    return signin_running(access.root)


def test_link_record_holds_scope_and_revision(link_signin):
    from claudlobby.plane.owner_connected import OwnerConnected
    from claudlobby.plane.owner_setup import managed_setup, _uuid
    from claudlobby.plane.provider_signin import LINK_SECONDS, LINK_UNUSED_SECONDS, link_approval, link_snapshot
    link=link_signin; approval=link.approval; access=link.access
    package=managed_setup(access.root)
    with access._connection() as conn:
        binding=access.workspace()._active_binding(conn,access._grant(conn).principal)
        names={row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert _uuid(approval.generation) and approval.provider_signin==link.scope
    assert approval.source_revision==package.source_revision and approval.installation==str(package.installation.directory)
    assert approval.binding_snapshot==OwnerConnected._snapshot(binding)
    assert (approval.created_at,approval.unused_until,approval.expires_at)==(link.clock[0],link.clock[0]+900,link.clock[0]+1500)
    assert (LINK_UNUSED_SECONDS,LINK_SECONDS)==(900,1500)
    assert _link_row(access)==link_snapshot(approval) and link_approval(_link_row(access))==approval
    assert json.loads(_link_row(access)).keys()=={'generation','binding_snapshot','installation','source_revision',
        'provider_signin','created_at','unused_until','expires_at'}
    # Its own table: no team table is made, and no attempt is written.
    assert not names & {'setup_metadata','setup_grant','setup_previews'} and _attempts(access)==[]
    assert link.engine.signin.link.authorize(link.reader)==approval


def test_link_signin_refused_without_link_approval(setup_authority,monkeypatch,tmp_path):
    from claudlobby.plane import owner_source
    from claudlobby.plane.provider_signin import LinkStore
    store,access,team,reader,clock,_=setup_authority
    monkeypatch.setattr(owner_source,'start_state',lambda root,installation:'started')
    scope,*_=_link_provider(access,monkeypatch,tmp_path)
    engine=SetupEngine(access)
    def refused(generation,expected):
        before=access.path.read_bytes()
        for call in (lambda a:engine.signin_begin(reader,generation,a),lambda a:engine.signin_status(reader,generation,a),
                     lambda a:engine.signin_code(reader,generation,a,'accepted#'+'b'*32),lambda a:engine.signin_cancel(reader,generation,a)):
            with pytest.raises((AccessDenied,AccessUnavailable)) as raised: call(str(uuid4()))
            assert (raised.type is AccessUnavailable if expected is AccessUnavailable else raised.value.code==expected)
        assert access.path.read_bytes()==before and engine.signin._thread is None
        assert not (access.root/'state/provider-signin.lock').exists() and not (access.root/'state/provider-signin').exists()
    try:
        refused(str(uuid4()),AccessUnavailable)  # no link approval: today's team call, which has no team tables to read
        first=LinkStore(access).write(scope)
        refused(team.generation,'setup_denied')  # a team approval's generation (never allowed here)
        second=LinkStore(access).write(scope)
        assert second.generation!=first.generation
        refused(first.generation,'setup_denied')  # a link approval that a later yes replaced
        aged={**json.loads(_link_row(access))}  # the same approval, made LINK_SECONDS ago: its end is now
        for key in ('created_at','unused_until','expires_at'): aged[key]-=1500
        with access._connection(write=True) as conn: conn.execute('UPDATE setup_link SET snapshot=?',(json.dumps(aged),))
        refused(second.generation,'setup_denied')  # past its end
        assert _attempts(access)==[]
    finally:engine.signin.shutdown()


@pytest.mark.parametrize('team_tables',[False,True])
def test_link_approval_cannot_preview_or_launch(link_signin,team_tables):
    from claudlobby.plane.owner_setup import _SCHEMA
    link=link_signin; engine=link.engine; generation=link.approval.generation
    if team_tables:  # the team tables, with no team approval in them: the link approval still admits
        with link.access._connection(write=True) as conn:
            for statement in _SCHEMA: conn.execute(statement)
        assert engine.signin.link.authorize(link.reader)==link.approval
    before=link.access.path.read_bytes()
    request={'generation':generation,'request_id':str(uuid4()),'config_fingerprint':'c'*64,'source_revision':link.approval.source_revision}
    for call in (lambda:engine.preview(link.reader,generation,'Improve checkout tests'),lambda:engine.launch(link.reader,**request),
                 lambda:engine.receipt(link.reader,**request),lambda:engine.context(link.reader)):
        with pytest.raises((AccessDenied,AccessUnavailable)) as raised: call()
        # No team tables: the team admission cannot read them. With them: it finds no team approval.
        assert (raised.value.code=='setup_not_allowed' if team_tables else raised.type is AccessUnavailable),raised.value
    assert link.access.path.read_bytes()==before and not link.args.dispatches and not (link.access.root/'local').exists()


def test_link_begin_refused_after_update(link_signin,monkeypatch):
    from claudlobby import resources
    link=link_signin
    package=resources.get_resources()
    monkeypatch.setattr(resources,'get_resources',lambda *a,**k:SimpleNamespace(installation=package.installation,source_revision='c'*40))
    with pytest.raises(AccessDenied) as raised: link.engine.signin_begin(link.reader,link.approval.generation,str(uuid4()))
    assert raised.value.code=='setup_source_changed'
    assert _attempts(link.access)==[] and link.engine.signin._thread is None and not _lease(link.access)


def test_link_begin_refused_when_binary_changes(link_signin):
    link=link_signin
    link.program.write_text('#!/bin/sh\nexit 0\n')
    with pytest.raises(ValueError): link.engine.signin_begin(link.reader,link.approval.generation,str(uuid4()))
    assert _attempts(link.access)==[] and link.engine.signin._thread is None and not _lease(link.access)


def test_link_write_refused_while_signin_live(link_signin):
    from claudlobby.plane.provider_signin import LinkStore
    link=link_signin; attempt=str(uuid4())
    link.engine.signin_begin(link.reader,link.approval.generation,attempt)
    _signin_until(link.engine,link.approval,link.reader,attempt,'waiting')
    before=_link_row(link.access)
    with pytest.raises(AccessDenied) as raised: LinkStore(link.access).write(link.scope)
    assert raised.value.code=='signin_live' and _link_row(link.access)==before
    assert _signin_until(link.engine,link.approval,link.reader,attempt,'waiting')['url']


def test_second_link_request_during_live_signin_resumes_and_replaces_nothing_across_processes(link_signin,monkeypatch):
    import subprocess,sys
    from claudlobby.plane import owner_setup
    monkeypatch.setattr(owner_setup,'_linux',lambda:True)
    link=link_signin; engine=link.engine; attempt=str(uuid4())
    engine.signin_begin(link.reader,link.approval.generation,attempt)  # the lasting service, process 1
    _signin_until(engine,link.approval,link.reader,attempt,'waiting')
    before=_link_row(link.access)
    # A real second process sees the sign-in as live, with nothing of process 1's memory.
    probe=subprocess.run([sys.executable,'-c','import sys;from pathlib import Path;'
        'from claudlobby.plane.provider_signin import signin_running;print(signin_running(Path(sys.argv[1])))',
        str(link.access.root)],capture_output=True,text=True,timeout=60)
    assert probe.returncode==0 and probe.stdout.strip()=='True',probe.stderr
    with pytest.raises(AccessDenied) as raised: _second_process(link).write(link.scope)  # the guide's run, its own store
    assert raised.value.code=='signin_live'
    assert _link_row(link.access)==before, 'the approval in use is byte-identical'
    engine.signin.link.authorize(link.reader,expected=link.approval)  # process 1's next expected= check
    resumed=engine.signin_status(link.reader,link.approval.generation,attempt)
    assert resumed['state']=='waiting' and resumed['url'], 'the view resumes the same live attempt'
    part=engine.state(link.reader)['provider']
    assert (part['state'],part['link'],part['signin']['phase'],part['signin']['attempt_id'])==('not_linked','available','waiting',attempt)
    assert [row['attempt_id'] for row in _attempts(link.access)]==[attempt]


def test_stale_lease_after_crash_does_not_block(link_signin):
    import signal,subprocess,sys
    from claudlobby.plane.provider_signin import _lease_path
    link=link_signin; path=_lease_path(link.access.root)
    holder=subprocess.Popen([sys.executable,'-c','import fcntl,os,sys,time\n'
        'fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600);fcntl.flock(fd,fcntl.LOCK_EX)\n'
        'print("held",flush=True);time.sleep(120)',str(path)],stdout=subprocess.PIPE,text=True)
    try:
        assert holder.stdout.readline().strip()=='held'
        assert _lease(link.access) is True
        with pytest.raises(AccessDenied,match='signin_live'): _second_process(link).write(link.scope)
    finally:
        holder.send_signal(signal.SIGKILL); holder.wait(timeout=30); holder.stdout.close()
    assert path.is_file() and _lease(link.access) is False, 'the file stays and reads as free'
    approval=_second_process(link).write(link.scope)  # a new link write succeeds
    attempt=str(uuid4())
    assert link.engine.signin_begin(link.reader,approval.generation,attempt)['attempt_id']==attempt  # and a new begin
    _signin_until(link.engine,approval,link.reader,attempt,'waiting')
    assert _lease(link.access) is True and path.read_text()==attempt+'\n'


def test_quiet_child_keeps_lease_until_process_exit(link_signin,monkeypatch):
    import os,signal,subprocess
    from claudlobby.plane import provider_signin
    link=link_signin; engine=link.engine
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
        engine.signin_begin(link.reader,link.approval.generation,attempt)
        ended=_signin_ended(engine,link.approval,link.reader,attempt)
        assert (ended['state'],ended['reason'])==('unknown','child_quiet_unproven')
        assert _lease(link.access) is True and len(engine.signin._kept)==1, 'its worker keeps the lease'
        before=_link_row(link.access)
        with pytest.raises(AccessDenied) as raised: _second_process(link).write(link.scope)
        assert raised.value.code=='signin_unconfirmed' and _link_row(link.access)==before  # not a live sign-in to resume
        with pytest.raises(AccessDenied) as raised: engine.signin_begin(link.reader,link.approval.generation,str(uuid4()))
        assert raised.value.code=='signin_unconfirmed' and len(_attempts(link.access))==1  # and begin, before its retry rule
        for fd in engine.signin._kept: os.close(fd)  # process 1 exits: the system closes its files
        engine.signin._kept.clear()
        assert _lease(link.access) is False
        assert _second_process(link).write(link.scope).generation!=link.approval.generation
    finally:
        for child in children:
            try: os.killpg(child.pid,signal.SIGKILL)
            except (ProcessLookupError,PermissionError): pass
            child.wait(timeout=5)


def test_begin_takes_lease_inside_transaction(link_signin,monkeypatch):
    from claudlobby.plane.provider_signin import LinkStore
    link=link_signin; engine=link.engine
    # A link write that lands between begin's admission and its transaction: begin finds its
    # approval replaced, refuses and inserts nothing, and leaves the lease free.
    admit=engine.signin._admit; landed=[]
    def admit_then_write(reader,grant,**kwargs):
        admit(reader,grant,**kwargs)
        if not landed: landed.append(LinkStore(link.access).write(link.scope))
    monkeypatch.setattr(engine.signin,'_admit',admit_then_write)
    with pytest.raises(AccessDenied) as raised: engine.signin_begin(link.reader,link.approval.generation,str(uuid4()))
    assert raised.value.code=='setup_denied' and landed and _attempts(link.access)==[]
    assert engine.signin._thread is None and _lease(link.access) is False
    monkeypatch.setattr(engine.signin,'_admit',admit)
    # A link write after begin's commit sees the lease held: never a starting row with a free lease.
    approval=landed[0]; attempt=str(uuid4())
    seen=[]
    insert=engine.signin._begin
    def observed(reader,grant,attempt):
        result=insert(reader,grant,attempt)
        seen.append((_lease(link.access),[row['state'] for row in _attempts(link.access)]))
        return result
    monkeypatch.setattr(engine.signin,'_begin',observed)
    engine.signin_begin(link.reader,approval.generation,attempt)
    assert seen==[(True,['starting'])], 'committed with the lease already held'
    with pytest.raises(AccessDenied,match='signin_live'): LinkStore(link.access).write(link.scope)
    assert _link_row(link.access)==provider_snapshot(approval)


def provider_snapshot(approval):
    from claudlobby.plane.provider_signin import link_snapshot
    return link_snapshot(approval)


@pytest.mark.parametrize('kind',['symlink','owner','second_link','mode'])
def test_lease_file_not_private_refuses(link_signin,monkeypatch,kind):
    import os
    from claudlobby.plane import provider_signin
    link=link_signin; path=provider_signin._lease_path(link.access.root)
    other=path.with_name('elsewhere.lock'); other.touch(mode=0o600)
    if kind=='symlink': path.symlink_to(other)
    elif kind=='second_link': os.link(other,path)
    else:
        path.touch(mode=0o600)
        if kind=='mode': path.chmod(0o644)
    with monkeypatch.context() as patch:
        if kind=='owner':
            uid=os.geteuid(); patch.setattr(provider_signin.os,'geteuid',lambda:uid+1)
        for call in (lambda:provider_signin.signin_running(link.access.root),lambda:provider_signin.take_lease(link.access.root,str(uuid4())),
                     lambda:_second_process(link).write(link.scope),
                     lambda:link.engine.signin_begin(link.reader,link.approval.generation,str(uuid4()))):
            with pytest.raises(AccessUnavailable): call()
    assert _attempts(link.access)==[] and link.engine.signin._thread is None


def test_link_worker_loop_reads_no_start_state(link_signin,monkeypatch):
    # "Started" is checked when the approval is written and when a sign-in begins, never by authorize,
    # which the worker's loop calls about every 0.1 s.
    from claudlobby.plane import owner_source
    link=link_signin; engine=link.engine; attempt=str(uuid4())
    def start_state(root,installation): raise AssertionError('start_state read on the sign-in path')
    engine.signin_begin(link.reader,link.approval.generation,attempt)  # begin may read it
    monkeypatch.setattr(owner_source,'start_state',start_state)
    engine.signin.link.authorize(link.reader,expected=link.approval)
    _signin_until(engine,link.approval,link.reader,attempt,'waiting')  # the worker's loop admits about every 0.1 s
    engine.signin_code(link.reader,link.approval.generation,attempt,'accepted#'+'b'*32)
    assert _signin_until(engine,link.approval,link.reader,attempt,'connected')['state']=='connected'
    assert link.env.read_text()=='export ROUTE=private\n'+f"export CLAUDE_CODE_OAUTH_TOKEN='{link.token}'\n"
    with pytest.raises(AssertionError,match='start_state'): _second_process(link).write(link.scope)  # the write reads it


def _bad_rows():
    good=lambda link:json.loads(_link_row(link.access))
    def change(**fields): return lambda link:{**good(link),**fields}
    def drop(key): return lambda link:{k:v for k,v in good(link).items() if k!=key}
    def offsets(unused,end): return lambda link:{**good(link),'unused_until':good(link)['created_at']+unused,'expires_at':good(link)['created_at']+end}
    return {'missing key':drop('expires_at'),'extra key':change(account='x'),'generation not a UUID':change(generation='not-a-uuid'),
            'unused 899':offsets(899,1500),'end 1501':offsets(900,1501),'time NaN':change(created_at=float('nan')),
            'time a boolean':change(created_at=True),'bad scope':lambda link:{**good(link),'provider_signin':{**good(link)['provider_signin'],'sha256':'0'*64}},
            'revision not hex':change(source_revision='g'*40),'not an object':lambda link:[1]}


@pytest.mark.parametrize('case',sorted(_bad_rows()))
def test_link_record_rejects_bad_rows(link_signin,case):
    from claudlobby.plane.provider_signin import LinkStore
    link=link_signin
    value=_bad_rows()[case](link)
    with link.access._connection(write=True) as conn:
        conn.execute('UPDATE setup_link SET snapshot=? WHERE singleton=1',(json.dumps(value),))
    with link.access._connection() as conn:
        with pytest.raises(AccessUnavailable): LinkStore._read(conn)
    with pytest.raises(AccessUnavailable): link.engine.signin.link.authorize(link.reader)
    with pytest.raises(AccessUnavailable): link.engine.signin_begin(link.reader,link.approval.generation,str(uuid4()))
    assert _attempts(link.access)==[]


def test_link_record_rejects_a_second_row(link_signin):
    from claudlobby.plane.provider_signin import LinkStore
    link=link_signin; row=_link_row(link.access)
    with link.access._connection(write=True) as conn:  # a table without the singleton check, as no core writes it
        conn.execute('DROP TABLE setup_link')
        conn.execute('CREATE TABLE setup_link(singleton INTEGER,snapshot TEXT)')
        conn.executemany('INSERT INTO setup_link VALUES(?,?)',[(1,row),(2,row)])
    with link.access._connection() as conn:
        with pytest.raises(AccessUnavailable): LinkStore._read(conn)


L_STEPS=['L1','L2','L3','L4','L5','L6','L7']


def _refuse(code):
    def refuse(*_): raise AccessDenied(code)
    return refuse


@pytest.mark.parametrize('row',['L1','L2','L3','L4','L5','L6','L6b','L7'])
def test_link_refusal_order_writer(link_signin,monkeypatch,row):
    import os
    from claudlobby.plane import owner_setup, owner_source, provider_signin
    from claudlobby.plane.owner_workspace import OwnerWorkspace
    from claudlobby.plane.provider_signin import LinkStore, ProviderSignin, take_lease
    link=link_signin; access=OwnerAccess(link.access.root,clock=link.access._clock)
    before=_link_row(link.access); held=None
    log=[]
    def wrap(name,owner,attribute,replace=None,static=False):
        original=getattr(owner,attribute)
        def step(*args,**kwargs):
            if name not in log: log.append(name)  # each check by its first run (a check may read through another)
            return (replace or original)(*args,**kwargs)
        monkeypatch.setattr(owner,attribute,staticmethod(step) if static else step)
    expected={'L1':'setup_denied','L2':'workspace_binding_required','L3':'setup_source_changed','L4':'use_full_guide',
              'L5':'not_started','L6':'signin_live','L6b':'signin_unconfirmed'}.get(row)
    if row=='L4':
        OwnerSetup(link.access).allow(link.team)
    if row=='L6': held=take_lease(link.access.root,str(uuid4()))
    if row=='L6b':
        held=take_lease(link.access.root,str(uuid4()))
        with link.access._connection(write=True) as conn:
            ProviderSignin.initialize(conn)
            conn.execute('INSERT INTO setup_provider VALUES(?,?,?,?,?,?,NULL)',(str(uuid4()),link.approval.generation,'unknown','child_quiet_unproven',0,0))
    if row=='L7':
        with link.access._connection(write=True) as conn:
            ProviderSignin.initialize(conn)
            conn.execute('INSERT INTO setup_provider VALUES(?,?,?,?,?,?,NULL)',(str(uuid4()),link.approval.generation,'waiting',None,0,0))
    rows_before=_attempts(link.access)
    wrap('L1',access,'_grant',(lambda conn:None) if row=='L1' else None)
    wrap('L2',OwnerWorkspace,'_active_binding',_refuse('workspace_binding_required') if row=='L2' else None)
    wrap('L3',owner_setup,'managed_setup',_refuse('setup_source_changed') if row=='L3' else None)
    wrap('L4',provider_signin,'team_retained')
    wrap('L5',owner_source,'start_state',(lambda root,installation:'fresh') if row=='L5' else None)
    wrap('L6',provider_signin,'signin_running')
    wrap('L7',ProviderSignin,'initialize',static=True)
    try:
        if expected is None:
            assert LinkStore(access).write(link.scope).generation!=link.approval.generation
            assert log==L_STEPS and _attempts(link.access)==rows_before, 'the ended row is left as it is'
            return
        with pytest.raises(AccessDenied) as raised: LinkStore(access).write(link.scope)
        assert raised.value.code==expected
        upto=L_STEPS.index(row[:2])+1
        assert log==L_STEPS[:upto], 'every earlier check ran and passed, none after it'
        assert _link_row(link.access)==before and _attempts(link.access)==rows_before, 'a refusal writes nothing'
    finally:
        if held is not None: os.close(held)


A_STEPS=['A1','A3','A5']


@pytest.mark.parametrize('row',['A1','A2','A3','A4 binding','A4 time','A4 expected','A5'])
def test_link_refusal_order_admission(link_signin,monkeypatch,row):
    from dataclasses import replace
    from claudlobby.plane import provider_signin
    link=link_signin; store=link.engine.signin.link; log=[]
    def wrap(name,owner,attribute,replacement=None,static=False):
        original=getattr(owner,attribute)
        def step(*args,**kwargs):
            if name not in log: log.append(name)  # each check by its first run (a check may read through another)
            return (replacement or original)(*args,**kwargs)
        monkeypatch.setattr(owner,attribute,staticmethod(step) if static else step)
    if row=='A2':
        with link.access._connection(write=True) as conn: conn.execute('DELETE FROM setup_link')
    if row=='A3':
        with link.access._connection(write=True) as conn: conn.execute("UPDATE setup_link SET snapshot='{}'")
    if row=='A4 time':  # the approval made LINK_SECONDS ago; the session stays live
        aged=json.loads(_link_row(link.access))
        for key in ('created_at','unused_until','expires_at'): aged[key]-=1500
        with link.access._connection(write=True) as conn: conn.execute('UPDATE setup_link SET snapshot=?',(json.dumps(aged),))
    if row=='A4 binding':
        changed=json.loads(_link_row(link.access)); changed['binding_snapshot']='{}'
        with link.access._connection(write=True) as conn: conn.execute('UPDATE setup_link SET snapshot=?',(json.dumps(changed),))
    if row=='A5': OwnerSetup(link.access).allow(link.team)
    wrap('A1',store.connected,'_admit',_refuse('connected_session_denied') if row=='A1' else None)
    wrap('A3',provider_signin.LinkStore,'_read',static=True)
    wrap('A5',provider_signin,'team_retained')
    rows_before=_attempts(link.access)
    kwargs={'expected':replace(link.approval,expires_at=link.approval.expires_at+1)} if row=='A4 expected' else {}
    with pytest.raises((AccessDenied,AccessUnavailable)) as raised: store.authorize(link.reader,**kwargs)
    code=getattr(raised.value,'code',None)
    assert code==({'A1':'connected_session_denied','A2':'setup_not_allowed','A3':None,'A5':'setup_denied'}.get(row,'setup_denied'))
    assert log==A_STEPS[:{'A1':1,'A2':2,'A3':2,'A5':3}.get(row,2)]
    assert _attempts(link.access)==rows_before


def test_older_core_reads_database_with_link_table(link_signin):
    link=link_signin
    assert _link_row(link.access) is not None
    store=OwnerSetup(link.access)  # today's reads, which look only at the three team tables
    assert store.retained_state().state=='absent' and store.retained_state().grant is None and store.local() is None


def test_team_signin_never_opens_lease(synthetic_signin,monkeypatch):
    # The lease is the link path's only: a team sign-in never opens, probes or takes it, and keeps none.
    from claudlobby.plane import provider_signin
    engine,grant,reader,*_=synthetic_signin
    attempt=str(uuid4())
    def never(*args,**kwargs): raise AssertionError('the team sign-in reached the sign-in lease')
    for name in ('_lease_path','take_lease','signin_running'): monkeypatch.setattr(provider_signin,name,never)
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        _signin_until(engine,grant,reader,attempt,'waiting')
        assert not (engine.root/'state/provider-signin.lock').exists()
        assert engine.signin.store_for(grant) is engine.store
        engine.signin_code(reader,grant.generation,attempt,'accepted#'+'b'*32)
        assert _signin_until(engine,grant,reader,attempt,'connected')['state']=='connected'
        assert engine.signin._kept==[] and not (engine.root/'state/provider-signin.lock').exists()
    finally:engine.signin.shutdown()


def test_worker_closes_lease_after_leaving_lock(link_signin,monkeypatch):
    # The worker closes the lease after its last row and outside self._lock, so a begin that takes the
    # lock the moment the worker leaves it (a "Try again") takes the lease within its tries, not refused.
    import os,time
    from claudlobby.plane import provider_signin
    link=link_signin; engine=link.engine; signin=engine.signin; first=str(uuid4()); second=str(uuid4())
    leases=set(); seen=[]
    take=provider_signin.take_lease
    def taken(root,attempt):
        fd=take(root,attempt)
        if fd is not None: leases.add(fd)
        return fd
    close=os.close
    def closing(fd):
        if fd in leases:
            leases.discard(fd)
            rows={row['attempt_id']:(row['state'],row['reason']) for row in _attempts(link.access)}
            seen.append((signin._lock._is_owned(),rows.get(first)))
            time.sleep(0.2)  # a begin arriving now must not wait on this close
        return close(fd)
    monkeypatch.setattr(provider_signin,'take_lease',taken)
    monkeypatch.setattr(provider_signin.os,'close',closing)
    engine.signin_begin(link.reader,link.approval.generation,first)
    _signin_until(engine,link.approval,link.reader,first,'waiting')
    signin._cancel.set()  # the worker ends cancelled/effect_uncertain, no code sent: a retryable ending
    deadline=time.monotonic()+8
    while [row['state'] for row in _attempts(link.access)]!=['cancelled']:
        assert time.monotonic()<deadline; time.sleep(0.005)
    assert engine.signin_begin(link.reader,link.approval.generation,second)['attempt_id']==second
    assert seen[:1]==[(False,('cancelled','effect_uncertain'))], 'closed outside the lock, after the last row'
    assert _signin_until(engine,link.approval,link.reader,second,'waiting')['url']
    assert _lease(link.access) is True and provider_signin._lease_path(link.access.root).read_text()==second+'\n'


def test_link_try_again_after_timeout_same_approval(link_signin,monkeypatch):
    # Try again: a begin right after a timed-out attempt, under the same approval, is admitted.
    import time
    from claudlobby.plane import provider_signin
    link=link_signin; engine=link.engine; first=str(uuid4()); second=str(uuid4())
    monkeypatch.setattr(provider_signin,'SECONDS',0.5)  # no code can be taken: it times out at once
    engine.signin_begin(link.reader,link.approval.generation,first)
    deadline=time.monotonic()+8
    while [(row['state'],row['reason']) for row in _attempts(link.access)]!=[('needs_attention','timeout')]:
        assert time.monotonic()<deadline; time.sleep(0.005)
    monkeypatch.setattr(provider_signin,'SECONDS',600)
    assert engine.signin_begin(link.reader,link.approval.generation,second)['attempt_id']==second
    assert _signin_until(engine,link.approval,link.reader,second,'waiting')['url']
    rows={row['attempt_id']:row for row in _attempts(link.access)}
    assert (rows[first]['retired'],rows[second]['retired'])==(1,0) and rows[second]['generation']==link.approval.generation
    assert _lease(link.access) is True and _link_row(link.access)==provider_snapshot(link.approval), 'the same approval, unchanged'



# Begin under a link approval (C1b): spent, the unused window, started, the ended rule, the cap per
# approval, the service's own stop and the parts read's provider.signin.

def _age(link,seconds):
    """The stored approval, made ``seconds`` earlier (its session stays live): returns the new record."""
    from claudlobby.plane.provider_signin import link_approval
    aged=json.loads(_link_row(link.access))
    for key in ('created_at','unused_until','expires_at'): aged[key]-=seconds
    with link.access._connection(write=True) as conn: conn.execute('UPDATE setup_link SET snapshot=?',(json.dumps(aged),))
    return link_approval(_link_row(link.access))


def _force_approval(link):
    """A new link approval written as the writer would, but whatever the lease (to test begin alone)."""
    from dataclasses import replace
    from claudlobby.plane.provider_signin import link_snapshot
    now=link.access._now()
    approval=replace(link.approval,generation=str(uuid4()),created_at=now,unused_until=now+900,expires_at=now+1500)
    with link.access._connection(write=True) as conn:
        conn.execute('UPDATE setup_link SET snapshot=? WHERE singleton=1',(link_snapshot(approval),))
    return approval


def _put_row(access,generation,state,reason=None,code=0,retired=0,attempt=None):
    from claudlobby.plane.provider_signin import ProviderSignin
    attempt=attempt or str(uuid4())
    with access._connection(write=True) as conn:
        ProviderSignin.initialize(conn)
        if not retired: conn.execute('UPDATE setup_provider SET retired=1')
        conn.execute('INSERT INTO setup_provider VALUES(?,?,?,?,?,?,NULL)',(attempt,generation,state,reason,code,retired))
    return attempt


def _second_engine(link):
    return SetupEngine(OwnerAccess(link.access.root,clock=link.access._clock))


def test_link_spent_when_row_exists(link_signin):
    link=link_signin; engine=link.engine; first=str(uuid4()); second=str(uuid4())
    record=_link_row(link.access)
    engine.signin_begin(link.reader,link.approval.generation,first)
    assert [row['generation'] for row in _attempts(link.access)]==[link.approval.generation], 'spent: a row has its generation'
    assert _link_row(link.access)==record, 'nothing in the record changes'
    _signin_until(engine,link.approval,link.reader,first,'waiting')
    assert engine.signin_cancel(link.reader,link.approval.generation,first)['retryable'] is True
    _age(link,901)  # past its unused window: a spent approval is no longer held to it
    assert engine.signin_begin(link.reader,link.approval.generation,second)['attempt_id']==second


def test_link_refused_begin_does_not_spend(link_signin,monkeypatch):
    from claudlobby.plane import owner_source
    link=link_signin; engine=link.engine
    monkeypatch.setattr(owner_source,'start_state',lambda root,installation:'fresh')
    with pytest.raises(AccessDenied,match='not_started'): engine.signin_begin(link.reader,link.approval.generation,str(uuid4()))
    assert _attempts(link.access)==[] and engine.signin._thread is None and _lease(link.access) is False
    monkeypatch.setattr(owner_source,'start_state',lambda root,installation:'started')
    attempt=str(uuid4())
    assert engine.signin_begin(link.reader,link.approval.generation,attempt)['attempt_id']==attempt


def test_link_begin_idempotent_same_attempt(link_signin):
    link=link_signin; engine=link.engine; attempt=str(uuid4())
    engine.signin_begin(link.reader,link.approval.generation,attempt)
    thread=engine.signin._thread
    _signin_until(engine,link.approval,link.reader,attempt,'waiting')
    again=engine.signin_begin(link.reader,link.approval.generation,attempt)  # a begin resent after a timeout
    assert again['attempt_id']==attempt and again['state']=='waiting' and engine.signin._thread is thread
    assert [row['attempt_id'] for row in _attempts(link.access)]==[attempt]


@pytest.mark.parametrize('kind',['terminal','starting with the lease free','quiet child of an earlier process','live elsewhere'])
def test_link_new_approval_after_each_kind_of_ending(link_signin,kind):
    import os
    from claudlobby.plane.provider_signin import take_lease
    link=link_signin; engine=link.engine; held=None
    state,reason={'terminal':('needs_attention','provider_failed'),'starting with the lease free':('starting',None),
                  'quiet child of an earlier process':('unknown','child_quiet_unproven'),'live elsewhere':('waiting',None)}[kind]
    old=_put_row(link.access,link.approval.generation,state,reason)
    try:
        if kind=='live elsewhere':
            held=take_lease(link.access.root,old)  # its worker, in another process
            approval=_force_approval(link)
        else:
            approval=LinkStore_write(link)  # the writer finds it ended
        rows=_attempts(link.access); attempt=str(uuid4())
        if kind=='live elsewhere':
            with pytest.raises(AccessDenied,match='signin_live'): engine.signin_begin(link.reader,approval.generation,attempt)
            assert _attempts(link.access)==rows and engine.signin._thread is None
            return
        assert engine.signin_begin(link.reader,approval.generation,attempt)['attempt_id']==attempt
        retired={row['attempt_id']:row['retired'] for row in _attempts(link.access)}
        assert retired=={old:1,attempt:0}
    finally:
        if held is not None: os.close(held)


def LinkStore_write(link):
    from claudlobby.plane.provider_signin import LinkStore
    return LinkStore(link.access).write(link.scope)


def test_link_child_quiet_unproven_same_process_refused(link_signin,monkeypatch):
    import os,signal,subprocess
    from claudlobby.plane import provider_signin
    link=link_signin; engine=link.engine; other=_second_engine(link)
    monkeypatch.setattr(provider_signin,'SECONDS',0.5)
    children=[];original_popen=subprocess.Popen;original_getpgid=os.getpgid
    def observed_popen(argv,*args,**kwargs):
        child=original_popen(argv,*args,**kwargs)
        if argv[1:]==['setup-token']:children.append(child)
        return child
    def getpgid(pid):  # only a sign-in child's reap goes unproven
        if any(child.pid==pid for child in children): raise OSError('reap not proven')
        return original_getpgid(pid)
    monkeypatch.setattr(provider_signin.subprocess,'Popen',observed_popen)
    monkeypatch.setattr(provider_signin.os,'getpgid',getpgid)
    attempt=str(uuid4())
    try:
        engine.signin_begin(link.reader,link.approval.generation,attempt)
        assert _signin_ended(engine,link.approval,link.reader,attempt)['reason']=='child_quiet_unproven'
        part=engine.state(link.reader)['provider']['signin']
        assert (part['phase'],part['unconfirmed'],part['retryable'])==('ended',True,False)
        rows=_attempts(link.access)
        with pytest.raises(AccessDenied) as raised: engine.signin_begin(link.reader,link.approval.generation,str(uuid4()))
        assert raised.value.code=='signin_unconfirmed'  # Try again under the same approval
        approval=_force_approval(link)
        for begin in (engine.signin_begin,other.signin_begin):  # a new approval: the lasting service, then a second process
            with pytest.raises(AccessDenied) as raised: begin(link.reader,approval.generation,str(uuid4()))
            assert raised.value.code=='signin_unconfirmed'
        assert _attempts(link.access)==rows
        for fd in engine.signin._kept: os.close(fd)  # as if process 1 exited
        engine.signin._kept.clear()
        assert engine.state(link.reader)['provider']['signin']['unconfirmed'] is False
        with pytest.raises(AccessDenied,match='signin_unconfirmed'):  # process 1's own quiet child, whatever the lease
            engine.signin_begin(link.reader,approval.generation,str(uuid4()))
        fresh=str(uuid4())
        assert other.signin_begin(link.reader,approval.generation,fresh)['attempt_id']==fresh  # an earlier process's row
    finally:
        other.signin.shutdown()
        for fd in other.signin._kept: os.close(fd)
        for child in children:
            try: os.killpg(child.pid,signal.SIGKILL)
            except (ProcessLookupError,PermissionError): pass
            child.wait(timeout=5)


def test_link_begin_rechecks_approval_inside_transaction(link_signin,monkeypatch):
    link=link_signin; engine=link.engine
    admit=engine.signin._admit; landed=[]
    def admit_then_write(reader,grant,**kwargs):
        admit(reader,grant,**kwargs)
        if not landed: landed.append(LinkStore_write(link))  # a new yes after begin's admission
    monkeypatch.setattr(engine.signin,'_admit',admit_then_write)
    with pytest.raises(AccessDenied,match='setup_denied'): engine.signin_begin(link.reader,link.approval.generation,str(uuid4()))
    assert landed and _attempts(link.access)==[] and engine.signin._thread is None and _lease(link.access) is False


def test_link_begin_refused_when_not_started(link_signin,monkeypatch):
    from claudlobby.plane import owner_source
    link=link_signin; engine=link.engine; read=[]
    def start_state(root,installation): read.append(str(installation.directory)); return 'starting'
    monkeypatch.setattr(owner_source,'start_state',start_state)
    with pytest.raises(AccessDenied,match='not_started'): engine.signin_begin(link.reader,link.approval.generation,str(uuid4()))
    assert read==[link.approval.installation], 'begin reads it, once, for the approval\'s installation'
    assert _attempts(link.access)==[] and _lease(link.access) is False


def test_link_begin_refused_after_unused_window(link_signin,monkeypatch):
    from claudlobby.plane import owner_source
    link=link_signin; engine=link.engine
    _age(link,900)  # its unused window ends now
    monkeypatch.setattr(owner_source,'start_state',lambda root,installation:pytest.fail('read after the window'))
    with pytest.raises(AccessDenied) as raised: engine.signin_begin(link.reader,link.approval.generation,str(uuid4()))
    assert raised.value.code=='setup_denied' and _attempts(link.access)==[], 'the same word as an expired approval'
    monkeypatch.setattr(owner_source,'start_state',lambda root,installation:'started')
    assert 'signin' not in engine.state(link.reader)['provider'], 'a lapsed approval leaves the parts read'
    _age(link,-1)  # one second inside it
    attempt=str(uuid4())
    assert engine.signin_begin(link.reader,link.approval.generation,attempt)['attempt_id']==attempt


def test_link_cap_per_generation(link_signin):
    from claudlobby.plane.provider_signin import MAX_ATTEMPTS
    link=link_signin; engine=link.engine; gen=link.approval.generation
    for _ in range(MAX_ATTEMPTS+2): _put_row(link.access,str(uuid4()),'needs_attention','timeout',retired=1)  # other approvals'
    current=_put_row(link.access,gen,'needs_attention','timeout')
    assert engine.signin_status(link.reader,gen,current).get('retryable') is True, 'only its own attempts count'
    second=str(uuid4())
    assert engine.signin_begin(link.reader,gen,second)['attempt_id']==second
    _signin_until(engine,link.approval,link.reader,second,'waiting')
    assert engine.signin_cancel(link.reader,gen,second)['retryable'] is True
    for _ in range(MAX_ATTEMPTS-2): _put_row(link.access,gen,'needs_attention','timeout',retired=1)  # ten of its own
    assert 'retryable' not in engine.signin_status(link.reader,gen,second)
    rows=_attempts(link.access)
    with pytest.raises(AccessDenied,match='signin_original_attempt_required'): engine.signin_begin(link.reader,gen,str(uuid4()))
    assert _attempts(link.access)==rows
    approval=LinkStore_write(link)  # a new approval starts a new count
    third=str(uuid4())
    assert engine.signin_begin(link.reader,approval.generation,third)['attempt_id']==third


def test_shutdown_reads_service_stopped(link_signin):
    link=link_signin; engine=link.engine; attempt=str(uuid4())
    engine.signin_begin(link.reader,link.approval.generation,attempt)
    _signin_until(engine,link.approval,link.reader,attempt,'waiting')
    engine.signin.shutdown()
    row=[row for row in _attempts(link.access) if row['attempt_id']==attempt][0]
    assert (row['state'],row['reason'])==('cancelled','service_stopped')
    assert _lease(link.access) is False


def test_shutdown_team_signin_still_effect_uncertain(synthetic_signin):
    engine,grant,reader,*_=synthetic_signin
    attempt=str(uuid4())
    try:
        engine.signin_begin(reader,grant.generation,attempt)
        _signin_until(engine,grant,reader,attempt,'waiting')
        engine.signin.shutdown()
        assert (lambda row:(row['state'],row['reason']))(_signin_row(engine,attempt))==('cancelled','effect_uncertain')
    finally: engine.signin.shutdown()


def test_parts_read_signin_key_shape_and_linked_wins(link_signin):
    link=link_signin; engine=link.engine; gen=link.approval.generation; first=str(uuid4()); second=str(uuid4())
    keys={'generation','phase','attempt_id','state','reason','code_sent','retryable','unconfirmed'}
    def part(): return engine.state(link.reader)['provider'].get('signin')
    assert part()=={'generation':gen,'phase':'approved','attempt_id':None,'state':None,'reason':None,
                    'code_sent':False,'retryable':False,'unconfirmed':False}
    engine.signin_begin(link.reader,gen,first)
    _signin_until(engine,link.approval,link.reader,first,'waiting')
    waiting=part()
    assert set(waiting)==keys and (waiting['phase'],waiting['attempt_id'],waiting['state'])==('waiting',first,'waiting')
    assert 'url' not in json.dumps(engine.state(link.reader)) and 'claude.com' not in json.dumps(engine.state(link.reader))
    engine.signin_cancel(link.reader,gen,first)
    ended=part()
    assert (ended['phase'],ended['state'],ended['reason'],ended['code_sent'],ended['retryable'])==('ended','cancelled','effect_uncertain',False,True)
    engine.signin_begin(link.reader,gen,second)
    _signin_until(engine,link.approval,link.reader,second,'waiting')
    engine.signin_code(link.reader,gen,second,'accepted#'+'b'*32)
    _signin_until(engine,link.approval,link.reader,second,'connected')
    provider=engine.state(link.reader)['provider']
    assert provider['state']=='linked' and 'signin' not in provider, 'linked wins'


def test_two_concurrent_begins_one_wins(link_signin):
    import threading
    link=link_signin; engine=link.engine; other=_second_engine(link)
    barrier=threading.Barrier(2); results={}
    def begin(name,target,attempt):
        barrier.wait()
        try: results[name]=target.signin_begin(link.reader,link.approval.generation,attempt)['attempt_id']
        except AccessDenied as exc: results[name]=exc.code
    attempts={'here':str(uuid4()),'there':str(uuid4())}
    threads=[threading.Thread(target=begin,args=(name,target,attempts[name])) for name,target in (('here',engine),('there',other))]
    try:
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=30)
        won=[name for name in results if results[name]==attempts[name]]
        assert len(won)==1 and sorted(results.values())==sorted([attempts[won[0]],'signin_original_attempt_required'])
        assert [row['attempt_id'] for row in _attempts(link.access)]==[attempts[won[0]]]
        assert engine.state(link.reader)['provider']['signin']['attempt_id']==attempts[won[0]], 'the loser follows it'
    finally: other.signin.shutdown()


from contextlib import contextmanager  # noqa: E402  (the lease re-probe tests below)


class _ProbeClock:
    """provider_signin's ``time`` with a hook on ``sleep``: held_refusal sleeps between its probes."""
    def __init__(self,hook):
        import time
        self.hook,self.time,self.sleeps=hook,time,0
    def __getattr__(self,name): return getattr(self.time,name)
    def sleep(self,seconds):
        self.sleeps+=1
        self.hook(self.sleeps)
        self.time.sleep(seconds)


@contextmanager
def _held_elsewhere(root):
    """The sign-in lease held by another process; yields how to make it close it, as a worker does after its last row."""
    import subprocess,sys
    from claudlobby.plane.provider_signin import _lease_path
    holder=subprocess.Popen([sys.executable,'-c','import fcntl,os,sys\n'
        'fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600);fcntl.flock(fd,fcntl.LOCK_EX)\n'
        'print("held",flush=True);sys.stdin.readline()',str(_lease_path(root))],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    def release():
        if holder.poll() is None:
            holder.stdin.write('go\n'); holder.stdin.flush(); holder.wait(timeout=30)
    try:
        assert holder.stdout.readline().strip()=='held'
        yield release
    finally:
        release(); holder.stdin.close(); holder.stdout.close()


@pytest.mark.parametrize('released',[True,False])
@pytest.mark.parametrize('door',['writer','begin'])
def test_lease_released_after_the_last_row_reads_ended(link_signin,monkeypatch,door,released):
    """A worker in another process between its last row and closing its lease: the writer (L6b) and begin probe
    again, up to 50 times 0.01 s apart. Released in that time it has ended; still held, signin_unconfirmed.
    A quiet child's row is never probed again: its worker keeps the lease."""
    from claudlobby.plane import provider_signin
    link=link_signin; engine=link.engine
    _put_row(link.access,str(uuid4()),'needs_attention','provider_failed')
    approval=_force_approval(link) if door=='begin' else None
    rows=_attempts(link.access); record=_link_row(link.access)
    with _held_elsewhere(link.access.root) as release:
        clock=_ProbeClock(lambda count: release() if released and count==3 else None)
        monkeypatch.setattr(provider_signin,'time',clock)
        def act():
            if door=='writer': return _second_process(link).write(link.scope)
            return engine.signin_begin(link.reader,approval.generation,str(uuid4()))
        if released:
            answer=act()
            assert clock.sleeps==3
            if door=='writer': assert answer.generation!=link.approval.generation
            else: assert len(_attempts(link.access))==len(rows)+1
        else:
            with pytest.raises(AccessDenied) as raised: act()
            assert raised.value.code=='signin_unconfirmed' and clock.sleeps==50
            assert _attempts(link.access)==rows and _link_row(link.access)==record


def test_quiet_child_lease_is_not_probed_again(link_signin,monkeypatch):
    from claudlobby.plane import provider_signin
    from claudlobby.plane.provider_signin import held_refusal
    link=link_signin
    _put_row(link.access,str(uuid4()),'unknown','child_quiet_unproven')
    with _held_elsewhere(link.access.root):
        clock=_ProbeClock(lambda count: None)
        monkeypatch.setattr(provider_signin,'time',clock)
        with pytest.raises(AccessDenied,match='signin_unconfirmed'): _second_process(link).write(link.scope)
        assert clock.sleeps==0
        assert held_refusal(link.access.root,None)=='signin_live' and clock.sleeps==0

# 04b2: the computer starts the finish. After a marked Launch the guide starts the terminal's setup
# waiter (where the one shared check holds), the finish then runs with no terminal, and the status
# says who will finish. The Launch result itself never changes.
LAUNCH_KEYS = ["config_fingerprint", "generation", "request_id", "source_revision", "state"]
FRAMING_KEYS = ["seq", "session_id", "type", "version"]


def _launch_data(request):
    return {"state": "launch_handoff", **request}


def _private_state(access, monkeypatch):
    """A browser installation's ``state/`` is private; and no bot, fleet or release selector is set."""
    from claudlobby.commands import host_owner
    from claudlobby.commands.host_owner_setup import _STARTER_REFUSED
    (access.root / "state").chmod(0o700)
    for name in (*host_owner._GENERATED, *_STARTER_REFUSED):
        monkeypatch.delenv(name, raising=False)


def _stand_in_waiter(monkeypatch, script="pass"):
    """The waiter's argv, as ``setup_handoff_argv`` would give it, replaced by a short real child."""
    import sys
    from claudlobby import installation
    monkeypatch.setattr(installation, "setup_handoff_argv",
                        lambda _installation, _data: [sys.executable, "-I", "-c", script])


def _guide_dispatch(access, monkeypatch, data):
    """Today's guide (``owner_stdio.dispatch``) whose run returns this Launch's handoff: the frames the
    page receives and the exit code. Stand-ins: admission, the fixed profile and the start frame."""
    import contextlib, os
    from claudlobby.command_result import CommandOutput
    from claudlobby.commands import host_owner_setup, owner_setup_consent, owner_stdio
    from claudlobby.context import resolve_paths
    paths = resolve_paths(root=access.root)
    monkeypatch.setattr(owner_stdio, "_selectors", lambda args: None)
    monkeypatch.setattr(owner_stdio, "_admit", lambda args, value: paths)
    monkeypatch.setattr(owner_setup_consent, "FixedOwnerProfile",
                        lambda *_a, **_k: SimpleNamespace(runtime=None, environment=contextlib.nullcontext))
    monkeypatch.setattr(host_owner_setup, "run", lambda *_a, **_k: CommandOutput(dict(data)))
    monkeypatch.setattr(host_owner_setup, "run_computer", lambda *_a, **_k: pytest.fail("ran the computer run"))
    incoming, client = os.pipe()
    observer, outgoing = os.pipe()

    class Channel(owner_stdio.OwnerStdio):
        def __init__(self, _in, _out):
            super().__init__(incoming, outgoing)

        def start(self, *, computer=False):
            return {"profile": "owner-setup-v1", "expected_uid": os.getuid(), "source_revision": "a" * 40,
                    "binding_sha256": "b" * 64, "finish_setup": None}

    monkeypatch.setattr(owner_stdio, "OwnerStdio", Channel)
    try:
        code = owner_stdio.dispatch(SimpleNamespace(stdio_status=False, stdio_computer=False, root=str(access.root)))
        os.close(outgoing)
        outgoing = None
        frames = [json.loads(line) for line in os.read(observer, 65536).splitlines()]
    finally:
        for fd in (incoming, client, observer, outgoing):
            if fd is not None:
                os.close(fd)
    return code, frames


def _unattended(access, request, monkeypatch, *, gate=None):
    """``host owner setup --_finish-setup <ids>`` with no terminal on stdin, as the waiter execs it."""
    from claudlobby.commands import host_owner, operator_context
    for name in host_owner._GENERATED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(host_owner.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(host_owner, "_terminal", lambda root: pytest.fail("a terminal was opened"))
    monkeypatch.setattr(operator_context, "require_operator_context", gate or (lambda root=None: None))
    return host_owner.dispatch(SimpleNamespace(root=str(access.root), fleet=None, seed=False, json=False,
                                               owner_action="setup", finish_setup=list(request.values()),
                                               target_fleet=None))


@pytest.mark.parametrize("start", ["computer", "not_supported", "waiter_unavailable", "unmarked"])
def test_launch_handoff_result_keys_unchanged(setup_authority, monkeypatch, start):
    """B1: the website's in-page connection accepts ``launch_handoff`` only with exactly its five keys.
    Who will finish goes to the Preview row (``setup.finish``), never into the result."""
    from claudlobby import installation
    from claudlobby.commands import host_owner_setup
    engine, grant, request, record = (completed_setup if start == "unmarked" else marked_setup)(setup_authority, monkeypatch)
    access = setup_authority[1]
    _private_state(access, monkeypatch)
    monkeypatch.setattr(host_owner_setup, "computer_can_finish", lambda root: start != "not_supported")
    if start == "waiter_unavailable":
        def stale(_installation, _data):
            raise installation.InstallationError("synthetic: the installed launcher cannot finish setup")
        monkeypatch.setattr(installation, "setup_handoff_argv", stale)
    else:
        _stand_in_waiter(monkeypatch)
    code, frames = _guide_dispatch(access, monkeypatch, _launch_data(request))
    assert code == 0 and frames[-1]["type"] == "result"
    assert sorted(frames[-1]) == sorted([*LAUNCH_KEYS, *FRAMING_KEYS])
    assert {key: frames[-1][key] for key in LAUNCH_KEYS} == _launch_data(request)
    assert preview_payload(access, grant).get("finish") == {
        "computer": {"starter": "computer"}, "not_supported": {"starter": "page", "reason": "not_supported"},
        "waiter_unavailable": {"starter": "page", "reason": "waiter_unavailable"}, "unmarked": None}[start]


@pytest.mark.parametrize("cause", ["log_exists", "state_refused", "popen_raises"])
def test_waiter_start_failure_keeps_launch_handoff(setup_authority, monkeypatch, cause):
    """R1: a waiter that cannot be started never turns a good Launch into an error frame."""
    import subprocess
    from claudlobby.commands import host_owner_setup, owner_stdio
    from claudlobby.context import resolve_paths
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    _private_state(access, monkeypatch)
    monkeypatch.setattr(host_owner_setup, "computer_can_finish", lambda root: True)
    _stand_in_waiter(monkeypatch)
    log = access.root / "state" / f"setup-finish-{request['request_id']}.log"
    if cause == "log_exists":
        log.write_text("an earlier start\n")
    elif cause == "state_refused":
        (access.root / "state").chmod(0o755)
    else:
        def refuse(*_args, **_kwargs):
            raise OSError("synthetic: the process cannot be spawned")
        monkeypatch.setattr(subprocess, "Popen", refuse)
    code, frames = _guide_dispatch(access, monkeypatch, _launch_data(request))
    assert code == 0 and frames[-1]["type"] == "result" and frames[-1]["state"] == "launch_handoff"
    assert sorted(frames[-1]) == sorted([*LAUNCH_KEYS, *FRAMING_KEYS])
    assert not log.exists() or log.read_text() == ("an earlier start\n" if cause == "log_exists" else "")
    status = owner_stdio._status(resolve_paths(root=access.root), access)
    assert status["setup"]["finish"] == {"starter": "page", "reason": "waiter_unavailable"}
    assert status["setup"]["messages"] is None


def test_waiter_child_has_only_null_stdin_and_log(setup_authority, monkeypatch):
    """N3: no descriptor but ``/dev/null`` and the log. An inherited SSH channel descriptor would hold the
    page's session open for up to 600 seconds; an inherited ``runtime-use.lock`` descriptor would hold a
    reader lease against the cutover. Read by the started child itself, so it holds on any platform."""
    import fcntl, os, stat, time
    from claudlobby.commands import host_owner
    from claudlobby.commands.host_owner_setup import _STARTER_REFUSED, _start_waiter
    from claudlobby.context import resolve_paths
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    _private_state(access, monkeypatch)
    _stand_in_waiter(monkeypatch, "\n".join([
        "import json, os",
        "fds = []",
        "for fd in range(1024):",
        "    try:",
        "        info = os.fstat(fd)",
        "    except OSError:",
        "        continue",
        "    fds.append([fd, info.st_dev, info.st_ino])",
        "null = os.stat(os.devnull)",
        "print(json.dumps({'fds': fds, 'null': [null.st_dev, null.st_ino], 'cwd': os.getcwd(), 'umask': os.umask(0),",
        "                  'leader': os.getsid(0) == os.getpid(), 'env': sorted(os.environ)}), flush=True)", ""]))
    channel, other = os.pipe()  # Stands in for the page's SSH channel, inheritable as a session's descriptors are.
    os.set_inheritable(channel, True)
    lease = os.open(access.root / "state/runtime-use.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lease, fcntl.LOCK_SH)
    os.set_inheritable(lease, True)
    log = access.root / "state" / f"setup-finish-{request['request_id']}.log"
    try:
        assert _start_waiter(resolve_paths(root=access.root), _launch_data(request)) is True
        deadline = time.monotonic() + 30
        while not log.read_text().endswith("\n"):
            assert time.monotonic() < deadline, "the started child wrote nothing"
            time.sleep(0.05)
    finally:
        for fd in (channel, other, lease):
            os.close(fd)
    seen, info = json.loads(log.read_text()), log.stat()
    assert stat.S_IMODE(info.st_mode) == 0o600 and log.parent == access.root / "state"
    assert seen["fds"] == [[0, *seen["null"]], [1, info.st_dev, info.st_ino], [2, info.st_dev, info.st_ino]]
    assert seen["cwd"] == "/" and seen["umask"] == 0o077 and seen["leader"] is True
    assert not set(seen["env"]) & {*host_owner._GENERATED, *_STARTER_REFUSED}


@pytest.mark.parametrize("name", ["BOT_ID", "FLEET_ROOT", "CLAUDLOBBY_RELEASE_ID", "CLAUDLOBBY_ARTIFACT_ID"])
def test_waiter_is_never_started_with_a_bot_fleet_or_release_selector(setup_authority, monkeypatch, name):
    """N2: the guide's fixed profile environment, as it is; one with a generated or release key starts nothing."""
    from claudlobby.commands.host_owner_setup import _start_waiter
    from claudlobby.context import resolve_paths
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    _private_state(access, monkeypatch)
    _stand_in_waiter(monkeypatch)
    monkeypatch.setenv(name, "synthetic")
    assert _start_waiter(resolve_paths(root=access.root), _launch_data(request)) is False
    assert not (access.root / "state" / f"setup-finish-{request['request_id']}.log").exists()


@pytest.mark.parametrize("case", ["unmarked_with_a_mark_in_the_result", "marked_with_a_page_finish_in_the_result",
                                  "another_launch"])
def test_waiter_started_only_from_stored_mark(setup_authority, monkeypatch, case):
    """Only the stored approval's ``grant.messages`` starts the waiter: nothing the run hands over (or a
    page sent) can cause or suppress it. An unmarked approval starts nothing and records nothing."""
    from claudlobby.commands import host_owner_setup
    from claudlobby.commands.host_owner_setup import start_computer_finish
    from claudlobby.context import resolve_paths
    marked = case != "unmarked_with_a_mark_in_the_result"
    engine, grant, request, record = (marked_setup if marked else completed_setup)(setup_authority, monkeypatch)
    access = setup_authority[1]
    started = []
    monkeypatch.setattr(host_owner_setup, "computer_can_finish", lambda root: True)
    monkeypatch.setattr(host_owner_setup, "_start_waiter", lambda paths, data: started.append(data) or True)
    data = _launch_data(request)
    if case == "unmarked_with_a_mark_in_the_result":
        data.update(messages={"sender": "card"}, sends_goal=True, finish="computer")
    elif case == "marked_with_a_page_finish_in_the_result":
        data.update(finish="page", messages=None)
    else:
        data["request_id"] = str(uuid4())
    value = start_computer_finish(resolve_paths(root=access.root), access, data)
    if case == "marked_with_a_page_finish_in_the_result":
        assert value == {"starter": "computer"} and len(started) == 1
        assert preview_payload(access, grant)["finish"] == {"starter": "computer"}
    else:
        assert value is None and started == [] and "finish" not in preview_payload(access, grant)


@pytest.mark.parametrize("by", ["computer", "page", "terminal"])
def test_by_computer_only_from_unattended_branch(setup_authority, monkeypatch, by):
    """G1: ``by`` is ``page``, ``terminal`` or ``computer``; ``computer`` only from ``_unattended_finish``.
    Unattended, the finish's line (which goes to its log) never carries the name."""
    from claudlobby.commands import host_owner_setup as guide
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    card = FakeCard(access)
    if by == "computer":
        monkeypatch.setattr(guide, "_CardEffects", lambda *_: card)
        result = _unattended(access, request, monkeypatch)
        answer = result.data["messages"]
        assert result.lines == ("setup_messages state=allowed by=computer goal=sent",)
    else:
        answer = finish(setup_authority, request, card, monkeypatch, consent=by == "page")["messages"]
    assert answer == {"state": "allowed", "by": by, "alias": ALIAS, "renamed": False, "goal": {"state": "sent"}}
    assert len(card.sent) == 1 and message_grants(access) == [(ACTOR_UID, ALIAS)]


def test_unattended_unmarked_refused_before_output(setup_authority, monkeypatch):
    """An approval without the mark is refused unattended: no ``_say``, no ``/dev/tty``, nothing written."""
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import host_owner, host_owner_setup as guide
    engine, grant, request, record = completed_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    before = preview_payload(access, grant)
    monkeypatch.setattr(guide, "_say", lambda *_: pytest.fail("the finish wrote to a terminal"))
    monkeypatch.setattr(host_owner, "_setup_messages", lambda *_a, **_k: pytest.fail("the optional cards ran"))
    monkeypatch.setattr(guide, "_CardEffects", lambda *_: pytest.fail("the card's finish ran"))
    monkeypatch.setattr(host_owner, "io", SimpleNamespace(FileIO=lambda *_a, **_k: pytest.fail("/dev/tty was opened")))
    with pytest.raises(CommandFailure, match="owner changes require an interactive operator terminal"):
        _unattended(access, request, monkeypatch)
    assert preview_payload(access, grant) == before and message_grants(access) == []


@pytest.mark.parametrize("computer", ["linux", "mac", "managed_runtime", "other_launcher"])
def test_preview_finish_matches_start_predicate(setup_authority, monkeypatch, computer):
    """R5: the Preview says ``computer`` only where the guide's own start check holds; a Mac's service
    manager, the managed runtime or a launcher ``setup_handoff_argv`` refuses says ``page``, and the
    guide there starts nothing and records ``not_supported``."""
    import os, sys
    from claudlobby import installation as module, resources
    from claudlobby.commands import host_owner_setup
    from claudlobby.commands.host_owner_setup import computer_can_finish, start_computer_finish
    from claudlobby.context import resolve_paths
    store, access, grant, reader, *_ = setup_authority
    launcher = resources.get_resources().installation.cli
    body = Path(module.__file__).read_bytes() if computer != "other_launcher" else b"raise SystemExit(6)\n"
    launcher.write_bytes(b"#!" + os.path.realpath(sys.executable).encode() + b"\n" + body)
    launcher.chmod(0o755)
    monkeypatch.setattr(host_owner_setup, "platform", SimpleNamespace(system=lambda: "Darwin" if computer == "mac" else "Linux"))
    if computer == "managed_runtime":
        monkeypatch.setattr(module, "managed_browser_runtime", lambda _installation: object())
    else:
        monkeypatch.setattr(module, "managed_browser_runtime", lambda _installation: None)
    holds = computer == "linux"
    if computer == "mac":
        from claudlobby.plane import owner_setup

        with monkeypatch.context() as patch:
            patch.setattr(owner_setup, "managed_setup", lambda *_: pytest.fail("unsupported platform inspected Git"))
            assert computer_can_finish(access.root) is False
    else:
        assert computer_can_finish(access.root) is holds
    grant = replace(grant, messages={"alias": ALIAS})
    store.allow(grant)
    engine = SetupEngine(access)
    preview = engine.preview(reader, grant.generation, "Review requested local code")
    assert preview["finish"] == ("computer" if holds else "page")
    engine.launch(reader, **binding(preview))
    started = []
    monkeypatch.setattr(host_owner_setup, "_start_waiter", lambda paths, data: started.append(data) or True)
    value = start_computer_finish(resolve_paths(root=access.root), access, _launch_data(binding(preview)))
    assert value == ({"starter": "computer"} if holds else {"starter": "page", "reason": "not_supported"})
    assert len(started) == (1 if holds else 0)


def test_a_dead_in_progress_reads_stale_and_a_press_resumes_it_with_the_same_name(setup_authority, monkeypatch):
    """R4 (core half): in progress with no process holding the finish's lock is stale (``running: false``),
    in progress while one holds it is ``running: true``, and a press resumes the dead finish under its name."""
    import fcntl, os
    from claudlobby.commands.host_owner_setup import _keep, card_status
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    _keep(access, grant, {"starter": "computer"}, key="finish")
    from claudlobby.commands import host_owner_setup as guide
    dying = FakeCard(access)
    dying.register = lambda alias, check: (_ for _ in ()).throw(KeyboardInterrupt())  # Killed while registering.
    monkeypatch.setattr(guide, "_CardEffects", lambda *_: dying)
    with pytest.raises(KeyboardInterrupt):
        _unattended(access, request, monkeypatch)
    status = lambda: card_status(access, OwnerSetup(access).retained_state())
    assert status()["messages"]["state"] == "in_progress"
    assert status()["finish"] == {"starter": "computer", "running": False}
    fd = os.open(access.root / "state" / f"setup-finish-{record['request_id']}.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # A finish alive and holding its lock.
        assert status()["finish"] == {"starter": "computer", "running": True}
    finally:
        os.close(fd)
    pressed = FakeCard(access, taken={"ada-lovelace"})  # Its own registration now reads as taken.
    answer = finish(setup_authority, request, pressed, monkeypatch)["messages"]
    assert (answer["state"], answer["alias"], answer["renamed"], answer["by"]) == ("allowed", ALIAS, False, "computer")
    assert pressed.registered == [ALIAS] and len(pressed.sent) == 1
    assert status()["finish"] == {"starter": "computer"}, "running is read only while in progress"


@pytest.mark.parametrize("point", ["start", "record"])
def test_a_signal_during_the_start_or_its_record_keeps_launch_handoff(setup_authority, monkeypatch, point):
    """A SIGTERM or SIGHUP (``interrupted`` turns either into ``KeyboardInterrupt``) while the guide starts the
    computer's finish, or while it records who finishes, leaves the Launch's five-key result and exit 0."""
    import subprocess
    from claudlobby.commands import host_owner_setup, owner_stdio
    from claudlobby.context import resolve_paths
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    _private_state(access, monkeypatch)
    monkeypatch.setattr(host_owner_setup, "computer_can_finish", lambda root: True)
    _stand_in_waiter(monkeypatch)
    started = []
    if point == "start":
        real = subprocess.Popen

        def signalled(*args, **kwargs):
            started.append(real(*args, **kwargs))  # The waiter is already started, and not yet recorded.
            raise KeyboardInterrupt()
        monkeypatch.setattr(subprocess, "Popen", signalled)
    else:
        def signalled(*_args, **_kwargs):
            raise KeyboardInterrupt()
        monkeypatch.setattr(host_owner_setup, "_keep", signalled)
    code, frames = _guide_dispatch(access, monkeypatch, _launch_data(request))
    for child in started:
        child.wait(timeout=10)
    assert len(started) == (1 if point == "start" else 0)
    assert code == 0 and frames[-1]["type"] == "result" and frames[-1]["state"] == "launch_handoff"
    assert sorted(frames[-1]) == sorted([*LAUNCH_KEYS, *FRAMING_KEYS])
    assert {key: frames[-1][key] for key in LAUNCH_KEYS} == _launch_data(request)
    status = owner_stdio._status(resolve_paths(root=access.root), access)
    assert status["setup"]["finish"] == (
        {"starter": "page", "reason": "waiter_unavailable"} if point == "start" else None)
    assert status["setup"]["messages"] is None


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="the cut-off finish runs in a forked child")
@pytest.mark.parametrize("cut", ["before_the_request", "while_the_transport_runs", "after_the_send"])
def test_a_hanging_unattended_finish_is_cut_off_and_the_page_sends_once(setup_authority, monkeypatch, tmp_path, cut):
    """At its deadline the computer's own finish ends at once, wherever it hangs: it sends nothing further,
    the kernel frees its lock and the status reads stale (``running: false``), and the page's finish carries
    it on. Cut off before the goal's request was committed, the page sends the goal; cut off after the send
    and before the record, the page finds the request by its derived id and sends nothing. Cut off while the
    native transport child runs (its own session, so the exit leaves it), that child may still finish the
    paste; the request was committed with its transport unrecorded, so the page finds it by the same id,
    records ``unknown`` (``send_uncertain``) and sends nothing."""
    import os, signal, subprocess, sys, time
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.commands.host_owner_setup import _keep, card_status, goal_request_id
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    _keep(access, grant, {"starter": "computer"}, key="finish")
    sent, landed = tmp_path / "sent", tmp_path / "landed"

    class Hanging(FakeCard):
        def send(self, alias, request_id, text, check):
            if cut == "after_the_send":
                super().send(alias, request_id, text, check)
                sent.write_text(request_id)
            elif cut == "while_the_transport_runs":
                check(SimpleNamespace(caller=SimpleNamespace(uid=ACTOR_UID, alias=alias)),
                      SimpleNamespace(peer=SimpleNamespace(uid=LEAD_UID)))
                sent.write_text(request_id)  # Committed and reserved before any native effect.
                subprocess.Popen([sys.executable, "-I", "-c", "import sys, time; time.sleep(1); "
                                  "open(sys.argv[1], 'w').write('pasted')", str(landed)],
                                 stdin=subprocess.DEVNULL, start_new_session=True)  # As the transport's _run.
            time.sleep(60)  # A send that never returns.

    monkeypatch.setattr(guide, "_CardEffects", lambda *_: Hanging(access))
    monkeypatch.setattr(guide, "UNATTENDED_FINISH_DEADLINE_S", 0.5)
    pid = os.fork()
    if pid == 0:  # The waiter's exec of the finish, with no terminal.
        code = 0
        try:
            _unattended(access, request, monkeypatch)
        except BaseException:
            code = 1
        os._exit(code)  # Reached only when the deadline did not end it.
    limit = time.monotonic() + 30
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if time.monotonic() > limit:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("the unattended finish was not cut off")
        time.sleep(0.05)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == guide._DEADLINE_EXIT
    from claudlobby import request_queries

    pressed = FakeCard(access)
    goal_id = goal_request_id(record["request_id"])
    if cut != "before_the_request":
        assert sent.read_text() == goal_id
        # What the cut-off send left: the committed request under the derived id.
        pressed.receipts[goal_id] = _receipt(ACTOR_UID, LEAD_UID, preview_payload(access, grant)["goal"])
    if cut == "while_the_transport_runs":
        pressed.receipts[goal_id].transmissions = (SimpleNamespace(transport=None),)

    def lookup(root, fleet_uid, request_id):
        assert (root, fleet_uid, request_id) == (access.root, FLEET_UID, goal_id)
        view = pressed.receipts.get(request_id)
        if view is None:
            raise request_queries.RequestNotFoundError()
        return view

    monkeypatch.setattr(request_queries, "read_request", lookup)
    read = lambda: card_status(access, OwnerSetup(access).retained_state())
    before = preview_payload(access, grant)
    assert before["messages"]["goal"] == {"request_id": goal_id, "state": "pending"}
    state = read()
    expected = {
        "before_the_request": {"state": "pending"},
        "while_the_transport_runs": {"state": "unknown", "reason": "send_uncertain"},
        "after_the_send": {"state": "sent"},
    }[cut]
    assert state["messages"]["state"] == "allowed" and state["messages"]["goal"] == expected
    assert state["finish"] == ({"starter": "computer", "running": False} if cut == "before_the_request"
                               else {"starter": "computer"})
    assert preview_payload(access, grant) == before, "status leaves the original receipt untouched"
    if cut == "before_the_request":
        def unavailable(*_args):
            raise request_queries.RequestUnavailableError()
        with monkeypatch.context() as patch:
            patch.setattr(request_queries, "read_request", unavailable)
            state = read()
            assert state["messages"]["goal"] == {"state": "unknown", "reason": "receipt_unavailable"}
            assert state["finish"] == {"starter": "computer"}
            assert preview_payload(access, grant) == before
    if cut == "while_the_transport_runs":
        # The transport child outlives the finish and may complete the paste after the exit.
        limit = time.monotonic() + 15
        while not landed.exists() and time.monotonic() < limit:
            time.sleep(0.05)
        assert landed.read_text() == "pasted"
    answer = finish(setup_authority, request, pressed, monkeypatch)["messages"]
    assert answer["state"] == "allowed" and answer["by"] == "computer"
    assert answer["goal"] == ({"state": "unknown", "reason": "send_uncertain"} if cut == "while_the_transport_runs"
                              else {"state": "sent"})
    assert len(pressed.sent) == (1 if cut == "before_the_request" else 0), "the goal is sent once in all"
    assert pressed.registered == [], "the checked transaction stood; nothing is registered again"
    assert read()["finish"] == {"starter": "computer"}


def test_gate_unknown_unattended_writes_nothing(setup_authority, monkeypatch):
    """R6: the gate answering "could not be verified" at the moment units start leaves ``setup.messages`` absent."""
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.commands.host_owner_setup import card_status
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    monkeypatch.setattr(guide, "_CardEffects", lambda *_: pytest.fail("the finish ran past the gate"))

    def unknown(root=None):
        raise CommandFailure("unavailable", "operator ancestry could not be verified")
    with pytest.raises(CommandFailure, match="could not be verified"):
        _unattended(access, request, monkeypatch, gate=unknown)
    assert "messages" not in preview_payload(access, grant)
    assert card_status(access, OwnerSetup(access).retained_state())["messages"] is None


def test_unattended_finish_with_bot_variables_is_refused(setup_authority, monkeypatch):
    """The only gate claim tested: a call carrying a bot's variables is refused, and nothing is written."""
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import host_owner
    from claudlobby.commands import host_owner_setup as guide
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    monkeypatch.setattr(guide, "_CardEffects", lambda *_: pytest.fail("the finish ran for a bot"))
    monkeypatch.setenv("BOT_ID", "synthetic-bot")
    with pytest.raises(CommandFailure):
        host_owner.dispatch(SimpleNamespace(root=str(access.root), fleet=None, seed=False, json=False,
                                            owner_action="setup", finish_setup=list(request.values()), target_fleet=None))
    with pytest.raises(CommandFailure, match="outside generated bot context"):
        with host_owner._unattended_finish(access.root):
            pytest.fail("the unattended finish was admitted with a bot's variables")
    assert "messages" not in preview_payload(access, grant)


def test_unattended_gate_from_a_detached_child_admits_no_bot_variables(tmp_path):
    """The gate's environment half, run in a real detached child as the waiter's finish is: passes with no
    bot variables and refuses with ``BOT_ID``. (Its ancestry half needs a real service manager: the live
    check before merging reads it.)"""
    import os, subprocess, sys
    from claudlobby.commands import host_owner
    script = "from claudlobby.commands.operator_context import require_operator_context\nrequire_operator_context()\n"
    environment = {key: value for key, value in os.environ.items() if key not in host_owner._GENERATED}
    clean = subprocess.run([sys.executable, "-c", script], env=environment, cwd="/", stdin=subprocess.DEVNULL,
                           capture_output=True, start_new_session=True, timeout=60)
    bot = subprocess.run([sys.executable, "-c", script], env={**environment, "BOT_ID": "synthetic-bot"}, cwd="/",
                         stdin=subprocess.DEVNULL, capture_output=True, start_new_session=True, timeout=60)
    assert clean.returncode == 0, clean.stderr
    assert bot.returncode != 0 and b"outside generated bot context" in bot.stderr


def test_finish_log_in_state_dir(setup_authority, monkeypatch, capsys):
    """R2: the log sits in ``state/``, never in the composer's ``runtime/`` tree; created once (``O_EXCL``),
    mode 0600, a planted symlink refused; the unattended finish's output carries neither the name nor the
    goal; ``setup-status`` reports only its path and existence; the status run carries no path."""
    import os, stat
    from claudlobby.command_result import emit, CommandResult
    from claudlobby.commands import host_owner, host_owner_setup as guide, owner_stdio
    from claudlobby.commands.host_owner_setup import _open_finish_log
    from claudlobby.context import resolve_paths
    engine, grant, request, record = marked_setup(setup_authority, monkeypatch)
    access = setup_authority[1]
    _private_state(access, monkeypatch)
    state = access.root / "state"
    log = state / f"setup-finish-{record['request_id']}.log"
    planted = state / "planted-target"
    log.symlink_to(planted)
    with pytest.raises(OSError):
        _open_finish_log(access.root, record["request_id"])
    assert not planted.exists()
    log.unlink()
    os.close(_open_finish_log(access.root, record["request_id"]))
    assert stat.S_IMODE(log.stat().st_mode) == 0o600 and log.parent == state
    assert not (access.root / "runtime").exists() or not list((access.root / "runtime").glob("setup-finish-*"))
    with pytest.raises(FileExistsError):
        _open_finish_log(access.root, record["request_id"])
    monkeypatch.setattr(guide, "_CardEffects", lambda *_: FakeCard(access))
    result = _unattended(access, request, monkeypatch)
    emit(CommandResult(True, "host.owner.setup", None, None, result.data, None), json_output=False, lines=result.lines)
    printed = capsys.readouterr()
    assert "ada-lovelace" not in printed.out + printed.err and "Review requested local code" not in printed.out + printed.err
    monkeypatch.setattr(host_owner.sys.stdin, "isatty", lambda: False)
    status = host_owner.dispatch(SimpleNamespace(root=str(access.root), fleet=None, seed=False, json=True,
                                                 owner_action="setup-status"))
    assert status.data["card"]["log"] == {"path": str(log), "exists": True}
    log.unlink()
    status = host_owner.dispatch(SimpleNamespace(root=str(access.root), fleet=None, seed=False, json=True,
                                                 owner_action="setup-status"))
    assert status.data["card"]["log"] == {"path": str(log), "exists": False}
    page = json.dumps(owner_stdio._status(resolve_paths(root=access.root), access))
    assert "setup-finish" not in page and str(state) not in page and '"log"' not in page
