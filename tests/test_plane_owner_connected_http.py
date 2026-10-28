"""Actual canonical reads/crypto over synthetic current native authority."""
import asyncio
import contextlib
import json
import sqlite3
import threading
import time
import types

import jwt
import pytest
from fastapi.testclient import TestClient

from claudlobby.plane.owner_access import AccessDenied, AccessUnavailable, VerifiedReader
from claudlobby.plane.owner_browser import COOKIE_NAME
from claudlobby.plane.session_assertion import PURPOSE
from claudlobby.plane.workspace_assertion import verify_workspace_assertion
from tests.test_plane_owner_workspace_http import http, post as bootstrap_post, token as enrollment_token, OWNER, HOST, PLANE
from tests.test_plane_workspace_assertion import profile

PREFIX = "/api/owner/connected/"


def approve_workspace(http):
    app, access, _, _, profile = http
    c = access.workspace().begin(OWNER, issuer=app.workspace_bootstrap.config.issuer, operational_origin=PLANE)
    a = verify_workspace_assertion(enrollment_token(http, {"nonce": c.nonce, "host_uid": c.owner.host_uid}),
        json.dumps({"keys": [profile[1]]}).encode(), expected=access.workspace().expected(c.nonce, OWNER))
    proposal = access.workspace().stage(a, OWNER)
    access.workspace().confirm(expected_proposal=proposal)
    return c.owner


@pytest.fixture
def connected_http(http):
    owner = approve_workspace(http)
    http[1].connected().initialize(expected_owner=owner)
    return http


def headers(token=None):
    return {"Origin": PLANE, **({"Authorization": "Bearer " + token} if token else {})}


def post(client, action, body=None, token=None, extra=None):
    return client.post(PREFIX + action, content=json.dumps(body if body is not None else {}),
        headers={**headers(token), "Content-Type": "application/json", "X-Claudlobby-Owner": "1", **(extra or {})})


def assertion(fixture, c, **changes):
    app, _, _, _, profile = fixture
    claims = {**profile[3], "iss": app.workspace_bootstrap.config.issuer,
        "host_uid": c["host_uid"], "nonce": c["nonce"], "operational_origin": PLANE,
        "purpose": PURPOSE, "aud": f"urn:claudfather:host:{c['host_uid']}:workspace-read-session:v1",
        "binding_revision": c["binding_revision"], "binding_generation": c["binding_generation"], **changes}
    return jwt.encode(claims, profile[0], algorithm="EdDSA", headers={"kid": profile[1]["kid"]})


def login(client, fixture):
    challenge = post(client, "challenge")
    assert challenge.status_code == 202, challenge.text
    c = challenge.json()
    result = post(client, "issue", {"nonce": c["nonce"], "assertion": assertion(fixture, c)})
    assert result.status_code == 200, result.text
    return result.json()


async def bounded(awaitable, what, state, seconds=5):
    """Wait at most `seconds` of real time, so a wait that cannot end fails with its
    state, read before the wait is cancelled (#2388, #2406). Real time, because
    move_clock's paused() stops the loop's own timers."""
    task = asyncio.ensure_future(awaitable)
    expired = threading.Event()
    timer = asyncio.get_running_loop().run_in_executor(None, expired.wait, seconds)
    try:
        await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        if not task.done():
            raise AssertionError(f"waited {seconds} s for {what}: {state()}")
        return task.result()
    finally:
        # Not awaited: returning a thread hop later would give other tasks time to
        # finish work that a check after this wait should still find pending (#2406).
        expired.set()
        timer.cancel()
        task.cancel()


def status(task):
    """pending, done or cancelled: a cancelled task is done() as well (#2406)."""
    return "cancelled" if task.cancelled() else "done" if task.done() else "pending"


