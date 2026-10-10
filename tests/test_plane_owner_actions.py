"""Synthetic authenticated HTTP and native receiver fixtures; no ingress/live proof."""
import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from claudlobby.plane import owner_actions
from claudlobby.plane.owner_browser import COOKIE_NAME, create_owner_browser_app
from claudlobby.plane.owner_source import bind_source
from tests.test_plane_owner_messages import gateway, native_receiver  # noqa: F401
from tests.test_activation import cold, tmp_path  # noqa: F401
from tests.test_releases import installed  # noqa: F401
from tests.test_task_read_cli import active  # noqa: F401
from tests.test_plane_two_fleets import _Sampler
from tests.test_plane_owner_browser import ORIGIN, _post, _raw_http


@pytest.fixture
def browser_actions(gateway):
    adapter, reader, owner, ctx, options = gateway
    bind_source(adapter.root)
    principal = [reader.principal]
    async def verify(_scope):
        return principal[0]
    app = create_owner_browser_app(adapter.root, verify_principal=verify,
        expected_origin=ORIGIN, sampler=_Sampler([]), package=adapter.package)
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set(COOKIE_NAME, reader.token, domain="plane.example.test", path="/")
    return app, client, adapter, reader, owner, ctx, options, principal


def post(client, action, payload, **kwargs):
    return _post(client, "actions/" + action, body=json.dumps(payload).encode(), **kwargs)


def context(client):
    response = post(client, "context", {"room": "example"})
    assert response.status_code == 200, response.text
    return response.json()


def metadata(ctx, target):
    return {"scope": ctx["scope"], "kind": "message", "target": {
        "recipient": target, "task_id": None}, "request_id": str(uuid4()),
        "submitted_at": "2026-10-10T01:00:00.000Z"}


def test_context_and_send_receipt_renewal(browser_actions, monkeypatch):
    _, client, adapter, _, _, ctx, _, _ = browser_actions
    calls = native_receiver(monkeypatch)
    first = context(client)
    assert first["room"] == first["scope"]["fleet"] == "example"
    assert first["simulation"] is False and first["actions"] == ["message"]
    assert {r["id"] for r in first["recipients"]} == {a.uid for a in ctx.bots.values()}
    assert str(adapter.root) not in json.dumps(first)
    assert _post(client, "renew").status_code == 200
    assert context(client) == first
    request = metadata(first, ctx.bots["worker"].uid)
    sent = post(client, "send", {**request, "body": "Synthetic exact body"})
    assert sent.status_code == 200 and sent.json() == {"version": 1, **request, "status": "delivered"}
    assert sent.headers["cache-control"] == "no-store, private"
    assert "body" not in sent.json()
    assert post(client, "receipt", request).json() == sent.json()
    assert post(client, "send", {**request, "body": "Synthetic exact body"}).json() == sent.json()
    assert len(calls) == 1


@pytest.mark.parametrize("bad", ["scope", "recipient", "kind", "task", "uuid", "extra", "body", "time"])
def test_invalid_binding_or_shape_never_dispatches(browser_actions, monkeypatch, bad):
    _, client, _, _, _, ctx, _, _ = browser_actions
    calls = native_receiver(monkeypatch)
    request = {**metadata(context(client), ctx.bots["worker"].uid), "body": "Must not send"}
    if bad == "scope": request["scope"]["viewer"] = "forged"
    if bad == "recipient": request["target"]["recipient"] = "actor_" + "f" * 32
    if bad == "kind": request["kind"] = "feedback"
    if bad == "task": request["target"]["task_id"] = "task_" + "a" * 32
    if bad == "uuid": request["request_id"] = "not-a-uuid"
    if bad == "extra": request["actor"] = "human:forged"
    if bad == "body": request["body"] = "x" * 2001
    if bad == "time": request["submitted_at"] = "bad"
    assert post(client, "send", request).status_code == 403
    assert calls == []


