"""Isolated nudge HTTP composition and canonical recording; synthetic receiver."""
from dataclasses import replace
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from claudlobby import task_operations
from claudlobby.plane import owner_nudge_actions
from claudlobby.plane.db import db_file
from claudlobby.plane.owner_browser import COOKIE_NAME, create_owner_browser_app
from claudlobby.plane.owner_access import AccessDenied
from claudlobby.plane.owner_nudges import OwnerNudges
from claudlobby.request_queries import read_request
from claudlobby.request_receipts import RequestStore
from tests.test_plane_owner_nudges import gateway, receiver  # noqa: F401
from tests.test_activation import cold, tmp_path  # noqa: F401
from tests.test_releases import installed  # noqa: F401
from tests.test_task_read_cli import active  # noqa: F401
from tests.test_plane_owner_browser import ORIGIN, _post
from tests.test_plane_two_fleets import _Sampler


@pytest.fixture
def nudge_http(gateway):
    adapter, reader, ctx, options, selection = gateway
    async def verify(_scope):
        return reader.principal
    app = create_owner_browser_app(adapter.root, verify_principal=verify,
        expected_origin=ORIGIN, sampler=_Sampler([]), package=adapter.package)
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set(COOKIE_NAME, reader.token, domain="plane.example.test", path="/")
    return app, client, adapter, reader, ctx, options, selection


def post(client, action, payload):
    return _post(client, 'actions/' + action, body=json.dumps(payload).encode())


def context(client):
    result = post(client, 'context', {'room': 'example', 'kind': 'nudge'})
    assert result.status_code == 200, result.text
    return result.json()


def selection(fixture, assignment_id=None):
    _, client, _, _, _, options, _ = fixture
    capability = context(client)
    return {'version': 2, 'request_id': str(uuid4()), 'kind': 'nudge',
        'scope': capability['scope'], 'target': {'recipient': capability['recipients'][0]['id'],
            'task_id': options['task_id'], 'assignment_id': assignment_id,
            'release_id': capability['release_id']}, 'submitted_at': '2026-10-10T01:00:00.000Z'}


def prepared(fixture, assignment_id=None, body='Check selected work'):
    row = selection(fixture, assignment_id)
    response = post(fixture[1], 'prepare', {**row, 'body': body})
    assert response.status_code == 200, response.text
    return response.json()


def counts(adapter):
    with sqlite3.connect(db_file(adapter.root)) as conn:
        return tuple(conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                     for table in ('events', 'communications', 'assignments'))


def test_prepare_is_nonmutating_and_pins_canonical_queued_null(nudge_http):
    app, client, adapter, _, ctx, options, _ = nudge_http
    before = counts(adapter)
    paths = sorted(str(p) for p in (adapter.root/'state/requests').rglob('*.json'))
    row = selection(nudge_http)
    body = ' exact reason\nwith spaces '
    response = post(client, 'prepare', {**row, 'body': body})
    assert response.status_code == 200
    assert response.json() == {**row, 'semantic_sha256': task_operations.nudge_semantic_digest(
        options['task_id'], reason=body, by=ctx.caller.alias, expected_assignment_id=None)}
    assert 'status' not in response.json() and 'body' not in response.json()
    assert counts(adapter) == before
    assert sorted(str(p) for p in (adapter.root/'state/requests').rglob('*.json')) == paths
    assert app._action_workers == len(app._inflight) == 0


