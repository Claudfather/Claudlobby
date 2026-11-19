"""SSH owner source admission and finite policy carrier, without native access."""

import asyncio
import dataclasses
import os
import json
import threading
import base64

import pytest

from claudlobby.plane.owner_access import AccessDenied, PrincipalRef


def test_server_source_is_captured_once_and_cannot_follow_environment_changes(
    monkeypatch,
):
    from claudlobby.plane.owner_ssh import SSHSessionSource

    monkeypatch.setenv("SSH_CONNECTION", "100.64.0.42 4242 100.64.0.7 22")
    source = SSHSessionSource.capture()
    monkeypatch.setenv("SSH_CONNECTION", "100.64.0.99 4242 100.64.0.7 22")
    assert str(source.client_ip) == "100.64.0.42"
    assert str(source.server_ip) == "100.64.0.7"
    with pytest.raises(dataclasses.FrozenInstanceError):
        source.client_port = 99


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        " 100.64.0.42 4242 100.64.0.7 22",
        "100.64.0.42  4242 100.64.0.7 22",
        "100.64.0.42 04242 100.64.0.7 22",
        "100.64.0.42 0 100.64.0.7 22",
        "100.64.0.42 65536 100.64.0.7 22",
        "192.0.2.1 4242 100.64.0.7 22",
        "100.64.0.42 4242 127.0.0.1 22",
        "100.64.0.42 4242 100.64.0.7 22 extra",
        "fe80::1%lo0 4242 100.64.0.7 22",
    ],
)
def test_absent_ambiguous_or_non_tailnet_server_source_refuses_without_fallback(
    monkeypatch, raw
):
    from claudlobby.plane.owner_ssh import SSHSessionSource

    monkeypatch.setenv("SSH_CLIENT", "100.64.0.42 4242 22")
    if raw is None:
        monkeypatch.delenv("SSH_CONNECTION", raising=False)
    else:
        monkeypatch.setenv("SSH_CONNECTION", raw)
    with pytest.raises(AccessDenied):
        SSHSessionSource.capture()


def test_distinct_ssh_verifier_ignores_forged_http_identity_and_checks_server_destination(
    monkeypatch,
):
    from claudlobby.plane.owner_ssh import SSHSessionSource, SSHSessionVerifier
    from claudlobby.plane.owner_ingress import NativePrincipalResolver

    monkeypatch.setenv("SSH_CONNECTION", "100.64.0.42 4242 100.64.0.7 22")
    resolver = NativePrincipalResolver(
        tailscale_binary=__import__("pathlib").Path("/usr/bin/true")
    )
    calls = []

    async def command(*args):
        calls.append(args)
        if args == ("debug", "prefs"):
            return {"ControlURL": "https://controlplane.tailscale.com"}
        if args == ("status", "--json"):
            return {
                "BackendState": "Running",
                "Self": {
                    "ID": "fixture-server",
                    "PublicKey": "fixture-key",
                    "UserID": 123,
                    "DNSName": "fixture.example.ts.net.",
                    "TailscaleIPs": ["100.64.0.7"],
                    "Online": True,
                },
            }
        assert args == ("whois", "--json", "--proto=tcp", "100.64.0.42")
        return {
            "Node": {"User": 123, "Addresses": ["100.64.0.42/32"]},
            "UserProfile": {"ID": 123},
        }

    monkeypatch.setattr(resolver, "_command", command)
    verifier = SSHSessionVerifier(SSHSessionSource.capture(), resolver)

    async def run():
        assert await verifier.origin() == "https://fixture.example.ts.net"
        assert await verifier(
            {
                "client": ("192.0.2.1", 1),
                "headers": [(b"x-forwarded-for", b"100.64.0.99")],
            }
        ) == PrincipalRef("tailscale:controlplane.tailscale.com", "123")

    asyncio.run(run())
    assert ("whois", "--json", "--proto=tcp", "100.64.0.99") not in calls


def test_finite_operation_map_cannot_become_http_or_shell_proxy():
    from claudlobby.commands.owner_api_stdio import operation_request

    method, path, query, body, headers = operation_request(
        "session-status", {}, "A" * 43, None
    )
    assert (method, path, query, body) == (
        "GET",
        "/api/owner/connected/status",
        b"",
        b"",
    )
    assert headers == [(b"authorization", b"Bearer " + b"A" * 43)]
    for operation, value in [
        ("https://elsewhere/", {}),
        ("pair", {"principal": "forged"}),
        ("setup-launch", {"command": "touch /tmp/no"}),
        ("read-task", {"task_id": "../private"}),
    ]:
        with pytest.raises(ValueError):
            operation_request(operation, value, None, None)