@pytest.mark.parametrize("received,altered,expected", [(False, False, "recorded"), (True, True, "recorded")])
def test_only_exact_receiver_integrity_is_delivered(browser_actions, monkeypatch, received, altered, expected):
    _, client, _, _, _, ctx, _, _ = browser_actions
    calls = native_receiver(monkeypatch, received=received, altered=altered)
    request = metadata(context(client), ctx.bots["worker"].uid)
    assert post(client, "send", {**request, "body": "Exact bytes"}).json()["status"] == expected
    assert post(client, "receipt", request).json()["status"] == expected
    assert len(calls) == 1


def test_missing_request_is_unknown_not_rejected_or_sent(browser_actions, monkeypatch):
    _, client, _, _, _, ctx, _, _ = browser_actions
    calls = native_receiver(monkeypatch)
    request = metadata(context(client), ctx.bots["worker"].uid)
    assert post(client, "receipt", request).json() == {"version": 1, **request, "status": "unknown"}
    assert calls == []


@pytest.mark.parametrize("failure", ["principal", "ungranted", "source", "origin", "intent", "cookie"])
def test_every_action_authorizes_current_boundary(browser_actions, monkeypatch, failure):
    _, client, adapter, _, owner, ctx, _, principal = browser_actions
    calls = native_receiver(monkeypatch)
    request = {**metadata(context(client), ctx.bots["worker"].uid), "body": "Refused"}
    kwargs = {}
    if failure == "principal":
        from claudlobby.plane.owner_access import PrincipalRef
        principal[0] = PrincipalRef("synthetic-verifier", "other-owner")
    if failure == "ungranted": adapter.access.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
    if failure == "source":
        import sqlite3
        from claudlobby.plane.db import db_file
        with sqlite3.connect(db_file(adapter.root)) as conn:
            conn.execute("UPDATE owner_source_binding SET host_uid=?", ("host_" + "f" * 32,))
    if failure == "origin": kwargs["headers"] = {"Origin": "https://evil.example.test"}
    if failure == "intent": kwargs["drop"] = ["X-Claudlobby-Owner"]
    if failure == "cookie": client.cookies.clear()
    assert post(client, "send", request, **kwargs).status_code == 403
    assert calls == []


@pytest.mark.parametrize("body", [b'{"room":"example","room":"example"}', b'{"room":"example","extra":1}', b'{"room":NaN}', b' ' * 32769])
def test_strict_bounded_json(browser_actions, body):
    _, client, *_ = browser_actions
    assert _post(client, "actions/context", body=body).status_code == 403


def test_source_or_grant_revoked_during_effect_hides_response(browser_actions, monkeypatch):
    _, client, adapter, _, owner, ctx, _, _ = browser_actions
    calls = native_receiver(monkeypatch)
    request = metadata(context(client), ctx.bots["worker"].uid)
    original = owner_actions.OwnerMessages.inspect
    def revoke(self, *args, **kwargs):
        observed = original(self, *args, **kwargs)
        adapter.access.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
        return observed
    monkeypatch.setattr(owner_actions.OwnerMessages, "inspect", revoke)
    response = post(client, "send", {**request, "body": "Already admitted"})
    assert response.status_code == 403 and response.json() == {"state": "denied"}
    assert len(calls) == 1


def test_cancelled_http_wait_preserves_owned_send(browser_actions, monkeypatch):
    import threading
    app, client, _, reader, _, ctx, _, _ = browser_actions
    request = {**metadata(context(client), ctx.bots["worker"].uid), "body": "Owned pending effect"}
    started, finish, complete = threading.Event(), threading.Event(), threading.Event()
    def operation(*args):
        started.set()
        assert finish.wait(5)
        complete.set()
        return {"status": "unknown"}
    monkeypatch.setattr(app.actions, "operation", operation)
    async def exercise():
        task = asyncio.create_task(_raw_http(app, "/api/owner/actions/send", headers=[
            (b"cookie", f"{COOKIE_NAME}={reader.token}".encode())], chunks=[{
                "type": "http.request", "body": json.dumps(request).encode(), "more_body": False}]))
        while not started.is_set():
            await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert app._inflight
        finish.set()
        while app._inflight:
            await asyncio.sleep(.01)
        assert complete.is_set()
    asyncio.run(asyncio.wait_for(exercise(), 5))


