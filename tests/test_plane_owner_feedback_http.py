"""Isolated feedback HTTP composition and canonical feedback recording; synthetic receiver."""
from dataclasses import replace
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from claudlobby import task_operations
from claudlobby.message_payload import MessageBody
from claudlobby.plane import owner_feedback_actions
from claudlobby.plane.db import db_file
from claudlobby.plane.owner_browser import COOKIE_NAME, create_owner_browser_app
from claudlobby.plane.owner_access import AccessDenied
from claudlobby.plane.owner_feedback import OwnerFeedback
from claudlobby.request_queries import read_request
from claudlobby.request_receipts import RequestStore
from tests.test_plane_owner_feedback import gateway, receiver  # noqa: F401
from tests.test_activation import cold, tmp_path  # noqa: F401
from tests.test_releases import installed  # noqa: F401
from tests.test_task_read_cli import active  # noqa: F401
from tests.test_plane_owner_browser import ORIGIN, _post
from tests.test_plane_two_fleets import _Sampler


@pytest.fixture
def feedback_http(gateway):
    adapter, reader, ctx, options, selection = gateway
    async def verify(_scope):
        return reader.principal
    app = create_owner_browser_app(adapter.root, verify_principal=verify,
        expected_origin=ORIGIN, sampler=_Sampler([]), package=adapter.package)
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set(COOKIE_NAME, reader.token, domain="plane.example.test", path="/")
    return app, client, adapter, reader, ctx, options, selection


def post(client, action, payload, **kwargs):
    return _post(client, 'actions/' + action, body=json.dumps(payload).encode(), **kwargs)


def context(client):
    result = post(client, 'context', {'room': 'example', 'kind': 'feedback'})
    assert result.status_code == 200, result.text
    return result.json()


def selection(fixture, assignment_id=None):
    _, client, _, _, _, options, _ = fixture
    capability = context(client)
    return {'version': 2, 'request_id': str(uuid4()), 'kind': 'feedback',
        'scope': capability['scope'], 'target': {'recipient': capability['recipients'][0]['id'],
            'task_id': options['task_id'], 'assignment_id': assignment_id,
            'release_id': capability['release_id']}, 'submitted_at': '2026-10-10T01:00:00.000Z'}


def prepared(fixture, assignment_id=None, body='Comment on selected work'):
    row = selection(fixture, assignment_id)
    response = post(fixture[1], 'prepare', {**row, 'body': body})
    assert response.status_code == 200, response.text
    return response.json()


def counts(adapter):
    with sqlite3.connect(db_file(adapter.root)) as conn:
        return tuple(conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                     for table in ('events', 'communications', 'assignments'))


def test_prepare_is_nonmutating_and_pins_canonical_queued_null(feedback_http):
    app, client, adapter, _, ctx, options, _ = feedback_http
    before = counts(adapter)
    paths = sorted(str(p) for p in (adapter.root/'state/requests').rglob('*.json'))
    row = selection(feedback_http)
    body = ' exact comment — café\nwith spaces '
    response = post(client, 'prepare', {**row, 'body': body})
    assert response.status_code == 200
    assert response.json() == {**row, 'semantic_sha256': task_operations.feedback_semantic_digest(
        options['task_id'], body=MessageBody.from_input(body), expected_assignment_id=None)}
    assert 'status' not in response.json() and 'body' not in response.json()
    assert counts(adapter) == before
    assert sorted(str(p) for p in (adapter.root/'state/requests').rglob('*.json')) == paths
    assert app._action_workers == len(app._inflight) == 0