def test_context_grants_are_independent_and_message_v1_is_unchanged(nudge_http):
    _, client, adapter, _, ctx, options, _ = nudge_http
    access, grant = adapter.access, options['expected_grant']
    nudge = context(client)
    assert nudge['version'] == 2 and nudge['actions'] == ['nudge']
    assert nudge['recipients'] == [{'id': ctx.bots['manager'].uid, 'label': 'manager', 'lead': True}]
    assert post(client, 'context', {'room': 'example'}).status_code == 403
    messages = access.allow_messages(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
        actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    message = post(client, 'context', {'room': 'example'}).json()
    assert message['version'] == 1 and message['actions'] == ['message'] and 'release_id' not in message
    assert context(client) == nudge
    access.revoke_messages(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid, expected_grant=messages)
    assert context(client) == nudge
    access.allow_messages(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
        actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    message = post(client, 'context', {'room': 'example'}).json()
    access.revoke_nudges(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid, expected_grant=grant)
    assert post(client, 'context', {'room': 'example', 'kind': 'nudge'}).status_code == 403
    assert post(client, 'context', {'room': 'example'}).json() == message
    replacement = access.allow_nudges(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
        actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    assert replacement.generation != grant.generation
    assert context(client)['scope']['viewer'] != nudge['scope']['viewer']
    assert post(client, 'context', {'room': 'example'}).json() == message


@pytest.mark.parametrize('case', ['missing_assignment', 'empty_assignment', 'wrong_task', 'wrong_manager',
    'wrong_scope', 'wrong_release', 'bool_version', 'naive_time', 'extra', 'empty_reason', 'long_reason', 'surrogate'])
def test_prepare_strict_schema_refuses_without_recording(nudge_http, case):
    _, client, adapter, *_ = nudge_http
    payload = {**selection(nudge_http), 'body': 'Reason'}
    target = payload['target']
    if case == 'missing_assignment': target.pop('assignment_id')
    elif case == 'empty_assignment': target['assignment_id'] = ''
    elif case == 'wrong_task': target['task_id'] = 'wi_' + 'f'*32
    elif case == 'wrong_manager': target['recipient'] = 'actor_' + 'f'*32
    elif case == 'wrong_scope': payload['scope']['viewer'] = 'foreign'
    elif case == 'wrong_release': target['release_id'] = 'r-' + '0'*64
    elif case == 'bool_version': payload['version'] = True
    elif case == 'naive_time': payload['submitted_at'] = '2026-10-10T01:00:00'
    elif case == 'extra': payload['actor'] = 'human:forged'
    elif case == 'empty_reason': payload['body'] = ' '
    elif case == 'long_reason': payload['body'] = 'x'*2001
    else: payload['body'] = '\ud800'
    before = counts(adapter)
    response = post(client, 'prepare', payload)
    assert response.status_code == 403, response.text
    assert response.json() == {'state': 'denied'}
    assert counts(adapter) == before


def test_assigned_prepare_and_stale_assignment_refusal(nudge_http):
    _, client, adapter, _, ctx, options, _ = nudge_http
    row = selection(nudge_http)
    assigned = task_operations.assign(ctx, str(uuid4()), options['task_id'], bot_id='worker')
    before = counts(adapter)
    assert post(client, 'prepare', {**row, 'body': 'Old selection'}).status_code == 403
    current = prepared(nudge_http, assigned.assignment_id)
    assert current['target']['assignment_id'] == assigned.assignment_id
    assert counts(adapter) == before


@pytest.mark.parametrize('received,altered,status', [(True, False, 'delivered'), (False, False, 'recorded'), (True, True, 'recorded')])
def test_send_and_receipt_use_canonical_pair_and_receiver_integrity(nudge_http, monkeypatch, received, altered, status):
    _, client, adapter, _, ctx, *_ = nudge_http
    calls, repairs = receiver(monkeypatch, received=received, altered=altered)
    row = prepared(nudge_http)
    response = post(client, 'send', {**row, 'body': 'Check selected work'})
    assert response.status_code == 200 and response.json() == {**row, 'status': status}
    retained = read_request(adapter.root, ctx.fleet_uid, row['request_id'])
    assert retained.operation == 'task.nudge'
    assert {f.family for f in retained.stages[0].expected_facts} == {'task', 'communication'}
    assert retained.stages[0].proof.status == 'committed'
    assert post(client, 'receipt', row).json() == response.json()
    assert post(client, 'send', {**row, 'body': 'Check selected work'}).json() == response.json()
    assert len(calls) == len(repairs) == 1


def test_changed_reason_old_uuid_and_digest_never_inherit_success(nudge_http, monkeypatch):
    _, client, *_ = nudge_http
    calls, _ = receiver(monkeypatch)
    row = prepared(nudge_http)
    assert post(client, 'send', {**row, 'body': 'Check selected work'}).json()['status'] == 'delivered'
    refusal = post(client, 'send', {**row, 'body': 'Changed reason'})
    assert refusal.json() == {'state': 'denied', 'effect': 'not_started'}
    changed = post(client, 'prepare', {**{k:v for k,v in row.items() if k!='semantic_sha256'}, 'body':'Changed reason'}).json()
    assert changed['semantic_sha256'] != row['semantic_sha256']
    assert post(client, 'send', {**changed, 'body':'Changed reason'}).json()['status'] == 'unknown'
    assert post(client, 'receipt', changed).json()['status'] == 'unknown'
    assert post(client, 'receipt', row).json()['status'] == 'delivered'
    assert len(calls) == 1


def test_original_receipt_uses_retained_assignment_and_release(nudge_http, monkeypatch):
    app, client, adapter, _, ctx, options, _ = nudge_http
    calls, _ = receiver(monkeypatch)
    row = prepared(nudge_http)
    assert post(client, 'send', {**row, 'body':'Check selected work'}).json()['status'] == 'delivered'
    assigned = task_operations.assign(ctx, str(uuid4()), options['task_id'], bot_id='worker')
    original = app.actions.nudges._context
    def projected_current_release(*args):
        capability, grant = original(*args)
        return {**capability, 'release_id':'r-'+'1'*64}, grant
    monkeypatch.setattr(app.actions.nudges, '_context', projected_current_release)
    # Current capability projection moves; original receipt fields never rebind.
    assert post(client, 'receipt', row).json() == {**row, 'status':'delivered'}
    for target in ({**row['target'], 'assignment_id':assigned.assignment_id},
                   {**row['target'], 'release_id':'r-'+'1'*64}):
        assert post(client, 'receipt', {**row, 'target':target}).json()['status'] == 'unknown'
    assert len(calls) == 1


def test_missing_receipt_never_claims_rejected(nudge_http):
    row = prepared(nudge_http)
    assert post(nudge_http[1], 'receipt', row).json() == {**row, 'status':'unknown'}


def test_reservation_failure_records_pair_but_never_delivers_or_replays(nudge_http, monkeypatch):
    _, client, *_ = nudge_http
    calls, _ = receiver(monkeypatch)
    row = prepared(nudge_http)
    original = RequestStore._save
    def broken(store, receipt):
        if receipt.request_id == row['request_id'] and receipt.message_attempts:
            raise OSError('private reservation persistence path')
        return original(store, receipt)
    with monkeypatch.context() as fault:
        fault.setattr(RequestStore, '_save', broken)
        sent = post(client, 'send', {**row, 'body':'Check selected work'})
    assert sent.json() == {**row, 'status':'recorded'}
    assert post(client, 'receipt', row).json() == sent.json()
    assert post(client, 'send', {**row, 'body':'Check selected work'}).json() == sent.json()
    assert calls == []


@pytest.mark.parametrize('action', ['context','prepare','send','receipt'])
def test_revocation_at_response_boundary_hides_metadata(nudge_http, monkeypatch, action):
    app, client, adapter, _, ctx, options, _ = nudge_http
    calls, _ = receiver(monkeypatch)
    row = prepared(nudge_http)
    payload = ({'room':'example','kind':'nudge'} if action=='context' else
        {**{k:v for k,v in row.items() if k!='semantic_sha256'},'body':'Check selected work'} if action=='prepare' else
        {**row,'body':'Check selected work'} if action=='send' else row)
    original = app.actions.admit_response
    def revoked(which, reader, result):
        adapter.access.revoke_nudges(expected_owner=options['expected_grant'].owner,
                                    fleet_uid=ctx.fleet_uid)
        return original(which, reader, result)
    monkeypatch.setattr(app.actions, 'admit_response', revoked)
    response = post(client, action, payload)
    assert response.status_code == 403 and response.json() == {'state':'denied'}
    assert row['request_id'] not in response.text
    assert len(calls) == (1 if action=='send' else 0)


def test_prepare_source_admission_and_task_read_share_transaction(nudge_http, monkeypatch):
    _, client, *_ = nudge_http
    connections = []
    admit, show = owner_nudge_actions.admit_source, owner_nudge_actions.show_task
    def admitted(conn, host):
        assert conn.in_transaction
        connections.append(conn)
        return admit(conn, host)
    def shown(conn, *args, **kwargs):
        assert connections[-1] is conn and conn.in_transaction
        return show(conn, *args, **kwargs)
    monkeypatch.setattr(owner_nudge_actions, 'admit_source', admitted)
    monkeypatch.setattr(owner_nudge_actions, 'show_task', shown)
    assert post(client, 'prepare', {**selection(nudge_http),'body':'Reason'}).status_code == 200
    assert len(connections) == 2  # computation and held-response re-admission


@pytest.mark.parametrize('method',['GET','HEAD','PUT'])
def test_prepare_method_refusal_preserves_allow(nudge_http, method):
    response = nudge_http[1].request(method, '/api/owner/actions/prepare')
    assert response.status_code == 405 and response.headers['Allow'] == 'POST'


def test_prepare_and_message_context_share_worker_ceiling(nudge_http, monkeypatch):
    import asyncio
    import threading
    from tests.test_plane_owner_browser import _raw_http
    app, _, _, reader, *_ = nudge_http
    release = threading.Event()
    entered = []
    def held(*args):
        entered.append(1)
        assert release.wait(5)
        return {'version':2}
    monkeypatch.setattr(app.actions, 'prepare', held)
    monkeypatch.setattr(app.actions, 'context', held)
    monkeypatch.setattr(app.actions, 'admit_response', lambda *args: None)
    async def request(action):
        return await _raw_http(app, '/api/owner/actions/'+action,
            headers=[(b'cookie', f'{COOKIE_NAME}={reader.token}'.encode())],
            chunks=[{'type':'http.request','body':b'{}','more_body':False}])
    async def exercise():
        requests = [asyncio.create_task(request('prepare'))] + [asyncio.create_task(request('context')) for _ in range(7)]
        try:
            while len(entered) != 8: await asyncio.sleep(.01)
            assert app._action_workers == 8
            assert (await request('prepare'))[0]['status'] == 503
            requests[0].cancel()
            with pytest.raises(asyncio.CancelledError): await requests[0]
            assert app._action_workers == 8
            assert (await request('context'))[0]['status'] == 503
        finally:
            release.set()
            await asyncio.gather(*requests, return_exceptions=True)
            while app._inflight: await asyncio.sleep(.01)
        assert app._action_workers == 0
        assert (await request('prepare'))[0]['status'] == 200
    asyncio.run(asyncio.wait_for(exercise(), 7))


def test_source_change_during_prepare_refuses_before_private_metadata(nudge_http, monkeypatch):
    _, client, adapter, *_ = nudge_http
    row = selection(nudge_http)
    original = owner_nudge_actions.show_task
    def changed(conn, *args, **kwargs):
        task = original(conn, *args, **kwargs)
        with sqlite3.connect(db_file(adapter.root)) as writer:
            writer.execute("UPDATE owner_source_binding SET host_uid='host_' || ?", ('f'*32,))
        return task
    monkeypatch.setattr(owner_nudge_actions, 'show_task', changed)
    response = post(client, 'prepare', {**row,'body':'Private selected reason'})
    assert response.status_code == 403
    assert response.json() == {'state':'denied'}
    assert row['request_id'] not in response.text and 'Private' not in response.text


@pytest.mark.parametrize('problem',['terminal','unresolved'])
def test_prepare_rejects_terminal_and_unresolved_task(nudge_http, problem):
    _, client, adapter, _, ctx, options, _ = nudge_http
    row = selection(nudge_http)
    if problem == 'terminal':
        task_operations.withdraw(ctx, str(uuid4()), options['task_id'], reason='Closed fixture')
    else:
        from tests.test_task_state import _insert
        with sqlite3.connect(db_file(adapter.root)) as conn:
            for worker in ctx.bots.values():
                _insert(conn, 'assignments', assignment_id='asg_'+uuid4().hex,
                    work_item_id=options['task_id'], fleet_uid=ctx.fleet_uid,
                    assignee_uid=worker.uid, assigned_by_uid=ctx.caller.uid)
            conn.execute('UPDATE assignments SET host_uid=?',(ctx.host_uid,))
    before = counts(adapter)
    result = post(client,'prepare',{**row,'body':'No task mutation'})
    assert result.status_code == 403 and result.json() == {'state':'denied'}
    assert counts(adapter) == before


def test_reallowed_nudge_generation_refuses_old_row_only(nudge_http, monkeypatch):
    _, client, adapter, _, ctx, options, _ = nudge_http
    calls, _ = receiver(monkeypatch)
    row = prepared(nudge_http)
    grant = options['expected_grant']
    adapter.access.revoke_nudges(expected_owner=grant.owner,fleet_uid=ctx.fleet_uid,expected_grant=grant)
    adapter.access.allow_nudges(expected_owner=grant.owner,fleet_uid=ctx.fleet_uid,
                              actor_uid=ctx.caller.uid,actor_alias=ctx.caller.alias)
    assert post(client,'send',{**row,'body':'Check selected work'}).json() == {'state':'denied','effect':'not_started'}
    assert post(client,'receipt',row).json() == {'state':'denied'}
    assert calls == []


def test_raw_presemantic_adapter_failure_cannot_confirm_changed_uuid_intent(nudge_http, monkeypatch):
    _, client, *_ = nudge_http
    calls, _ = receiver(monkeypatch)
    row = prepared(nudge_http)
    assert post(client,'send',{**row,'body':'Check selected work'}).json()['status']=='delivered'
    changed = post(client,'prepare',{**{k:v for k,v in row.items() if k!='semantic_sha256'},'body':'New reason'}).json()
    def before_semantics(*args,**kwargs):
        raise ValueError('private canonical presemantic failure')
    monkeypatch.setattr(OwnerNudges,'nudge',before_semantics)
    result=post(client,'send',{**changed,'body':'New reason'})
    assert result.json()=={**changed,'status':'unknown'}
    assert len(calls)==1


def test_adapter_entry_refusal_never_marks_not_started(nudge_http, monkeypatch):
    _, client, *_ = nudge_http
    calls, _ = receiver(monkeypatch)
    row=prepared(nudge_http)
    original=OwnerNudges.nudge
    def post_effect(self,*args,**kwargs):
        original(self,*args,**kwargs)
        raise AccessDenied('private post-effect refusal')
    monkeypatch.setattr(OwnerNudges,'nudge',post_effect)
    result=post(client,'send',{**row,'body':'Check selected work'})
    assert result.status_code==403 and result.json()=={'state':'denied'}
    assert post(client,'receipt',row).json()['status']=='delivered'
    assert len(calls)==1
