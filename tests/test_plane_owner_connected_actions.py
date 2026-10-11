"""Fixed carrier/grant conjunction over synthetic local workspace evidence."""
import sqlite3

import pytest

from claudlobby.plane.owner_access import AccessDenied, VerifiedReader
from claudlobby.plane.owner_action_authorization import ConnectedActionAuthorization, action_authorization, scope_binding, scope_inputs
from tests.test_plane_owner_connected import connected, evidence  # noqa: F401
from tests.test_plane_owner_workspace import local, OWNER, ORIGIN, ISSUER  # noqa: F401

FLEET = "fleet_" + "a" * 32
ACTOR = "actor_" + "b" * 32


@pytest.mark.parametrize("messages,nudges,feedback", [(m,n,f) for m in (False,True) for n in (False,True) for f in (False,True)])
def test_connected_session_and_independent_local_grants(connected, messages, nudges, feedback):
    store, access, _, binding, clock = connected
    reader = VerifiedReader(OWNER, store.issue(evidence(store, clock), OWNER).token)
    policy = ConnectedActionAuthorization(access, ISSUER, ORIGIN)
    for kind, enabled in (("message",messages),("nudge",nudges),("feedback",feedback)):
        if enabled:
            getattr(access,"allow_"+{"message":"messages","nudge":"nudges","feedback":"feedback"}[kind])(
                expected_owner=binding.owner,fleet_uid=FLEET,actor_uid=ACTOR,actor_alias="human:operator")
    before = access.path.read_bytes()
    assert policy.authorize_read(reader.token, OWNER, host_uid=binding.owner.host_uid) == binding.owner
    for kind, enabled in (("message",messages),("nudge",nudges),("feedback",feedback)):
        fn=getattr(policy,"authorize_"+kind)
        if enabled:
            assert fn(reader.token,OWNER,host_uid=binding.owner.host_uid,fleet_uid=FLEET).actor_uid == ACTOR
        else:
            with pytest.raises(AccessDenied):fn(reader.token,OWNER,host_uid=binding.owner.host_uid,fleet_uid=FLEET)
    assert access.path.read_bytes() == before
    with sqlite3.connect(access.path) as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_connected_session_and_grant_share_one_connection(connected, monkeypatch):
    store, access, _, binding, clock = connected
    token=store.issue(evidence(store,clock),OWNER).token
    grant=access.allow_messages(expected_owner=binding.owner,fleet_uid=FLEET,actor_uid=ACTOR,actor_alias="human:operator")
    observed=[];admit=store._admit;lookup=access._action_grant
    def record_admit(conn,*args):observed.append(conn);return admit(conn,*args)
    def record_grant(conn,*args):assert conn is observed[0];return lookup(conn,*args)
    monkeypatch.setattr(store,"_admit",record_admit);monkeypatch.setattr(access,"_action_grant",record_grant)
    assert store.authorize_action("message",token,OWNER,host_uid=binding.owner.host_uid,fleet_uid=FLEET,operational_origin=ORIGIN)[1] == grant
    assert len(observed)==1


def test_carrier_purpose_and_full_binding_scope_are_pinned(connected):
    store, access, workspace, binding, clock = connected
    token=store.issue(evidence(store,clock),OWNER).token
    policy=ConnectedActionAuthorization(access,ISSUER,ORIGIN)
    reader=VerifiedReader(OWNER,token)
    direct=access.open_session(OWNER)
    with pytest.raises(AccessDenied):policy.authorize_read(direct.token,OWNER,host_uid=binding.owner.host_uid)
    with pytest.raises(AccessDenied):access.authorize_read(token,OWNER,host_uid=binding.owner.host_uid)
    first=scope_inputs(["example"],scope_binding(policy,reader,binding.owner.host_uid))
    renewed=store.renew(token,OWNER,host_uid=binding.owner.host_uid,operational_origin=ORIGIN)
    assert scope_inputs(["example"],scope_binding(policy,VerifiedReader(OWNER,renewed.token),binding.owner.host_uid))==first
    assert scope_inputs(["example"],scope_binding(access,reader,binding.owner.host_uid))==["example"]
    with pytest.raises(ValueError):action_authorization(access.root,lambda:True)
    with pytest.raises(ValueError):action_authorization(access.root.parent,policy)
    for wrong in [ConnectedActionAuthorization(access,"https://wrong.example.test",ORIGIN),ConnectedActionAuthorization(access,ISSUER,"https://wrong.example.test")]:
        with pytest.raises(AccessDenied):wrong.authorize_read(renewed.token,OWNER,host_uid=binding.owner.host_uid)
    workspace.revoke(expected_binding=binding,expected_owner=binding.owner)
    with pytest.raises(AccessDenied):policy.scope_binding(VerifiedReader(OWNER,renewed.token),binding.owner.host_uid)


def test_connected_permission_authority_is_explicit_and_generation_bound(connected):
    store, access, _, binding, clock = connected
    token = store.issue(evidence(store, clock), OWNER).token
    policy = ConnectedActionAuthorization(access, ISSUER, ORIGIN)
    target = dict(host_uid=binding.owner.host_uid, fleet_uid=FLEET)
    with pytest.raises(AccessDenied, match='permissions_not_allowed'):
        policy.authorize_permission(token, OWNER, **target)
    grant = access.allow_permissions(expected_owner=binding.owner, fleet_uid=FLEET,
        actor_uid=ACTOR, actor_alias='human:operator')
    assert policy.authorize_permission(token, OWNER, **target) == grant
    access.revoke_permissions(expected_owner=binding.owner, fleet_uid=FLEET, expected_grant=grant)
    with pytest.raises(AccessDenied, match='permissions_not_allowed'):
        policy.authorize_permission(token, OWNER, **target)
    assert policy.authorize_read(token, OWNER, host_uid=binding.owner.host_uid) == binding.owner