def move_clock(monkeypatch, module):
    """Put the event loop's timers and `module`'s time.monotonic() on one clock, real
    time plus an offset. Only that module's monotonic moves: its other `time`
    attributes are the real ones, and owner_connected_http's own `time` (the read and
    preflight deadlines) does not move. advance(seconds) adds to the offset, so a
    deadline passes where the scenario says, not where a stall puts it (#2388).
    Inside paused() the clock stands still. Past its deadline the answer needs no
    clock time, nor the drain beyond the work it waits for, so one that needs some
    (an answer 1.5 s late, a drain 2 s late) never ends and fails its bound; a stall
    shorter than the bound cannot fail it (#2406)."""
    offset, still = [0.0], [None]
    def now():
        return (time.monotonic() if still[0] is None else still[0]) + offset[0]
    clock = types.SimpleNamespace(**vars(time))
    clock.monotonic = now
    monkeypatch.setattr(asyncio.get_running_loop(), "time", now)
    monkeypatch.setattr(module, "time", clock)
    def advance(seconds):
        offset[0] += seconds
    @contextlib.contextmanager
    def paused():
        still[0] = time.monotonic()
        try:
            yield
        finally:
            still[0] = None
    return advance, paused


def test_actual_crypto_read_profile_status_canonical_reads_and_cookie_separation(connected_http):
    app, access, _, _, _ = connected_http
    with TestClient(app, base_url=HOST) as client:
        ready = login(client, connected_http)
        assert set(ready) == {"version", "state", "token", "expires_in", "read_profile", "session_context"}
        assert ready["read_profile"]["profile"] == "direct-owner-read-v1"
        assert set(ready["session_context"]) == {"issuer", "subject", "workspace_id", "binding_revision", "binding_generation", "operational_origin"}
        status = client.get(PREFIX + "status", headers=headers(ready["token"]))
        assert status.status_code == 200 and "token" not in status.json()
        for suffix in ("fleets", "tasks?fleet=data", "channel?fleet=data&limit=120", "overview", "summary"):
            result = client.get(PREFIX + "read/" + suffix, headers=headers(ready["token"]))
            assert result.status_code == 200 and result.json()["state"] == "ok", result.text
            assert result.headers["access-control-allow-origin"] == PLANE
            assert "set-cookie" not in result.headers and "access-control-allow-credentials" not in result.headers
        # Copying a bearer into the direct cookie never authorizes the other door.
        assert client.get("/api/tasks", headers={"Cookie": COOKIE_NAME + "=" + ready["token"]}).status_code == 403
        direct = access.open_session(OWNER)
        assert client.get(PREFIX + "status", headers=headers(direct.token)).status_code == 403
        assert client.get(PREFIX + "read/actions/send", headers=headers(ready["token"])).status_code == 400
        assert post(client, "read/tasks", token=ready["token"]).status_code == 405


@pytest.mark.parametrize("extra", [{"Origin": "null"}, {"Origin": HOST}, {"Host": "wrong.example"},
    {"Cookie": "any=value"}, {"Authorization": "Bearer fake"}, {"X-Claudlobby-Owner": "0"}])
def test_wrong_boundary_or_credential_door_never_creates_nonce(connected_http, extra):
    with TestClient(connected_http[0], base_url=HOST) as client:
        result = post(client, "challenge", extra=extra)
        assert result.status_code == 403
        if "Origin" in extra or "Host" in extra or "Cookie" in extra:
            assert "access-control-allow-origin" not in result.headers
    with sqlite3.connect(connected_http[1].path) as conn:
        assert conn.execute("SELECT count(*) FROM connected_challenges").fetchone()[0] == 0


def test_explicit_init_missing_unavailable_not_cookie_or_status_repair(http):
    app = http[0]
    approve_workspace(http)
    with TestClient(app, base_url=HOST) as client:
        result = post(client, "challenge")
        assert result.status_code == 503  # optional connected schema not initialized
    # Tables still absent; native pairing and direct cookie path are unaffected.
    with sqlite3.connect(http[1].path) as conn:
        assert "connected_metadata" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}


@pytest.mark.parametrize("change", [{"purpose": "claudfather-workspace-host-bootstrap-v1"},
    {"binding_revision": 2}, {"sub": "00000000-0000-4000-8000-000000000001"}])
def test_wrong_signed_session_evidence_retains_nonce(connected_http, change):
    with TestClient(connected_http[0], base_url=HOST) as client:
        c = post(client, "challenge").json()
        refused = post(client, "issue", {"nonce": c["nonce"], "assertion": assertion(connected_http, c, **change)})
        assert refused.status_code == 403
        assert connected_http[1].connected().expected(c["nonce"], OWNER).nonce == c["nonce"]