def test_context_grants_are_independent_and_message_v1_is_unchanged(feedback_http):
    _, client, adapter, _, ctx, options, _ = feedback_http
    access, grant = adapter.access, options['expected_grant']
    feedback = context(client)
    assert feedback['version'] == 2 and feedback['actions'] == ['feedback']
    assert feedback['recipients'] == [{'id': ctx.bots['manager'].uid, 'label': 'manager', 'lead': True}]
    assert post(client, 'context', {'room': 'example'}).status_code == 403
    messages = access.allow_messages(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
        actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    message = post(client, 'context', {'room': 'example'}).json()
    assert message['version'] == 1 and message['actions'] == ['message'] and 'release_id' not in message
    assert context(client) == feedback
    access.revoke_messages(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid, expected_grant=messages)
    assert context(client) == feedback
    access.allow_messages(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
        actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    message = post(client, 'context', {'room': 'example'}).json()
    access.allow_nudges(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
        actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    nudge = post(client, 'context', {'room': 'example', 'kind': 'nudge'}).json()
    assert nudge['actions'] == ['nudge'] and nudge['scope']['viewer'] != feedback['scope']['viewer']
    access.revoke_feedback(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid, expected_grant=grant)
    assert post(client, 'context', {'room': 'example', 'kind': 'feedback'}).status_code == 403
    assert post(client, 'context', {'room': 'example'}).json() == message
    assert post(client, 'context', {'room': 'example', 'kind': 'nudge'}).json() == nudge
    replacement = access.allow_feedback(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
        actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    assert replacement.generation != grant.generation
    assert context(client)['scope']['viewer'] != feedback['scope']['viewer']
    assert post(client, 'context', {'room': 'example'}).json() == message
    assert post(client, 'context', {'room': 'example', 'kind': 'nudge'}).json() == nudge


@pytest.mark.parametrize('case', ['missing_assignment', 'empty_assignment', 'wrong_task', 'wrong_manager',
    'wrong_scope', 'wrong_release', 'bool_version', 'naive_time', 'extra', 'empty_reason', 'long_reason', 'surrogate',
    'nul', 'wrong_kind', 'noncanonical_uuid', 'forged_grant', 'reply_parent'])
def test_prepare_strict_schema_refuses_without_recording(feedback_http, case):
    _, client, adapter, *_ = feedback_http
    payload = {**selection(feedback_http), 'body': 'Reason'}
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
    elif case == 'surrogate': payload['body'] = '\ud800'
    elif case == 'nul': payload['body'] = 'bad\x00body'
    elif case == 'wrong_kind': payload['kind'] = 'approve'
    elif case == 'noncanonical_uuid': payload['request_id'] = payload['request_id'].upper()
    elif case == 'forged_grant': payload['grant'] = 'forged'
    else: target['reply_to_msg_id'] = 'msg_' + 'f'*32
    before = counts(adapter)
    response = post(client, 'prepare', payload)
    assert response.status_code == 403, response.text
    assert response.json() == {'state': 'denied'}
    assert counts(adapter) == before


def test_assigned_prepare_and_stale_assignment_refusal(feedback_http):
    _, client, adapter, _, ctx, options, _ = feedback_http
    row = selection(feedback_http)
    assigned = task_operations.assign(ctx, str(uuid4()), options['task_id'], bot_id='worker')
    before = counts(adapter)
    assert post(client, 'prepare', {**row, 'body': 'Old selection'}).status_code == 403
    current = prepared(feedback_http, assigned.assignment_id)
    assert current['target']['assignment_id'] == assigned.assignment_id
    assert counts(adapter) == before


@pytest.mark.parametrize('received,altered,status', [(True, False, 'delivered'), (False, False, 'recorded'), (True, True, 'recorded')])
def test_send_and_receipt_use_one_chat_and_receiver_integrity(feedback_http, monkeypatch, received, altered, status):
    _, client, adapter, _, ctx, *_ = feedback_http
    calls, repairs = receiver(monkeypatch, received=received, altered=altered)
    row = prepared(feedback_http)
    with sqlite3.connect(db_file(adapter.root)) as conn:
        before = conn.execute("SELECT count(*) FROM events WHERE kind='task'").fetchone()[0]
    response = post(client, 'send', {**row, 'body': 'Comment on selected work'})
    assert response.status_code == 200 and response.json() == {**row, 'status': status}
    retained = read_request(adapter.root, ctx.fleet_uid, row['request_id'])
    assert retained.operation == 'task.feedback'
    assert len(retained.stages[0].expected_facts) == 1
    assert retained.stages[0].expected_facts[0].family == 'communication'
    assert retained.stages[0].proof.status == 'committed'
    with sqlite3.connect(db_file(adapter.root)) as conn:
        assert conn.execute('SELECT message_class, work_item_id, assignment_id, body FROM communications').fetchall() == [
            ('chat', row['target']['task_id'], None, 'Comment on selected work')]
        assert conn.execute("SELECT count(*) FROM events WHERE kind='task'").fetchone()[0] == before
        assert conn.execute('SELECT count(*) FROM assignments').fetchone()[0] == 0
    assert post(client, 'receipt', row).json() == response.json()
    assert post(client, 'send', {**row, 'body': 'Comment on selected work'}).json() == response.json()
    assert len(calls) == 1 and repairs == []


def test_changed_body_old_uuid_and_digest_never_inherit_success(feedback_http, monkeypatch):
    _, client, *_ = feedback_http
    calls, _ = receiver(monkeypatch)
    row = prepared(feedback_http)
    assert post(client, 'send', {**row, 'body': 'Comment on selected work'}).json()['status'] == 'delivered'
    refusal = post(client, 'send', {**row, 'body': 'Changed reason'})
    assert refusal.json() == {'state': 'denied', 'effect': 'not_started'}
    changed = post(client, 'prepare', {**{k:v for k,v in row.items() if k!='semantic_sha256'}, 'body':'Changed reason'}).json()
    assert changed['semantic_sha256'] != row['semantic_sha256']
    assert post(client, 'send', {**changed, 'body':'Changed reason'}).json()['status'] == 'unknown'
    assert post(client, 'receipt', changed).json()['status'] == 'unknown'
    assert post(client, 'receipt', row).json()['status'] == 'delivered'
    assert len(calls) == 1


def test_original_receipt_uses_retained_assignment_and_release(feedback_http, monkeypatch):
    app, client, adapter, _, ctx, options, _ = feedback_http
    calls, _ = receiver(monkeypatch)
    row = prepared(feedback_http)
    assert post(client, 'send', {**row, 'body':'Comment on selected work'}).json()['status'] == 'delivered'
    assigned = task_operations.assign(ctx, str(uuid4()), options['task_id'], bot_id='worker')
    original = app.actions.feedback._context
    def projected_current_release(*args):
        capability, grant = original(*args)
        return {**capability, 'release_id':'r-'+'1'*64}, grant
    monkeypatch.setattr(app.actions.feedback, '_context', projected_current_release)
    # Current capability projection moves; original receipt fields never rebind.
    assert post(client, 'receipt', row).json() == {**row, 'status':'delivered'}
    for target in ({**row['target'], 'assignment_id':assigned.assignment_id},
                   {**row['target'], 'release_id':'r-'+'1'*64}):
        assert post(client, 'receipt', {**row, 'target':target}).json()['status'] == 'unknown'
    assert len(calls) == 1


def test_missing_receipt_never_claims_rejected(feedback_http):
    row = prepared(feedback_http)
    assert post(feedback_http[1], 'receipt', row).json() == {**row, 'status':'unknown'}


def test_reservation_failure_records_comment_but_never_delivers_or_replays(feedback_http, monkeypatch):
    _, client, *_ = feedback_http
    calls, _ = receiver(monkeypatch)
    row = prepared(feedback_http)
    original = RequestStore._save
    def broken(store, receipt):
        if receipt.request_id == row['request_id'] and receipt.message_attempts:
            raise OSError('private reservation persistence path')
        return original(store, receipt)
    with monkeypatch.context() as fault:
        fault.setattr(RequestStore, '_save', broken)
        sent = post(client, 'send', {**row, 'body':'Comment on selected work'})
    assert sent.json() == {**row, 'status':'recorded'}
    assert post(client, 'receipt', row).json() == sent.json()
    assert post(client, 'send', {**row, 'body':'Comment on selected work'}).json() == sent.json()
    assert calls == []


@pytest.mark.parametrize('action', ['context','prepare','send','receipt'])
def test_revocation_at_response_boundary_hides_metadata(feedback_http, monkeypatch, action):
    app, client, adapter, _, ctx, options, _ = feedback_http
    calls, _ = receiver(monkeypatch)
    row = prepared(feedback_http)
    payload = ({'room':'example','kind':'feedback'} if action=='context' else
        {**{k:v for k,v in row.items() if k!='semantic_sha256'},'body':'Comment on selected work'} if action=='prepare' else
        {**row,'body':'Comment on selected work'} if action=='send' else row)
    original = app.actions.admit_response
    def revoked(which, reader, result):
        adapter.access.revoke_feedback(expected_owner=options['expected_grant'].owner,
                                    fleet_uid=ctx.fleet_uid)
        return original(which, reader, result)
    monkeypatch.setattr(app.actions, 'admit_response', revoked)
    response = post(client, action, payload)
    assert response.status_code == 403 and response.json() == {'state':'denied'}
    assert row['request_id'] not in response.text
    assert len(calls) == (1 if action=='send' else 0)


def test_prepare_source_admission_and_task_read_share_transaction(feedback_http, monkeypatch):
    _, client, *_ = feedback_http
    connections = []
    admit, show = owner_feedback_actions.admit_source, owner_feedback_actions.show_task
    def admitted(conn, host):
        assert conn.in_transaction
        connections.append(conn)
        return admit(conn, host)
    def shown(conn, *args, **kwargs):
        assert connections[-1] is conn and conn.in_transaction
        return show(conn, *args, **kwargs)
    monkeypatch.setattr(owner_feedback_actions, 'admit_source', admitted)
    monkeypatch.setattr(owner_feedback_actions, 'show_task', shown)
    assert post(client, 'prepare', {**selection(feedback_http),'body':'Reason'}).status_code == 200
    assert len(connections) == 2  # computation and held-response re-admission


@pytest.mark.parametrize('method',['GET','HEAD','PUT'])
def test_prepare_method_refusal_preserves_allow(feedback_http, method):
    response = feedback_http[1].request(method, '/api/owner/actions/prepare')
    assert response.status_code == 405 and response.headers['Allow'] == 'POST'


def test_prepare_and_message_context_share_worker_ceiling(feedback_http, monkeypatch):
    import asyncio
    import threading
    from tests.test_plane_owner_browser import _raw_http
    app, _, _, reader, *_ = feedback_http
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


def test_source_change_during_prepare_refuses_before_private_metadata(feedback_http, monkeypatch):
    _, client, adapter, *_ = feedback_http
    row = selection(feedback_http)
    original = owner_feedback_actions.show_task
    def changed(conn, *args, **kwargs):
        task = original(conn, *args, **kwargs)
        with sqlite3.connect(db_file(adapter.root)) as writer:
            writer.execute("UPDATE owner_source_binding SET host_uid='host_' || ?", ('f'*32,))
        return task
    monkeypatch.setattr(owner_feedback_actions, 'show_task', changed)
    response = post(client, 'prepare', {**row,'body':'Private selected reason'})
    assert response.status_code == 403
    assert response.json() == {'state':'denied'}
    assert row['request_id'] not in response.text and 'Private' not in response.text




def test_reallowed_feedback_generation_refuses_old_row_only(feedback_http, monkeypatch):
    _, client, adapter, _, ctx, options, _ = feedback_http
    calls, _ = receiver(monkeypatch)
    row = prepared(feedback_http)
    grant = options['expected_grant']
    adapter.access.revoke_feedback(expected_owner=grant.owner,fleet_uid=ctx.fleet_uid,expected_grant=grant)
    adapter.access.allow_feedback(expected_owner=grant.owner,fleet_uid=ctx.fleet_uid,
                              actor_uid=ctx.caller.uid,actor_alias=ctx.caller.alias)
    assert post(client,'send',{**row,'body':'Comment on selected work'}).json() == {'state':'denied','effect':'not_started'}
    assert post(client,'receipt',row).json() == {'state':'denied'}
    assert calls == []


def test_raw_presemantic_adapter_failure_cannot_confirm_changed_uuid_intent(feedback_http, monkeypatch):
    _, client, *_ = feedback_http
    calls, _ = receiver(monkeypatch)
    row = prepared(feedback_http)
    assert post(client,'send',{**row,'body':'Comment on selected work'}).json()['status']=='delivered'
    changed = post(client,'prepare',{**{k:v for k,v in row.items() if k!='semantic_sha256'},'body':'New reason'}).json()
    def before_semantics(*args,**kwargs):
        raise ValueError('private canonical presemantic failure')
    monkeypatch.setattr(OwnerFeedback,'submit',before_semantics)
    result=post(client,'send',{**changed,'body':'New reason'})
    assert result.json()=={**changed,'status':'unknown'}
    assert len(calls)==1


def test_adapter_entry_refusal_never_marks_not_started(feedback_http, monkeypatch):
    _, client, *_ = feedback_http
    calls, _ = receiver(monkeypatch)
    row=prepared(feedback_http)
    original=OwnerFeedback.submit
    def post_effect(self,*args,**kwargs):
        original(self,*args,**kwargs)
        raise AccessDenied('private post-effect refusal')
    monkeypatch.setattr(OwnerFeedback,'submit',post_effect)
    result=post(client,'send',{**row,'body':'Comment on selected work'})
    assert result.status_code==403 and result.json()=={'state':'denied'}
    assert post(client,'receipt',row).json()['status']=='delivered'
    assert len(calls)==1


@pytest.mark.parametrize('state', ['queued', 'assigned', 'completed', 'failed', 'cancelled'])
def test_feedback_task_selection_includes_terminal_null_without_lifecycle_effect(feedback_http, monkeypatch, state):
    from claudlobby.report_payload import ReportPayload
    _, client, adapter, _, ctx, options, _ = feedback_http
    assignment = None
    if state in {'assigned', 'completed', 'failed'}:
        assigned = task_operations.assign(ctx, str(uuid4()), options['task_id'], bot_id='worker')
        assignment = assigned.assignment_id
        worker = replace(ctx, caller=ctx.bots['worker'], caller_fleet_uid=ctx.fleet_uid)
        if state == 'completed':
            task_operations.complete(worker, str(uuid4()), assignment, ReportPayload('completed', summary='Done'))
            assignment = None
        elif state == 'failed':
            task_operations.fail(worker, str(uuid4()), assignment, ReportPayload('failed', reason='Could not finish'))
            assignment = None
    elif state == 'cancelled':
        task_operations.withdraw(ctx, str(uuid4()), options['task_id'], reason='Closed fixture')
    with sqlite3.connect(db_file(adapter.root)) as conn:
        before = conn.execute("SELECT event_id, event, detail FROM events WHERE kind='task' ORDER BY ingest_seq").fetchall()
        assignments = conn.execute('SELECT * FROM assignments ORDER BY ingest_seq').fetchall()
        comments = conn.execute('SELECT count(*) FROM communications').fetchone()[0]
    if state in {'completed', 'failed'}:
        historical = {**selection(feedback_http, assigned.assignment_id), 'body':'Wrong historical selection'}
        assert post(client, 'prepare', historical).status_code == 403
    calls, repairs = receiver(monkeypatch)
    row = prepared(feedback_http, assignment)
    assert post(client, 'send', {**row, 'body':'Comment on selected work'}).json() == {**row, 'status':'delivered'}
    with sqlite3.connect(db_file(adapter.root)) as conn:
        assert conn.execute("SELECT event_id, event, detail FROM events WHERE kind='task' ORDER BY ingest_seq").fetchall() == before
        assert conn.execute('SELECT * FROM assignments ORDER BY ingest_seq').fetchall() == assignments
        assert conn.execute('SELECT count(*) FROM communications').fetchone()[0] == comments + 1
    assert len(calls) == 1 and repairs == []


@pytest.mark.parametrize('problem', ['wrong_fleet', 'unresolved'])
def test_prepare_rejects_unresolved_or_other_fleet(feedback_http, problem):
    _, client, adapter, _, ctx, options, _ = feedback_http
    row = selection(feedback_http)
    with sqlite3.connect(db_file(adapter.root)) as conn:
        if problem == 'wrong_fleet':
            conn.execute('UPDATE work_items SET fleet_uid=NULL WHERE work_item_id=?', (options['task_id'],))
        else:
            from tests.test_task_state import _insert
            for worker in ctx.bots.values():
                _insert(conn, 'assignments', assignment_id='asg_'+uuid4().hex,
                    work_item_id=options['task_id'], fleet_uid=ctx.fleet_uid,
                    assignee_uid=worker.uid, assigned_by_uid=ctx.caller.uid)
            conn.execute('UPDATE assignments SET host_uid=?', (ctx.host_uid,))
    before = counts(adapter)
    assert post(client, 'prepare', {**row, 'body':'No new fact'}).json() == {'state':'denied'}
    assert counts(adapter) == before


@pytest.mark.parametrize('boundary', ['after_prepare', 'adapter_entry'])
def test_assignment_change_cannot_record_or_notify(feedback_http, monkeypatch, boundary):
    _, client, adapter, _, ctx, options, _ = feedback_http
    row = prepared(feedback_http)
    calls, repairs = receiver(monkeypatch)
    if boundary == 'after_prepare':
        task_operations.assign(ctx, str(uuid4()), options['task_id'], bot_id='worker')
    else:
        original = OwnerFeedback.submit
        def raced(self, *args, **kwargs):
            task_operations.assign(ctx, str(uuid4()), options['task_id'], bot_id='worker')
            return original(self, *args, **kwargs)
        monkeypatch.setattr(OwnerFeedback, 'submit', raced)
    response = post(client, 'send', {**row, 'body':'Comment on selected work'})
    assert response.json() == {**row, 'status':'unknown'}
    assert post(client, 'receipt', row).json() == {**row, 'status':'unknown'}
    with sqlite3.connect(db_file(adapter.root)) as conn:
        assert conn.execute("SELECT count(*) FROM communications WHERE message_class='chat'").fetchone()[0] == 0
    assert calls == repairs == []


@pytest.mark.parametrize('problem', ['host', 'origin', 'intent', 'cookie', 'principal', 'session', 'source'])
def test_feedback_obeys_existing_http_authority_gates(feedback_http, monkeypatch, problem):
    from claudlobby.plane.owner_access import PrincipalRef
    app, client, adapter, reader, *_ = feedback_http
    row = prepared(feedback_http)
    calls, repairs = receiver(monkeypatch)
    kwargs = {}
    if problem == 'host': kwargs['headers'] = {'Host':'foreign.example.test'}
    elif problem == 'origin': kwargs['headers'] = {'Origin':'https://foreign.example.test'}
    elif problem == 'intent': kwargs['drop'] = ['X-Claudlobby-Owner']
    elif problem == 'cookie': client.cookies.clear()
    elif problem == 'session': adapter.access.renew_session(reader.token, reader.principal)
    elif problem == 'principal':
        async def foreign(_scope): return PrincipalRef('synthetic-verifier','other')
        monkeypatch.setattr(app, 'verify_principal', foreign)
    else:
        with sqlite3.connect(db_file(adapter.root)) as conn:
            conn.execute("UPDATE work_items SET host_uid='foreign-host'")
    response = post(client, 'send', {**row, 'body':'Comment on selected work'}, **kwargs)
    assert response.status_code == 403
    assert row['request_id'] not in response.text and 'Comment' not in response.text
    assert calls == repairs == []


@pytest.mark.parametrize('body', [b'{"kind":"feedback","kind":"feedback","room":"example"}',
    b'{"kind":"feedback","room":NaN}', b' ' * 32769])
def test_feedback_strict_bounded_json(feedback_http, body):
    assert _post(feedback_http[1], 'actions/context', body=body).status_code == 403


@pytest.mark.parametrize('change', ['session', 'source', 'manager', 'release', 'assignment'])
def test_prepare_closing_boundary_hides_changed_selection(feedback_http, monkeypatch, change):
    app, client, adapter, reader, ctx, options, _ = feedback_http
    row = selection(feedback_http)
    original = app.actions.admit_response
    def changed(action, current_reader, result):
        if change == 'session': adapter.access.renew_session(reader.token, reader.principal)
        elif change == 'source':
            with sqlite3.connect(db_file(adapter.root)) as conn:
                conn.execute("UPDATE work_items SET host_uid='foreign-host'")
        elif change == 'assignment':
            task_operations.assign(ctx, str(uuid4()), options['task_id'], bot_id='worker')
        else:
            current = app.actions.feedback._context
            def new_context(*args):
                context, grant = current(*args)
                if change == 'manager':
                    context['recipients'] = [{'id':ctx.bots['worker'].uid,'label':'worker','lead':True}]
                else: context['release_id'] = 'r-'+'f'*64
                return context, grant
            monkeypatch.setattr(app.actions.feedback, '_context', new_context)
        return original(action, current_reader, result)
    monkeypatch.setattr(app.actions, 'admit_response', changed)
    response = post(client, 'prepare', {**row, 'body':'Private comment'})
    assert response.status_code == 403 and response.json() == {'state':'denied'}


def test_lost_response_is_recovered_only_from_original_receipt(feedback_http, monkeypatch):
    _, client, _, _, _, _, _ = feedback_http
    calls, repairs = receiver(monkeypatch)
    row = prepared(feedback_http)
    submit = OwnerFeedback.submit
    def lose_result(self, *args, **kwargs):
        submit(self, *args, **kwargs)
        raise OSError('lost response after committed effect')
    with monkeypatch.context() as loss:
        loss.setattr(OwnerFeedback, 'submit', lose_result)
        loss.setattr(OwnerFeedback, 'inspect', lambda *a, **k: (_ for _ in ()).throw(OSError('receipt read temporarily unavailable')))
        assert post(client, 'send', {**row, 'body':'Comment on selected work'}).json() == {**row,'status':'unknown'}
    monkeypatch.setattr(OwnerFeedback, 'submit', lambda *a, **k: pytest.fail('receipt must never submit'))
    assert post(client, 'receipt', row).json() == {**row, 'status':'delivered'}
    assert len(calls) == 1 and repairs == []


@pytest.mark.parametrize('invalid', ['bad\x00text', '\ud800', ' ', 'x'*2001])
def test_invalid_body_on_previous_uuid_never_inherits_delivery(feedback_http, monkeypatch, invalid):
    _, client, *_ = feedback_http
    calls, repairs = receiver(monkeypatch)
    row = prepared(feedback_http)
    assert post(client, 'send', {**row, 'body':'Comment on selected work'}).json()['status'] == 'delivered'
    response = post(client, 'send', {**row, 'body':invalid})
    assert response.json() == {'state':'denied','effect':'not_started'}
    assert post(client, 'receipt', row).json()['status'] == 'delivered'
    assert len(calls) == 1 and repairs == []


@pytest.mark.parametrize('alteration', ['body', 'assignment', 'missing', 'sender', 'recipient'])
def test_receipt_requires_exact_linked_chat_and_participant_proof(feedback_http, monkeypatch, alteration):
    _, client, adapter, _, ctx, *_ = feedback_http
    calls, repairs = receiver(monkeypatch)
    row = prepared(feedback_http)
    assert post(client, 'send', {**row, 'body':'Comment on selected work'}).json()['status'] == 'delivered'
    with sqlite3.connect(db_file(adapter.root)) as conn:
        if alteration == 'body': conn.execute("UPDATE communications SET body='Different comment'")
        elif alteration == 'assignment': conn.execute("UPDATE communications SET assignment_id='asg_' || ?", ('f'*32,))
        elif alteration == 'missing': conn.execute('DELETE FROM communications')
        elif alteration == 'sender': conn.execute('UPDATE communications SET sender_uid=?, sender_alias=?',
            (ctx.bots['worker'].uid, ctx.bots['worker'].alias))
        else: conn.execute('UPDATE communications SET recipient_uid=?, recipient_alias=?',
            (ctx.bots['worker'].uid, ctx.bots['worker'].alias))
    result = post(client, 'receipt', row)
    if alteration in {'sender','recipient'}:
        assert result.status_code == 403 and result.json() == {'state':'denied'}
    else:
        assert result.json() == {**row,'status':'unknown'}
    assert len(calls) == 1 and repairs == []


@pytest.mark.parametrize('messages,nudges,feedback', [
    (False,False,False), (False,False,True), (False,True,False), (False,True,True),
    (True,False,False), (True,False,True), (True,True,False), (True,True,True)])
def test_capabilities_are_independently_granted(feedback_http, messages, nudges, feedback):
    _, client, adapter, _, ctx, options, _ = feedback_http
    access, grant = adapter.access, options['expected_grant']
    fields = dict(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
                  actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    if messages: access.allow_messages(**fields)
    if nudges: access.allow_nudges(**fields)
    if not feedback: access.revoke_feedback(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid)
    for kind, allowed in [('message',messages),('nudge',nudges),('feedback',feedback)]:
        payload = {'room':'example'} if kind == 'message' else {'room':'example','kind':kind}
        result = post(client, 'context', payload)
        assert result.status_code == (200 if allowed else 403)
        if allowed: assert result.json()['actions'] == [kind]
    assert client.get('/api/tasks').status_code == 200


def test_feedback_regrant_keeps_original_message_and_nudge_receipts(feedback_http, monkeypatch):
    from tests.test_plane_owner_messages import native_receiver
    from tests.test_plane_owner_nudges import receiver as nudge_receiver
    _, client, adapter, _, ctx, options, _ = feedback_http
    grant = options['expected_grant']
    fields = dict(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
                  actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    adapter.access.allow_messages(**fields)
    adapter.access.allow_nudges(**fields)
    message_context = post(client, 'context', {'room':'example'}).json()
    message = {'request_id':str(uuid4()),'kind':'message','scope':message_context['scope'],
        'target':{'recipient':ctx.bots['manager'].uid,'task_id':None}, 'submitted_at':'2026-10-10T01:00:00Z'}
    with monkeypatch.context() as native:
        message_calls = native_receiver(native)
        assert post(client, 'send', {**message,'body':'Unrelated message'}).json()['status'] == 'delivered'
    nudge_context = post(client, 'context', {'room':'example','kind':'nudge'}).json()
    nudge = {**selection(feedback_http), 'kind':'nudge','scope':nudge_context['scope']}
    prepared_nudge = post(client, 'prepare', {**nudge,'body':'Unrelated nudge'}).json()
    with monkeypatch.context() as native:
        nudge_calls, _ = nudge_receiver(native)
        assert post(client, 'send', {**prepared_nudge,'body':'Unrelated nudge'}).json()['status'] == 'delivered'
    for reallow in (False, True):
        if reallow: adapter.access.allow_feedback(**fields)
        else: adapter.access.revoke_feedback(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid)
        assert post(client, 'receipt', message).json() == {'version':1,**message,'status':'delivered'}
        assert post(client, 'receipt', prepared_nudge).json() == {**prepared_nudge,'status':'delivered'}
    assert len(message_calls) == len(nudge_calls) == 1