def test_missing_activation_returns_generic_unavailable(browser_actions):
    _, client, adapter, *_ = browser_actions
    (adapter.root / "state/selected-release.json").unlink()
    response = post(client, "context", {"room": "example"})
    assert response.status_code == 503 and response.json() == {"state": "unavailable"}


def test_response_body_admission_hides_success_after_revocation(browser_actions, monkeypatch):
    app, client, adapter, _, owner, ctx, _, _ = browser_actions
    request = metadata(context(client), ctx.bots["worker"].uid)
    original = app.actions.admit_response
    def revoke(action, reader, result):
        adapter.access.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
        return original(action, reader, result)
    monkeypatch.setattr(app.actions, "admit_response", revoke)
    response = post(client, "receipt", request)
    assert response.status_code == 403 and response.json() == {"state": "denied"}
    assert request["request_id"] not in response.text


def test_nested_duplicate_key_refused(browser_actions):
    _, client, *_ = browser_actions
    body = b'{"scope":{"fleet":"example","fleet":"example"}}'
    assert _post(client, "actions/send", body=body).status_code == 403


def test_reused_uuid_different_body_never_claims_new_body_delivered(browser_actions, monkeypatch):
    _, client, _, _, _, ctx, _, _ = browser_actions
    calls = native_receiver(monkeypatch)
    request = metadata(context(client), ctx.bots["worker"].uid)
    assert post(client, "send", {**request, "body": "First body"}).json()["status"] == "delivered"
    assert post(client, "send", {**request, "body": "Different body"}).json()["status"] == "unknown"
    assert post(client, "receipt", request).json()["status"] == "delivered"
    assert len(calls) == 1


def test_action_worker_saturation_cancellation_and_capacity_recovery(browser_actions, monkeypatch):
    import threading
    app, _, _, reader, *_ = browser_actions
    release = threading.Event()
    entered = []
    lock = threading.Lock()
    def held_context(_reader, _payload):
        with lock:
            entered.append(1)
        assert release.wait(5)
        return {"scope": {"fleet": "example"}, "room": "example"}
    monkeypatch.setattr(app.actions, "context", held_context)
    monkeypatch.setattr(app.actions, "admit_response", lambda *args: None)
    async def request():
        return await _raw_http(app, "/api/owner/actions/context", headers=[
            (b"cookie", f"{COOKIE_NAME}={reader.token}".encode())], chunks=[{
                "type": "http.request", "body": b'{"room":"example"}', "more_body": False}])
    async def exercise():
        requests = [asyncio.create_task(request()) for _ in range(8)]
        try:
            while len(entered) != 8:
                await asyncio.sleep(.01)
            assert app._action_workers == len(app._inflight) == 8
            overflow = await request()
            assert overflow[0]["status"] == 503
            assert b'"state":"unavailable"' in overflow[-1]["body"]
            overflow_send = await _raw_http(app, "/api/owner/actions/send", headers=[
                (b"cookie", f"{COOKIE_NAME}={reader.token}".encode())], chunks=[{
                    "type": "http.request", "body": b"{}", "more_body": False}])
            assert overflow_send[0]["status"] == 503
            assert json.loads(overflow_send[-1]["body"]) == {"state": "unavailable", "effect": "not_started"}
            assert len(entered) == 8
            requests[0].cancel()
            with pytest.raises(asyncio.CancelledError): await requests[0]
            assert app._action_workers == len(app._inflight) == 8
            assert (await request())[0]["status"] == 503
        finally:
            release.set()
            await asyncio.gather(*requests, return_exceptions=True)
            while app._inflight:
                await asyncio.sleep(.01)
        assert app._action_workers == 0
        assert (await request())[0]["status"] == 200
        assert app._action_workers == len(app._inflight) == 0
        assert len(entered) == 9
    asyncio.run(asyncio.wait_for(exercise(), 7))