def test_precise_methods_and_preflight_legacy_unchanged(connected_http):
    with TestClient(connected_http[0], base_url=HOST) as client:
        legacy = client.get("/api/owner/workspace/challenge", headers=headers())
        assert legacy.status_code == 405 and legacy.headers["allow"] == "POST, OPTIONS"
        assert client.get(PREFIX + "issue", headers=headers()).status_code == 405
        pre = {"Origin": PLANE, "Access-Control-Request-Method": "GET",
               "Access-Control-Request-Headers": "authorization"}
        result = client.options(PREFIX + "status", headers=pre)
        assert result.status_code == 204 and result.headers["access-control-allow-methods"] == "GET"
        assert "access-control-allow-credentials" not in result.headers
        assert client.options(PREFIX + "issue", headers=pre).status_code == 403
        pre["Access-Control-Request-Method"] = "HEAD"
        assert client.options(PREFIX + "read/tasks", headers=pre).status_code == 204


def test_rotate_logout_and_source_outage_preflight_preserves_token(connected_http):
    app, access, _, _, _ = connected_http
    with TestClient(app, base_url=HOST) as client:
        first = login(client, connected_http)
        old_source = app.connected_reads._source
        def unavailable():
            raise AccessUnavailable()
        app.connected_reads._source = unavailable
        assert post(client, "renew", token=first["token"]).status_code == 503
        app.connected_reads._source = old_source
        assert client.get(PREFIX + "status", headers=headers(first["token"])).status_code == 200
        # Renewal needs neither issuer availability nor another attestation.
        async def issuer_down(_):
            raise RuntimeError("not disclosed")
        app.connected_reads.keys.get = issuer_down
        renewed = post(client, "renew", token=first["token"])
        assert renewed.status_code == 200 and renewed.json()["token"] != first["token"]
        assert client.get(PREFIX + "status", headers=headers(first["token"])).status_code == 403
        token = renewed.json()["token"]
        assert post(client, "logout", token=token).json() == {"version": 1, "state": "signed_out"}
        assert client.get(PREFIX + "status", headers=headers(token)).status_code == 403
        assert access.current_grant().active and access.workspace().status()[0] == "active"


def test_current_native_loss_and_binding_revoke_gate_both_status_and_read(connected_http):
    app, access, identity, _, _ = connected_http
    with TestClient(app, base_url=HOST) as client:
        ready = login(client, connected_http)
        identity[0] = type(OWNER)(OWNER.namespace, "1002")
        assert client.get(PREFIX + "status", headers=headers(ready["token"])).status_code == 403
        assert client.get(PREFIX + "read/tasks", headers=headers(ready["token"])).status_code == 403
        identity[0] = OWNER
        _, b = access.workspace().status()
        access.workspace().revoke(expected_binding=b, expected_owner=b.owner)
        assert client.get(PREFIX + "status", headers=headers(ready["token"])).status_code == 403
        assert client.get(PREFIX + "read/tasks", headers=headers(ready["token"])).status_code == 403


def test_closing_native_check_fences_held_private_read(connected_http):
    app, access, identity, clock, _ = connected_http
    from tests.test_plane_owner_connected import evidence
    s = access.connected().issue(evidence(access.connected(), clock), OWNER)
    called = []
    async def producer(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        identity[0] = type(OWNER)(OWNER.namespace, "1002")
        await send({"type": "http.response.body", "body": b"private held body"})
    app.connected_reads.read_app.app = producer
    async def run():
        scope = {"type": "http", "method": "GET", "path": PREFIX + "read/tasks", "query_string": b"",
                 "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode()),
                             (b"authorization", ("Bearer " + s.token).encode())]}
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            called.append(message)
        await app.connected_reads(scope, receive, send)
    asyncio.run(run())
    assert called[0]["status"] == 403
    assert b"private held body" not in b"".join(m.get("body", b"") for m in called)


