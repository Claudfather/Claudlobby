"""Synthetic connected identity; real optional authority/plans, no native effects."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sqlite3
from uuid import uuid4

import pytest

from claudlobby.plane.owner_access import OwnerAccess, AccessDenied, AccessUnavailable, VerifiedReader
from claudlobby.plane.owner_setup import OwnerSetup, SetupEngine
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


def test_setup_uses_locally_frozen_connection_without_exposing_credential_refs(setup_authority):
    import json
    from claudlobby.commands.setup import _template_github
    from claudlobby.config import load_fleet
    from claudlobby.github_connections import resolve_github_binding

    store,access,original,reader,clock,effects=setup_authority
    source=access.root.parent/'connections.yaml'
    connection={"name":"Team App","mode":"independent","app_id":123,"installation_id":456,
        "permissions":{"contents":"write"},"credential_refs":{"private_key_path_env":"TEAM_APP_KEY"}}
    source.write_text(json.dumps({"fleet":{"github":{"connections":{"team-app":connection}}}}))
    grant=store.candidate(OWNER,template=original.template,fleet=original.fleet,repository=original.repository,
        release_id=original.release_id,release_seal=original.release_seal,install_directory=original.install_directory,
        github_policy=_template_github(source,"team-app"))
    store.allow(grant)
    source.write_text('changed after approval')
    engine=SetupEngine(access)
    context=engine.context(reader)
    assert context['github']['name']=='Team App' and context['github']['repository']==grant.repository
    assert 'TEAM_APP_KEY' not in json.dumps(context) and 'credential_refs' not in json.dumps(context)
    engine.preview(reader,grant.generation,'Improve signup')
    fleet,_=load_fleet(access.root/'local/shop/fleet.yaml')
    assert all(resolve_github_binding(fleet,name,grant.repository).app_id==123 for name in fleet.bots)
    assert not effects


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