@pytest.mark.parametrize("error,status", [("conflict", 403), ("unavailable", 503)])
def test_context_binding_errors_are_generic_and_recover_capacity(browser_actions, monkeypatch, error, status):
    from claudlobby.operation_context import OperationContextError, OperationContextUnavailableError
    app, client, *_ = browser_actions
    exception = OperationContextUnavailableError if error == "unavailable" else OperationContextError
    def missing(*args, **kwargs):
        raise exception("private historical alias and database path")
    monkeypatch.setattr(owner_actions, "bind_task_context", missing)
    response = post(client, "context", {"room": "example"})
    assert response.status_code == status
    assert response.json() == {"state": "denied" if status == 403 else "unavailable"}
    assert "historical" not in response.text
    assert app._action_workers == len(app._inflight) == 0


def test_historical_room_returns_generic_unavailable(browser_actions):
    app, client, *_ = browser_actions
    response = post(client, "context", {"room": "historical-room"})
    assert response.status_code == 503 and response.json() == {"state": "unavailable"}
    assert app._action_workers == 0


@pytest.mark.parametrize("refusal", ["grant", "body", "scope"])
def test_pre_adapter_refusal_only_marks_this_submission(browser_actions, monkeypatch, refusal):
    _, client, adapter, _, owner, ctx, _, _ = browser_actions
    calls = native_receiver(monkeypatch)
    request = {**metadata(context(client), ctx.bots["worker"].uid), "body": "Kept draft"}
    if refusal == "grant":
        adapter.access.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
    elif refusal == "body": request["body"] = " "
    else: request["scope"] = {**request["scope"], "viewer": "stale-viewer"}
    response = post(client, "send", request)
    assert response.status_code == 403
    assert response.json() == {"state": "denied", "effect": "not_started"}
    assert calls == []
    # Receipt refusal cannot resolve an earlier request, even with no record.
    request.pop("body")
    assert "effect" not in post(client, "receipt", request).json()


@pytest.mark.parametrize("where", ["context", "response"])
def test_unexpected_action_context_error_is_generic_unavailable(browser_actions, monkeypatch, where):
    app, client, *_ = browser_actions
    def malformed(*args):
        raise KeyError("private malformed activation record")
    monkeypatch.setattr(app.actions, "_context" if where == "context" else "admit_response", malformed)
    response = post(client, "context", {"room": "example"})
    assert response.status_code == 503
    assert response.json() == {"state": "unavailable"}
    assert app._action_workers == len(app._inflight) == 0


@pytest.mark.parametrize("where", ["adapter", "held_response"])
def test_post_adapter_failure_never_marks_not_started(browser_actions, monkeypatch, where):
    app, client, adapter, _, owner, ctx, _, _ = browser_actions
    calls = native_receiver(monkeypatch)
    request = {**metadata(context(client), ctx.bots["worker"].uid), "body": "Admitted effect"}
    if where == "adapter":
        original = owner_actions.OwnerMessages.send
        def revoke(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            adapter.access.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
            from claudlobby.plane.owner_access import AccessDenied
            raise AccessDenied("private post effect refusal")
        monkeypatch.setattr(owner_actions.OwnerMessages, "send", revoke)
    else:
        def broken(*args): raise KeyError("private response error")
        monkeypatch.setattr(app.actions, "admit_response", broken)
    response = post(client, "send", request)
    assert response.status_code == (403 if where == "adapter" else 503)
    assert "effect" not in response.json() and "private" not in response.text
    assert len(calls) == 1


def test_reused_delivered_uuid_refusal_does_not_claim_original_rejected(browser_actions, monkeypatch):
    _, client, adapter, _, owner, ctx, _, _ = browser_actions
    calls = native_receiver(monkeypatch)
    request = {**metadata(context(client), ctx.bots["worker"].uid), "body": "Original bytes"}
    assert post(client, "send", request).json()["status"] == "delivered"
    adapter.access.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
    refused = post(client, "send", request)
    assert refused.json() == {"state": "denied", "effect": "not_started"}
    assert "status" not in refused.json()  # not an original-UUID rejected receipt
    request.pop("body")
    assert "effect" not in post(client, "receipt", request).json()
    assert len(calls) == 1
