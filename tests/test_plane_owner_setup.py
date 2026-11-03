"""Synthetic connected identity; real optional authority/plans, no native effects."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sqlite3
from uuid import uuid4

import pytest

from claudlobby.plane.owner_access import OwnerAccess, AccessDenied, AccessUnavailable, VerifiedReader
from claudlobby.plane.owner_setup import OwnerSetup, SetupEngine
from claudlobby.activation_state import read_selection as _canonical_read_selection
from claudlobby.plane.ids import ensure_host_uid
from tests.test_plane_owner_workspace import OWNER, proposed
from tests.test_plane_owner_connected import evidence
from tests.test_setup_commands import team_setup


@pytest.fixture
def setup_authority(team_setup, monkeypatch):
    from claudlobby.commands import releases
    from claudlobby import releases as release_store
    args, root, effects = team_setup
    ensure_host_uid(root / 'state')
    clock = [1800000000.0]
    access = OwnerAccess.initialize(root, clock=lambda:clock[0], before_first_fleet=True)
    pair = access.begin_pairing(OWNER)
    owner = access.confirm_pairing(pair.token,expected_principal=OWNER)
    workspace = access.workspace(); workspace.initialize(expected_owner=owner)
    binding = workspace.confirm(expected_proposal=proposed((access,workspace,owner,clock)))
    connected = access.connected(); connected.initialize(expected_owner=owner)
    session = connected.issue(evidence(connected,clock),OWNER)
    units = root.parent / 'units'; units.mkdir()
    store = OwnerSetup(access)
    grant = store.candidate(OWNER,template='revenue-web-app',fleet='shop',repository='example-org/shop',
        release_id='r-'+'a'*64,release_seal='b'*64,install_directory=str(units))
    monkeypatch.setattr(releases,'_release',lambda *_:SimpleNamespace(seal_sha256=grant.release_seal))
    monkeypatch.setattr(release_store,'read_release',lambda *_:SimpleNamespace(seal_sha256=grant.release_seal))
    return store,access,grant,VerifiedReader(OWNER,session.token),clock,effects


def allow(fixture):
    store,access,grant,reader,clock,effects=fixture
    store.allow(grant)
    return SetupEngine(access),grant,reader


def test_no_setup_grant_or_optional_schema_never_repairs(setup_authority):
    store,access,grant,reader,*_=setup_authority
    before=access.path.read_bytes()
    with pytest.raises(AccessUnavailable): store.authorize(reader)
    assert access.path.read_bytes()==before
    store.allow(grant)
    assert store.authorize(reader)==grant
    # A direct cookie cannot be used as a connected setup credential.
    with pytest.raises(AccessDenied): store.authorize(VerifiedReader(OWNER,access.open_session(OWNER).token))


def test_preview_uses_canonical_plan_no_activation_and_can_change_owned_goal(setup_authority):
    from claudlobby.config_plan import read_plan
    engine,grant,reader=allow(setup_authority)
    first=engine.preview(reader,grant.generation,'Improve checkout tests')
    assert first['activation']=='not_requested' and first['fleets']==['shop']
    plan=read_plan(engine.root,first['plan_id']); plan.check_fresh()
    assert plan.release_id==grant.release_id and plan.release_seal==grant.release_seal
    second=engine.preview(reader,grant.generation,'Improve signup tests')
    assert second['plan_id']!=first['plan_id']
    with pytest.raises(ValueError): plan.check_fresh()
    assert not setup_authority[-1]


@pytest.mark.parametrize('other',['other-fleet','other-runtime','target-library','projects'])
def test_whole_host_plan_cannot_include_unapproved_inputs(setup_authority,other):
    engine,grant,reader=allow(setup_authority)
    path=engine.root/'local'/('other' if other.startswith('other') else 'shop')
    path.mkdir(parents=True)
    if other=='other-fleet': (path/'fleet.yaml').write_text('fleet: {name: other}\n')
    elif other=='other-runtime': (path/'runtime').mkdir()
    elif other=='target-library': (path/'library').mkdir()
    else: (path/'projects.yaml').write_text('projects: {}\n')
    with pytest.raises(AccessDenied): engine.preview(reader,grant.generation,'Improve checkout tests')
    assert not setup_authority[-1]


def test_plan_expiry_and_changed_inputs_refuse_without_consuming_launch(setup_authority):
    engine,grant,reader=allow(setup_authority)
    plan=engine.preview(reader,grant.generation,'Improve checkout tests')
    path=engine.root/'local/shop/fleet.yaml'; path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(ValueError): engine.launch(reader,grant.generation,plan['plan_id'],str(uuid4()))
    assert engine.store.retained_request(reader) is None and not setup_authority[-1]
    path.write_bytes(path.read_bytes()[:-1])
    setup_authority[4][0]=plan['expires_at']
    with pytest.raises(AccessDenied): engine.launch(reader,grant.generation,plan['plan_id'],str(uuid4()))
    assert engine.store.retained_request(reader) is None


def test_one_launch_original_journal_pure_recovery_and_no_gap_filling(setup_authority,monkeypatch):
    from claudlobby.commands import host
    engine,grant,reader=allow(setup_authority)
    plan=engine.preview(reader,grant.generation,'Improve checkout tests'); uid=str(uuid4())
    journal={'verification':'verified','status':'active','plan_id':plan['plan_id'],'release_id':grant.release_id}
    count=[]
    def activate(args,root):
        count.append(args.activation_id)
        assert engine.store.retained_request(reader)['request_id']==uid
    monkeypatch.setattr(host,'_activate',activate)
    monkeypatch.setattr(host,'_recorded',lambda root,request:journal if count else None)
    result=engine.launch(reader,grant.generation,plan['plan_id'],uid)
    assert result['state']=='active' and count==[uid]
    assert engine.launch(reader,grant.generation,plan['plan_id'],uid)==result
    assert engine.receipt(reader,grant.generation,plan['plan_id'],uid)==result and count==[uid]
    with pytest.raises(AccessDenied):engine.launch(reader,grant.generation,plan['plan_id'],str(uuid4()))
    monkeypatch.setattr(host,'_recorded',lambda *_:None)
    assert engine.launch(reader,grant.generation,plan['plan_id'],uid)['state']=='unknown'
    assert count==[uid]  # Even a missing original journal does not authorize gap filling.


def test_lost_response_owned_effect_inspected_not_repeated(setup_authority,monkeypatch):
    from claudlobby.commands import host
    engine,grant,reader=allow(setup_authority)
    plan=engine.preview(reader,grant.generation,'Improve checkout tests'); uid=str(uuid4()); calls=[]
    def activate(*_):
        calls.append(uid); raise RuntimeError('lost caller response')
    monkeypatch.setattr(host,'_activate',activate)
    monkeypatch.setattr(host,'_recorded',lambda *_:{'verification':'verified','status':'activating','plan_id':plan['plan_id'],'release_id':grant.release_id})
    with pytest.raises(RuntimeError):engine.launch(reader,grant.generation,plan['plan_id'],uid)
    assert engine.receipt(reader,grant.generation,plan['plan_id'],uid)['state']=='activating'
    assert engine.launch(reader,grant.generation,plan['plan_id'],uid)['state']=='activating' and calls==[uid]


def test_grant_revocation_binding_change_and_renewal_identity(setup_authority):
    store,access,grant,reader,clock,_=setup_authority
    store.allow(grant)
    binding=access.workspace().active_binding(OWNER)
    renewed=access.connected().renew(reader.token,OWNER,host_uid=binding.owner.host_uid,operational_origin=binding.operational_origin)
    new_reader=VerifiedReader(OWNER,renewed.token)
    assert store.authorize(new_reader)==grant
    store.revoke(grant)
    with pytest.raises(AccessDenied):store.authorize(new_reader)
    replacement=replace(grant,generation=str(uuid4()));store.allow(replacement)
    with pytest.raises(AccessDenied):store.authorize(new_reader,expected=grant)
    access.workspace().revoke(expected_binding=binding,expected_owner=binding.owner)
    with pytest.raises(AccessDenied):store.authorize(new_reader)


def test_partial_activation_identity_recovery_never_admits_recorder(setup_authority):
    from claudlobby.plane.owner_source import inspect_source
    engine,grant,reader=allow(setup_authority)
    plan=engine.preview(reader,grant.generation,'Improve checkout tests'); uid=str(uuid4())
    engine.store.reserve(reader,grant,plan['plan_id'],uid)
    # Only the original grant's reservation admits identity/journal recovery.
    from claudlobby.plane.db import db_file
    db_file(engine.root).write_bytes(b'not an admitted recorder')
    assert engine.store.recovery_host(OWNER)==setup_authority[1].current_grant().host_uid
    with pytest.raises((AccessDenied,AccessUnavailable)):inspect_source(engine.root)
    with pytest.raises(AccessDenied):engine.preview(reader,grant.generation,'Another goal')
    with pytest.raises(AccessDenied):engine.store.request(reader,grant,plan['plan_id'],str(uuid4()))


def test_local_withdrawal_and_reapproval_of_stale_workspace_grant(setup_authority):
    from tests.test_plane_owner_workspace import proposed
    store,access,grant,reader,clock,_=setup_authority
    store.allow(grant)
    workspace=access.workspace(); binding=workspace.active_binding(OWNER)
    workspace.revoke(expected_binding=binding,expected_owner=binding.owner)
    store.revoke(grant)
    workspace.confirm(expected_proposal=proposed((access,workspace,binding.owner,clock)))
    replacement=store.candidate(OWNER,template=grant.template,fleet=grant.fleet,repository=grant.repository,
        release_id=grant.release_id,release_seal=grant.release_seal,install_directory=grant.install_directory)
    store.allow(replacement)
    with pytest.raises(AccessDenied):store.revoke(grant)
    assert store.local()==replacement
    clock[0]=replacement.expires_at
    next_grant=store.candidate(OWNER,template=grant.template,fleet=grant.fleet,repository=grant.repository,
        release_id=grant.release_id,release_seal=grant.release_seal,install_directory=grant.install_directory)
    store.allow(next_grant)
    assert store.local()==next_grant


def test_local_cli_requires_exact_terminal_approval_and_retained_grant(setup_authority,monkeypatch):
    from claudlobby.__main__ import main
    from claudlobby import supervision_inventory
    from tests.test_host_owner import terminal
    store,access,grant,reader,clock,_=setup_authority
    monkeypatch.setattr(supervision_inventory,'Adapter',lambda _:SimpleNamespace(read=lambda _:f'manager\tLinux\ndirectory\t{grant.install_directory}\n'))
    flags=['--root',str(access.root),'host','owner','allow-setup','--template',grant.template,'--target-fleet',grant.fleet,
        '--repository',grant.repository,'--release',grant.release_id,'--install-directory',grant.install_directory]
    terminal(monkeypatch,iter(['NO']))
    assert main(flags)!=0
    assert store.local() is None
    terminal(monkeypatch,iter(['ALLOW-SETUP']))
    assert main(flags)==0
    retained=store.local(); assert retained.repository==grant.repository and retained.release_seal==grant.release_seal
    terminal(monkeypatch,iter(['REVOKE-SETUP']))
    assert main(['--root',str(access.root),'host','owner','revoke-setup'])==0
    assert store.local() is None


@pytest.mark.parametrize('malformed',["CREATE TABLE setup_metadata(singleton INTEGER,version INTEGER)",
    "CREATE VIEW setup_metadata AS SELECT 1 AS singleton,1 AS version",
    "CREATE TABLE setup_metadata(wrong INTEGER)"])
def test_optional_setup_absence_is_empty_but_partial_schema_refuses(setup_authority,malformed):
    store,access,*_=setup_authority
    before=access.path.read_bytes()
    assert store.local() is None and access.path.read_bytes()==before
    with access._connection(write=True) as conn:
        conn.execute(malformed)
    with pytest.raises(AccessUnavailable):store.local()


def replacement(fixture, grant, **changes):
    store,access,_,_,clock,_=fixture
    store.revoke(grant)
    newer=replace(grant,generation=str(uuid4()),created_at=clock[0],expires_at=clock[0]+86400,**changes)
    store.allow(newer)
    return newer


def test_compatible_expired_reapproval_owns_prior_bytes_not_prior_plan(setup_authority):
    engine,grant,reader=allow(setup_authority)
    first=engine.preview(reader,grant.generation,'Improve checkout tests')
    clock=setup_authority[4]; clock[0]=grant.expires_at
    newer=replace(grant,generation=str(uuid4()),created_at=clock[0],expires_at=clock[0]+86400)
    engine.store.allow(newer)
    session=setup_authority[1].connected().issue(evidence(setup_authority[1].connected(),clock),OWNER)
    reader=VerifiedReader(OWNER,session.token)
    assert engine.store.owned_declaration(reader,newer)
    with pytest.raises(AccessDenied):engine.store.reserve(reader,newer,first['plan_id'],str(uuid4()))
    second=engine.preview(reader,newer.generation,'Improve signup tests')
    assert second['generation']==newer.generation and second['plan_id']!=first['plan_id']


@pytest.mark.parametrize('changes',[{'repository':'example-org/other'},{'release_id':'r-'+'c'*64},
    {'release_seal':'d'*64},{'install_directory':'/different/units'},{'fleet':'other'},
    {'template':'different'}])
def test_reapproval_mismatch_does_not_own_prior_declaration(setup_authority,changes):
    engine,grant,reader=allow(setup_authority)
    engine.preview(reader,grant.generation,'Improve checkout tests')
    newer=replacement(setup_authority,grant,**changes)
    assert not engine.store.owned_declaration(reader,newer)


def test_reapproval_never_overwrites_changed_authored_bytes(setup_authority):
    from claudlobby.command_result import CommandFailure
    engine,grant,reader=allow(setup_authority)
    engine.preview(reader,grant.generation,'Improve checkout tests')
    newer=replacement(setup_authority,grant)
    path=engine.root/'local/shop/fleet.yaml'; path.write_bytes(path.read_bytes()+b'\n# operator-authored change\n')
    before=path.read_bytes()
    assert not engine.store.owned_declaration(reader,newer)
    with pytest.raises(CommandFailure):engine.preview(reader,newer.generation,'Improve signup tests')
    assert path.read_bytes()==before


def test_browser_receipt_authority_expires_and_revokes_but_local_journal_survives(setup_authority,monkeypatch):
    from claudlobby.commands import host
    engine,grant,reader=allow(setup_authority)
    plan=engine.preview(reader,grant.generation,'Improve checkout tests'); uid=str(uuid4())
    engine.store.reserve(reader,grant,plan['plan_id'],uid)
    journal={'verification':'verified','status':'activating','plan_id':plan['plan_id'],'release_id':grant.release_id}
    monkeypatch.setattr(host,'_recorded',lambda *_:journal)
    assert engine.receipt(reader,grant.generation,plan['plan_id'],uid)['state']=='activating'
    engine.store.revoke(grant)
    with pytest.raises(AccessDenied):engine.receipt(reader,grant.generation,plan['plan_id'],uid)
    assert host._recorded(engine.root,uid)==journal
    # Expiry is also bounded, even with a fresh connected session.
    engine.store.allow(grant)
    engine.store.reserve(reader,grant,plan['plan_id'],uid)
    setup_authority[4][0]=grant.expires_at
    with pytest.raises(AccessDenied):engine.receipt(reader,grant.generation,plan['plan_id'],uid)
    with pytest.raises(AccessDenied):engine.store.recovery_host(OWNER)
    assert host._recorded(engine.root,uid)==journal


@pytest.mark.parametrize("revoked", [False, True])
def test_retained_local_state_preserves_launch_after_expiry_and_revocation(setup_authority, revoked, monkeypatch, capsys):
    store, access, grant, reader, clock, _ = setup_authority
    assert store.retained_state().state == "absent"
    engine, _, _ = allow(setup_authority)
    preview = engine.preview(reader, grant.generation, "Improve checkout tests")
    request = str(uuid4())
    store.reserve(reader, grant, preview["plan_id"], request)
    if revoked:
        store.revoke(grant)
    clock[0] = grant.expires_at
    before = access.path.read_bytes()
    state = store.retained_state()
    assert state.state == ("revoked" if revoked else "expired")
    assert not state.effective_active and state.grant == grant
    assert (state.launch_id, state.launch_plan) == (request, preview["plan_id"])
    from claudlobby.__main__ import main
    import json
    monkeypatch.setattr(OwnerAccess, "_now", lambda _: clock[0])
    assert main(["--root", str(access.root), "host", "owner", "setup-status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["state"] == state.state and data["launch_id"] == request
    assert data["launch_plan"] == preview["plan_id"]
    assert access.path.read_bytes() == before


def test_guarded_setup_approval_refuses_a_launch_reserved_during_consent(setup_authority):
    store, access, grant, reader, clock, _ = setup_authority
    engine, _, _ = allow(setup_authority)
    preview = engine.preview(reader, grant.generation, "Improve checkout tests")
    before = store.retained_state()
    store.reserve(reader, grant, preview["plan_id"], str(uuid4()))
    clock[0] = grant.expires_at
    candidate = replace(grant, generation=str(uuid4()), created_at=clock[0], expires_at=clock[0]+86400)
    snapshot = access.path.read_bytes()
    with pytest.raises(AccessDenied):
        store.allow(candidate, expected_retained=before)
    assert access.path.read_bytes() == snapshot


@pytest.mark.parametrize("selected", [False, True])
def test_guide_retains_matching_approval_and_serves_without_reconsent(setup_authority, monkeypatch, selected):
    from contextlib import contextmanager
    import sys
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.commands import releases, host
    from claudlobby.command_result import CommandOutput
    from tests.test_plane_owner_workspace import ISSUER, ORIGIN
    from tests.test_host_owner import terminal
    store, access, grant, reader, clock, _ = setup_authority
    store.allow(grant)
    if selected:
        import json
        engine = SetupEngine(access)
        preview = engine.preview(reader, grant.generation, "Improve checkout tests")
        selection = {"schema": 1, "activation_id": str(uuid4()), "release_id": grant.release_id,
                     "plan_id": preview["plan_id"]}
        store.reserve(reader, grant, selection["plan_id"], selection["activation_id"])
        (access.root / "state/selected-release.json").write_text(json.dumps(selection))
        from claudlobby import activation_state
        monkeypatch.setattr(activation_state, "read_selection", _canonical_read_selection)
        # Feed retained journal evidence through the real canonical status renderer.
        evidence = host._status_data(access.root)
        evidence.update(selection=selection, selected_activation=selection, items=[])
        monkeypatch.setattr(host, "_host_releases", lambda *_: CommandOutput(evidence, grant.release_id))
        clock[0] = grant.expires_at  # Serving the original selected launch needs no renewal.
    monkeypatch.setattr(releases, "_release", lambda *_: SimpleNamespace(
        release_id=grant.release_id, seal_sha256=grant.release_seal, cli_path=Path(sys.executable).with_name("claudlobby")))
    monkeypatch.setattr(guide, "_native_directories", lambda _: (Path(grant.install_directory),))
    entered = []
    @contextmanager
    def serving(command, path, origin):
        entered.append(command)
        class Child:
            def wait(self): raise KeyboardInterrupt
        yield Child()
    monkeypatch.setattr(guide, "_serving", serving)
    console = terminal(monkeypatch, iter(("\n",)))
    args = SimpleNamespace(origin="https://host.example.test", tailscale=Path(sys.executable),
        tailscale_socket=None, socket=access.root/"state/owner/owner.sock", workspace_issuer=ISSUER,
        workspace_origin=ORIGIN, target_fleet=grant.fleet, repository=grant.repository,
        install_directory=Path(grant.install_directory))
    from claudlobby.context import resolve_paths
    before = access.path.read_bytes()
    output = guide.run(args, resolve_paths(root=access.root), access, console)
    assert output.data["state"] == "serving_stopped" and len(entered) == 1
    assert "Keep this terminal open" in console.getvalue()
    assert access.path.read_bytes() == before
    assert store.retained_state().grant == grant


def test_guide_refuses_retained_launch_without_replacing_or_starting_server(setup_authority, monkeypatch):
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.command_result import CommandFailure
    store, access, grant, reader, clock, _ = setup_authority
    engine, _, _ = allow(setup_authority)
    preview = engine.preview(reader, grant.generation, "Improve checkout tests")
    uid = str(uuid4()); store.reserve(reader, grant, preview["plan_id"], uid)
    clock[0] = grant.expires_at
    monkeypatch.setattr(guide, "_serving", lambda *_: pytest.fail("started a server"))
    before = access.path.read_bytes()
    with pytest.raises(CommandFailure) as caught:
        guide.run(SimpleNamespace(), SimpleNamespace(root=access.root), access, None)
    assert caught.value.data["launch_id"] == uid
    assert access.path.read_bytes() == before


def test_fresh_guide_runs_canonical_consents_then_keeps_foreground_serving(setup_authority, monkeypatch):
    from contextlib import contextmanager
    import sys
    from claudlobby.commands import host_owner_setup as guide, host_owner as console, releases
    from claudlobby import supervision_inventory
    from claudlobby.context import resolve_paths
    from tests.test_plane_owner_workspace import ISSUER, ORIGIN
    from tests.test_host_owner import terminal
    store, access, grant, _, clock, effects = setup_authority
    access.path.unlink()  # Owned synthetic installation, with no retained authority.
    monkeypatch.setattr(releases, "_release", lambda *_: SimpleNamespace(release_id=grant.release_id,
        seal_sha256=grant.release_seal, cli_path=Path(sys.executable).with_name("claudlobby")))
    directories = (Path(grant.install_directory),)
    monkeypatch.setattr(guide, "_native_directories", lambda _: directories)
    monkeypatch.setattr(supervision_inventory.Adapter, "read", lambda *a, **k: "synthetic catalog")
    monkeypatch.setattr(supervision_inventory, "_catalog", lambda _: ("launchd", "gui/1000", directories, (), ()))
    entered = []
    @contextmanager
    def serving(command, path, origin):
        entered.append(command)
        if "--workspace-issuer" not in command:
            challenge = access.begin_pairing(OWNER)
            monkeypatch.setattr(console, "_challenge", lambda *a, **k: challenge.token)
        else:
            workspace = access.workspace()
            proposal = proposed((access, workspace, access.current_grant(), clock))
            monkeypatch.setattr(console, "_challenge", lambda *a, **k: proposal.nonce)
        class Child:
            def wait(self): raise KeyboardInterrupt
        yield Child()
    monkeypatch.setattr(guide, "_serving", serving)
    output = terminal(monkeypatch, iter(word+"\n" for word in (
        "", "INITIALIZE", "PAIR", "INITIALIZE-WORKSPACE", "INITIALIZE-CONNECTED", "BIND-WORKSPACE", "ALLOW-SETUP")))
    args = SimpleNamespace(origin="https://host.example.test", tailscale=Path(sys.executable), tailscale_socket=None,
        socket=access.root/"state/owner/owner.sock", workspace_issuer=ISSUER, workspace_origin=ORIGIN,
        target_fleet=grant.fleet, repository=grant.repository, install_directory=directories[0])
    result = guide.run(args, resolve_paths(root=access.root), access, output)
    assert result.data["state"] == "serving_stopped" and len(entered) == 2
    assert access.workspace().status()[0] == "active"
    retained = store.retained_state()
    assert retained.state == "allowed" and retained.launch_id is None
    assert retained.grant.repository == grant.repository and not effects
    assert "Keep this terminal open" in output.getvalue()


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
            SimpleNamespace(cli_path=Path(sys.executable), release_id="r-" + "a" * 64))
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