def test_real_canonical_sse_stops_after_revocation_without_next_chunk(connected_http):
    app, access, identity, clock, _ = connected_http
    from tests.test_plane_owner_connected import evidence
    s = access.connected().issue(evidence(access.connected(), clock), OWNER)
    frames = []
    async def run():
        scope = {"type": "http", "method": "GET", "path": PREFIX + "read/stream", "query_string": b"cursor=0",
            "http_version": "1.1", "scheme": "https", "root_path": "", "server": ("host.example.test", 443),
            "asgi": {"spec_version": "2.4"}, "headers": [(b"host", b"host.example.test"),
                (b"origin", PLANE.encode()), (b"authorization", ("Bearer " + s.token).encode())]}
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            frames.append(message)
            if message["type"] == "http.response.body" and message.get("more_body"):
                identity[0] = type(OWNER)(OWNER.namespace, "1002")
        def state():
            return f"sent={[m.get('status', m['type']) for m in frames]}, readers={len(app.connected_reads._readers)}"
        await bounded(app.connected_reads(scope, receive, send), "the stream to stop", state)
    asyncio.run(run())
    bodies = [m for m in frames if m["type"] == "http.response.body"]
    assert bodies[0]["body"] == b"retry: 3000\n\n"
    assert bodies[-1]["body"] == b"" and not bodies[-1]["more_body"]
    assert not any(b"rows" in m["body"] for m in bodies)


def test_query_credentials_duplicate_headers_and_unknown_doors_refused(connected_http):
    with TestClient(connected_http[0], base_url=HOST) as client:
        ready = login(client, connected_http)
        for path in ("read/tasks?token=x", "read/tasks?fleet=data&fleet=support", "read/index.html", "unknown"):
            assert client.get(PREFIX + path, headers=headers(ready["token"])).status_code == 400
        dup = [("Origin", PLANE), ("Authorization", "Bearer " + ready["token"]),
               ("Authorization", "Bearer " + ready["token"])]
        assert client.get(PREFIX + "status", headers=dup).status_code == 403


def test_default_and_connected_deadline_roster(connected_http):
    app = connected_http[0]
    assert app.workspace_bootstrap.response_seconds({}) == 35
    for action in ("challenge", "status", "renew", "logout", "issue"):
        method = "GET" if action == "status" else "POST"
        assert app.connected_reads.response_seconds({"path": PREFIX + action, "method": method}) == (35 if action == "issue" else 15)
    assert app.connected_reads.response_seconds({"path": PREFIX + "issue", "method": "OPTIONS"}) == 15


def test_queued_deadline_no_nonce_late_effect_and_shutdown_drains(connected_http, monkeypatch):
    import claudlobby.plane.owner_workspace_http as module
    consumer = connected_http[0].connected_reads
    async def scenario():
        advance, paused = move_clock(monkeypatch, module)
        entered, release = asyncio.Event(), asyncio.Event()
        original = module.run_in_threadpool
        async def delayed(fn, *args, **kwargs):
            # The shipped _blocking wrapper determines whether its queued
            # guarded callable is abandoned before actually starting.
            entered.set()
            await release.wait()
            return await original(fn, *args, **kwargs)
        monkeypatch.setattr(module, "run_in_threadpool", delayed)
        scope = {"type": "http", "method": "POST", "path": PREFIX + "challenge", "query_string": b"",
            "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode()),
                (b"content-type", b"application/json"), (b"x-claudlobby-owner", b"1")]}
        async def receive():
            return {"type": "http.request", "body": b"{}"}
        output = []
        async def send(message):
            output.append(message)
        request = asyncio.create_task(consumer(scope, receive, send))
        def state():
            return (f"request={status(request)}, sent={[m.get('status', m['type']) for m in output]}, "
                    f"workers={len(consumer._workers)}, operations={len(consumer._operations)}")
        await bounded(entered.wait(), "the first threadpool call", state)
        # Its deadline passes now that the call is queued. A 30 ms wall-clock
        # deadline could fire first on a stalled loop, and then nothing set
        # `entered` and this wait never ended (#2388).
        advance(consumer.response_seconds(scope))
        with paused():  # the answer is due now, and the drain once released (#2406)
            await bounded(request, "the response", state)
            assert output[0]["status"] == 503, state()
            release.set()
            await bounded(consumer.shutdown(), "shutdown", state)
        assert not consumer._workers and not consumer._operations
        with sqlite3.connect(connected_http[1].path) as conn:
            assert conn.execute("SELECT count(*) FROM connected_challenges").fetchone()[0] == 0
    asyncio.run(scenario())


