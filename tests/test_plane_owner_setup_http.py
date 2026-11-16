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
from tests.test_plane_owner_setup import setup_authority, binding, synthetic_signin
from tests.test_setup_commands import team_setup
from tests.test_plane_owner_connected_http import login, assertion, headers
from tests.test_plane_two_fleets import _Sampler

HOST='https://host.example.test'
PREFIX='/api/owner/connected/setup/'

@pytest.fixture(params=["repository", "local"] )
def setup_http(setup_authority,profile,monkeypatch,request,tmp_path):
    store,access,grant,reader,clock,effects=setup_authority
    if request.param == "local":
        monkeypatch.setenv("HOME", str(tmp_path))
        project=tmp_path/"project"; project.mkdir()
        grant=store.candidate(OWNER,template="local-project",fleet="shop",repository=None,
            project_directory=str(project),install_directory=grant.install_directory)
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
        assert s['setup_profile']['profile']=='first-team-setup-v2'
        before=access.path.read_bytes()
        assert post(client,'context',s['token']).status_code==503
        assert access.path.read_bytes()==before
        assert client.get('/api/owner/connected/read/overview',headers=headers(s['token'])).status_code in (403,503)
        assert client.post('/api/owner/connected/actions/context',json={'room':'shop'},headers={**headers(s['token']),'X-Claudlobby-Owner':'1'}).status_code==503
        app.connected_setup.engine.store.allow(grant)
        context=post(client,'context',s['token'])
        assert context.status_code==200 and context.json()['request'] is None
        assert context.json()['version'] == (3 if grant.template == 'local-project' else 2)
        assert context.json()['template'] == grant.template
        assert 'set-cookie' not in context.headers and 'access-control-allow-credentials' not in context.headers


def test_http_plan_exact_launch_pure_original_receipt_and_partial_source_transition(setup_http,monkeypatch):
    from claudlobby import git_update
    from claudlobby.plane.db import db_file
    app,access,_,_,_,grant=setup_http
    app.connected_setup.engine.store.allow(grant)
    calls=[]
    def apply(installation,*,expected_request_id):
        calls.append(expected_request_id)
        assert app.connected_setup.engine.store.local()==grant
        db_file(access.root).write_bytes(b"unbound partially-created recorder")
        return {"update":git_update.read_update(installation),"dispatch":"submitted"}
    monkeypatch.setattr(git_update,"apply_or_continue",apply)
    with TestClient(app,base_url=HOST) as client:
        s=session(client,setup_http); token=s['token']
        preview=post(client,'preview',token,{'generation':grant.generation,'goal':'Improve checkout tests'})
        assert preview.status_code==200,preview.text
        plan=preview.json(); request=binding(plan)
        result=post(client,'launch',token,request)
        assert result.status_code==200 and result.json()['state']=='quiesce',result.text
        assert calls==[request['request_id']]
        status=client.get('/api/owner/connected/status',headers=headers(token))
        assert status.status_code==200 and status.json()['state']=='setup_only' and 'read_profile' not in status.json()
        assert post(client,'receipt',token,request).json()==result.json()
        assert post(client,'launch',token,request).json()==result.json() and len(calls)==1
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
        for override in ({'project_directory':'/browser-chosen'}, {'account_directory':'/browser-profile'},
                         {'managed_owner':{'origin':'https://browser.example.test'}},
                         {'template':'local-project'}, {'repository':'other/repo'}):
            assert post(client,'preview',s['token'],{'generation':grant.generation,'goal':'Improve tests',**override}).status_code==403
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


def test_provider_signin_routes_require_local_scope_and_never_accept_paths(setup_http):
    app,access,_,_,_,grant=setup_http
    app.connected_setup.engine.store.allow(grant)
    with TestClient(app,base_url=HOST) as client:
        token=session(client,setup_http)['token']
        args={'generation':grant.generation,'attempt_id':str(uuid4())}
        for operation in ['signin-begin','signin-status','signin-code','signin-cancel']:
            body={**args,**({'code':'private-code#private-state'} if operation=='signin-code' else {})}
            response=post(client,operation,token,body)
            assert response.status_code==403
            assert 'private-code' not in response.text
            assert post(client,operation,token,{**body,'binary':'/browser-choice'}).status_code==403


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


@pytest.mark.parametrize("changes",[{"release_id":"r-"+"a"*64},{"source_revision":None},
    {"config_fingerprint":"p-"+"b"*64},{"source_revision":"a"*39}])
def test_v2_launch_rejects_mixed_or_missing_provenance_before_engine(setup_http,monkeypatch,changes):
    app,access,_,_,_,grant=setup_http
    app.connected_setup.engine.store.allow(grant)
    with TestClient(app,base_url=HOST) as client:
        token=session(client,setup_http)["token"]
        preview=post(client,"preview",token,{"generation":grant.generation,"goal":"Improve checkout tests"})
        assert preview.status_code==200,preview.text
        request=binding(preview.json())
        monkeypatch.setattr(app.connected_setup.engine,"launch",lambda *_a,**_k:pytest.fail("entered launch"))
        result=post(client,"launch",token,{**request,**changes})
        assert result.status_code==403


@pytest.mark.parametrize('finish',['shutdown','success'])
def test_provider_signin_real_http_and_shutdown_drain(synthetic_signin,profile,finish):
    import time
    engine,grant,_,_,token,env=synthetic_signin
    access=engine.store.access
    async def verify(scope):return OWNER
    app=create_owner_browser_app(access.root,verify_principal=verify,expected_origin=HOST,
        sampler=_Sampler({}),workspace_bootstrap=WorkspaceBootstrapConfig(ISSUER,ORIGIN))
    app.access._clock=access._clock
    async def keys(_):return json.dumps({'keys':[profile[1]]}).encode()
    app.workspace_bootstrap.keys.get=keys
    fixture=app,access,[OWNER],[1800000000.0],profile,grant
    with TestClient(app,base_url=HOST) as client:
        bearer=session(client,fixture)['token']
        assert post(client,'context',bearer).json()['provider_signin'] is True
        args={'generation':grant.generation,'attempt_id':str(uuid4())}
        begin=post(client,'signin-begin',bearer,args)
        assert begin.status_code==200,begin.text
        assert post(client,'signin-begin',bearer,{**args,'attempt_id':str(uuid4())}).status_code==403
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            status=post(client,'signin-status',bearer,args)
            if status.json().get('state')=='waiting':break
            time.sleep(.025)
        assert status.json()['state']=='waiting',status.text
        worker=app.connected_setup.engine.signin._thread
        for code in ['sensitive#wrong','accepted\n#'+'b'*32,'a'*2306]:
            rejected=post(client,'signin-code',bearer,{**args,'code':code})
            assert rejected.status_code==403 and 'sensitive' not in rejected.text
        assert token not in status.text
        if finish=='success':
            assert post(client,'signin-code',bearer,{**args,'code':'accepted#'+'b'*32}).status_code==200
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                status=post(client,'signin-status',bearer,args)
                if status.json().get('state')=='connected':break
                time.sleep(.025)
            assert status.json()['state']=='connected',status.text
            assert token not in status.text
    assert not worker.is_alive()
    if finish=='shutdown':assert env.read_text()=='export ROUTE=private\n'
    else:assert env.read_text().count('CLAUDE_CODE_OAUTH_TOKEN=')==1
