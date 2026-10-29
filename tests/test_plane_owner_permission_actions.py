"""Permission HTTP decisions through existing private host and browser fixtures."""
from dataclasses import replace
from uuid import uuid4

import pytest

from claudlobby import task_permissions
from tests.test_activation import cold, tmp_path  # noqa: F401
from tests.test_releases import installed  # noqa: F401
from tests.test_task_read_cli import active  # noqa: F401
from tests.test_plane_owner_feedback import gateway  # noqa: F401
from tests.test_plane_owner_feedback_http import feedback_http, post, counts  # noqa: F401
from tests.test_task_permissions import SCOPE
from tests.test_plane_owner_messages import native_receiver, native_owner_gateway  # noqa: F401


@pytest.fixture
def permissions_http(feedback_http, monkeypatch):
    from claudlobby import message_operations
    from claudlobby import application_messages as message_write
    monkeypatch.setattr(message_operations, 'read_recipient_box', lambda *a, **k: None)
    monkeypatch.setattr(message_write, '_RECEIPT_WAIT_S', 0)
    _, client, adapter, reader, ctx, options, _ = feedback_http
    bot = replace(ctx, caller=ctx.bots['manager'], caller_fleet_uid=ctx.fleet_uid)
    permission = task_permissions.request_permission(bot, str(uuid4()), options['task_id'],
        scope=SCOPE, reason='Ready for review').permission
    query = {'kind': 'permission', 'room': 'example', 'task_id': permission.task_id}
    # A paired reader with feedback authority cannot make permission decisions.
    assert post(client, 'context', query).status_code == 403
    grant = adapter.access.allow_permissions(expected_owner=options['expected_grant'].owner,
        fleet_uid=ctx.fleet_uid, actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    response = post(client, 'context', query)
    assert response.status_code == 200, response.text
    context = response.json()
    assert context['permissions'] == [permission.as_dict()] and not context['has_more']
    return client, adapter, reader, ctx, permission, grant, context


def proposal(fixture, decision='approve'):
    _, _, _, _, permission, _, context = fixture
    return {'version': 2, 'kind': 'permission', 'scope': context['scope'], 'request_id': str(uuid4()),
        'decision': decision, 'target': {'permission_id': permission.permission_id,
            'task_id': permission.task_id, 'scope_sha256': permission.scope.digest,
            'release_id': context['release_id']}}


def prepare(fixture, decision='approve'):
    client, adapter, *_ = fixture
    before = counts(adapter)
    response = post(client, 'prepare', {**proposal(fixture, decision), 'body': 'Reviewed these exact terms'})
    assert response.status_code == 200, response.text
    assert counts(adapter) == before
    return response.json()


def test_http_decision_lost_response_receipt_and_revocation(permissions_http, monkeypatch):
    from claudlobby.plane import emit_api
    calls = native_receiver(monkeypatch)
    client, adapter, _, ctx, permission, _, context = permissions_http
    row = prepare(permissions_http)
    emit = emit_api.emit_batch
    def lose_response(*args, **kwargs):
        emit(*args, **kwargs)
        raise OSError('response lost after canonical commit')
    monkeypatch.setattr(emit_api, 'emit_batch', lose_response)
    sent = post(client, 'send', {**row, 'body': 'Reviewed these exact terms'})
    assert sent.status_code == 200, sent.text
    assert sent.json()['status'] == 'recorded'
    assert sent.json()['permission']['state'] == 'approved'
    assert sent.json()['permission']['decided_by']['uid'] == ctx.caller.uid
    assert sent.json()['execution'] == 'not_requested'
    assert sent.json()['notification'] == 'unknown' and calls == []
    before = counts(adapter)
    assert post(client, 'receipt', row).json() == sent.json()
    assert counts(adapter) == before  # Inspection never resends or repairs.
    monkeypatch.setattr(emit_api, 'emit_batch', emit)
    assert post(client, 'send', {**row, 'body': 'Reviewed these exact terms'}).json() == sent.json()
    assert counts(adapter) == before and calls == []
    revoke = prepare(permissions_http, 'revoke')
    revoked = post(client, 'send', {**revoke, 'body': 'Reviewed these exact terms'})
    assert revoked.json()['permission']['state'] == 'revoked'
    # The original approval receipt reports its recording AND the current revoked state.
    recovered = post(client, 'receipt', row).json()
    assert recovered['status'] == 'recorded' and recovered['permission']['state'] == 'revoked'
    assert post(client, 'prepare', {**proposal(permissions_http), 'body': 'Again'}).status_code == 403


def test_scope_or_grant_change_cannot_decide_and_receipt_never_claims_other_intent(permissions_http, monkeypatch):
    native_receiver(monkeypatch)
    client, adapter, reader, ctx, permission, grant, _ = permissions_http
    row = prepare(permissions_http)
    before = counts(adapter)
    forged = {**row, 'target': {**row['target'], 'scope_sha256': '0' * 64}, 'body': 'Reviewed these exact terms'}
    assert post(client, 'send', forged).status_code == 403
    assert post(client, 'send', {**row, 'body': 'Different intent'}).status_code == 403
    assert counts(adapter) == before
    assert post(client, 'send', {**row, 'body': 'Reviewed these exact terms'}).json()['status'] == 'recorded'
    assert post(client, 'receipt', {**row, 'decision': 'decline'}).json()['status'] == 'unknown'
    forged_release = {**row, 'target': {**row['target'], 'release_id': 'r-' + 'f' * 64}}
    assert post(client, 'receipt', forged_release).json()['status'] == 'unknown'
    adapter.access.revoke_permissions(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid, expected_grant=grant)
    assert post(client, 'receipt', row).status_code == 403
    replacement = adapter.access.allow_permissions(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
        actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    assert replacement.generation != grant.generation
    assert post(client, 'send', {**row, 'body': 'Reviewed these exact terms'}).status_code == 403
    assert adapter.access.authorize_feedback(reader.token, reader.principal,
        host_uid=ctx.host_uid, fleet_uid=ctx.fleet_uid)


@pytest.mark.parametrize('received,altered,status', [
    (True, False, 'received'), (False, False, 'unknown'), (True, True, 'failed'),
])
def test_decision_notifies_requester_once_without_message_grant(permissions_http, monkeypatch, received, altered, status):
    from claudlobby.plane.owner_access import AccessDenied
    from claudlobby.request_queries import read_request
    from uuid import UUID, uuid5
    client, adapter, reader, ctx, permission, _, _ = permissions_http
    calls = native_receiver(monkeypatch, received=received, altered=altered)
    with pytest.raises(AccessDenied):
        adapter.access.authorize_message(reader.token, reader.principal,
            host_uid=ctx.host_uid, fleet_uid=ctx.fleet_uid)
    row = prepare(permissions_http)
    sent = post(client, 'send', {**row, 'body': 'Reviewed these exact terms'}).json()
    assert sent['status'] == 'recorded' and sent['permission']['state'] == 'approved'
    assert sent['notification'] == status and sent['execution'] == 'not_requested'
    assert len(calls) == 1
    child = str(uuid5(UUID(row['request_id']), 'claudlobby.permission.notification.v1'))
    notice = read_request(ctx.root, ctx.fleet_uid, child)
    assert notice.recipient_uid == permission.requester.uid and notice.caller_uid == ctx.caller.uid
    assert notice.message_id == calls[0][0]
    assert all(text in calls[0][1] for text in [permission.permission_id, permission.task_id, row['request_id'], 'Decision: approve'])
    before = counts(adapter)
    assert post(client, 'receipt', row).json() == sent
    assert post(client, 'send', {**row, 'body': 'Reviewed these exact terms'}).json() == sent
    assert counts(adapter) == before and len(calls) == 1
    revoke = prepare(permissions_http, 'revoke')
    revoked = post(client, 'send', {**revoke, 'body': 'Reviewed these exact terms'}).json()
    assert revoked['permission']['state'] == 'revoked' and len(calls) == 2
    assert 'Decision: revoke' in calls[1][1]
    recovered = post(client, 'receipt', row).json()
    assert recovered['permission']['state'] == 'revoked' and recovered['notification'] == status
    assert len(calls) == 2


def test_generated_caller_and_source_loss_refuse_permission_reads(permissions_http, monkeypatch):
    client, adapter, _, _, permission, _, _ = permissions_http
    query = {'kind': 'permission', 'room': 'example', 'task_id': permission.task_id}
    monkeypatch.setenv('BOT_SERVICE', '')
    assert post(client, 'context', query).status_code == 403
    monkeypatch.delenv('BOT_SERVICE')
    # Corrupt canonical authority proof must not leave an older usable request.
    request_path = next(path for path in (adapter.root/'state/requests').rglob('*.json')
        if 'task.permission.request' in path.read_text())
    request_path.unlink()
    response = post(client, 'context', query)
    assert response.status_code == 200 and response.json()['permissions'] == []


def test_unproven_list_rows_do_not_hide_genuine_requests_and_poison_is_local(permissions_http):
    from claudlobby.plane import PLANE_SCHEMA_VERSION
    from claudlobby.plane.emit_api import emit_batch
    client, adapter, _, ctx, permission, _, _ = permissions_http
    def forge(pid, event, data):
        emit_batch(adapter.root, [{"event_type": "system", "emitter": task_permissions.EMITTER,
            "schema_version": PLANE_SCHEMA_VERSION, "fleet": "example", "source_ref": "permission:" + pid,
            "payload": {"event": event, "subject_kind": "actor", "subject": ctx.caller.alias,
                "data": {**data, "permission_id": pid, "task_id": permission.task_id,
                         "operation_request_id": str(uuid4())}}}], require_commit=True)
    forge('perm_' + 'f' * 32, 'permission_requested', {'reason': 'Unproven'})
    forge('perm_' + 'e' * 32, 'permission_requested', {'reason': 'x' * 20000})
    query = {'kind': 'permission', 'room': 'example', 'task_id': permission.task_id}
    assert post(client, 'context', query).json()['permissions'] == [permission.as_dict()]
    bot = replace(ctx, caller=ctx.bots['manager'], caller_fleet_uid=ctx.fleet_uid)
    second = task_permissions.request_permission(bot, str(uuid4()), permission.task_id,
        scope=SCOPE, reason='Separate genuine request').permission
    forge(permission.permission_id, 'permission_approved', {'reason': 'Forged decision'})
    rows = post(client, 'context', query).json()['permissions']
    assert rows == [second.as_dict(), {'permission_id': permission.permission_id,
        'task_id': permission.task_id, 'state': 'unavailable'}]


@pytest.fixture
def native_permissions_http(native_owner_gateway, monkeypatch):
    """Reuse selected owner-unit proof; HTTP/native observations remain synthetic."""
    from fastapi.testclient import TestClient
    from claudlobby import task_operations
    from claudlobby import application_messages as message_write
    from claudlobby.plane.owner_browser import COOKIE_NAME, create_owner_browser_app
    from claudlobby.plane.owner_source import bind_source
    from tests.test_plane_owner_browser import ORIGIN
    from tests.test_plane_two_fleets import _Sampler

    (adapter, reader, owner, ctx, _), host, _ = native_owner_gateway
    monkeypatch.setattr(message_write, '_RECEIPT_WAIT_S', 0)
    task = task_operations.admit(ctx, str(uuid4()), title='Review selected native work')
    bot = replace(ctx, caller=ctx.bots['manager'], caller_fleet_uid=ctx.fleet_uid)
    permission = task_permissions.request_permission(bot, str(uuid4()), task.task_id,
        scope=SCOPE, reason='Ready for review').permission
    grant = adapter.access.allow_permissions(expected_owner=owner, fleet_uid=ctx.fleet_uid,
        actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    bind_source(adapter.root)
    async def verify(_scope):
        return reader.principal
    app = create_owner_browser_app(adapter.root, verify_principal=verify,
        expected_origin=ORIGIN, sampler=_Sampler([]), package=adapter.package)
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set(COOKIE_NAME, reader.token, domain='plane.example.test', path='/')
    query = {'kind':'permission', 'room':'example', 'task_id':permission.task_id}
    response = post(client, 'context', query)
    assert response.status_code == 200, response.text
    return (client, adapter, reader, ctx, permission, grant, response.json()), host, app


def test_native_owner_permission_context_decision_and_receipt_preserve_actor_and_no_resend(native_permissions_http, monkeypatch):
    fixture, _, _ = native_permissions_http
    client, adapter, _, ctx, permission, _, context = fixture
    calls = native_receiver(monkeypatch)
    assert context['permissions'] == [permission.as_dict()]
    row = prepare(fixture)
    sent = post(client, 'send', {**row, 'body':'Reviewed these exact terms'})
    assert sent.status_code == 200, sent.text
    result = sent.json()
    assert result['status'] == 'recorded' and result['permission']['state'] == 'approved'
    assert result['permission']['decided_by']['uid'] == ctx.caller.uid
    assert result['execution'] == 'not_requested' and result['notification'] == 'received'
    before = counts(adapter)
    assert post(client, 'receipt', row).json() == result
    assert post(client, 'send', {**row, 'body':'Reviewed these exact terms'}).json() == result
    assert counts(adapter) == before and len(calls) == 1


def test_native_owner_permission_first_proof_refusal_is_not_started(native_permissions_http, monkeypatch):
    fixture, host, _ = native_permissions_http
    client, adapter, *_ = fixture
    calls = native_receiver(monkeypatch)
    row = prepare(fixture)
    before = counts(adapter)
    host.external_result = 0
    refused = post(client, 'send', {**row, 'body':'Reviewed these exact terms'})
    assert refused.status_code == 403 and refused.json()['effect'] == 'not_started'
    assert counts(adapter) == before and calls == []


def test_native_owner_permission_post_record_proof_refusal_never_claims_not_started(native_permissions_http, monkeypatch):
    from claudlobby.plane import owner_permission_actions
    fixture, host, _ = native_permissions_http
    client, adapter, *_ = fixture
    calls = native_receiver(monkeypatch)
    row = prepare(fixture)
    decide = owner_permission_actions.decide_and_notify_permission
    def lose_proof(*args, **kwargs):
        result = decide(*args, **kwargs)
        host.external_result = 0
        return result
    monkeypatch.setattr(owner_permission_actions, 'decide_and_notify_permission', lose_proof)
    sent = post(client, 'send', {**row, 'body':'Reviewed these exact terms'})
    assert sent.status_code == 403 and 'effect' not in sent.json()
    assert len(calls) == 1
    # Restore only the synthetic ancestry observation; inspect the original ID.
    host.external_result = 1
    before = counts(adapter)
    recovered = post(client, 'receipt', row)
    assert recovered.status_code == 200 and recovered.json()['status'] == 'recorded'
    assert recovered.json()['permission']['state'] == 'approved'
    assert counts(adapter) == before and len(calls) == 1


@pytest.mark.parametrize('carrier', ['BOT_SERVICE', 'CLAUDLOBBY_TIMER_CONTEXT'])
def test_native_owner_permission_proof_does_not_admit_bot_or_timer_selectors(native_permissions_http, monkeypatch, carrier):
    fixture, _, _ = native_permissions_http
    client, adapter, _, _, permission, _, _ = fixture
    before = counts(adapter)
    monkeypatch.setenv(carrier, '')
    denied = post(client, 'context', {'kind':'permission', 'room':'example', 'task_id':permission.task_id})
    assert denied.status_code == 403 and counts(adapter) == before