def test_started_work_retains_capacity_after_timeout_until_drained(connected_http, monkeypatch):
    import claudlobby.plane.owner_workspace_http as module
    consumer = connected_http[0].connected_reads
    entered, release = threading.Event(), threading.Event()
    original = consumer.connected.begin
    def held(principal):
        entered.set()
        assert release.wait(3), "waited 3 s for the scenario to release begin"
        return original(principal)
    monkeypatch.setattr(consumer.connected, "begin", held)
    async def scenario():
        advance, paused = move_clock(monkeypatch, module)
        scope = {"type": "http", "method": "POST", "path": PREFIX + "challenge", "query_string": b"",
            "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode()),
                (b"content-type", b"application/json"), (b"x-claudlobby-owner", b"1")]}
        async def receive():
            return {"type": "http.request", "body": b"{}"}
        output = []
        async def send(message):
            output.append(message)
        request = asyncio.create_task(consumer(scope, receive, send))
        def state():
            return (f"request={status(request)}, sent={[m.get('status', m['type']) for m in output]}, "
                    f"workers={len(consumer._workers)}, operations={len(consumer._operations)}")
        assert await asyncio.to_thread(entered.wait, 5), "waited 5 s for begin to start: " + state()
        advance(consumer.response_seconds(scope))  # its deadline passes while begin runs (#2388)
        with paused():  # the answer is due now (#2406)
            await bounded(request, "the response", state)
        assert output[0]["status"] == 503 and len(consumer._workers) == 1, state()
        draining = asyncio.create_task(consumer.shutdown())
        await asyncio.sleep(.01)
        assert not draining.done()  # started transaction is still owned
        release.set()
        with paused():  # the drain ends with begin (#2406)
            await bounded(draining, "shutdown", state)
        assert not consumer._workers and not consumer._operations
        with sqlite3.connect(connected_http[1].path) as conn:
            assert conn.execute("SELECT count(*) FROM connected_challenges").fetchone()[0] == 1
        # The response stayed unknown; no nonce is recovered or automatically retried.
    try:
        asyncio.run(scenario())
    finally:
        release.set()


def _challenge_scope():
    return {"type": "http", "method": "POST", "path": PREFIX + "challenge", "query_string": b"",
        "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode()),
            (b"content-type", b"application/json"), (b"x-claudlobby-owner", b"1")]}


def _recorded_source(consumer, monkeypatch):
    """The first threadpool call a challenge makes is _source: record each run of it."""
    ran, original = [], consumer._source
    def source():
        ran.append("source")
        return original()
    monkeypatch.setattr(consumer, "_source", source)
    return ran


def test_a_queued_call_reached_after_its_deadline_never_runs(connected_http, monkeypatch):
    """The pool reaches the queued call once its deadline has passed but before
    the loop has run the deadline's cancel: the guard's own deadline check is
    what refuses it."""
    import claudlobby.plane.owner_workspace_http as module
    consumer = connected_http[0].connected_reads
    ran = _recorded_source(consumer, monkeypatch)
    async def scenario():
        advance, paused = move_clock(monkeypatch, module)
        entered, release = asyncio.Event(), asyncio.Event()
        async def reached(fn, *args, **kwargs):
            # Released, the call runs at once on this thread: the loop has not
            # yet run the deadline's cancel when the guard decides.
            entered.set()
            await release.wait()
            return fn(*args, **kwargs)
        monkeypatch.setattr(module, "run_in_threadpool", reached)
        scope = _challenge_scope()
        async def receive():
            return {"type": "http.request", "body": b"{}"}
        output = []
        async def send(message):
            output.append(message)
        request = asyncio.create_task(consumer(scope, receive, send))
        def state():
            return (f"request={status(request)}, sent={[m.get('status', m['type']) for m in output]}, "
                    f"ran={ran}, workers={len(consumer._workers)}, operations={len(consumer._operations)}")
        await bounded(entered.wait(), "the first threadpool call", state)
        advance(consumer.response_seconds(scope))
        release.set()  # in the same step, so the call starts before the cancel runs
        with paused():
            await bounded(request, "the response", state)
            assert output[0]["status"] == 503, state()
            await bounded(consumer.shutdown(), "shutdown", state)
        assert ran == [], state()
        assert not consumer._workers and not consumer._operations
    asyncio.run(scenario())


