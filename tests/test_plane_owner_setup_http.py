"""Real finite ASGI/crypto doors; synthetic identity/release and no native launch."""
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient

from claudlobby.plane.owner_browser import create_owner_browser_app
from claudlobby.plane.owner_workspace_http import WorkspaceBootstrapConfig
from tests.test_plane_owner_workspace import OWNER, ISSUER, ORIGIN, USER, WORKSPACE
from tests.test_plane_workspace_assertion import profile
from tests.test_plane_owner_setup import setup_authority
from tests.test_setup_commands import team_setup
from tests.test_plane_owner_connected_http import login, assertion, headers
from tests.test_plane_two_fleets import _Sampler

HOST='https://host.example.test'
PREFIX='/api/owner/connected/setup/'

@pytest.fixture
def setup_http(setup_authority,profile,monkeypatch):
    store,access,grant,reader,clock,effects=setup_authority
    principal=[OWNER]
    async def verify(scope):return principal[0]
    app=create_owner_browser_app(access.root,verify_principal=verify,expected_origin=HOST,
        sampler=_Sampler({}),workspace_bootstrap=WorkspaceBootstrapConfig(ISSUER,ORIGIN))
    app.access._clock=lambda:clock[0]
    async def keys(_):return json.dumps({'keys':[profile[1]]}).encode()
    app.workspace_bootstrap.keys.get=keys
    return app,access,principal,clock,profile,grant


def session(client,fixture):
    from tests.test_plane_owner_connected_http import post
    c=post(client,'challenge').json()
    result=post(client,'issue',{'nonce':c['nonce'],'assertion':assertion(fixture[:5],c,sub=USER,workspace_id=WORKSPACE)})
    assert result.status_code==200,result.text
    return result.json()


def post(client,operation,token,body=None,extra=None):
    return client.post(PREFIX+operation,content=json.dumps(body or {}),headers={**headers(token),
        'Content-Type':'application/json','X-Claudlobby-Owner':'1',**(extra or {})})


def test_setup_only_real_session_no_read_or_action_handle_and_no_http_grant_repair(setup_http):
    app,access,_,_,_,grant=setup_http
    with TestClient(app,base_url=HOST) as client:
        s=session(client,setup_http)
        assert s['state']=='setup_only' and 'read_profile' not in s
        assert s['setup_profile']['profile']=='first-team-setup-v1'
        before=access.path.read_bytes()
        assert post(client,'context',s['token']).status_code==503
        assert access.path.read_bytes()==before
        assert client.get('/api/owner/connected/read/overview',headers=headers(s['token'])).status_code in (403,503)
        assert client.post('/api/owner/connected/actions/context',json={'room':'shop'},headers={**headers(s['token']),'X-Claudlobby-Owner':'1'}).status_code==503
        app.connected_setup.engine.store.allow(grant)
        context=post(client,'context',s['token'])
        assert context.status_code==200 and context.json()['request'] is None
        assert 'set-cookie' not in context.headers and 'access-control-allow-credentials' not in context.headers


