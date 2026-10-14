"""Real action engines over synthetic native principal/binding/receiver fixtures."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from claudlobby import message_operations
from claudlobby.message_transport import TransportOutcome
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.owner_browser import create_owner_browser_app
from claudlobby.plane.owner_workspace_http import WorkspaceBootstrapConfig
from claudlobby.plane.workspace_assertion import WorkspaceAttestation
from claudlobby.plane.session_assertion import SessionAttestation
from tests.test_plane_owner_feedback import gateway  # noqa: F401
from tests.test_activation import cold, tmp_path  # noqa: F401
from tests.test_releases import installed  # noqa: F401
from tests.test_task_read_cli import active  # noqa: F401
from tests.test_plane_owner_workspace import OWNER, ISSUER, ORIGIN
from tests.test_plane_two_fleets import _Sampler

HOST="https://host.example.test"
PREFIX="/api/owner/connected/actions/"


@pytest.fixture
def connected_actions(gateway):
    adapter, _, ctx, options, _ = gateway
    access=adapter.access;old=access.current_grant();access.revoke_owner(expected_revision=old.revision)
    pair=access.begin_pairing(OWNER);owner=access.confirm_pairing(pair.token,expected_principal=OWNER)
    ws=access.workspace();ws.initialize(expected_owner=owner)
    c=ws.begin(OWNER,issuer=ISSUER,operational_origin=ORIGIN);now=int(time.time())
    a=WorkspaceAttestation(ISSUER,str(uuid4()),str(uuid4()),owner.host_uid,c.nonce,ORIGIN,now,now+120,str(uuid4()))
    binding=ws.confirm(expected_proposal=ws.stage(a,OWNER))
    store=access.connected();store.initialize(expected_owner=owner);c=store.begin(OWNER)
    token=store.issue(SessionAttestation(c.expected,now,now+120,str(uuid4())),OWNER).token
    principal=[OWNER]
    async def verify(scope):return principal[0]
    app=create_owner_browser_app(adapter.root,verify_principal=verify,expected_origin=HOST,
        sampler=_Sampler([]),package=adapter.package,workspace_bootstrap=WorkspaceBootstrapConfig(ISSUER,ORIGIN))
    client=TestClient(app,base_url=HOST)
    return app,client,adapter,ctx,options,access,owner,binding,token,principal


def post(fixture, operation, payload, **extra):
    return fixture[1].post(PREFIX+operation,content=json.dumps(payload),headers={"Origin":ORIGIN,
        "Authorization":"Bearer "+fixture[8],"Content-Type":"application/json","X-Claudlobby-Owner":"1",**extra})


def allow(fixture,kind):
    _,_,_,ctx,_,access,owner,*_=fixture
    return getattr(access,"allow_"+{"message":"messages","nudge":"nudges","feedback":"feedback"}[kind])(
        expected_owner=owner,fleet_uid=ctx.fleet_uid,actor_uid=ctx.caller.uid,actor_alias=ctx.caller.alias)


def context(fixture,kind):
    response=post(fixture,"context",{"room":"example",**({"kind":kind} if kind!="message" else {})})
    assert response.status_code==200,response.text
    return response.json()


def row(fixture,kind):
    c=context(fixture,kind);target={"recipient":c["recipients"][0]["id"],"task_id":None}
    if kind!="message":target.update(task_id=fixture[4]["task_id"],assignment_id=None,release_id=c["release_id"])
    return {**({"version":2} if kind!="message" else {}),"request_id":str(uuid4()),"kind":kind,
        "scope":c["scope"],"target":target,"submitted_at":"2026-10-10T01:00:00.000Z"}


def receiver(monkeypatch, received=True):
    calls=[]
    def transport(package,destination,*,message_id,body,**kwargs):
        wire=body.encode();digest="sha256:"+hashlib.sha256(wire).hexdigest();calls.append((message_id,body))
        if received:emit_batch(destination.root,[{"event_type":"transmission","emitter":"synthetic-connected-receiver",
            "event_id":"ev_"+uuid4().hex,"occurred_at":datetime.now(timezone.utc).isoformat(),"fleet":destination.fleet,
            "payload":{"msg_id":message_id,"attempt_no":1,"carrier":"tmux","destination":f"bot:{destination.fleet}/{destination.session}",
                "state":"received","received_bytes":len(wire),"received_sha256":digest}}])
        return TransportOutcome("submitted",digest,len(wire),0)
    send=message_operations.send_message;committed=message_operations.send_committed_native_attempt
    monkeypatch.setattr(message_operations,"send_message",lambda *a,**k:send(*a,**k,transport=transport,clear=lambda *a,**k:None))
    monkeypatch.setattr(message_operations,"send_committed_native_attempt",lambda *a,**k:committed(*a,**k,transport=transport))
    monkeypatch.setattr(message_operations,"read_recipient_box",lambda *a,**k:None)
    return calls


@pytest.mark.parametrize("kind",["message","nudge","feedback"])
def test_real_engines_exact_receipt_and_replay(connected_actions,monkeypatch,kind):
    f=connected_actions;allow(f,kind);calls=receiver(monkeypatch);r=row(f,kind);body=" exact comment — café\n  literal <tag> "
    if kind!="message":
        p=post(f,"prepare",{**r,"body":body});assert p.status_code==200,p.text;r=p.json()
    sent=post(f,"send",{**r,"body":body});assert sent.status_code==200,sent.text
    assert sent.json()["status"]=="delivered" and "body" not in sent.json()
    assert post(f,"receipt",r).json()==sent.json()
    assert post(f,"send",{**r,"body":body}).json()==sent.json()
    assert len(calls)==1
    changed=post(f,"send",{**r,"body":body+" changed"});assert changed.status_code in (200,403)
    assert changed.status_code==403 or changed.json()["status"]=="unknown"
    assert len(calls)==1
    assert "set-cookie" not in sent.headers and "access-control-allow-credentials" not in sent.headers


@pytest.mark.parametrize("m,n,f",[(m,n,f) for m in (0,1) for n in (0,1) for f in (0,1)])
def test_http_grants_independent_and_do_not_pause_reads(connected_actions,m,n,f):
    fixture=connected_actions
    for kind,on in (("message",m),("nudge",n),("feedback",f)):
        if on:allow(fixture,kind)
    for kind,on in (("message",m),("nudge",n),("feedback",f)):
        response=post(fixture,"context",{"room":"example",**({"kind":kind} if kind!="message" else {})})
        assert response.status_code==(200 if on else 403)
    assert fixture[1].get("/api/owner/connected/status",headers={"Origin":ORIGIN,"Authorization":"Bearer "+fixture[8]}).status_code==200


@pytest.mark.parametrize("bad",[{"Origin":"null"},{"Host":"wrong.example.test"},{"Cookie":"fake=x"},{"Authorization":"Bearer fake"},{"X-Claudlobby-Owner":"0"}])
def test_boundaries_have_no_effect(connected_actions,monkeypatch,bad):
    f=connected_actions;allow(f,"message");calls=receiver(monkeypatch);r=row(f,"message")
    response=post(f,"send",{**r,"body":"must not send"},**bad)
    assert response.status_code==403 and calls==[]


def test_closing_binding_and_grant_refusal_withhold_outcome(connected_actions,monkeypatch):
    f=connected_actions;allow(f,"feedback");calls=receiver(monkeypatch);r=row(f,"feedback")
    r=post(f,"prepare",{**r,"body":"comment"}).json()
    action=f[0].connected_actions.actions;original=action.operation
    def revoke(*args):
        result=original(*args);f[5].workspace().revoke(expected_binding=f[7],expected_owner=f[6]);return result
    monkeypatch.setattr(action,"operation",revoke)
    response=post(f,"send",{**r,"body":"comment"})
    assert response.status_code==403 and response.json()=={"state":"denied"} and len(calls)==1


def test_exact_preflight_methods_and_strict_json(connected_actions):
    f=connected_actions;allow(f,"message")
    headers={"Origin":ORIGIN,"Access-Control-Request-Method":"POST","Access-Control-Request-Headers":"Authorization, Content-Type, X-Claudlobby-Owner"}
    response=f[1].options(PREFIX+"send",headers=headers);assert response.status_code==204
    assert response.headers["access-control-allow-origin"]==ORIGIN
    assert f[1].get(PREFIX+"send",headers={"Origin":ORIGIN}).status_code==405
    assert post(f,"context",{"room":"example"},Authorization="Bearer "+f[5].open_session(OWNER).token).status_code==403
    for raw in ['{"room":"example","room":"example"}', '{"room":NaN}', '[]', '{'+ ' '*33000 +'}']:
        r=f[1].post(PREFIX+"context",content=raw,headers={"Origin":ORIGIN,"Authorization":"Bearer "+f[8],"Content-Type":"application/json","X-Claudlobby-Owner":"1"})
        assert r.status_code==403


def test_action_queue_cancellation_and_shutdown_retain_started_work(connected_actions,monkeypatch):
    import threading
    import httpx
    f=connected_actions;allow(f,"message");handler=f[0].connected_actions
    entered=threading.Event();release=threading.Event();original=handler.actions.context
    def held(*args):
        result=original(*args);entered.set();assert release.wait(5);return result
    monkeypatch.setattr(handler.actions,"context",held)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f[0]),base_url=HOST) as client:
            headers={"Origin":ORIGIN,"Authorization":"Bearer "+f[8],"Content-Type":"application/json","X-Claudlobby-Owner":"1"}
            jobs=[asyncio.create_task(client.post(PREFIX+"context",json={"room":"example"},headers=headers)) for _ in range(8)]
            for _ in range(200):
                if len(handler._workers)==8 and entered.is_set():break
                await asyncio.sleep(.005)
            assert len(handler._workers)==8 and entered.is_set()
            jobs[0].cancel()
            with pytest.raises(asyncio.CancelledError):await jobs[0]
            assert len(handler._workers)==8
            overflow=await client.post(PREFIX+"send",json={},headers=headers)
            assert overflow.status_code==503 and overflow.json()["effect"]=="not_started"
            shutdown=asyncio.create_task(handler.shutdown());await asyncio.sleep(.03)
            assert not shutdown.done() and len(handler._workers)==8
            release.set();await shutdown;await asyncio.gather(*jobs[1:]);await asyncio.sleep(0)
            assert not handler._workers
    try:asyncio.run(run())
    finally:release.set()


def test_started_send_timeout_unknown_then_pure_receipt(connected_actions,monkeypatch):
    import threading
    import httpx
    f=connected_actions;allow(f,"feedback");calls=receiver(monkeypatch);r=row(f,"feedback");r=post(f,"prepare",{**r,"body":"comment"}).json()
    handler=f[0].connected_actions;entered=threading.Event();release=threading.Event();original=handler.actions.operation
    def held(*args):
        result=original(*args);entered.set();assert release.wait(5);return result
    monkeypatch.setattr(handler.actions,"operation",held);monkeypatch.setattr(handler,"response_seconds",lambda scope:.5)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f[0]),base_url=HOST) as client:
            headers={"Origin":ORIGIN,"Authorization":"Bearer "+f[8],"Content-Type":"application/json","X-Claudlobby-Owner":"1"}
            response=await client.post(PREFIX+"send",json={**r,"body":"comment"},headers=headers)
            assert entered.is_set() and len(calls)==1 and response.status_code==503 and response.json()=={"state":"unavailable"}
            assert len(handler._workers)==1
            release.set()
            for _ in range(100):
                if not handler._workers:break
                await asyncio.sleep(.01)
            assert not handler._workers
            monkeypatch.setattr(handler.actions,"operation",original);monkeypatch.setattr(handler,"response_seconds",lambda scope:15)
            receipt=await client.post(PREFIX+"receipt",json=r,headers=headers)
            assert receipt.json()["status"]=="delivered" and len(calls)==1
    try:asyncio.run(run())
    finally:release.set()


@pytest.mark.parametrize("kind", ["message", "nudge", "feedback"])
def test_context_uses_one_binding_snapshot_for_both_hashes(connected_actions, monkeypatch, kind):
    from claudlobby.plane.owner_access import VerifiedReader
    from claudlobby.plane.owner_action_authorization import ConnectedActionAuthorization
    f = connected_actions
    allow(f, kind)
    observed = []
    original = ConnectedActionAuthorization.scope_binding
    def binding_once(policy, reader, host):
        binding = original(policy, reader, host)
        observed.append(binding)
        return binding
    monkeypatch.setattr(ConnectedActionAuthorization, "scope_binding", binding_once)
    actions = f[0].connected_actions.actions
    engine = actions if kind == "message" else getattr(actions, "nudges" if kind == "nudge" else "feedback")
    result, grant = engine._context(VerifiedReader(OWNER, f[8]), "example")
    assert len(observed) == 1
    base = [grant.fleet_uid, OWNER.namespace, OWNER.subject, grant.owner.revision,
            grant.generation, grant.actor_uid, grant.actor_alias]
    if kind != "message":
        base.insert(0, "task." + kind)
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    assert result["scope"]["workspace"] == digest([str(f[2].root), grant.owner.host_uid, observed[0]])
    assert result["scope"]["viewer"] == digest([*base, observed[0]])


@pytest.mark.parametrize("timed_out", [False, True])
def test_failed_preflight_never_reports_submission_effect(connected_actions, monkeypatch, timed_out):
    f = connected_actions
    handler = f[0].connected_actions
    headers = {"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
               "Access-Control-Request-Headers": "Authorization, Content-Type, X-Claudlobby-Owner"}
    if timed_out:
        async def held_principal(scope):
            await asyncio.sleep(10)
        monkeypatch.setattr(handler, "verify_principal", held_principal)
        monkeypatch.setattr(handler, "response_seconds", lambda scope: .02)
    else:
        headers["Access-Control-Request-Headers"] = "Content-Type"
    response = f[1].options(PREFIX + "send", headers=headers)
    assert response.status_code == (503 if timed_out else 403)
    assert response.json() == {"state": "unavailable" if timed_out else "denied"}