def test_a_call_its_caller_abandoned_never_runs_once_a_worker_reaches_it(connected_http, monkeypatch):
    """The caller is cancelled while its call waits in a worker thread, with the
    clock held short of the deadline: the cancel lets the response go without
    the worker, and the abandon flag keeps the call from running once the worker
    reaches it."""
    import claudlobby.plane.owner_workspace_http as module
    consumer = connected_http[0].connected_reads
    ran = _recorded_source(consumer, monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = module.run_in_threadpool
    async def dispatched(fn, *args, **kwargs):
        def held():
            # In a worker thread before the guard runs, where a cancel no longer reaches it.
            # It outwaits the 5 s bound on the response, so that bound decides.
            entered.set()
            try:
                assert release.wait(30), "waited 30 s for the scenario to release the worker"
                return fn(*args, **kwargs)
            finally:
                finished.set()
        return await original(held)
    monkeypatch.setattr(module, "run_in_threadpool", dispatched)
    async def scenario():
        _, paused = move_clock(monkeypatch, module)
        scope = _challenge_scope()
        async def receive():
            return {"type": "http.request", "body": b"{}"}
        output = []
        async def send(message):
            output.append(message)
        def state():
            return (f"request={status(request)}, sent={[m.get('status', m['type']) for m in output]}, "
                    f"ran={ran}, workers={len(consumer._workers)}, operations={len(consumer._operations)}")
        try:
            with paused():  # neither the deadline nor its timer comes
                request = asyncio.create_task(consumer(scope, receive, send))
                assert await asyncio.to_thread(entered.wait, 5), "waited 5 s for a worker: " + state()
                operation, = consumer._operations
                consumer._cancel_operation(operation)  # what the deadline's timer calls
                await bounded(request, "the response", state)
                assert output[0]["status"] == 503, state()
                release.set()
                assert await asyncio.to_thread(finished.wait, 5), "waited 5 s for the worker: " + state()
                assert ran == [], state()
                await bounded(consumer.shutdown(), "shutdown", state)
            assert not consumer._workers and not consumer._operations
        finally:
            release.set()
    asyncio.run(scenario())


def test_finite_read_deadline_no_private_body_and_shutdown_stream_ownership(connected_http, monkeypatch):
    import claudlobby.plane.owner_connected_http as module
    consumer = connected_http[0].connected_reads
    async def held(scope, receive, send):
        await asyncio.Event().wait()
    consumer.read_app = held
    monkeypatch.setattr(module, "READ_SECONDS", .03)
    with TestClient(connected_http[0], base_url=HOST) as client:
        result = client.get(PREFIX + "read/tasks", headers=headers("A"*43))
        assert result.status_code == 503 and not consumer._readers


@pytest.mark.parametrize("closing_loss", [None, "expired", "revoked", "source"])
def test_real_sql_timeout_signal_is_sanitized_and_closing_admission_wins(connected_http, monkeypatch, closing_loss):
    from claudlobby.plane import view

    app, access, _, clock, _ = connected_http
    ticks = []
    def now():
        ticks.append(len(ticks) * 2.0)
        return ticks[-1]
    def expensive(conn, root):
        try:
            return conn.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL"
                                " SELECT x+1 FROM n WHERE x<1000000000)"
                                " SELECT sum(x) FROM n").fetchone()[0]
        finally:
            # Changes occur after opening admission, before the real closing
            # source/credential check; none may be hidden by the retry signal.
            if closing_loss == "expired":
                clock[0] += 3600
            elif closing_loss == "revoked":
                _, binding = access.workspace().status()
                access.workspace().revoke(expected_binding=binding, expected_owner=binding.owner)
            elif closing_loss == "source":
                (root / "state" / "host-uid").unlink()
    with TestClient(app, base_url=HOST) as client:
        ready = login(client, connected_http)
        monkeypatch.setattr(view, "time", types.SimpleNamespace(monotonic=now))
        monkeypatch.setattr(view, "_fetch_summary", expensive)
        result = client.get(PREFIX + "read/summary", headers=headers(ready["token"]))
        assert ticks[-1] == view._QUERY_SECONDS
        if closing_loss is None:
            assert result.status_code == 200
            assert result.json() == {"state": "unreadable", "reason": "query_timeout",
                                     "remediation": "query timed out — retry this read"}
        else:
            assert result.status_code == (503 if closing_loss == "source" else 403)
            assert result.json()["state"] == ("unavailable" if closing_loss == "source" else "denied")
            assert "reason" not in result.json()
        assert "data" not in result.json() and "provenance" not in result.json()