def test_http_plan_exact_launch_pure_original_receipt_and_partial_source_transition(setup_http,monkeypatch):
    from claudlobby.commands import host
    from claudlobby.plane.db import db_file
    app,access,_,_,_,grant=setup_http
    app.connected_setup.engine.store.allow(grant)
    calls=[]; journal=[None]
    def activate(args,root):
        calls.append(args.activation_id)
        assert app.connected_setup.engine.store.local()==grant
        db_file(root).write_bytes(b'unbound partially-created recorder')
        journal[0]={'verification':'verified','status':'activating','plan_id':args.plan_id,'release_id':grant.release_id}
    monkeypatch.setattr(host,'_activate',activate)
    monkeypatch.setattr(host,'_recorded',lambda *_:journal[0])
    with TestClient(app,base_url=HOST) as client:
        s=session(client,setup_http); token=s['token']
        preview=post(client,'preview',token,{'generation':grant.generation,'goal':'Improve checkout tests'})
        assert preview.status_code==200,preview.text
        plan=preview.json(); request={'generation':grant.generation,'plan_id':plan['plan_id'],'request_id':str(uuid4())}
        result=post(client,'launch',token,request)
        assert result.status_code==200 and result.json()['state']=='activating',result.text
        assert calls==[request['request_id']]
        status=client.get('/api/owner/connected/status',headers=headers(token))
        assert status.status_code==200 and status.json()['state']=='setup_only' and 'read_profile' not in status.json()
        assert post(client,'receipt',token,request).json()==result.json()
        assert post(client,'launch',token,request).json()==result.json() and len(calls)==1
        assert post(client,'repository',token,request).status_code==403  # Legacy grant never acquired clone authority.
        assert post(client,'repository',token,{**request,'repository':'other/repo'}).status_code==403
        assert post(client,'receipt',token,{**request,'request_id':str(uuid4())}).status_code==403
        assert post(client,'preview',token,{'generation':grant.generation,'goal':'Another goal'}).status_code==403
        assert client.get('/api/owner/connected/read/overview',headers=headers(token)).status_code in (403,503)


@pytest.mark.parametrize('extra',[{'Origin':HOST},{'Cookie':'owner=fake'},{'Host':'wrong.example'},{'X-Claudlobby-Owner':'0'}])
def test_setup_exact_boundary_denies_without_declaration(setup_http,extra):
    app,access,_,_,_,grant=setup_http
    app.connected_setup.engine.store.allow(grant)
    with TestClient(app,base_url=HOST) as client:
        s=session(client,setup_http)
        result=post(client,'preview',s['token'],{'generation':grant.generation,'goal':'Improve checkout tests'},extra)
        assert result.status_code==403
        assert not (access.root/'local').exists()


def test_strict_unknown_duplicate_json_and_preflight_no_effect_claim(setup_http):
    app,access,_,_,_,grant=setup_http
    app.connected_setup.engine.store.allow(grant)
    with TestClient(app,base_url=HOST) as client:
        s=session(client,setup_http)
        assert post(client,'preview',s['token'],{'generation':grant.generation,'goal':'Improve tests','install_directory':'/browser-chosen'}).status_code==403
        assert post(client,'preview',s['token'],{'generation':grant.generation,'goal':'Improve tests','github_policy':'browser-chosen'}).status_code==403
        assert client.post(PREFIX+'preview',content='{"goal":"a","goal":"b"}',headers={**headers(s['token']),'Content-Type':'application/json','X-Claudlobby-Owner':'1'}).status_code==403
        preflight=client.options(PREFIX+'launch',headers={'Origin':ORIGIN,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'Authorization,Content-Type,X-Claudlobby-Owner'})
        assert preflight.status_code==204 and not preflight.content
        assert not (access.root/'local').exists()


@pytest.mark.parametrize('goal',[None,42,True,[],{},'x'*2001])
def test_invalid_preview_goal_refuses_before_engine_entry(setup_http,monkeypatch,goal):
    app,access,_,_,_,grant=setup_http
    app.connected_setup.engine.store.allow(grant)
    def forbidden(*_,**__):pytest.fail('preview engine entered')
    monkeypatch.setattr(app.connected_setup.engine,'preview',forbidden)
    with TestClient(app,base_url=HOST) as client:
        s=session(client,setup_http)
        result=post(client,'preview',s['token'],{'generation':grant.generation,'goal':goal})
        assert result.status_code==403
        assert not (access.root/'local').exists()