def test_api_body_requires_ack_and_end_follows_canonical_shutdown():
    from claudlobby.commands.owner_api_stdio import APIChannel, run_operation

    incoming, client = os.pipe()
    observer, outgoing = os.pipe()
    stopped = []

    class App:
        async def __call__(self, scope, receive, send):
            assert scope["path"] == "/api/owner/connected/status"
            assert scope["client"] == ("ssh-session", 0)
            assert not any(k == b"x-forwarded-for" for k, _ in scope["headers"])
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"set-cookie", b"forbidden"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"state":"ready"}'})

        async def shutdown(self):
            stopped.append(True)

    channel = APIChannel(incoming, outgoing)

    def run():
        asyncio.run(
            run_operation(
                channel,
                App(),
                "session-status",
                {},
                "A" * 43,
                None,
                origin="https://fixture.example.ts.net",
            )
        )

    thread = threading.Thread(target=run)
    thread.start()

    def frame():
        raw = bytearray()
        while not raw.endswith(b"\n"):
            raw.extend(os.read(observer, 1))
        return json.loads(raw)

    head = frame()
    assert head["type"] == "headers" and head["headers"] == {
        "content-type": "application/json"
    }
    chunk = frame()
    assert base64.b64decode(chunk["data"]) == b'{"state":"ready"}'
    assert not stopped
    ack = {
        "version": 1,
        "type": "ack",
        "seq": 1,
        "session_id": channel.session_id,
        "chunk_id": 1,
    }
    os.write(
        client, (json.dumps(ack, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    end = frame()
    thread.join(2)
    assert end["type"] == "end" and end["bytes_sent"] == 17 and stopped == [True]
    assert not thread.is_alive()
    for fd in (incoming, client, observer, outgoing):
        os.close(fd)


# --- one-operation channel: cancellation, drain and refusal -----------------

from types import SimpleNamespace
import hashlib
import subprocess
import sys
import time

from tests.test_owner_stdio import fixed_profile, darwin_fixed_profile  # noqa: F401  (fixtures)

TOKEN = "A" * 43
ORIGIN = "https://fixture.example.ts.net"


def wire(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


class Client:
    """The browser side of one API exec: two pipes, exact frames."""

    def __init__(self):
        from claudlobby.commands.owner_api_stdio import APIChannel

        self.incoming, self.client = os.pipe()
        self.observer, self.outgoing = os.pipe()
        self.channel = APIChannel(self.incoming, self.outgoing)
        self.seq = 0
        self.owned, self.gates = (
            [],
            [],
        )  # threads started and gates held for this client's test

    def own(self, thread, *gates):
        self.owned.append(thread)
        self.gates.extend(gate for gate in gates if gate is not None)
        return thread

    def settle(self):
        """Unconditional finalization, also after a failed assertion: release every
        owned gate, let each owned thread end through its own drain, and only then
        close this side. A thread that still lives is a failure, never left behind."""
        try:
            for gate in self.gates:
                gate.set()
            for thread in self.owned:
                thread.join(10)
                if thread.is_alive() and self.client is not None:
                    self.hang_up()  # the lost-channel drain releases a thread waiting on input
                    thread.join(10)
        finally:
            self.close()
        assert not any(thread.is_alive() for thread in self.owned), (
            "a test-owned thread outlived its test"
        )

    def send(self, kind, **values):
        self.seq += 1
        os.write(
            self.client,
            wire(
                {
                    "version": 1,
                    "type": kind,
                    "seq": self.seq,
                    "session_id": self.channel.session_id,
                    **values,
                }
            ),
        )

    def frame(self, timeout=5):
        import select

        raw = bytearray()
        end = time.monotonic() + timeout
        while not raw.endswith(b"\n"):
            assert select.select(
                [self.observer], [], [], max(0, end - time.monotonic())
            )[0], "no frame"
            part = os.read(self.observer, 1)
            assert part, "channel closed without a frame"
            raw.extend(part)
        value = json.loads(raw)
        assert raw == wire(value), "frames are canonical sorted JSON"
        return value

    def quiet(self):
        import select

        return not select.select([self.observer], [], [], 0.2)[0]

    def hang_up(self):
        os.close(self.client)
        self.client = None

    def close(self):
        for fd in (self.incoming, self.client, self.observer, self.outgoing):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass  # already closed by an earlier close
        self.client = self.incoming = self.observer = self.outgoing = None


@pytest.fixture
def client():
    value = Client()
    try:
        yield value
    finally:
        value.settle()


def start_frame(**changes):
    return {
        "profile": "owner-api-v1",
        "expected_uid": 501,
        "source_revision": "a" * 40,
        "binding_sha256": "b" * 64,
        "operation": "session-status",
        "input": {},
        "token": TOKEN,
        "last_event_id": None,
        **changes,
    }


class Recorder:
    """A stand-in policy app that records the order of its own lifecycle."""

    def __init__(self, body):
        self.body, self.events = body, []

    async def __call__(self, scope, receive, send):
        self.events.append("request")
        try:
            await self.body(scope, receive, send)
            self.events.append("returned")
        except asyncio.CancelledError:
            self.events.append("cancelled")
            raise

    async def shutdown(self):
        self.events.append("shutdown")


def run_in_thread(
    client, app, operation="session-status", payload=None, token=TOKEN, last=None
):
    from claudlobby.commands.owner_api_stdio import run_operation

    outcome = []

    def work():
        try:
            asyncio.run(
                run_operation(
                    client.channel,
                    app,
                    operation,
                    payload or {},
                    token,
                    last,
                    origin=ORIGIN,
                )
            )
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    thread = client.own(threading.Thread(target=work))
    thread.start()
    return thread, outcome


def test_cancel_before_start_is_a_healthy_end_not_a_refusal(client):
    """Published wire: end(status=0,bytes_sent=0,cancelled=true), then exit 0."""
    result = []
    thread = threading.Thread(
        target=lambda: result.append(asyncio.run(client.channel.start()))
    )
    thread.start()
    assert client.frame() == {
        "version": 1,
        "type": "hello",
        "seq": 1,
        "profile": "owner-api-v1",
        "session_id": client.channel.session_id,
    }
    client.send("cancel")
    thread.join(5)
    assert result == [None]
    client.channel.end_cancelled()
    assert client.frame() == {
        "version": 1,
        "type": "end",
        "seq": 2,
        "session_id": client.channel.session_id,
        "status": 0,
        "bytes_sent": 0,
        "cancelled": True,
    }


def test_cancel_received_during_admission_never_reaches_a_policy_handler(client):
    async def body(scope, receive, send):
        pytest.fail("a cancelled operation entered policy")

    app = Recorder(body)
    client.send("cancel")
    thread, outcome = run_in_thread(client, app)
    end = client.frame()
    thread.join(5)
    assert outcome == [None] and app.events == ["shutdown"]
    assert (end["type"], end["status"], end["bytes_sent"], end["cancelled"]) == (
        "end",
        0,
        0,
        True,
    )


def test_stream_cancel_after_headers_drains_the_handler_before_end(client):
    """An active read stream is cancelled by protocol, drained, then ended."""
    waiting = threading.Event()

    async def body(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {"type": "http.response.body", "body": b"event: one\n\n", "more_body": True}
        )
        waiting.set()
        await asyncio.Event().wait()  # a held stream with no idle deadline

    app = Recorder(body)
    threads = threading.active_count()
    thread, outcome = run_in_thread(client, app, "read-stream", {"cursor": "0"})
    assert client.frame()["type"] == "headers"
    chunk = client.frame()
    assert (
        base64.b64decode(chunk["data"]) == b"event: one\n\n" and chunk["chunk_id"] == 1
    )
    client.send("ack", chunk_id=1)
    assert waiting.wait(5) and client.quiet(), "no end while the stream is still held"
    client.send("cancel")
    end = client.frame(10)
    thread.join(10)
    assert outcome == [None] and not thread.is_alive()
    assert (end["type"], end["status"], end["bytes_sent"], end["cancelled"]) == (
        "end",
        200,
        12,
        True,
    )
    assert app.events == ["request", "cancelled", "shutdown"], (
        "end follows the complete drain"
    )
    deadline = time.monotonic() + 5
    while threading.active_count() > threads and time.monotonic() < deadline:
        time.sleep(0.05)
    assert threading.active_count() <= threads, "the control reader joined before exit"


def test_lost_channel_drains_but_never_reports_a_complete_end(client):
    """EOF is unknown to the caller: drain, refuse, and emit no end frame."""
    waiting = threading.Event()

    async def body(scope, receive, send):
        waiting.set()
        await asyncio.Event().wait()

    app = Recorder(body)
    thread, outcome = run_in_thread(client, app)
    assert waiting.wait(5)
    client.hang_up()
    thread.join(10)
    from claudlobby.commands.owner_stdio import StdioRefused

    assert isinstance(outcome[0], StdioRefused) and outcome[0].code == "disconnected"
    assert app.events == ["request", "cancelled", "shutdown"] and client.quiet()


def test_wrong_acknowledgement_refuses_even_when_a_handler_swallows_the_fault(client):
    async def body(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            await send({"type": "http.response.body", "body": b"first"})
        except Exception:
            pass  # a handler that converts faults into responses cannot hide this one

    app = Recorder(body)
    thread, outcome = run_in_thread(client, app)
    assert client.frame()["type"] == "headers" and client.frame()["type"] == "chunk"
    client.send("ack", chunk_id=7)
    thread.join(10)
    from claudlobby.commands.owner_stdio import StdioRefused

    assert isinstance(outcome[0], StdioRefused) and outcome[0].code == "invalid_request"
    assert app.events[-1] == "shutdown" and client.quiet(), (
        "no end frame after a carrier fault"
    )


def test_body_is_chunked_one_credit_at_a_time_and_capped(client):
    async def body(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x" * 40000})

    app = Recorder(body)
    thread, outcome = run_in_thread(client, app)
    assert client.frame()["type"] == "headers"
    sizes = []
    for expected in (1, 2, 3):
        chunk = client.frame()
        assert chunk["chunk_id"] == expected and client.quiet(), (
            "no chunk before the previous ack"
        )
        sizes.append(len(base64.b64decode(chunk["data"])))
        client.send("ack", chunk_id=expected)
    end = client.frame()
    thread.join(5)
    assert (
        sizes == [16384, 16384, 7232]
        and end["bytes_sent"] == 40000
        and end["cancelled"] is False
    )
    assert outcome == [None]


def test_internal_scope_is_fixed_and_carries_no_request_selected_identity(client):
    seen = {}

    async def body(scope, receive, send):
        seen.update(scope)
        seen["request"] = await receive()
        await send(
            {
                "type": "http.response.start",
                "status": 202,
                "headers": [(b"set-cookie", b"never")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    thread, outcome = run_in_thread(client, Recorder(body), "pair", {}, None)
    head = client.frame()
    end = client.frame()
    thread.join(5)
    assert outcome == [None] and head["headers"] == {} and end["status"] == 202
    headers = dict(seen["headers"])
    assert (seen["method"], seen["path"], seen["query_string"]) == (
        "POST",
        "/api/owner/bootstrap/pair",
        b"",
    )
    assert headers == {
        b"host": b"fixture.example.ts.net",
        b"origin": b"https://plane.claudfather.ai",
        b"content-type": b"application/json",
        b"x-claudlobby-owner": b"1",
    }
    assert seen["client"] == ("ssh-session", 0) and seen["request"]["body"] == b"{}"


@pytest.mark.parametrize(
    "operation", ["setup-signin-begin", "setup-signin-code", "setup-signin-cancel"]
)
def test_process_bound_signin_operations_refuse_before_any_engine_entry(
    client, operation
):
    """One exec cannot own the single retained sign-in attempt; it must not burn it."""
    from claudlobby.commands.owner_stdio import StdioRefused

    payload = {
        "generation": "11111111-1111-4111-8111-111111111111",
        "attempt_id": "22222222-2222-4222-8222-222222222222",
    }
    if operation.endswith("code"):
        payload["code"] = "c#s"
    outcome = []

    def work():
        try:
            outcome.append(asyncio.run(client.channel.start()))
        except StdioRefused as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=work)
    thread.start()
    client.frame()
    client.send("start", **start_frame(operation=operation, input=payload))
    thread.join(5)
    assert outcome == ["operation_requires_persistent_owner"]


@pytest.mark.parametrize(
    "change",
    [
        {"profile": "owner-setup-v1"},
        {"expected_uid": 0},
        {"source_revision": "main"},
        {"operation": "host update apply"},
        {"input": {"path": "/etc/passwd"}},
        {"token": None},
        {"operation": "pair", "token": TOKEN},
        {"last_event_id": "5"},
        {"finish_setup": None},
        {"principal": {"subject": "1"}},
        {"headers": {"x-forwarded-for": "100.64.0.9"}},
    ],
)
def test_start_frame_is_closed_and_refuses_selected_identity_or_route(client, change):
    from claudlobby.commands.owner_stdio import StdioRefused

    outcome = []

    def work():
        try:
            outcome.append(asyncio.run(client.channel.start()))
        except StdioRefused as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=work)
    thread.start()
    client.frame()
    client.send("start", **start_frame(**change))
    thread.join(5)
    assert outcome == ["invalid_request"]


def test_lifespan_closes_after_the_operation_and_before_return(client):
    from claudlobby.commands.owner_api_stdio import serve

    events = []

    class App:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "lifespan":
                assert (await receive())["type"] == "lifespan.startup"
                events.append("startup")
                await send({"type": "lifespan.startup.complete"})
                assert (await receive())["type"] == "lifespan.shutdown"
                events.append("lifespan-shutdown")
                await send({"type": "lifespan.shutdown.complete"})
                return
            events.append("request")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def shutdown(self):
            events.append("shutdown")

    outcome = []
    request = start_frame()
    thread = threading.Thread(
        target=lambda: outcome.append(
            asyncio.run(serve(client.channel, App(), request, origin=ORIGIN))
        )
    )
    thread.start()
    assert client.frame()["type"] == "headers" and client.frame()["type"] == "end"
    thread.join(5)
    assert outcome == [None] and events == [
        "startup",
        "request",
        "shutdown",
        "lifespan-shutdown",
    ]


class CanonicalReads:
    """The actual finite read owner around an inner handler, as the policy app mounts it."""

    def __init__(self, inner):
        from claudlobby.plane.finite_reads import FiniteReads

        self.owner, self.inner, self.events = FiniteReads(), inner, []

    async def __call__(self, scope, receive, send):
        self.events.append("request")
        await self.owner.run(self.inner, scope, receive, send)
        self.events.append("returned")

    async def shutdown(self):
        await self.owner.shutdown()
        self.events.append("shutdown")


async def _until_disconnect(receive):
    while (await receive())["type"] != "http.disconnect":
        pass


def test_canonical_read_owner_abandoned_partial_response_is_never_a_successful_end(
    client, monkeypatch
):
    from claudlobby.plane import finite_reads
    from claudlobby.commands.owner_stdio import StdioRefused

    monkeypatch.setattr(finite_reads, "READ_SECONDS", 0.3)

    async def inner(scope, receive, send):
        assert scope["path"] == "/api/owner/connected/status"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"part', "more_body": True})
        await _until_disconnect(receive)  # the owner abandons delivery at its deadline

    app = CanonicalReads(inner)
    thread, outcome = run_in_thread(client, app)
    assert client.frame()["type"] == "headers"
    chunk = client.frame()
    client.send("ack", chunk_id=chunk["chunk_id"])
    thread.join(10)
    assert app.events == ["request", "returned", "shutdown"], (
        "the owner returned normally"
    )
    assert (
        isinstance(outcome[0], StdioRefused)
        and outcome[0].code == "response_incomplete"
    )
    assert client.quiet(), "no end frame for an abandoned response"


def test_canonical_read_owner_complete_response_still_ends_normally(client):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})

    app = CanonicalReads(inner)
    thread, outcome = run_in_thread(client, app)
    assert client.frame()["type"] == "headers"
    client.send("ack", chunk_id=client.frame()["chunk_id"])
    end = client.frame()
    thread.join(5)
    assert outcome == [None] and (
        end["status"],
        end["bytes_sent"],
        end["cancelled"],
    ) == (200, 2, False)


def test_headers_only_or_absent_response_that_returns_is_unknown_not_successful(client):
    from claudlobby.commands.owner_stdio import StdioRefused

    async def body(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    thread, outcome = run_in_thread(client, Recorder(body))
    assert client.frame()["type"] == "headers"
    thread.join(5)
    assert (
        isinstance(outcome[0], StdioRefused)
        and outcome[0].code == "response_incomplete"
        and client.quiet()
    )


def test_caller_cancel_that_unwinds_normally_stays_cancelled(client):
    """A handler that swallows the disconnect and returns must not read as success or refusal."""
    started = threading.Event()

    async def body(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x", "more_body": True})
        started.set()
        await _until_disconnect(receive)  # returns normally on protocol cancel

    thread, outcome = run_in_thread(client, Recorder(body))
    assert client.frame()["type"] == "headers"
    client.send("ack", chunk_id=client.frame()["chunk_id"])
    assert started.wait(5)
    client.send("cancel")
    end = client.frame(10)
    thread.join(10)
    assert outcome == [None] and (end["type"], end["cancelled"]) == ("end", True)


def test_caller_cancel_after_final_body_stays_cancelled(client):
    """Completion and cancellation are independent; never a successful end, never a replay licence."""
    finished = threading.Event()

    async def body(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x", "more_body": False})
        finished.set()
        await _until_disconnect(
            receive
        )  # still active after the final body; unwinds normally on cancel

    thread, outcome = run_in_thread(client, Recorder(body))
    assert client.frame()["type"] == "headers"
    client.send("ack", chunk_id=client.frame()["chunk_id"])
    assert finished.wait(5)
    client.send("cancel")
    end = client.frame(10)
    thread.join(10)
    assert outcome == [None] and (end["type"], end["cancelled"]) == ("end", True)


def test_component_shutdown_failure_keeps_original_outcome_and_drains_the_rest(client):
    from claudlobby.commands.owner_stdio import StdioRefused

    drained = []

    class Component:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        async def shutdown(self):
            drained.append(self.name)
            if self.fail:
                raise RuntimeError("component cleanup failed")

    async def body(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await _until_disconnect(receive)

    app = Recorder(body)
    app.pairing_bootstrap = Component("pairing", fail=True)
    app.connected_setup = Component("setup")
    app.workspace_bootstrap = Component("workspace")
    thread, outcome = run_in_thread(client, app)
    assert client.frame()["type"] == "headers"
    client.hang_up()
    thread.join(15)
    assert not thread.is_alive(), "the control reader was stopped and joined"
    assert isinstance(outcome[0], StdioRefused) and outcome[0].code == "disconnected", (
        "the original outcome survives"
    )
    assert drained == ["pairing", "setup", "workspace"] and "shutdown" in app.events


def test_component_shutdown_failure_alone_is_surfaced(client):
    async def body(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})

    app = Recorder(body)

    class Bad:
        async def shutdown(self):
            raise RuntimeError("component cleanup failed")

    app.connected_reads = Bad()
    thread, outcome = run_in_thread(client, app)
    assert client.frame()["type"] == "headers"
    client.send("ack", chunk_id=client.frame()["chunk_id"])
    thread.join(10)
    assert isinstance(outcome[0], RuntimeError) and client.quiet(), (
        "cleanup failure surfaces; no end frame"
    )


def lifespan_app(events, *, shutdown=None, finish=True, hold=False):
    class App:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "lifespan":
                assert (await receive())["type"] == "lifespan.startup"
                await send({"type": "lifespan.startup.complete"})
                assert (await receive())["type"] == "lifespan.shutdown"
                events.append("lifespan-shutdown")
                if shutdown is not None:
                    await shutdown()
                if finish:
                    await send({"type": "lifespan.shutdown.complete"})
                return
            await send({"type": "http.response.start", "status": 200, "headers": []})
            if hold:
                await _until_disconnect(receive)
                return
            await send({"type": "http.response.body", "body": b""})

        async def shutdown(self):
            events.append("shutdown")

    return App()


def run_serve(client, app):
    from claudlobby.commands.owner_api_stdio import serve

    outcome = []

    def work():
        try:
            outcome.append(
                asyncio.run(serve(client.channel, app, start_frame(), origin=ORIGIN))
            )
        except BaseException as exc:
            outcome.append(exc)

    thread = client.own(threading.Thread(target=work))
    thread.start()
    return thread, outcome


def test_end_is_emitted_only_after_lifespan_shutdown_completes(client):
    events, release = [], threading.Event()
    client.gates.append(release)

    async def slow_shutdown():
        while not release.is_set():
            await asyncio.sleep(0.02)

    thread, outcome = run_serve(client, lifespan_app(events, shutdown=slow_shutdown))
    assert client.frame()["type"] == "headers"
    deadline = time.monotonic() + 5
    while "lifespan-shutdown" not in events and time.monotonic() < deadline:
        time.sleep(0.02)
    assert events == ["shutdown", "lifespan-shutdown"], (
        "components drain before the lifespan"
    )
    assert client.quiet(), "no end while the lifespan is still shutting down"
    release.set()
    assert client.frame()["type"] == "end"
    thread.join(5)
    assert outcome == [None]


@pytest.mark.parametrize("mode", ["raises", "never-completes"])
def test_lifespan_shutdown_failure_is_surfaced_and_emits_no_end(client, mode):
    from claudlobby.commands.owner_stdio import StdioRefused

    events = []

    async def failing():
        raise RuntimeError("lifespan cleanup failed")

    app = lifespan_app(
        events, shutdown=failing if mode == "raises" else None, finish=False
    )
    thread, outcome = run_serve(client, app)
    assert client.frame()["type"] == "headers"
    thread.join(15)
    assert isinstance(outcome[0], StdioRefused) and outcome[0].code == "shutdown_failed"
    assert client.quiet(), "a failed shutdown never reports a normal end"


def test_pending_lifespan_reaches_the_actual_shutdown_timeout(client):
    from claudlobby.commands.owner_stdio import StdioRefused

    events = []

    async def pending():
        await (
            asyncio.Event().wait()
        )  # genuinely pending: never returns, never completes

    thread, outcome = run_serve(
        client, lifespan_app(events, shutdown=pending, finish=False)
    )
    assert client.frame()["type"] == "headers"
    thread.join(30)
    assert not thread.is_alive() and "lifespan-shutdown" in events
    assert (
        isinstance(outcome[0], StdioRefused) and outcome[0].code == "shutdown_timeout"
    )
    assert client.quiet(), "a timed-out shutdown never reports an end"


def test_original_operation_failure_is_kept_when_shutdown_also_fails(client):
    from claudlobby.commands.owner_stdio import StdioRefused

    events = []
    thread, outcome = run_serve(client, lifespan_app(events, finish=False, hold=True))
    assert client.frame()["type"] == "headers"
    client.hang_up()
    thread.join(15)
    assert isinstance(outcome[0], StdioRefused) and outcome[0].code == "disconnected"
    assert (
        isinstance(outcome[0].__context__, StdioRefused)
        and outcome[0].__context__.code == "shutdown_failed"
    )


def test_abandoned_response_keeps_its_outcome_when_a_component_also_fails_and_the_reader_joins(
    client, monkeypatch
):
    """Incomplete delivery outranks the cleanup failure; the open-input reader is joined in the drain."""
    from claudlobby.plane import finite_reads
    from claudlobby.commands.owner_api_stdio import run_operation
    from claudlobby.commands.owner_stdio import StdioRefused

    monkeypatch.setattr(finite_reads, "READ_SECONDS", 0.3)

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"part', "more_body": True})
        await _until_disconnect(receive)

    app = CanonicalReads(inner)
    drained = []

    class Bad:
        async def shutdown(self):
            drained.append("bad")
            raise RuntimeError("component cleanup failed")

    class Good:
        async def shutdown(self):
            drained.append("good")

    app.pairing_bootstrap, app.workspace_bootstrap = Bad(), Good()
    reads, original, seen = [0], client.channel.read, []

    def read(allowed, timeout=0.25):
        reads[0] += 1
        try:
            return original(allowed, timeout)
        finally:
            reads[0] -= 1

    client.channel.read = read

    async def main():
        try:
            await run_operation(
                client.channel, app, "session-status", {}, TOKEN, None, origin=ORIGIN
            )
        except BaseException as exc:
            # Observed inside the loop, before asyncio.run cancels or joins anything itself.
            seen.append((exc, reads[0], len(asyncio.all_tasks()) - 1))

    thread = threading.Thread(target=lambda: asyncio.run(main()))
    thread.start()
    assert client.frame()["type"] == "headers"
    client.send("ack", chunk_id=client.frame()["chunk_id"])
    thread.join(10)
    assert not thread.is_alive() and client.client is not None, (
        "the input stayed open throughout"
    )
    error, active_reads, other_tasks = seen[0]
    assert isinstance(error, StdioRefused) and error.code == "response_incomplete", (
        "the original outcome survives"
    )
    assert isinstance(error.__context__, RuntimeError), (
        "the cleanup failure is kept beside it"
    )
    assert (active_reads, other_tasks) == (0, 0), (
        "the control reader had stopped and joined"
    )
    assert drained == ["bad", "good"] and app.events == [
        "request",
        "returned",
        "shutdown",
    ]
    assert client.quiet(), "no end frame"


# --- owner-signin-v1: one retained attempt, one bounded conversation --------

IDENTITY = {
    "generation": "11111111-1111-4111-8111-111111111111",
    "attempt_id": "22222222-2222-4222-8222-222222222222",
}


def signin_frame(**changes):
    return start_frame(
        **{
            "profile": "owner-signin-v1",
            "operation": "setup-signin-begin",
            "input": dict(IDENTITY),
            **changes,
        }
    )


def call(client, call_id, operation="setup-signin-status", token=TOKEN, **changes):
    client.send(
        "call",
        call_id=call_id,
        operation=operation,
        input={**IDENTITY, **changes},
        token=token,
    )


def attempt(state, **more):
    return 200, {**IDENTITY, "state": state, "reason": None, **more}


class SigninApp:
    """A stand-in policy app for framing cases: scripted answers, recorded lifecycle."""

    def __init__(self, *answers, gate=None):
        self.answers, self.events, self.gate = list(answers), [], gate

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            assert (await receive())["type"] == "lifespan.startup"
            await send({"type": "lifespan.startup.complete"})
            assert (await receive())["type"] == "lifespan.shutdown"
            self.events.append("lifespan-shutdown")
            await send({"type": "lifespan.shutdown.complete"})
            return
        assert scope["path"].startswith("/api/owner/connected/setup/signin-") and scope[
            "client"
        ] == ("ssh-session", 0)
        operation = scope["path"].rsplit("/", 1)[1]
        assert {
            k: v
            for k, v in json.loads((await receive())["body"]).items()
            if k != "code"
        } == IDENTITY
        self.events.append(operation)
        answer = self.answers.pop(0)
        if answer == "hold":
            try:
                await _until_disconnect(receive)
            finally:
                self.events.append("unwound")
            return
        status, body = answer
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"private, no-store"),
                    (b"set-cookie", b"never"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": raw})

    async def shutdown(self):
        while self.gate is not None and not self.gate.is_set():
            await asyncio.sleep(0.02)
        self.events.append("shutdown")


def run_conversation(client, app, request=None, origin=ORIGIN):
    from claudlobby.commands.owner_api_stdio import serve

    outcome = []

    def work():
        try:
            outcome.append(
                asyncio.run(
                    serve(client.channel, app, request or signin_frame(), origin=origin)
                )
            )
        except BaseException as exc:
            outcome.append(exc)

    thread = client.own(threading.Thread(target=work), getattr(app, "gate", None))
    thread.start()
    return thread, outcome


def body_of(reply):
    assert reply["type"] == "reply", reply
    return json.loads(base64.b64decode(reply["data"], validate=True))


@pytest.mark.parametrize(
    "change,admitted",
    [
        ({}, True),
        ({"operation": "setup-signin-status"}, False),
        ({"operation": "session-status", "input": {}}, False),
        (
            {"operation": "setup-signin-code", "input": {**IDENTITY, "code": "c#s"}},
            False,
        ),
        ({"last_event_id": "5"}, False),
        ({"token": None}, False),
        ({"input": {"generation": IDENTITY["generation"]}}, False),
        ({"profile": "owner-signin-v2"}, False),
        ({"call_id": 1}, False),
    ],
)
def test_signin_profile_starts_only_with_begin_and_the_same_closed_start(
    client, change, admitted
):
    from claudlobby.commands.owner_stdio import StdioRefused

    outcome = []

    def work():
        try:
            outcome.append(asyncio.run(client.channel.start()))
        except StdioRefused as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=work)
    thread.start()
    assert client.frame()["profile"] == "owner-api-v1", "the hello bytes are unchanged"
    client.send("start", **signin_frame(**change))
    thread.join(5)
    if admitted:
        assert (
            outcome[0]["operation"] == "setup-signin-begin"
            and client.channel.admitted is not None
        )
    else:
        assert outcome == ["invalid_request"] and client.channel.admitted is None


def test_signin_conversation_replies_per_call_and_ends_only_after_the_whole_drain(
    client,
):
    gate = threading.Event()
    app = SigninApp(
        attempt("starting"),
        attempt("waiting", url="https://claude.com/x"),
        (403, {"state": "denied"}),
        attempt("waiting", url="https://claude.com/x"),
        attempt("connected"),
        gate=gate,
    )
    thread, outcome = run_conversation(client, app)
    first = client.frame()
    raw = json.dumps(attempt("starting")[1]).encode()
    assert first == {
        "version": 1,
        "type": "reply",
        "seq": 1,
        "session_id": client.channel.session_id,
        "call_id": 1,
        "status": 200,
        "data": base64.b64encode(raw).decode(),
        "headers": {
            "content-type": "application/json",
            "cache-control": "private, no-store",
        },
    }
    assert client.quiet(), "a reply is a call receipt: no end, chunk or ack follows it"
    call(client, 2)
    assert body_of(client.frame())["url"] == "https://claude.com/x"
    call(client, 3, "setup-signin-code", code="wrong#state")
    denied = client.frame()
    assert (denied["call_id"], denied["status"], body_of(denied)) == (
        3,
        403,
        {"state": "denied"},
    )
    call(client, 4, "setup-signin-code", code="right#state")
    assert client.frame()["status"] == 200, (
        "a refused code leaves the same conversation open"
    )
    call(client, 5)
    last = client.frame()
    assert (last["call_id"], body_of(last)["state"]) == (5, "connected")
    assert client.quiet() and thread.is_alive(), (
        "the terminal reply is not the end: the drain is still running"
    )
    assert "shutdown" not in app.events
    gate.set()
    end = client.frame()
    thread.join(5)
    sent = sum(
        len(json.dumps(x[1]).encode())
        for x in (
            attempt("starting"),
            attempt("waiting", url="https://claude.com/x"),
            (403, {"state": "denied"}),
            attempt("waiting", url="https://claude.com/x"),
            attempt("connected"),
        )
    )
    assert end == {
        "version": 1,
        "type": "end",
        "seq": 6,
        "session_id": client.channel.session_id,
        "status": 200,
        "bytes_sent": sent,
        "cancelled": False,
    }
    assert outcome == [None] and app.events == [
        "signin-begin",
        "signin-status",
        "signin-code",
        "signin-code",
        "signin-status",
        "shutdown",
        "lifespan-shutdown",
    ]


@pytest.mark.parametrize(
    "case",
    [
        "pipelined",
        "duplicate",
        "skipped",
        "attempt",
        "generation",
        "begin-again",
        "foreign",
        "ack",
        "extra-field",
        "boolean-id",
        "bad-token",
        "beyond-limit",
        "after-closing",
    ],
)
def test_signin_call_frames_are_refused_before_any_canonical_entry(
    client, monkeypatch, case
):
    from claudlobby.commands import owner_api_stdio
    from claudlobby.commands.owner_stdio import StdioRefused

    gate = threading.Event()
    other = "33333333-3333-4333-8333-333333333333"
    entered = ["signin-begin"]
    if case == "pipelined":
        app = SigninApp("hold")
    elif case == "after-closing":
        app = SigninApp(attempt("cancelled"), gate=gate)
    else:
        app = SigninApp(attempt("starting"), attempt("waiting"), attempt("waiting"))
    if case == "beyond-limit":
        monkeypatch.setattr(owner_api_stdio, "CALL_LIMIT", 2)
    thread, outcome = run_conversation(client, app)
    if case == "pipelined":
        deadline = time.monotonic() + 5
        while not app.events and time.monotonic() < deadline:
            time.sleep(0.01)
        call(
            client, 2
        )  # valid in itself, but begin has no reply yet: never queued for later
    else:
        assert client.frame()["call_id"] == 1
        if case in {"duplicate", "beyond-limit"}:
            call(client, 2)
            assert client.frame()["call_id"] == 2
            entered.append("signin-status")
            call(client, 2 if case == "duplicate" else 3)
        elif case == "skipped":
            call(client, 3)
        elif case == "attempt":
            call(client, 2, attempt_id=other)
        elif case == "generation":
            call(client, 2, generation=other)
        elif case == "begin-again":
            call(client, 2, "setup-signin-begin")
        elif case == "foreign":
            client.send(
                "call", call_id=2, operation="session-status", input={}, token=TOKEN
            )
        elif case == "ack":
            client.send("ack", chunk_id=1)
        elif case == "extra-field":
            client.send(
                "call",
                call_id=2,
                operation="setup-signin-status",
                input=dict(IDENTITY),
                token=TOKEN,
                last_event_id=None,
            )
        elif case == "boolean-id":
            call(client, True)
        elif case == "bad-token":
            call(client, 2, token="short")
        else:
            call(client, 2)  # the closing reply was sent; the drain is held open
    if case == "after-closing":
        time.sleep(0.6)  # more than one reader poll
    gate.set()
    thread.join(10)
    assert not thread.is_alive()
    assert isinstance(outcome[0], StdioRefused) and outcome[0].code == "invalid_request"
    assert [e for e in app.events if e.startswith("signin-")] == entered, (
        "the refused frame never reached policy"
    )
    assert app.events[-2:] == ["shutdown", "lifespan-shutdown"], (
        "the drain still completed"
    )
    assert client.quiet(), "no reply and no end after a refused frame"


def wait_until(predicate, what, seconds=10):
    end = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < end, what
        time.sleep(
            0.01
        )  # only a poll of an observed condition, never an ordering criterion


def fill_output(client):
    """Fill the real output pipe with junk until a nonblocking write would block, so the
    reply writer genuinely cannot proceed; the peer then drains exactly this many bytes."""
    import fcntl

    flags = fcntl.fcntl(client.outgoing, fcntl.F_GETFL)
    fcntl.fcntl(client.outgoing, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    total = 0
    try:
        while True:
            try:
                total += os.write(client.outgoing, b"x" * 512)
            except BlockingIOError:
                return total
    finally:
        fcntl.fcntl(client.outgoing, fcntl.F_SETFL, flags)


def drain_output(client, count):
    while count:
        count -= len(os.read(client.observer, min(count, 4096)))


def watch_blocked(client, monkeypatch):
    """Observe, without ever blocking, each readiness wait of the writer that found no room."""
    from claudlobby.commands import owner_api_stdio

    blocked = []
    real_select = owner_api_stdio.select.select

    class Observed:
        def __getattr__(self, name):
            return getattr(owner_api_stdio.select, name)

        def select(self, readable, writable, errored, timeout=None):
            if writable == [client.outgoing]:
                # a zero-time probe of the real pipe signals the wait before it starts
                probe = real_select(readable, writable, errored, 0)
                if not probe[1]:
                    blocked.append(1)
            return real_select(readable, writable, errored, timeout)

    monkeypatch.setattr(owner_api_stdio, "select", Observed())
    return blocked


def watch_writes(client, monkeypatch):
    """Record each positive write the reply writer makes to the real output pipe.

    A delegating stand-in for the writer module's ``os``: every call is the real one;
    only a write to this client's output descriptor that returned bytes is noted. The
    junk that filled the pipe was written by the test before this is installed, so it
    can never count as reply progress."""
    from claudlobby.commands import owner_api_stdio

    written = []
    real_os = owner_api_stdio.os

    class Observed:
        def __getattr__(self, name):
            return getattr(real_os, name)

        def write(self, fd, data):
            count = real_os.write(fd, data)  # a would-block raises before any record
            if fd == client.outgoing and count > 0:
                written.append(count)
            return count

    monkeypatch.setattr(owner_api_stdio, "os", Observed())
    return written


def blocked_reply(client, monkeypatch, *answers):
    """Start a conversation whose first reply is partly written and then cannot proceed.

    Room is given to the real pipe a little at a time until the writer is observed to
    write reply bytes; then it must be observed blocked again, with no further write."""
    junk = fill_output(client)
    blocked = watch_blocked(client, monkeypatch)
    written = watch_writes(client, monkeypatch)
    app = SigninApp(*answers)
    thread, outcome = run_conversation(client, app)
    wait_until(lambda: blocked, "the writer is waiting for room")
    assert not written, "the full pipe admitted no reply byte before room was freed"
    freed = 0
    end = time.monotonic() + 10
    while not written:
        assert time.monotonic() < end, "no reply byte was written once room was freed"
        # Only the known filler may be read: never block on an empty pipe.
        if freed < junk:
            step = min(1024, junk - freed)
            drain_output(client, step)
            freed += step
        probe_end = time.monotonic() + 0.3
        while not written and time.monotonic() < probe_end:
            time.sleep(0.01)  # a poll of an observed condition, never an ordering criterion
    mark = len(blocked)
    wait_until(
        lambda: len(blocked) > mark + 1,
        "the writer is blocked again part way through the reply",
    )
    progress = list(written)  # every step the freed room admitted has now landed
    mark = len(blocked)
    wait_until(
        lambda: len(blocked) > mark + 1,
        "the writer stays blocked, with no further reply byte",
    )
    assert written == progress, "the writer made no further progress while blocked"
    return app, thread, outcome, junk - freed


def test_cancel_during_blocked_reply_output_starts_the_drain_and_never_ends_successfully(
    client, monkeypatch
):
    """The peer stops draining mid-reply: cancellation is read, the writer is halted and
    joined, canonical shutdown runs while the output is still blocked, and the torn reply
    is an explicit incomplete outcome, never an end."""
    from claudlobby.commands.owner_stdio import StdioRefused

    app, thread, outcome, junk = blocked_reply(
        client, monkeypatch, attempt("starting", padding="x" * 9000)
    )
    client.send("cancel")
    wait_until(
        lambda: "shutdown" in app.events,
        "canonical shutdown began while output stayed blocked",
    )
    thread.join(10)
    assert not thread.is_alive()
    assert (
        len(outcome) == 1
        and isinstance(outcome[0], StdioRefused)
        and outcome[0].code == "response_incomplete"
    ), outcome
    assert app.events == ["signin-begin", "shutdown", "lifespan-shutdown"]
    assert client.channel.torn, (
        "a half-written frame closes the channel to further output"
    )
    import select

    rest = bytearray()
    while select.select([client.observer], [], [], 0)[0]:
        rest.extend(os.read(client.observer, 65536))
    assert b'"type":"end"' not in bytes(rest), "no successful end after a torn reply"


def test_lost_input_during_blocked_reply_output_also_drains_without_an_end(
    client, monkeypatch
):
    from claudlobby.commands.owner_stdio import StdioRefused

    app, thread, outcome, junk = blocked_reply(
        client, monkeypatch, attempt("starting", padding="x" * 9000)
    )
    client.hang_up()
    wait_until(
        lambda: "shutdown" in app.events,
        "canonical shutdown began while output stayed blocked",
    )
    thread.join(10)
    assert not thread.is_alive()
    assert (
        len(outcome) == 1
        and isinstance(outcome[0], StdioRefused)
        and outcome[0].code in {"response_incomplete", "disconnected"}
    ), outcome
    assert app.events == ["signin-begin", "shutdown", "lifespan-shutdown"]


def test_output_descriptor_flag_is_restored_after_every_step(client):
    import fcntl

    before = fcntl.fcntl(client.outgoing, fcntl.F_GETFL) & os.O_NONBLOCK
    client.channel.emit("hello", profile="owner-api-v1")
    assert fcntl.fcntl(client.outgoing, fcntl.F_GETFL) & os.O_NONBLOCK == before == 0
    assert fcntl.fcntl(client.incoming, fcntl.F_GETFL) & os.O_NONBLOCK == 0
    assert client.frame()["type"] == "hello"


def test_signin_call_read_while_the_reply_is_blocked_stays_early_after_the_commit(
    client, monkeypatch
):
    """Deterministic: the next call is read (and observed) while the reply cannot be written;
    the output is then released and the commit completes before the reader goes on. The frame
    keeps the classification of its read and is refused. Events, not sleeps, order every step."""
    from claudlobby.commands.owner_stdio import StdioRefused

    junk = fill_output(client)
    blocked = watch_blocked(client, monkeypatch)
    observed, proceed, committed = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    client.gates.append(proceed)
    real_read, real_emit = client.channel.read, client.channel.emit

    def read(allowed, timeout=0.25):
        frame = real_read(allowed, timeout)
        if frame is not None and frame["type"] == "call":
            observed.set()  # completed read, before it is classified
            proceed.wait(10)
        return frame

    def emit(kind, *args, **values):
        try:
            return real_emit(kind, *args, **values)
        finally:
            if kind == "reply":
                committed.set()

    client.channel.read, client.channel.emit = read, emit
    # The second scripted reply is terminal, so a failing assertion cannot strand the conversation.
    app = SigninApp(attempt("starting"), attempt("cancelled"))
    thread, outcome = run_conversation(client, app)
    wait_until(
        lambda: blocked, "the reply writer is waiting for room, so admission has passed"
    )
    call(client, 2)
    assert observed.wait(10), "the call was read while the reply was still blocked"
    assert not committed.is_set()
    drain_output(client, junk)  # release the output; the reply commits
    assert committed.wait(10), (
        "the reply was written and committed before the reader went on"
    )
    proceed.set()
    thread.join(10)
    assert not thread.is_alive()
    assert (
        len(outcome) == 1
        and isinstance(outcome[0], StdioRefused)
        and outcome[0].code == "invalid_request"
    ), outcome
    assert app.events == ["signin-begin", "shutdown", "lifespan-shutdown"], (
        "only begin reached the engine"
    )
    assert client.frame()["call_id"] == 1


def test_signin_call_after_the_whole_reply_is_ordinary(client, monkeypatch):
    app = SigninApp(attempt("starting"), attempt("waiting"))
    thread, outcome = run_conversation(client, app)
    first = client.frame()
    assert (first["type"], first["call_id"]) == ("reply", 1)
    call(client, 2)
    second = client.frame()
    assert (second["type"], second["call_id"]) == ("reply", 2)
    client.send("cancel")
    thread.join(10)
    assert not thread.is_alive()
    assert app.events[:2] == ["signin-begin", "signin-status"]


@pytest.mark.parametrize(
    "mode",
    [
        "cancel-idle",
        "cancel-in-call",
        "eof-idle",
        "eof-in-call",
        "signal",
        "idle",
        "call-observation",
        "clock",
        "oversize",
        "denied-begin",
        "unavailable-status",
        "unnamed-attempt",
        "cancel-call",
    ],
)
def test_signin_conversation_closes_through_the_same_full_drain(
    client, monkeypatch, mode
):
    from claudlobby.commands import owner_api_stdio
    from claudlobby.commands.owner_stdio import StdioRefused

    held = mode in {"cancel-in-call", "eof-in-call", "call-observation"}
    begin = {
        "oversize": (200, b"x" * 16385),
        "denied-begin": (403, {"state": "denied"}),
        "unnamed-attempt": (
            200,
            {
                **IDENTITY,
                "attempt_id": "33333333-3333-4333-8333-333333333333",
                "state": "connected",
                "reason": None,
            },
        ),
    }.get(mode, attempt("starting"))
    answers = [begin] + (["hold"] if held else [])
    answers += {
        "unavailable-status": [(503, {"state": "unavailable"})],
        "cancel-call": [attempt("waiting")],
        "clock": [attempt("waiting")] * 300,
    }.get(mode, [])
    app = SigninApp(*answers)
    if mode == "idle":
        monkeypatch.setattr(owner_api_stdio, "IDLE_SECONDS", 0.4)
    if mode == "call-observation":
        monkeypatch.setattr(owner_api_stdio, "CALL_SECONDS", 0.4)
    if mode == "clock":
        client.channel.admitted = (
            time.monotonic() - owner_api_stdio.CONVERSATION_SECONDS + 1.2
        )
    started = time.monotonic()
    thread, outcome = run_conversation(client, app)
    replies = []
    if mode not in {"oversize", "unnamed-attempt"}:
        replies.append(client.frame())
    if held or mode in {"unavailable-status", "cancel-call"}:
        call(
            client,
            2,
            "setup-signin-cancel" if mode == "cancel-call" else "setup-signin-status",
        )
        if held:
            deadline = time.monotonic() + 5
            while len(app.events) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        else:
            replies.append(client.frame())
    if mode.startswith("cancel-i"):
        client.send("cancel")
    elif mode.startswith("eof"):
        client.hang_up()
    elif mode == "signal":
        client.channel.interrupt()
    elif mode == "clock":
        # Status frames keep arriving and being answered; none renews the conversation clock.
        number = 1
        while thread.is_alive() and time.monotonic() - started < 10:
            number += 1
            call(client, number)
            import select

            if not select.select([client.observer], [], [], 1)[0]:
                break
            replies.append(client.frame())
            time.sleep(0.05)
        assert number > 3 and all(
            body_of(x)["state"] in {"starting", "waiting"} for x in replies
        )
    thread.join(10)
    assert not thread.is_alive()
    assert app.events[-2:] == ["shutdown", "lifespan-shutdown"], (
        "every close runs the complete drain"
    )
    if held:
        assert "unwound" in app.events, "the handler in flight was drained first"
    normal = {
        "cancel-idle": (200, True),
        "cancel-in-call": (200, True),
        "denied-begin": (403, False),
        "unavailable-status": (503, False),
        "cancel-call": (200, False),
    }
    refused = {
        "eof-idle": "disconnected",
        "eof-in-call": "disconnected",
        "signal": "interrupted",
        "idle": "expired",
        "call-observation": "expired",
        "clock": "expired",
        "oversize": "output_limit",
        "unnamed-attempt": "invalid_response",
    }
    if mode in normal:
        end = client.frame()
        assert outcome == [None] and (end["type"], end["status"], end["cancelled"]) == (
            "end",
            *normal[mode],
        )
        assert end["bytes_sent"] == sum(
            len(base64.b64decode(x["data"])) for x in replies
        )
        if mode == "denied-begin":
            assert time.monotonic() - started < 5, (
                "no idle wait when there is no owned attempt"
            )
    else:
        assert (
            isinstance(outcome[0], StdioRefused) and outcome[0].code == refused[mode]
        ), outcome
        assert client.quiet(), "never a normal end"
    if mode == "clock":
        assert 1 <= time.monotonic() - started < 6


# The actual policy app, setup door, engine and provider owner; only the provider CLI is synthetic.
from tests.test_plane_owner_setup import setup_authority, synthetic_signin  # noqa: E402,F401  (fixtures)
from tests.test_setup_commands import team_setup  # noqa: E402,F401  (fixture)

HOST = "https://host.example.test"


def canonical_app(access, monkeypatch):
    from claudlobby.commands import owner_api_stdio
    from claudlobby.plane.owner_browser import create_owner_browser_app
    from claudlobby.plane.owner_workspace_http import WorkspaceBootstrapConfig
    from tests.test_plane_owner_workspace import OWNER, ISSUER, ORIGIN as PLANE
    from tests.test_plane_two_fleets import _Sampler

    monkeypatch.setattr(owner_api_stdio, "OPERATIONAL_ORIGIN", PLANE)

    async def verify(scope):
        assert scope["client"] == ("ssh-session", 0)
        return OWNER

    app = create_owner_browser_app(
        access.root,
        verify_principal=verify,
        expected_origin=HOST,
        sampler=_Sampler({}),
        workspace_bootstrap=WorkspaceBootstrapConfig(ISSUER, PLANE),
    )
    app.access._clock = access._clock
    return app


def poll_until(client, number, payload, bearer, wanted):
    """Status calls on the open conversation until the canonical state appears."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        number += 1
        client.send(
            "call",
            call_id=number,
            operation="setup-signin-status",
            input=payload,
            token=bearer,
        )
        reply = client.frame(10)
        assert reply["call_id"] == number and reply["status"] == 200, reply
        value = body_of(reply)
        if value["state"] == wanted:
            return number, value
        assert value["state"] in {"starting", "waiting"}, value
        time.sleep(0.025)
    pytest.fail("the synthetic attempt never reached " + wanted)


def ordinary_status(access, monkeypatch, payload, bearer):
    """A later one-operation process: a new app that never owned the attempt."""
    from claudlobby.commands.owner_api_stdio import serve

    other = Client()
    try:
        request = start_frame(
            operation="setup-signin-status", input=payload, token=bearer
        )
        thread = other.own(
            threading.Thread(
                target=lambda: asyncio.run(
                    serve(
                        other.channel,
                        canonical_app(access, monkeypatch),
                        request,
                        origin=HOST,
                    )
                )
            )
        )
        thread.start()
        head = other.frame(10)
        chunk = other.frame(10)
        other.send("ack", chunk_id=chunk["chunk_id"])
        assert other.frame(10)["type"] == "end"
        thread.join(10)
        return head["status"], json.loads(base64.b64decode(chunk["data"]))
    finally:
        other.settle()


def test_one_canonical_app_owns_the_attempt_from_begin_to_connected(
    client, synthetic_signin, monkeypatch
):
    from uuid import uuid4

    engine, grant, reader, _program, secret, env = synthetic_signin
    access, bearer = engine.store.access, reader.token
    payload = {"generation": grant.generation, "attempt_id": str(uuid4())}
    app = canonical_app(access, monkeypatch)
    owner = app.connected_setup.engine.signin
    thread, outcome = run_conversation(
        client,
        app,
        start_frame(
            profile="owner-signin-v1",
            operation="setup-signin-begin",
            input=payload,
            token=bearer,
        ),
        origin=HOST,
    )
    begin = client.frame(10)
    assert (begin["call_id"], begin["status"]) == (1, 200), begin
    assert (
        body_of(begin)["state"] in {"starting", "waiting"}
        and body_of(begin)["attempt_id"] == payload["attempt_id"]
    )
    worker = owner._thread
    assert worker.is_alive()
    number, waiting = poll_until(client, 1, payload, bearer, "waiting")
    assert waiting["url"].startswith("https://claude.com/")
    client.send(
        "call",
        call_id=number + 1,
        operation="setup-signin-code",
        input={**payload, "code": "sensitive#wrong"},
        token=bearer,
    )
    refused = client.frame(10)
    assert (refused["status"], body_of(refused)) == (403, {"state": "denied"}), (
        "the canonical generic refusal"
    )
    client.send(
        "call",
        call_id=number + 2,
        operation="setup-signin-code",
        input={**payload, "code": "accepted#" + "b" * 32},
        token=bearer,
    )
    assert client.frame(10)["status"] == 200
    assert app.connected_setup.engine.signin is owner and owner._thread is worker, (
        "the same original attempt"
    )
    number, connected = poll_until(client, number + 2, payload, bearer, "connected")
    end = client.frame(15)
    assert end["type"] == "end" and (end["status"], end["cancelled"]) == (200, False)
    assert not worker.is_alive(), (
        "end follows the joined worker, not just the terminal row"
    )
    thread.join(10)
    assert outcome == [None]
    assert env.read_text().count("CLAUDE_CODE_OAUTH_TOKEN=") == 1
    assert ordinary_status(access, monkeypatch, payload, bearer) == (
        200,
        {**payload, "state": "connected", "reason": None},
    )


@pytest.mark.parametrize("loss", ["eof", "expiry", "worker-exit"])
def test_lost_conversation_drains_the_owned_attempt_and_never_admits_a_replacement(
    client, synthetic_signin, monkeypatch, loss
):
    from uuid import uuid4
    from claudlobby.commands.owner_stdio import StdioRefused

    engine, grant, reader, _program, _secret, env = synthetic_signin
    access, bearer = engine.store.access, reader.token
    payload = {"generation": grant.generation, "attempt_id": str(uuid4())}
    app = canonical_app(access, monkeypatch)
    thread, outcome = run_conversation(
        client,
        app,
        start_frame(
            profile="owner-signin-v1",
            operation="setup-signin-begin",
            input=payload,
            token=bearer,
        ),
        origin=HOST,
    )
    assert client.frame(10)["status"] == 200
    worker = app.connected_setup.engine.signin._thread
    number, _ = poll_until(client, 1, payload, bearer, "waiting")
    if loss == "eof":
        client.hang_up()
        thread.join(15)
        assert (
            isinstance(outcome[0], StdioRefused) and outcome[0].code == "disconnected"
        )
    elif loss == "worker-exit":
        # The worker finishes between calls. Its absence ends nothing: only the next
        # canonical status, naming the original attempt as terminal, closes the conversation.
        app.connected_setup.engine.signin._cancel.set()
        worker.join(10)
        assert not worker.is_alive() and client.quiet() and thread.is_alive()
        client.send(
            "call",
            call_id=number + 1,
            operation="setup-signin-status",
            input=payload,
            token=bearer,
        )
        final = client.frame(10)
        assert final["status"] == 200 and body_of(final)["state"] == "cancelled"
        end = client.frame(15)
        thread.join(15)
        assert outcome == [None] and (end["type"], end["status"], end["cancelled"]) == (
            "end",
            200,
            False,
        )
    else:
        # The setup approval lapses between calls: the next call is the canonical generic denial.
        now = access._clock()
        app.access._clock = access._clock = lambda: now + 10**7
        client.send(
            "call",
            call_id=number + 1,
            operation="setup-signin-status",
            input=payload,
            token=bearer,
        )
        denied = client.frame(10)
        assert (denied["status"], body_of(denied)) == (403, {"state": "denied"}), (
            "no hidden state is disclosed"
        )
        end = client.frame(15)
        thread.join(15)
        assert outcome == [None] and (end["type"], end["status"], end["cancelled"]) == (
            "end",
            403,
            False,
        )
        app.access._clock = access._clock = lambda: now
    assert not thread.is_alive() and not worker.is_alive(), (
        "the owned worker was joined in the drain"
    )
    assert env.read_text() == "export ROUTE=private\n"
    status, retained = ordinary_status(access, monkeypatch, payload, bearer)
    assert status == 200 and retained["attempt_id"] == payload["attempt_id"]
    assert retained["state"] in {"cancelled", "needs_attention"}, retained
    # A new process: the retained original ends at once without a worker; a new identity is denied.
    for changed, expected in (
        (payload, 200),
        ({**payload, "attempt_id": str(uuid4())}, 403),
    ):
        again, fresh = Client(), canonical_app(access, monkeypatch)
        try:
            second, result = run_conversation(
                again,
                fresh,
                start_frame(
                    profile="owner-signin-v1",
                    operation="setup-signin-begin",
                    input=changed,
                    token=bearer,
                ),
                origin=HOST,
            )
            reply = again.frame(10)
            end = again.frame(15)
            second.join(10)
            assert (reply["status"], end["type"], end["status"]) == (
                expected,
                "end",
                expected,
            ) and result == [None]
            if expected == 200:
                assert body_of(reply) == retained
            assert fresh.connected_setup.engine.signin._thread is None, (
                "no replacement attempt was started"
            )
        finally:
            again.settle()


def test_a_timeout_before_its_code_is_replaced_only_by_a_later_conversation(
    client, synthetic_signin, monkeypatch
):
    from uuid import uuid4
    from claudlobby.plane import provider_signin

    engine, grant, reader, program, _secret, env = synthetic_signin
    access, bearer = engine.store.access, reader.token
    monkeypatch.setattr(provider_signin, "SECONDS", 2)
    first = {"generation": grant.generation, "attempt_id": str(uuid4())}

    def begin(channel, app, payload):
        return run_conversation(
            channel,
            app,
            start_frame(
                profile="owner-signin-v1",
                operation="setup-signin-begin",
                input=payload,
                token=bearer,
            ),
            origin=HOST,
        )

    thread, outcome = begin(client, canonical_app(access, monkeypatch), first)
    assert client.frame(10)["status"] == 200
    number, _ = poll_until(client, 1, first, bearer, "waiting")
    number, ended = poll_until(client, number, first, bearer, "needs_attention")
    assert ended == {
        **first,
        "state": "needs_attention",
        "reason": "timeout",
        "retryable": True,
    }
    end = client.frame(15)
    thread.join(15)
    assert outcome == [None] and (end["type"], end["status"]) == ("end", 200), (
        "a terminal reply still closes the conversation"
    )
    assert ordinary_status(access, monkeypatch, first, bearer) == (200, ended)
    # A later conversation names a new attempt ID: a new worker and link, one approval.
    program.with_name("synthetic-provider-state").write_text("c" * 32)
    monkeypatch.setattr(provider_signin, "SECONDS", 300)
    second = {"generation": grant.generation, "attempt_id": str(uuid4())}
    again = Client()
    try:
        follow, result = begin(again, canonical_app(access, monkeypatch), second)
        begun = again.frame(10)
        assert begun["status"] == 200
        assert body_of(begun)["attempt_id"] == second["attempt_id"]
        number, waiting = poll_until(again, 1, second, bearer, "waiting")
        assert waiting["url"].endswith("&state=" + "c" * 32), "the new run's own link"
        assert type(waiting["expires_at"]) is int, "the new run's own deadline"
        again.send(
            "call",
            call_id=number + 1,
            operation="setup-signin-code",
            input={**second, "code": "accepted#" + "c" * 32},
            token=bearer,
        )
        assert again.frame(10)["status"] == 200
        poll_until(again, number + 1, second, bearer, "connected")
        assert again.frame(15)["type"] == "end"
        follow.join(15)
        assert result == [None]
    finally:
        again.settle()
    assert env.read_text().count("CLAUDE_CODE_OAUTH_TOKEN=") == 1
    assert ordinary_status(access, monkeypatch, first, bearer)[0] == 403, (
        "the replaced attempt is retired"
    )


# --- process entry: parser, dispatch and exit status ------------------------


def api_process(tmp_path, **env):
    from tests.conftest import constructed_env

    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "claudlobby",
            "--root",
            str(tmp_path / "never-created"),
            "host",
            "owner",
            "api",
            "--stdio",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=constructed_env(**env),
    )


def test_cli_cancel_before_headers_ends_and_exits_zero(tmp_path):
    process = api_process(tmp_path, SSH_CONNECTION="100.64.0.42 4242 100.64.0.7 22")
    try:
        hello = json.loads(process.stdout.readline())
        assert hello["type"] == "hello" and hello["profile"] == "owner-api-v1"
        process.stdin.write(
            wire(
                {
                    "version": 1,
                    "type": "cancel",
                    "seq": 1,
                    "session_id": hello["session_id"],
                }
            )
        )
        process.stdin.flush()
        end = json.loads(process.stdout.readline())
        assert (
            process.wait(15) == 0
            and process.stdout.read() == b""
            and process.stderr.read() == b""
        )
    finally:
        process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    assert end == {
        "version": 1,
        "type": "end",
        "seq": 2,
        "session_id": hello["session_id"],
        "status": 0,
        "bytes_sent": 0,
        "cancelled": True,
    }
    assert not (tmp_path / "never-created").exists()


@pytest.mark.parametrize(
    "env,code",
    [
        ({}, "owner_ssh_source_refused"),
        (
            {"SSH_CONNECTION": "192.0.2.1 4242 100.64.0.7 22"},
            "owner_ssh_source_refused",
        ),
        (
            {
                "SSH_CONNECTION": "100.64.0.42 4242 100.64.0.7 22",
                "BOT_NAME": "generated",
            },
            "operator_required",
        ),
    ],
)
def test_cli_refuses_missing_source_or_generated_context_before_hello(
    tmp_path, env, code
):
    process = api_process(tmp_path, SSH_CLIENT="100.64.0.42 4242 22", **env)
    try:
        output, errors = process.communicate(b"", 15)
    finally:
        process.kill()
        process.wait()
    frames = [json.loads(line) for line in output.splitlines()]
    assert process.returncode == 4 and errors == b"" and len(frames) == 1
    assert (frames[0]["type"], frames[0]["code"], frames[0]["retry"]) == (
        "error",
        code,
        False,
    )
    assert not (tmp_path / "never-created").exists()


def test_cli_api_route_requires_its_explicit_stdio_mode(tmp_path):
    from tests.conftest import constructed_env

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "claudlobby",
            "--root",
            str(tmp_path / "never-created"),
            "host",
            "owner",
            "api",
        ],
        input=b"",
        capture_output=True,
        timeout=15,
        env=constructed_env(SSH_CONNECTION="100.64.0.42 4242 100.64.0.7 22"),
    )
    assert result.returncode == 2 and b'"hello"' not in result.stdout


# --- admission shared with the guide ----------------------------------------


def test_api_admission_binds_current_source_without_a_finish_tuple(
    darwin_fixed_profile, monkeypatch
):
    from claudlobby.commands import owner_api_stdio, owner_stdio
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby.installation import Installation
    from claudlobby.plane.owner_ssh import SSHSessionSource

    paths, request, *_ = darwin_fixed_profile
    monkeypatch.setattr(owner_stdio, "_admit", lambda args, value: paths)
    monkeypatch.setenv("SSH_CONNECTION", "100.64.0.42 4242 100.64.0.7 22")
    source = SSHSessionSource.capture()
    args = SimpleNamespace(root=str(paths.root))
    api = {key: value for key, value in request.items() if key != "finish_setup"}
    assert owner_api_stdio.admit(args, api, source)[0] is paths
    # After the original update the installed revision differs from the
    # preparation receipt. The API still requires the exact installed revision.
    updated = "d" * 40
    paths.package.source_revision = updated
    monkeypatch.setattr(Installation, "preflight", lambda self, **kwargs: updated)
    assert (
        owner_api_stdio.admit(args, {**api, "source_revision": updated}, source)[
            1
        ].runtime
        is not None
    )
    with pytest.raises(StdioRefused, match="installation_changed"):
        owner_api_stdio.admit(args, api, source)  # stale revision
    with pytest.raises(StdioRefused, match="installation_changed"):
        FixedOwnerProfile(
            paths, {**request, "source_revision": updated}
        )  # the guide rule is unchanged
    with pytest.raises(StdioRefused, match="owner_ssh_source_refused"):
        owner_api_stdio.admit(args, api, ("100.64.0.42", 4242))


def test_api_refuses_a_profile_without_the_managed_mac_runtime(
    fixed_profile, monkeypatch
):
    from claudlobby.commands import owner_api_stdio, owner_stdio
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby.plane.owner_ssh import SSHSessionSource

    paths, request, *_ = fixed_profile
    monkeypatch.setattr(owner_stdio, "_admit", lambda args, value: paths)
    monkeypatch.setenv("SSH_CONNECTION", "100.64.0.42 4242 100.64.0.7 22")
    with pytest.raises(StdioRefused, match="unsupported_ssh_profile"):
        owner_api_stdio.admit(
            SimpleNamespace(root=str(paths.root)), request, SSHSessionSource.capture()
        )


def test_policy_app_mode_follows_existing_authority_not_the_request(
    monkeypatch, tmp_path
):
    from claudlobby.commands import owner_api_stdio
    from claudlobby.plane import owner_access, owner_browser, owner_source

    built = []
    owner = SimpleNamespace(active=False)
    monkeypatch.setattr(
        owner_access,
        "OwnerAccess",
        lambda root: SimpleNamespace(current_grant=lambda: owner),
    )
    monkeypatch.setattr(owner_source, "inspect_owner_setup", lambda root: None)

    def create(root, **options):
        built.append(options)
        if "workspace_bootstrap" in options and options["package"] == "unprepared":
            raise owner_access.AccessUnavailable()
        return "app"

    monkeypatch.setattr(owner_browser, "create_owner_browser_app", create)
    paths = SimpleNamespace(root=tmp_path, package="package")
    verifier = object()
    assert owner_api_stdio.owner_app(paths, verifier, ORIGIN) == "app"
    assert built[-1] == {
        "expected_origin": ORIGIN,
        "verify_principal": verifier,
        "package": "package",
        "pairing_origin": "https://plane.claudfather.ai",
    }
    owner.active = True
    owner_api_stdio.owner_app(paths, verifier, ORIGIN)
    config = built[-1]["workspace_bootstrap"]
    assert (config.issuer, config.operational_origin) == (
        "https://claudfather.ai",
        "https://plane.claudfather.ai",
    )
    assert (
        "pairing_origin" not in built[-1] and built[-1]["verify_principal"] is verifier
    )
    paths.package = "unprepared"
    owner_api_stdio.owner_app(paths, verifier, ORIGIN)
    assert set(built[-1]) == {"expected_origin", "verify_principal", "package"}


def test_server_destination_and_realm_drift_refuse(monkeypatch):
    from claudlobby.plane.owner_ssh import SSHSessionSource, SSHSessionVerifier
    from claudlobby.plane.owner_ingress import NativePrincipalResolver
    from claudlobby.plane.owner_access import AccessDenied

    monkeypatch.setenv("SSH_CONNECTION", "100.64.0.42 4242 100.64.0.7 22")
    resolver = NativePrincipalResolver(
        tailscale_binary=__import__("pathlib").Path("/usr/bin/true")
    )
    state = {
        "ControlURL": "https://controlplane.tailscale.com",
        "ips": ["100.64.0.7"],
        "id": "server",
    }

    async def command(*args):
        if args == ("debug", "prefs"):
            return {"ControlURL": state["ControlURL"]}
        if args == ("status", "--json"):
            return {
                "BackendState": "Running",
                "Self": {
                    "ID": state["id"],
                    "PublicKey": "key",
                    "UserID": 1,
                    "DNSName": "fixture.example.ts.net.",
                    "TailscaleIPs": state["ips"],
                    "Online": True,
                },
            }
        return {
            "Node": {"User": 123, "Addresses": ["100.64.0.42/32"]},
            "UserProfile": {"ID": 123},
        }

    monkeypatch.setattr(resolver, "_command", command)
    verifier = SSHSessionVerifier(SSHSessionSource.capture(), resolver)

    async def run():
        assert await verifier.origin() == ORIGIN
        state["id"] = "another-server"
        with pytest.raises(AccessDenied, match="owner_ssh_server_changed"):
            await verifier({})
        state["id"], state["ips"] = "server", ["100.64.0.8"]
        with pytest.raises(AccessDenied, match="owner_ssh_server_refused"):
            await verifier({})
        state["ips"], state["ControlURL"] = (
            ["100.64.0.7"],
            "https://headscale.example.invalid",
        )
        with pytest.raises(AccessDenied, match="owner_identity_control_refused"):
            await verifier({})

    asyncio.run(run())


# --- fresh SSH-controlled setup: create-only private policy ------------------


def ssh_profile(darwin_fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands import host_owner_setup

    paths, request, home, *_ = darwin_fixed_profile
    profile = FixedOwnerProfile(paths, request)
    monkeypatch.setattr(
        host_owner_setup, "_native_directories", lambda _paths: (profile.units,)
    )
    return profile, paths, home


def test_ssh_setup_proposes_and_creates_only_the_private_owner_policy(
    darwin_fixed_profile, monkeypatch
):
    from claudlobby import config

    profile, paths, _home = ssh_profile(darwin_fixed_profile, monkeypatch)
    proposals = []

    def approve(action, scope):
        assert not profile.policy.exists(), "nothing is written before approval"
        proposals.append((action, scope))

    profile.prepare_directories(
        SimpleNamespace(approve=approve), paths, ssh_policy=True
    )
    action, scope = proposals[0]
    content = profile.policy.read_bytes()
    assert action == "prepare_directories" and len(proposals) == 1
    assert scope["host_policy"] == {
        "path": str(profile.policy),
        "job": "plane-owner",
        "enroll": False,
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    assert (
        "SSH connection" in scope["effects"][-1]
        and str(profile.policy) not in scope["effects"][-1]
    )
    assert (
        profile.policy.stat().st_mode & 0o777 == 0o600
        and profile.policy.stat().st_nlink == 1
    )
    monkeypatch.setenv(config.HOST_OVERRIDE_ENV, str(profile.policy))
    jobs = config.load_host_jobs()
    monkeypatch.delenv(config.HOST_OVERRIDE_ENV)
    monkeypatch.setattr(
        config, "host_override_path", lambda: profile.policy.with_name("absent.yaml")
    )
    packaged = config.load_host_jobs()
    assert jobs["plane-owner"]["enroll"] is False
    assert {name: value for name, value in jobs.items() if name != "plane-owner"} == {
        name: value for name, value in packaged.items() if name != "plane-owner"
    }, "ordinary jobs stay canonical"
    assert {k: v for k, v in jobs["plane-owner"].items() if k != "enroll"} == {
        k: v for k, v in packaged["plane-owner"].items() if k != "enroll"
    }


def test_existing_private_policy_is_never_overwritten_or_repaired(
    darwin_fixed_profile, monkeypatch
):
    profile, paths, _home = ssh_profile(darwin_fixed_profile, monkeypatch)
    profile.policy.parent.mkdir(mode=0o700)
    existing = b"host:\n  jobs:\n    plane-owner:\n      enroll: true\n"
    profile.policy.write_bytes(existing)
    profile.policy.chmod(0o600)
    before = profile.policy.stat()
    proposals = []
    profile.prepare_directories(
        SimpleNamespace(approve=lambda action, scope: proposals.append(scope)),
        paths,
        ssh_policy=True,
    )
    after = profile.policy.stat()
    assert profile.policy.read_bytes() == existing and (
        before.st_ino,
        before.st_mtime_ns,
    ) == (after.st_ino, after.st_mtime_ns)
    assert proposals[0]["host_policy"] is None and not any(
        "SSH connection" in line for line in proposals[0]["effects"]
    )


def test_policy_node_appearing_during_approval_refuses_without_replacement(
    darwin_fixed_profile, monkeypatch
):
    from claudlobby.commands.owner_stdio import StdioRefused

    profile, paths, _home = ssh_profile(darwin_fixed_profile, monkeypatch)
    for path in (
        profile.project.parent,
        profile.project,
        profile.account,
        profile.policy.parent,
        profile.provider.parent,
        profile.units.parent.parent,
        profile.units.parent,
        profile.units,
    ):
        path.mkdir(mode=0o700, exist_ok=True)
    (profile.data / ".env").touch(mode=0o600)

    def approve(action, scope):
        profile.policy.write_bytes(b"host: {}\n")

    with pytest.raises(StdioRefused, match="prerequisite_changed"):
        profile.prepare_directories(
            SimpleNamespace(approve=approve), paths, ssh_policy=True
        )
    assert profile.policy.read_bytes() == b"host: {}\n"


def test_linux_and_https_preparation_scope_is_unchanged(fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands import host_owner_setup

    paths, request, *_ = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    monkeypatch.setattr(
        host_owner_setup, "_native_directories", lambda _paths: (profile.units,)
    )
    proposals = []
    profile.prepare_directories(
        SimpleNamespace(approve=lambda action, scope: proposals.append(scope)), paths
    )
    assert set(proposals[0]) == {
        "directories",
        "empty_private_environment",
        "provider_directory",
        "effects",
    }
    assert len(proposals[0]["effects"]) == 1 and not profile.policy.exists()


# --- SSH-controlled guide lifecycle -----------------------------------------


class GuideWorld:
    """Fakes for the canonical owners the guide calls; no host or native access."""

    def __init__(self, monkeypatch, tmp_path, *, enroll=False):
        from claudlobby.commands import host_owner, host_owner_setup
        from claudlobby.plane import owner_setup, owner_source, owner_service
        from claudlobby import config, git_update

        self.calls, self.states = [], []
        self.launch_after = 2
        self.polls = 0
        installation = SimpleNamespace(
            cli=tmp_path / "install/bin/claudlobby", directory=tmp_path / "install"
        )
        self.paths = SimpleNamespace(
            root=tmp_path / "data",
            package=SimpleNamespace(
                installation=installation, source_revision="a" * 40
            ),
        )
        self.grant = SimpleNamespace(
            generation="11111111-1111-4111-8111-111111111111",
            source_revision="a" * 40,
            managed_owner=None,
        )
        world = self

        class Setup:
            def __init__(self, store):
                pass

            def retained_state(self):
                world.polls += 1
                launched = world.polls > world.launch_after
                return SimpleNamespace(
                    state="absent",
                    launch_id="req" if launched else None,
                    launch_plan="plan" if launched else None,
                    grant=world.grant,
                    effective_active=True,
                )

            def local(self):
                return world.grant

            def operation(self, grant):
                return {"request_id": "req", "config_fingerprint": "plan"}

        monkeypatch.setattr(host_owner_setup, "OwnerSetup", Setup)
        monkeypatch.setattr(
            owner_setup, "managed_setup", lambda root: self.paths.package
        )
        monkeypatch.setattr(git_update, "read_update", lambda installation: None)
        monkeypatch.setattr(owner_source, "fresh_owner_setup", lambda *a, **k: None)
        monkeypatch.setattr(
            owner_service,
            "service_command",
            lambda *a: pytest.fail("built an owner serve command"),
        )
        monkeypatch.setattr(
            config, "load_host_jobs", lambda: {"plane-owner": {"enroll": enroll}}
        )
        monkeypatch.setattr(
            host_owner_setup,
            "_serving",
            lambda *a, **k: pytest.fail("started a listener"),
        )
        monkeypatch.setattr(
            host_owner_setup,
            "_ssh_origin",
            lambda source: self.calls.append(("origin", source)) or ORIGIN,
        )

        def configuration(args, paths, terminal, cli):
            self.args = args
            return (
                {
                    "origin": args.origin,
                    "socket": str(args.socket),
                    "workspace_issuer": args.workspace_issuer,
                    "workspace_origin": args.workspace_origin,
                },
                SimpleNamespace(
                    managed_owner=None, install_directory=tmp_path / "units"
                ),
            )

        monkeypatch.setattr(host_owner_setup, "_configuration", configuration)
        for name in (
            "_initialize_first_owner",
            "_confirm_owner",
            "_describe",
            "_initialize_connected",
            "_confirm_workspace",
            "_allow_setup",
        ):
            monkeypatch.setattr(
                host_owner, name, lambda *a, _name=name, **k: self.calls.append(_name)
            )
        monkeypatch.setattr(
            host_owner, "_active_owner", lambda store: SimpleNamespace(active=True)
        )
        self.owner = None
        self.store = SimpleNamespace(
            path=tmp_path / "absent-store",
            current_grant=lambda: self.owner,
            connected=lambda: SimpleNamespace(preparation_state=lambda: "absent"),
            workspace=lambda: SimpleNamespace(status=self.workspace),
        )
        self.bound = False
        self.profile = SimpleNamespace(
            runtime=object(),
            project=tmp_path / "project",
            account=tmp_path / "account",
            units=tmp_path / "units",
            provider=tmp_path / "provider",
            prepare_directories=lambda consent, paths, **kw: self.calls.append(
                ("directories", kw)
            ),
            prepare_provider=lambda consent, paths: self.calls.append("provider"),
            origin=lambda: pytest.fail("used the HTTPS ingress profile"),
        )
        self.consent = SimpleNamespace(
            state=lambda state, **data: self.states.append((state, data)),
            check_cancel=lambda: None,
        )

    def workspace(self):
        if "_confirm_workspace" in self.calls:
            return "active", SimpleNamespace(
                issuer="https://claudfather.ai",
                operational_origin="https://plane.claudfather.ai",
            )
        return "absent", None

    def run(self, source="captured-source"):
        from claudlobby.commands import host_owner_setup

        args = SimpleNamespace(finish_setup=None, template="revenue-web-app")
        return host_owner_setup.run(
            args,
            self.paths,
            self.store,
            None,
            consent=self.consent,
            profile=self.profile,
            ssh=source,
        )


def test_ssh_guide_pairs_binds_and_hands_off_launch_without_a_listener_or_service(
    monkeypatch, tmp_path
):
    from claudlobby.plane.owner_ssh import MAC_TAILSCALE
    import dataclasses

    world = GuideWorld(monkeypatch, tmp_path)
    monkeypatch.setattr(dataclasses, "asdict", lambda value: {})
    monkeypatch.setattr("claudlobby.commands.host_owner_setup.asdict", lambda value: {})
    result = world.run()
    assert world.calls[:3] == [
        ("directories", {"ssh_policy": True}),
        "provider",
        ("origin", "captured-source"),
    ]
    assert world.calls[3:] == [
        "_initialize_first_owner",
        "_confirm_owner",
        "_describe",
        "_initialize_connected",
        "_confirm_workspace",
        "_allow_setup",
    ]
    assert world.args.managed_owner is False and world.args.tailscale == MAC_TAILSCALE
    assert world.args.origin == ORIGIN, (
        "the logical key comes from native Self, not a frame"
    )
    assert [state for state, _ in world.states] == [
        "pairing_ready",
        "workspace_ready",
        "preview_launch_ready",
    ]
    assert all(
        data["transport"] == "mac_openssh" and data["origin"] == ORIGIN
        for _, data in world.states
    )
    assert result.data == {
        "state": "launch_handoff",
        "request_id": "req",
        "config_fingerprint": "plan",
        "generation": world.grant.generation,
        "source_revision": "a" * 40,
        "transport": "mac_openssh",
    }
    assert "No owner service is enrolled" in result.lines[-1]


def test_ssh_guide_never_repairs_an_existing_policy_that_enrolls_the_owner_service(
    monkeypatch, tmp_path
):
    from claudlobby.command_result import CommandFailure

    world = GuideWorld(monkeypatch, tmp_path, enroll=True)
    with pytest.raises(CommandFailure, match="explicit --managed-owner handover"):
        world.run()
    assert "_initialize_first_owner" not in world.calls and world.states == []


def test_ssh_guide_stops_when_the_setup_approval_lapses_instead_of_waiting_forever(
    monkeypatch, tmp_path
):
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import host_owner_setup

    world = GuideWorld(monkeypatch, tmp_path)
    monkeypatch.setattr("claudlobby.commands.host_owner_setup.asdict", lambda value: {})
    world.launch_after = 10**6
    original = host_owner_setup.OwnerSetup.retained_state

    def lapsed(self):
        value = original(self)
        value.effective_active = world.polls < 4
        return value

    monkeypatch.setattr(host_owner_setup.OwnerSetup, "retained_state", lapsed)
    with pytest.raises(CommandFailure, match="expired or changed"):
        world.run()


def test_ssh_guide_requires_the_managed_mac_profile_and_the_typed_channel(
    monkeypatch, tmp_path
):
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import host_owner_setup

    world = GuideWorld(monkeypatch, tmp_path)
    world.profile.runtime = None
    with pytest.raises(CommandFailure, match="managed Mac runtime"):
        world.run()
    assert world.calls == []
    with pytest.raises(CommandFailure, match="typed guide only"):
        host_owner_setup.run(
            SimpleNamespace(finish_setup=None, template="revenue-web-app"),
            world.paths,
            world.store,
            None,
            ssh="captured-source",
        )


def test_mac_guide_captures_the_server_source_before_the_profile_environment(
    darwin_fixed_profile, monkeypatch
):
    """dispatch refuses a missing tuple, and hands the captured one to run()."""
    from claudlobby.commands import owner_stdio, host_owner_setup
    from claudlobby.command_result import CommandOutput

    paths, request, *_ = darwin_fixed_profile
    seen = []

    def run(args, admitted, store, terminal, **options):
        seen.append((options.get("ssh"), os.environ.get("SSH_CONNECTION")))
        return CommandOutput({"state": "fixture"})

    monkeypatch.setattr(host_owner_setup, "run", run)
    monkeypatch.setattr(owner_stdio, "_selectors", lambda args: None)
    monkeypatch.setattr(owner_stdio, "_admit", lambda args, value: paths)
    for raw, expected in (
        ("100.64.0.42 4242 100.64.0.7 22", "result"),
        (None, "error"),
    ):
        incoming, client = os.pipe()
        observer, outgoing = os.pipe()

        class Channel(owner_stdio.OwnerStdio):
            def __init__(self, _in, _out):
                super().__init__(incoming, outgoing)

            def start(self):
                return dict(request)

        monkeypatch.setattr(owner_stdio, "OwnerStdio", Channel)
        if raw is None:
            monkeypatch.delenv("SSH_CONNECTION", raising=False)
        else:
            monkeypatch.setenv("SSH_CONNECTION", raw)
        try:
            code = owner_stdio.dispatch(
                SimpleNamespace(stdio_status=False, root=str(paths.root))
            )
            os.close(outgoing)
            outgoing = None
            frames = [
                json.loads(line) for line in os.read(observer, 65536).splitlines()
            ]
        finally:
            for fd in (incoming, client, observer, outgoing):
                if fd is not None:
                    os.close(fd)
        assert frames[-1]["type"] == expected
        if expected == "error":
            assert code == 4 and frames[-1]["code"] == "owner_ssh_source_refused"
    assert len(seen) == 1 and str(seen[0][0].client_ip) == "100.64.0.42"
    assert seen[0][1] is None, (
        "the tuple was captured before the fixed environment replaced it"
    )