def test_head_runs_real_canonical_get_queries_not_static_fallback(connected_http, monkeypatch):
    import claudlobby.plane.view as view
    calls = []
    original = view._fetch_tasks
    def observed(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(view, "_fetch_tasks", observed)
    with TestClient(connected_http[0], base_url=HOST) as client:
        ready = login(client, connected_http)
        for suffix in ("tasks?fleet=data", "tasks?fleet=unknown", "summary", "fleets", "channel?fleet=data&limit=1"):
            get = client.get(PREFIX + "read/" + suffix, headers=headers(ready["token"]))
            head = client.head(PREFIX + "read/" + suffix, headers=headers(ready["token"]))
            assert head.status_code == get.status_code == 200
            assert head.headers["content-type"] == get.headers["content-type"]
            assert head.headers["content-length"] == get.headers["content-length"]
            assert head.content == b""
            assert head.headers["access-control-allow-origin"] == PLANE
        assert len(calls) == 4  # the same canonical query ran for each GET/HEAD pair
        task = client.get(PREFIX + "read/tasks?fleet=data", headers=headers(ready["token"])).json()["data"]["tasks"][0]
        path = PREFIX + "read/tasks/" + task["task_id"] + "?fleet=data"
        get = client.get(path, headers=headers(ready["token"]))
        head = client.head(path, headers=headers(ready["token"]))
        assert head.status_code == get.status_code == 200
        assert head.headers["content-type"] == "application/json" and head.content == b""


def test_head_real_query_closing_native_loss_denies_without_any_body(connected_http, monkeypatch):
    import claudlobby.plane.view as view
    app, _, identity, _, _ = connected_http
    original = view._fetch_tasks
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        identity[0] = type(OWNER)(OWNER.namespace, "1002")
        return result
    with TestClient(app, base_url=HOST) as client:
        ready = login(client, connected_http)
        monkeypatch.setattr(view, "_fetch_tasks", changed)
        head = client.head(PREFIX + "read/tasks?fleet=data", headers=headers(ready["token"]))
        assert head.status_code == 403 and head.content == b""


def test_head_stream_uses_actual_bounded_canonical_producer_and_zero_asgi_body(connected_http):
    app, access, _, clock, _ = connected_http
    from tests.test_plane_owner_connected import evidence
    session = access.connected().issue(evidence(access.connected(), clock), OWNER)
    frames = []
    async def run():
        scope = {"type": "http", "method": "HEAD", "path": PREFIX + "read/stream", "query_string": b"cursor=0&once=0",
            "http_version": "1.1", "scheme": "https", "root_path": "", "server": ("host.example.test", 443),
            "asgi": {"spec_version": "2.4"}, "headers": [(b"host", b"host.example.test"),
                (b"origin", PLANE.encode()), (b"authorization", ("Bearer " + session.token).encode())]}
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            frames.append(message)
        def state():
            return f"sent={[m.get('status', m['type']) for m in frames]}, readers={len(app.connected_reads._readers)}"
        await bounded(app.connected_reads(scope, receive, send), "the HEAD stream", state, seconds=2)
        assert not app.connected_reads._readers
    asyncio.run(run())
    assert frames[0]["status"] == 200
    assert b"text/event-stream" in dict(frames[0]["headers"])[b"content-type"]
    bodies = [m for m in frames if m["type"] == "http.response.body"]
    assert bodies and all(m.get("body", b"") == b"" for m in bodies)
    assert not bodies[-1].get("more_body", False)


def test_missing_bearer_refuses_before_native_lookup_and_head_refusal_has_no_asgi_body(connected_http):
    consumer = connected_http[0].connected_reads
    calls = []
    async def should_not_verify(scope):
        calls.append(scope)
        return OWNER
    consumer.verify_principal = should_not_verify
    async def run():
        for action, method in (("read/tasks", "HEAD"), ("status", "GET")):
            output = []
            scope = {"type": "http", "method": method, "path": PREFIX + action, "query_string": b"",
                "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode())]}
            async def receive():
                raise AssertionError("no body read expected")
            async def send(message):
                output.append(message)
            await consumer(scope, receive, send)
            assert output[0]["status"] == 403
            if method == "HEAD":
                assert not b"".join(m.get("body", b"") for m in output)
    asyncio.run(run())
    assert not calls


@pytest.mark.parametrize("headers_started", [False, True])
def test_shutdown_cancelled_owned_stream_does_not_escape_request(connected_http, headers_started):
    consumer = connected_http[0].connected_reads
    async def run():
        entered = asyncio.Event()
        async def held(scope, receive, send):
            entered.set()
            if headers_started:
                await send({"type": "http.response.start", "status": 200, "headers": []})
            await asyncio.Event().wait()
        consumer.read_app = held
        scope = {"type": "http", "method": "GET", "path": PREFIX + "read/stream", "query_string": b"",
            "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode()), (b"authorization", b"Bearer " + b"A"*43)]}
        async def receive():
            await asyncio.Event().wait()
        output = []
        async def send(message):
            output.append(message)
        request = asyncio.create_task(consumer(scope, receive, send))
        def state():
            return (f"request={status(request)}, sent={[m.get('status', m['type']) for m in output]}, "
                    f"readers={len(consumer._readers)}")
        await bounded(entered.wait(), "the read app to start", state)
        await bounded(consumer.shutdown(), "shutdown", state)
        await bounded(request, "the request", state)
        assert not consumer._readers
        assert output[0]["status"] == (200 if headers_started else 503)
        if not headers_started:
            assert output[-1]["type"] == "http.response.body"
    asyncio.run(run())


def test_connected_selected_route_allowlist_matches_canonical_query_contract(connected_http):
    from claudlobby.plane.owner_connected_http import _READS
    app = connected_http[0].read_app.app
    routes = {r.path: r for r in app.routes if hasattr(r, "dependant")}
    for suffix, fields in _READS.items():
        route = routes["/api/" + suffix]
        assert "GET" in route.methods
        assert {p.name for p in route.dependant.query_params} == set(fields), suffix
    detail = routes["/api/tasks/{task_id}"]
    assert {p.name for p in detail.dependant.query_params} == {"fleet"}


@pytest.mark.parametrize("stopping", [False, True])
@pytest.mark.parametrize("suffix", ["tasks", "stream"])
def test_caller_cancellation_still_propagates_and_cleans_owned_reader(connected_http, stopping, suffix):
    consumer = connected_http[0].connected_reads
    async def run():
        entered = asyncio.Event()
        async def held(scope, receive, send):
            if suffix == "stream":
                await send({"type": "http.response.start", "status": 200, "headers": []})
            entered.set()
            await asyncio.Event().wait()
        consumer.read_app = held
        scope = {"type": "http", "method": "GET", "path": PREFIX + "read/" + suffix, "query_string": b"",
            "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode()), (b"authorization", b"Bearer " + b"A"*43)]}
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            pass
        request = asyncio.create_task(consumer(scope, receive, send))
        def state():
            return f"request={status(request)}, readers={len(consumer._readers)}, stopping={consumer._stopping}"
        await bounded(entered.wait(), "the read app to start", state)
        release = asyncio.Event()
        if stopping:
            # Real shutdown is already draining a started lifecycle worker when
            # the caller independently cancels this read request.
            worker = asyncio.create_task(release.wait())
            consumer._workers.add(worker)
            shutdown = asyncio.create_task(consumer.shutdown())
            await asyncio.sleep(0)
            assert consumer._stopping and not shutdown.done()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bounded(request, "the cancelled request", state)
        assert not consumer._readers
        if stopping:
            release.set()
            await bounded(shutdown, "shutdown", state)
            consumer._workers.discard(worker)
    asyncio.run(run())


@pytest.mark.parametrize("query", ["cursor=", "once=invalid"])
def test_head_stream_preserves_canonical_get_query_validation(connected_http, query):
    with TestClient(connected_http[0], base_url=HOST) as client:
        ready = login(client, connected_http)
        path = PREFIX + "read/stream?" + query
        get = client.get(path, headers=headers(ready["token"]))
        head = client.head(path, headers=headers(ready["token"]))
        assert head.status_code == get.status_code == 422
        assert head.headers["content-type"] == get.headers["content-type"]
        assert head.content == b""