def test_source_classification_and_foreign_recorder_never_recover(setup_http,monkeypatch):
    from claudlobby.plane.owner_source import SourceDenied,SourceUnavailable
    from claudlobby.plane.owner_access import AccessUnavailable
    app,*_=setup_http
    error=SourceDenied('foreign')
    def denied():raise error
    monkeypatch.setattr(app.connected_reads,'_setup_source',denied)
    with pytest.raises(SourceDenied) as raised:app.connected_reads._session_source(OWNER)
    assert raised.value is error
    error=SourceUnavailable('unavailable')
    with pytest.raises(SourceUnavailable) as raised:app.connected_reads._session_source(OWNER)
    assert raised.value is error  # No optional tables must not mask recorder error.
    error=AccessUnavailable()
    with pytest.raises(AccessUnavailable) as raised:app.connected_reads._session_source(OWNER)
    assert raised.value is error


@pytest.mark.parametrize('failure,status', [('generic',503),('foreign',403),('transient',503)])
def test_ready_never_labels_unadmitted_source_setup_only(setup_http,monkeypatch,failure,status):
    from claudlobby.plane.owner_access import AccessUnavailable
    from claudlobby.plane.owner_source import SourceDenied,SourceUnavailable
    app,*_=setup_http
    with TestClient(app,base_url=HOST) as client:
        s=session(client,setup_http)
        exception={'generic':AccessUnavailable(),'foreign':SourceDenied('foreign'),'transient':SourceUnavailable('transient')}[failure]
        def refused():raise exception
        monkeypatch.setattr(app.connected_reads,'_source',refused)
        # Initial and closing session admission succeed; the recorder check in
        # _ready is the actual fault point. Its fallback must prove setup-only,
        # not label a late source error as a successful setup profile.
        calls=[]
        def setup_source():
            calls.append(True)
            if len(calls)<=3:return app.access.current_grant().host_uid
            raise exception
        monkeypatch.setattr(app.connected_reads,'_setup_source',setup_source)
        response=client.get('/api/owner/connected/status',headers=headers(s['token']))
        assert response.status_code==status and response.json()['state']!='setup_only'


def test_managed_setup_retains_host_secret_and_checks_each_canonical_bot(setup_http, monkeypatch):
    """Real setup HTTP and private store; only the remote issuer is synthetic."""
    from dataclasses import replace
    from datetime import datetime, timezone
    from email.utils import formatdate
    import httpx
    import time
    from claudlobby.commands.setup import _template_github
    from claudlobby.dotenv import read
    from claudlobby.github_issuer import _binding
    from claudlobby.github_enrollment import request_digest
    from claudlobby.plane.owner_setup import SetupEngine

    app, access, _, _, _, original = setup_http
    source = access.root.parent / 'managed.yaml'
    source.write_text(json.dumps({'fleet': {'github': {'connections': {'team': {
        'name': 'Team App', 'mode': 'managed', 'app_id': 123, 'installation_id': 456,
        'permissions': {'contents': 'write'}, 'credential_refs': {
            'issuer_url_env': 'TEAM_ISSUER_URL', 'issuer_token_env': 'TEAM_ISSUER_TOKEN'}}}}}}))
    grant = replace(original, github_policy=_template_github(source, 'team'), repository_prepare=True,
        github_enrollment={'issuer_origin': 'https://issuer.example.test', 'repository_id': 789})
    engine = app.connected_setup.engine
    engine.store.allow(grant)
    requests, credentials = [], []
    approved, lose_reply = set(), [True]
    issuer_now = time.time() + 900  # Provider clock ahead of the host.
    def reply(status,body,headers=None):
        return httpx.Response(status,stream=httpx.ByteStream(json.dumps(body).encode()),headers=headers)
    def issuer(request):
        payload = json.loads(request.content)
        if request.url.path == '/v1/github/enroll':
            row = payload['request']; _binding(row['binding']); requests.append(row)
            assert payload['assertion'] == 'e30.e30.AAAA'
            if lose_reply[0]:
                lose_reply[0] = False
                raise httpx.ReadTimeout('synthetic lost reply')
            return reply(200, {'ticket':'A'*43, 'grant_id':'enrollment-'+row['request_id'],
                'new_issuance':'not_allowed','authorization_url':'https://issuer.example.test/v1/github/connect?ticket='+'A'*43})
        assert request.url.path == '/v1/github/token'
        row = payload['binding']; _binding(row); credentials.append(request.headers['Authorization'])
        assert row == engine.github.bindings(grant)[row['bot_id']]
        if row['bot_id'] not in approved:
            return reply(403, {'error':'issuance_denied'})
        return reply(200, {
            'token':'synthetic-installation-token','expires_at':datetime.fromtimestamp(issuer_now+3500,timezone.utc).isoformat(),
            'repositories':[{'id':789,'full_name':'example-org/shop'}], 'permissions':row['permissions'], 'revision':row['revision']},
            headers={'Date':formatdate(issuer_now,usegmt=True)})
    original_client = httpx.Client
    with TestClient(app,base_url=HOST) as client:
        token = session(client,setup_http)['token']
        monkeypatch.setattr(httpx,'Client',lambda **kw:original_client(transport=httpx.MockTransport(issuer),**kw))
        payload = {'generation':grant.generation,'bot':'lead'}
        challenge = post(client,'github_prepare',token,payload).json()
        assert challenge['version'] == 1
        assert post(client,'github_prepare',token,{**payload,'issuer_origin':'https://other.example.test'}).status_code == 403
        assert post(client,'github_prepare',token,{**payload,'bot':'other'}).status_code == 403
        assert post(client,'preview',token,{'generation':grant.generation,'goal':'Improve signup'}).status_code == 403
        submission = {'generation':grant.generation,'request_id':challenge['request_id'],'assertion':'e30.e30.AAAA'}
        assert post(client,'github_submit',token,submission).status_code == 503
        assert post(client,'github_prepare',token,payload).json() == challenge
        result = post(client,'github_submit',token,submission)
        assert result.status_code == 200, result.text
        assert result.json()['state'] == 'awaiting_consent' and requests[0] == requests[1]
        assert challenge['request_digest'] == request_digest(requests[-1])
        approved.add('lead')
        partial = post(client,'github_check',token,{'generation':grant.generation})
        assert partial.status_code == 200,partial.text
        assert partial.json()['state'] == 'needs_authorization'
        assert {r['bot']:r['state'] for r in partial.json()['bots']} == {
            'lead':'approved','engineer':'needs_consent','reviewer':'needs_consent'}
        env = access.root / 'local/shop/.env'
        assert not env.exists()
        approved.update({'engineer','reviewer'})
        checked = post(client,'github_check',token,{'generation':grant.generation})
        assert checked.status_code == 200,checked.text
        assert checked.json()['state'] == 'credentials_ready'
        private = read(env)
        assert set(private) == {'TEAM_ISSUER_URL','TEAM_ISSUER_TOKEN'}
        assert set(credentials) == {'Bearer '+private['TEAM_ISSUER_TOKEN']}
        assert env.stat().st_mode & 0o777 == 0o600
        context = post(client,'context',token)
        assert context.json()['github_enrollment']['credentials_ready'] is True
        assert private['TEAM_ISSUER_TOKEN'] not in context.text + result.text + checked.text + json.dumps(challenge)
        assert 'synthetic-installation-token' not in checked.text
        preview = post(client,'preview',token,{'generation':grant.generation,'goal':'Improve signup'})
        assert preview.status_code == 200,preview.text
        # A fresh engine/process and same-scope terminal reapproval keep the credential.
        engine.store.revoke(grant)
        newer = replace(grant,generation=str(uuid4()))
        engine.store.allow(newer)
        assert SetupEngine(access).github.owns_env(newer)
        refreshed = post(client,'github_prepare',token,{'generation':newer.generation,'bot':'lead'}).json()
        assert refreshed['credential_digest'] == challenge['credential_digest']
        assert refreshed['request_id'] == challenge['request_id']
        # Existing operator changes never get rewritten by a check.
        env.write_text('export OPERATOR_VALUE=keep\n')
        assert post(client,'github_check',token,{'generation':newer.generation}).status_code == 403
        assert env.read_text() == 'export OPERATOR_VALUE=keep\n'
