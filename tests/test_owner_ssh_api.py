"""SSH owner source admission and finite policy carrier, without native access."""
import asyncio
import dataclasses
import os
import json
import threading
import base64

import pytest

from claudlobby.plane.owner_access import AccessDenied, PrincipalRef


def test_server_source_is_captured_once_and_cannot_follow_environment_changes(monkeypatch):
    from claudlobby.plane.owner_ssh import SSHSessionSource
    monkeypatch.setenv('SSH_CONNECTION', '100.64.0.42 4242 100.64.0.7 22')
    source = SSHSessionSource.capture()
    monkeypatch.setenv('SSH_CONNECTION', '100.64.0.99 4242 100.64.0.7 22')
    assert str(source.client_ip) == '100.64.0.42'
    assert str(source.server_ip) == '100.64.0.7'
    with pytest.raises(dataclasses.FrozenInstanceError):
        source.client_port = 99


@pytest.mark.parametrize('raw', [None, '', ' 100.64.0.42 4242 100.64.0.7 22',
    '100.64.0.42  4242 100.64.0.7 22', '100.64.0.42 04242 100.64.0.7 22',
    '100.64.0.42 0 100.64.0.7 22', '100.64.0.42 65536 100.64.0.7 22',
    '192.0.2.1 4242 100.64.0.7 22', '100.64.0.42 4242 127.0.0.1 22',
    '100.64.0.42 4242 100.64.0.7 22 extra', 'fe80::1%lo0 4242 100.64.0.7 22'])
def test_absent_ambiguous_or_non_tailnet_server_source_refuses_without_fallback(monkeypatch, raw):
    from claudlobby.plane.owner_ssh import SSHSessionSource
    monkeypatch.setenv('SSH_CLIENT', '100.64.0.42 4242 22')
    if raw is None:
        monkeypatch.delenv('SSH_CONNECTION', raising=False)
    else:
        monkeypatch.setenv('SSH_CONNECTION', raw)
    with pytest.raises(AccessDenied):
        SSHSessionSource.capture()


def test_distinct_ssh_verifier_ignores_forged_http_identity_and_checks_server_destination(monkeypatch):
    from claudlobby.plane.owner_ssh import SSHSessionSource, SSHSessionVerifier
    from claudlobby.plane.owner_ingress import NativePrincipalResolver
    monkeypatch.setenv('SSH_CONNECTION', '100.64.0.42 4242 100.64.0.7 22')
    resolver = NativePrincipalResolver(tailscale_binary=__import__('pathlib').Path('/usr/bin/true'))
    calls = []
    async def command(*args):
        calls.append(args)
        if args == ('debug', 'prefs'):
            return {'ControlURL': 'https://controlplane.tailscale.com'}
        if args == ('status', '--json'):
            return {'BackendState': 'Running', 'Self': {'ID': 'fixture-server', 'PublicKey': 'fixture-key',
                'UserID': 123, 'DNSName': 'fixture.example.ts.net.', 'TailscaleIPs': ['100.64.0.7'], 'Online': True}}
        assert args == ('whois', '--json', '--proto=tcp', '100.64.0.42')
        return {'Node': {'User': 123, 'Addresses': ['100.64.0.42/32']}, 'UserProfile': {'ID': 123}}
    monkeypatch.setattr(resolver, '_command', command)
    verifier = SSHSessionVerifier(SSHSessionSource.capture(), resolver)
    async def run():
        assert await verifier.origin() == 'https://fixture.example.ts.net'
        assert await verifier({'client': ('192.0.2.1', 1), 'headers': [(b'x-forwarded-for', b'100.64.0.99')]}) == PrincipalRef('tailscale:controlplane.tailscale.com', '123')
    asyncio.run(run())
    assert ('whois', '--json', '--proto=tcp', '100.64.0.99') not in calls


def test_finite_operation_map_cannot_become_http_or_shell_proxy():
    from claudlobby.commands.owner_api_stdio import operation_request
    method, path, query, body, headers = operation_request('session-status', {}, 'A' * 43, None)
    assert (method, path, query, body) == ('GET', '/api/owner/connected/status', b'', b'')
    assert headers == [(b'authorization', b'Bearer ' + b'A' * 43)]
    for operation, value in [('https://elsewhere/', {}), ('pair', {'principal': 'forged'}),
            ('setup-launch', {'command': 'touch /tmp/no'}), ('read-task', {'task_id': '../private'})]:
        with pytest.raises(ValueError):
            operation_request(operation, value, None, None)


def test_api_body_requires_ack_and_end_follows_canonical_shutdown():
    from claudlobby.commands.owner_api_stdio import APIChannel, run_operation
    incoming, client = os.pipe()
    observer, outgoing = os.pipe()
    stopped = []
    class App:
        async def __call__(self, scope, receive, send):
            assert scope['path'] == '/api/owner/connected/status'
            assert scope['client'] == ('ssh-session', 0)
            assert not any(k == b'x-forwarded-for' for k, _ in scope['headers'])
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'application/json'), (b'set-cookie', b'forbidden')]})
            await send({'type': 'http.response.body', 'body': b'{"state":"ready"}'})
        async def shutdown(self):
            stopped.append(True)
    channel = APIChannel(incoming, outgoing)
    def run():
        asyncio.run(run_operation(channel, App(), 'session-status', {}, 'A' * 43, None,
                                  origin='https://fixture.example.ts.net'))
    thread = threading.Thread(target=run)
    thread.start()
    def frame():
        raw = bytearray()
        while not raw.endswith(b'\n'):
            raw.extend(os.read(observer, 1))
        return json.loads(raw)
    head = frame()
    assert head['type'] == 'headers' and head['headers'] == {'content-type': 'application/json'}
    chunk = frame()
    assert base64.b64decode(chunk['data']) == b'{"state":"ready"}'
    assert not stopped
    ack = {'version': 1, 'type': 'ack', 'seq': 1, 'session_id': channel.session_id, 'chunk_id': 1}
    os.write(client, (json.dumps(ack, sort_keys=True, separators=(',', ':')) + '\n').encode())
    end = frame()
    thread.join(2)
    assert end['type'] == 'end' and end['bytes_sent'] == 17 and stopped == [True]
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

TOKEN = 'A' * 43
ORIGIN = 'https://fixture.example.ts.net'


def wire(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')


class Client:
    """The browser side of one API exec: two pipes, exact frames."""
    def __init__(self):
        from claudlobby.commands.owner_api_stdio import APIChannel
        self.incoming, self.client = os.pipe()
        self.observer, self.outgoing = os.pipe()
        self.channel = APIChannel(self.incoming, self.outgoing)
        self.seq = 0

    def send(self, kind, **values):
        self.seq += 1
        os.write(self.client, wire({'version': 1, 'type': kind, 'seq': self.seq,
                                    'session_id': self.channel.session_id, **values}))

    def frame(self, timeout=5):
        import select
        raw = bytearray()
        end = time.monotonic() + timeout
        while not raw.endswith(b'\n'):
            assert select.select([self.observer], [], [], max(0, end - time.monotonic()))[0], 'no frame'
            part = os.read(self.observer, 1)
            assert part, 'channel closed without a frame'
            raw.extend(part)
        value = json.loads(raw)
        assert raw == wire(value), 'frames are canonical sorted JSON'
        return value

    def quiet(self):
        import select
        return not select.select([self.observer], [], [], .2)[0]

    def hang_up(self):
        os.close(self.client)
        self.client = None

    def close(self):
        for fd in (self.incoming, self.client, self.observer, self.outgoing):
            if fd is not None:
                os.close(fd)


@pytest.fixture
def client():
    value = Client()
    yield value
    value.close()


def start_frame(**changes):
    return {'profile': 'owner-api-v1', 'expected_uid': 501, 'source_revision': 'a' * 40,
            'binding_sha256': 'b' * 64, 'operation': 'session-status', 'input': {},
            'token': TOKEN, 'last_event_id': None, **changes}


class Recorder:
    """A stand-in policy app that records the order of its own lifecycle."""
    def __init__(self, body):
        self.body, self.events = body, []

    async def __call__(self, scope, receive, send):
        self.events.append('request')
        try:
            await self.body(scope, receive, send)
            self.events.append('returned')
        except asyncio.CancelledError:
            self.events.append('cancelled')
            raise

    async def shutdown(self):
        self.events.append('shutdown')


def run_in_thread(client, app, operation='session-status', payload=None, token=TOKEN, last=None):
    from claudlobby.commands.owner_api_stdio import run_operation
    outcome = []
    def work():
        try:
            asyncio.run(run_operation(client.channel, app, operation, payload or {}, token, last, origin=ORIGIN))
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)
    thread = threading.Thread(target=work)
    thread.start()
    return thread, outcome


def test_cancel_before_start_is_a_healthy_end_not_a_refusal(client):
    """Published wire: end(status=0,bytes_sent=0,cancelled=true), then exit 0."""
    result = []
    thread = threading.Thread(target=lambda: result.append(asyncio.run(client.channel.start())))
    thread.start()
    assert client.frame() == {'version': 1, 'type': 'hello', 'seq': 1, 'profile': 'owner-api-v1',
                              'session_id': client.channel.session_id}
    client.send('cancel')
    thread.join(5)
    assert result == [None]
    client.channel.end_cancelled()
    assert client.frame() == {'version': 1, 'type': 'end', 'seq': 2, 'session_id': client.channel.session_id,
                              'status': 0, 'bytes_sent': 0, 'cancelled': True}


def test_cancel_received_during_admission_never_reaches_a_policy_handler(client):
    async def body(scope, receive, send):
        pytest.fail('a cancelled operation entered policy')
    app = Recorder(body)
    client.send('cancel')
    thread, outcome = run_in_thread(client, app)
    end = client.frame()
    thread.join(5)
    assert outcome == [None] and app.events == ['shutdown']
    assert (end['type'], end['status'], end['bytes_sent'], end['cancelled']) == ('end', 0, 0, True)


def test_stream_cancel_after_headers_drains_the_handler_before_end(client):
    """An active read stream is cancelled by protocol, drained, then ended."""
    waiting = threading.Event()
    async def body(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'text/event-stream')]})
        await send({'type': 'http.response.body', 'body': b'event: one\n\n', 'more_body': True})
        waiting.set()
        await asyncio.Event().wait()  # a held stream with no idle deadline
    app = Recorder(body)
    threads = threading.active_count()
    thread, outcome = run_in_thread(client, app, 'read-stream', {'cursor': '0'})
    assert client.frame()['type'] == 'headers'
    chunk = client.frame()
    assert base64.b64decode(chunk['data']) == b'event: one\n\n' and chunk['chunk_id'] == 1
    client.send('ack', chunk_id=1)
    assert waiting.wait(5) and client.quiet(), 'no end while the stream is still held'
    client.send('cancel')
    end = client.frame(10)
    thread.join(10)
    assert outcome == [None] and not thread.is_alive()
    assert (end['type'], end['status'], end['bytes_sent'], end['cancelled']) == ('end', 200, 12, True)
    assert app.events == ['request', 'cancelled', 'shutdown'], 'end follows the complete drain'
    deadline = time.monotonic() + 5
    while threading.active_count() > threads and time.monotonic() < deadline:
        time.sleep(.05)
    assert threading.active_count() <= threads, 'the control reader joined before exit'


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
    assert isinstance(outcome[0], StdioRefused) and outcome[0].code == 'disconnected'
    assert app.events == ['request', 'cancelled', 'shutdown'] and client.quiet()


def test_wrong_acknowledgement_refuses_even_when_a_handler_swallows_the_fault(client):
    async def body(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        try:
            await send({'type': 'http.response.body', 'body': b'first'})
        except Exception:
            pass  # a handler that converts faults into responses cannot hide this one
    app = Recorder(body)
    thread, outcome = run_in_thread(client, app)
    assert client.frame()['type'] == 'headers' and client.frame()['type'] == 'chunk'
    client.send('ack', chunk_id=7)
    thread.join(10)
    from claudlobby.commands.owner_stdio import StdioRefused
    assert isinstance(outcome[0], StdioRefused) and outcome[0].code == 'invalid_request'
    assert app.events[-1] == 'shutdown' and client.quiet(), 'no end frame after a carrier fault'


def test_body_is_chunked_one_credit_at_a_time_and_capped(client):
    async def body(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'x' * 40000})
    app = Recorder(body)
    thread, outcome = run_in_thread(client, app)
    assert client.frame()['type'] == 'headers'
    sizes = []
    for expected in (1, 2, 3):
        chunk = client.frame()
        assert chunk['chunk_id'] == expected and client.quiet(), 'no chunk before the previous ack'
        sizes.append(len(base64.b64decode(chunk['data'])))
        client.send('ack', chunk_id=expected)
    end = client.frame()
    thread.join(5)
    assert sizes == [16384, 16384, 7232] and end['bytes_sent'] == 40000 and end['cancelled'] is False
    assert outcome == [None]


def test_internal_scope_is_fixed_and_carries_no_request_selected_identity(client):
    seen = {}
    async def body(scope, receive, send):
        seen.update(scope)
        seen['request'] = await receive()
        await send({'type': 'http.response.start', 'status': 202, 'headers': [(b'set-cookie', b'never')]})
        await send({'type': 'http.response.body', 'body': b''})
    thread, outcome = run_in_thread(client, Recorder(body), 'pair', {}, None)
    head = client.frame()
    end = client.frame()
    thread.join(5)
    assert outcome == [None] and head['headers'] == {} and end['status'] == 202
    headers = dict(seen['headers'])
    assert (seen['method'], seen['path'], seen['query_string']) == ('POST', '/api/owner/bootstrap/pair', b'')
    assert headers == {b'host': b'fixture.example.ts.net', b'origin': b'https://plane.claudfather.ai',
                       b'content-type': b'application/json', b'x-claudlobby-owner': b'1'}
    assert seen['client'] == ('ssh-session', 0) and seen['request']['body'] == b'{}'


@pytest.mark.parametrize('operation', ['setup-signin-begin', 'setup-signin-code', 'setup-signin-cancel'])
def test_process_bound_signin_operations_refuse_before_any_engine_entry(client, operation):
    """One exec cannot own the single retained sign-in attempt; it must not burn it."""
    from claudlobby.commands.owner_stdio import StdioRefused
    payload = {'generation': '11111111-1111-4111-8111-111111111111',
               'attempt_id': '22222222-2222-4222-8222-222222222222'}
    if operation.endswith('code'):
        payload['code'] = 'c#s'
    outcome = []
    def work():
        try:
            outcome.append(asyncio.run(client.channel.start()))
        except StdioRefused as exc:
            outcome.append(exc.code)
    thread = threading.Thread(target=work)
    thread.start()
    client.frame()
    client.send('start', **start_frame(operation=operation, input=payload))
    thread.join(5)
    assert outcome == ['operation_requires_persistent_owner']


@pytest.mark.parametrize('change', [
    {'profile': 'owner-setup-v1'}, {'expected_uid': 0}, {'source_revision': 'main'},
    {'operation': 'host update apply'}, {'input': {'path': '/etc/passwd'}}, {'token': None},
    {'operation': 'pair', 'token': TOKEN}, {'last_event_id': '5'}, {'finish_setup': None},
    {'principal': {'subject': '1'}}, {'headers': {'x-forwarded-for': '100.64.0.9'}}])
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
    client.send('start', **start_frame(**change))
    thread.join(5)
    assert outcome == ['invalid_request']


def test_lifespan_closes_after_the_operation_and_before_return(client):
    from claudlobby.commands.owner_api_stdio import serve
    events = []
    class App:
        async def __call__(self, scope, receive, send):
            if scope['type'] == 'lifespan':
                assert (await receive())['type'] == 'lifespan.startup'
                events.append('startup')
                await send({'type': 'lifespan.startup.complete'})
                assert (await receive())['type'] == 'lifespan.shutdown'
                events.append('lifespan-shutdown')
                await send({'type': 'lifespan.shutdown.complete'})
                return
            events.append('request')
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})
        async def shutdown(self):
            events.append('shutdown')
    outcome = []
    request = start_frame()
    thread = threading.Thread(target=lambda: outcome.append(
        asyncio.run(serve(client.channel, App(), request, origin=ORIGIN))))
    thread.start()
    assert client.frame()['type'] == 'headers' and client.frame()['type'] == 'end'
    thread.join(5)
    assert outcome == [None] and events == ['startup', 'request', 'shutdown', 'lifespan-shutdown']


# --- process entry: parser, dispatch and exit status ------------------------

def api_process(tmp_path, **env):
    from tests.conftest import constructed_env
    return subprocess.Popen([sys.executable, '-m', 'claudlobby', '--root', str(tmp_path / 'never-created'),
        'host', 'owner', 'api', '--stdio'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=constructed_env(**env))


def test_cli_cancel_before_headers_ends_and_exits_zero(tmp_path):
    process = api_process(tmp_path, SSH_CONNECTION='100.64.0.42 4242 100.64.0.7 22')
    try:
        hello = json.loads(process.stdout.readline())
        assert hello['type'] == 'hello' and hello['profile'] == 'owner-api-v1'
        process.stdin.write(wire({'version': 1, 'type': 'cancel', 'seq': 1, 'session_id': hello['session_id']}))
        process.stdin.flush()
        end = json.loads(process.stdout.readline())
        assert process.wait(15) == 0 and process.stdout.read() == b'' and process.stderr.read() == b''
    finally:
        process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    assert end == {'version': 1, 'type': 'end', 'seq': 2, 'session_id': hello['session_id'],
                   'status': 0, 'bytes_sent': 0, 'cancelled': True}
    assert not (tmp_path / 'never-created').exists()


@pytest.mark.parametrize('env,code', [
    ({}, 'owner_ssh_source_refused'),
    ({'SSH_CONNECTION': '192.0.2.1 4242 100.64.0.7 22'}, 'owner_ssh_source_refused'),
    ({'SSH_CONNECTION': '100.64.0.42 4242 100.64.0.7 22', 'BOT_NAME': 'generated'}, 'operator_required')])
def test_cli_refuses_missing_source_or_generated_context_before_hello(tmp_path, env, code):
    process = api_process(tmp_path, SSH_CLIENT='100.64.0.42 4242 22', **env)
    try:
        output, errors = process.communicate(b'', 15)
    finally:
        process.kill()
        process.wait()
    frames = [json.loads(line) for line in output.splitlines()]
    assert process.returncode == 4 and errors == b'' and len(frames) == 1
    assert (frames[0]['type'], frames[0]['code'], frames[0]['retry']) == ('error', code, False)
    assert not (tmp_path / 'never-created').exists()


def test_cli_api_route_requires_its_explicit_stdio_mode(tmp_path):
    from tests.conftest import constructed_env
    result = subprocess.run([sys.executable, '-m', 'claudlobby', '--root', str(tmp_path / 'never-created'),
        'host', 'owner', 'api'], input=b'', capture_output=True, timeout=15,
        env=constructed_env(SSH_CONNECTION='100.64.0.42 4242 100.64.0.7 22'))
    assert result.returncode == 2 and b'"hello"' not in result.stdout


# --- admission shared with the guide ----------------------------------------

def test_api_admission_binds_current_source_without_a_finish_tuple(darwin_fixed_profile, monkeypatch):
    from claudlobby.commands import owner_api_stdio, owner_stdio
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby.installation import Installation
    from claudlobby.plane.owner_ssh import SSHSessionSource
    paths, request, *_ = darwin_fixed_profile
    monkeypatch.setattr(owner_stdio, '_admit', lambda args, value: paths)
    monkeypatch.setenv('SSH_CONNECTION', '100.64.0.42 4242 100.64.0.7 22')
    source = SSHSessionSource.capture()
    args = SimpleNamespace(root=str(paths.root))
    api = {key: value for key, value in request.items() if key != 'finish_setup'}
    assert owner_api_stdio.admit(args, api, source)[0] is paths
    # After the original update the installed revision differs from the
    # preparation receipt. The API still requires the exact installed revision.
    updated = 'd' * 40
    paths.package.source_revision = updated
    monkeypatch.setattr(Installation, 'preflight', lambda self, **kwargs: updated)
    assert owner_api_stdio.admit(args, {**api, 'source_revision': updated}, source)[1].runtime is not None
    with pytest.raises(StdioRefused, match='installation_changed'):
        owner_api_stdio.admit(args, api, source)  # stale revision
    with pytest.raises(StdioRefused, match='installation_changed'):
        FixedOwnerProfile(paths, {**request, 'source_revision': updated})  # the guide rule is unchanged
    with pytest.raises(StdioRefused, match='owner_ssh_source_refused'):
        owner_api_stdio.admit(args, api, ('100.64.0.42', 4242))


def test_api_refuses_a_profile_without_the_managed_mac_runtime(fixed_profile, monkeypatch):
    from claudlobby.commands import owner_api_stdio, owner_stdio
    from claudlobby.commands.owner_stdio import StdioRefused
    from claudlobby.plane.owner_ssh import SSHSessionSource
    paths, request, *_ = fixed_profile
    monkeypatch.setattr(owner_stdio, '_admit', lambda args, value: paths)
    monkeypatch.setenv('SSH_CONNECTION', '100.64.0.42 4242 100.64.0.7 22')
    with pytest.raises(StdioRefused, match='unsupported_ssh_profile'):
        owner_api_stdio.admit(SimpleNamespace(root=str(paths.root)), request, SSHSessionSource.capture())


def test_policy_app_mode_follows_existing_authority_not_the_request(monkeypatch, tmp_path):
    from claudlobby.commands import owner_api_stdio
    from claudlobby.plane import owner_access, owner_browser, owner_source
    built = []
    owner = SimpleNamespace(active=False)
    monkeypatch.setattr(owner_access, 'OwnerAccess', lambda root: SimpleNamespace(current_grant=lambda: owner))
    monkeypatch.setattr(owner_source, 'inspect_owner_setup', lambda root: None)
    def create(root, **options):
        built.append(options)
        if 'workspace_bootstrap' in options and options['package'] == 'unprepared':
            raise owner_access.AccessUnavailable()
        return 'app'
    monkeypatch.setattr(owner_browser, 'create_owner_browser_app', create)
    paths = SimpleNamespace(root=tmp_path, package='package')
    verifier = object()
    assert owner_api_stdio.owner_app(paths, verifier, ORIGIN) == 'app'
    assert built[-1] == {'expected_origin': ORIGIN, 'verify_principal': verifier, 'package': 'package',
                         'pairing_origin': 'https://plane.claudfather.ai'}
    owner.active = True
    owner_api_stdio.owner_app(paths, verifier, ORIGIN)
    config = built[-1]['workspace_bootstrap']
    assert (config.issuer, config.operational_origin) == ('https://claudfather.ai', 'https://plane.claudfather.ai')
    assert 'pairing_origin' not in built[-1] and built[-1]['verify_principal'] is verifier
    paths.package = 'unprepared'
    owner_api_stdio.owner_app(paths, verifier, ORIGIN)
    assert set(built[-1]) == {'expected_origin', 'verify_principal', 'package'}


def test_server_destination_and_realm_drift_refuse(monkeypatch):
    from claudlobby.plane.owner_ssh import SSHSessionSource, SSHSessionVerifier
    from claudlobby.plane.owner_ingress import NativePrincipalResolver
    from claudlobby.plane.owner_access import AccessDenied
    monkeypatch.setenv('SSH_CONNECTION', '100.64.0.42 4242 100.64.0.7 22')
    resolver = NativePrincipalResolver(tailscale_binary=__import__('pathlib').Path('/usr/bin/true'))
    state = {'ControlURL': 'https://controlplane.tailscale.com', 'ips': ['100.64.0.7'], 'id': 'server'}
    async def command(*args):
        if args == ('debug', 'prefs'):
            return {'ControlURL': state['ControlURL']}
        if args == ('status', '--json'):
            return {'BackendState': 'Running', 'Self': {'ID': state['id'], 'PublicKey': 'key', 'UserID': 1,
                'DNSName': 'fixture.example.ts.net.', 'TailscaleIPs': state['ips'], 'Online': True}}
        return {'Node': {'User': 123, 'Addresses': ['100.64.0.42/32']}, 'UserProfile': {'ID': 123}}
    monkeypatch.setattr(resolver, '_command', command)
    verifier = SSHSessionVerifier(SSHSessionSource.capture(), resolver)
    async def run():
        assert await verifier.origin() == ORIGIN
        state['id'] = 'another-server'
        with pytest.raises(AccessDenied, match='owner_ssh_server_changed'):
            await verifier({})
        state['id'], state['ips'] = 'server', ['100.64.0.8']
        with pytest.raises(AccessDenied, match='owner_ssh_server_refused'):
            await verifier({})
        state['ips'], state['ControlURL'] = ['100.64.0.7'], 'https://headscale.example.invalid'
        with pytest.raises(AccessDenied, match='owner_identity_control_refused'):
            await verifier({})
    asyncio.run(run())


# --- fresh SSH-controlled setup: create-only private policy ------------------

def ssh_profile(darwin_fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands import host_owner_setup
    paths, request, home, *_ = darwin_fixed_profile
    profile = FixedOwnerProfile(paths, request)
    monkeypatch.setattr(host_owner_setup, '_native_directories', lambda _paths: (profile.units,))
    return profile, paths, home


def test_ssh_setup_proposes_and_creates_only_the_private_owner_policy(darwin_fixed_profile, monkeypatch):
    from claudlobby import config
    profile, paths, _home = ssh_profile(darwin_fixed_profile, monkeypatch)
    proposals = []
    def approve(action, scope):
        assert not profile.policy.exists(), 'nothing is written before approval'
        proposals.append((action, scope))
    profile.prepare_directories(SimpleNamespace(approve=approve), paths, ssh_policy=True)
    action, scope = proposals[0]
    content = profile.policy.read_bytes()
    assert action == 'prepare_directories' and len(proposals) == 1
    assert scope['host_policy'] == {'path': str(profile.policy), 'job': 'plane-owner', 'enroll': False,
                                    'sha256': hashlib.sha256(content).hexdigest()}
    assert 'SSH connection' in scope['effects'][-1] and str(profile.policy) not in scope['effects'][-1]
    assert profile.policy.stat().st_mode & 0o777 == 0o600 and profile.policy.stat().st_nlink == 1
    monkeypatch.setenv(config.HOST_OVERRIDE_ENV, str(profile.policy))
    jobs = config.load_host_jobs()
    monkeypatch.delenv(config.HOST_OVERRIDE_ENV)
    monkeypatch.setattr(config, 'host_override_path', lambda: profile.policy.with_name('absent.yaml'))
    packaged = config.load_host_jobs()
    assert jobs['plane-owner']['enroll'] is False
    assert {name: value for name, value in jobs.items() if name != 'plane-owner'} == {
        name: value for name, value in packaged.items() if name != 'plane-owner'}, 'ordinary jobs stay canonical'
    assert {k: v for k, v in jobs['plane-owner'].items() if k != 'enroll'} == {
        k: v for k, v in packaged['plane-owner'].items() if k != 'enroll'}


def test_existing_private_policy_is_never_overwritten_or_repaired(darwin_fixed_profile, monkeypatch):
    profile, paths, _home = ssh_profile(darwin_fixed_profile, monkeypatch)
    profile.policy.parent.mkdir(mode=0o700)
    existing = b'host:\n  jobs:\n    plane-owner:\n      enroll: true\n'
    profile.policy.write_bytes(existing)
    profile.policy.chmod(0o600)
    before = profile.policy.stat()
    proposals = []
    profile.prepare_directories(SimpleNamespace(approve=lambda action, scope: proposals.append(scope)),
                                paths, ssh_policy=True)
    after = profile.policy.stat()
    assert profile.policy.read_bytes() == existing and (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert proposals[0]['host_policy'] is None and not any('SSH connection' in line for line in proposals[0]['effects'])


def test_policy_node_appearing_during_approval_refuses_without_replacement(darwin_fixed_profile, monkeypatch):
    from claudlobby.commands.owner_stdio import StdioRefused
    profile, paths, _home = ssh_profile(darwin_fixed_profile, monkeypatch)
    for path in (profile.project.parent, profile.project, profile.account, profile.policy.parent, profile.provider.parent,
                 profile.units.parent.parent, profile.units.parent, profile.units):
        path.mkdir(mode=0o700, exist_ok=True)
    (profile.data / '.env').touch(mode=0o600)
    def approve(action, scope):
        profile.policy.write_bytes(b'host: {}\n')
    with pytest.raises(StdioRefused, match='prerequisite_changed'):
        profile.prepare_directories(SimpleNamespace(approve=approve), paths, ssh_policy=True)
    assert profile.policy.read_bytes() == b'host: {}\n'


def test_linux_and_https_preparation_scope_is_unchanged(fixed_profile, monkeypatch):
    from claudlobby.commands.owner_setup_consent import FixedOwnerProfile
    from claudlobby.commands import host_owner_setup
    paths, request, *_ = fixed_profile
    profile = FixedOwnerProfile(paths, request)
    monkeypatch.setattr(host_owner_setup, '_native_directories', lambda _paths: (profile.units,))
    proposals = []
    profile.prepare_directories(SimpleNamespace(approve=lambda action, scope: proposals.append(scope)), paths)
    assert set(proposals[0]) == {'directories', 'empty_private_environment', 'provider_directory', 'effects'}
    assert len(proposals[0]['effects']) == 1 and not profile.policy.exists()


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
        installation = SimpleNamespace(cli=tmp_path / 'install/bin/claudlobby', directory=tmp_path / 'install')
        self.paths = SimpleNamespace(root=tmp_path / 'data', package=SimpleNamespace(installation=installation, source_revision='a' * 40))
        self.grant = SimpleNamespace(generation='11111111-1111-4111-8111-111111111111', source_revision='a' * 40,
                                     managed_owner=None)
        world = self
        class Setup:
            def __init__(self, store): pass
            def retained_state(self):
                world.polls += 1
                launched = world.polls > world.launch_after
                return SimpleNamespace(state='absent', launch_id='req' if launched else None,
                    launch_plan='plan' if launched else None, grant=world.grant, effective_active=True)
            def local(self): return world.grant
            def operation(self, grant): return {'request_id': 'req', 'config_fingerprint': 'plan'}
        monkeypatch.setattr(host_owner_setup, 'OwnerSetup', Setup)
        monkeypatch.setattr(owner_setup, 'managed_setup', lambda root: self.paths.package)
        monkeypatch.setattr(git_update, 'read_update', lambda installation: None)
        monkeypatch.setattr(owner_source, 'fresh_owner_setup', lambda *a, **k: None)
        monkeypatch.setattr(owner_service, 'service_command', lambda *a: pytest.fail('built an owner serve command'))
        monkeypatch.setattr(config, 'load_host_jobs', lambda: {'plane-owner': {'enroll': enroll}})
        monkeypatch.setattr(host_owner_setup, '_serving', lambda *a, **k: pytest.fail('started a listener'))
        monkeypatch.setattr(host_owner_setup, '_ssh_origin', lambda source: self.calls.append(('origin', source)) or ORIGIN)
        def configuration(args, paths, terminal, cli):
            self.args = args
            return ({'origin': args.origin, 'socket': str(args.socket), 'workspace_issuer': args.workspace_issuer,
                     'workspace_origin': args.workspace_origin},
                    SimpleNamespace(managed_owner=None, install_directory=tmp_path / 'units'))
        monkeypatch.setattr(host_owner_setup, '_configuration', configuration)
        for name in ('_initialize_first_owner', '_confirm_owner', '_describe', '_initialize_connected',
                     '_confirm_workspace', '_allow_setup'):
            monkeypatch.setattr(host_owner, name, lambda *a, _name=name, **k: self.calls.append(_name))
        monkeypatch.setattr(host_owner, '_active_owner', lambda store: SimpleNamespace(active=True))
        self.owner = None
        self.store = SimpleNamespace(path=tmp_path / 'absent-store', current_grant=lambda: self.owner,
            connected=lambda: SimpleNamespace(preparation_state=lambda: 'absent'),
            workspace=lambda: SimpleNamespace(status=self.workspace))
        self.bound = False
        self.profile = SimpleNamespace(runtime=object(), project=tmp_path / 'project', account=tmp_path / 'account',
            units=tmp_path / 'units', provider=tmp_path / 'provider',
            prepare_directories=lambda consent, paths, **kw: self.calls.append(('directories', kw)),
            prepare_provider=lambda consent, paths: self.calls.append('provider'),
            origin=lambda: pytest.fail('used the HTTPS ingress profile'))
        self.consent = SimpleNamespace(state=lambda state, **data: self.states.append((state, data)),
                                       check_cancel=lambda: None)

    def workspace(self):
        if '_confirm_workspace' in self.calls:
            return 'active', SimpleNamespace(issuer='https://claudfather.ai', operational_origin='https://plane.claudfather.ai')
        return 'absent', None

    def run(self, source='captured-source'):
        from claudlobby.commands import host_owner_setup
        args = SimpleNamespace(finish_setup=None, template='revenue-web-app')
        return host_owner_setup.run(args, self.paths, self.store, None, consent=self.consent,
                                    profile=self.profile, ssh=source)


def test_ssh_guide_pairs_binds_and_hands_off_launch_without_a_listener_or_service(monkeypatch, tmp_path):
    from claudlobby.plane.owner_ssh import MAC_TAILSCALE
    import dataclasses
    world = GuideWorld(monkeypatch, tmp_path)
    monkeypatch.setattr(dataclasses, 'asdict', lambda value: {})
    monkeypatch.setattr('claudlobby.commands.host_owner_setup.asdict', lambda value: {})
    result = world.run()
    assert world.calls[:3] == [('directories', {'ssh_policy': True}), 'provider', ('origin', 'captured-source')]
    assert world.calls[3:] == ['_initialize_first_owner', '_confirm_owner', '_describe', '_initialize_connected',
                               '_confirm_workspace', '_allow_setup']
    assert world.args.managed_owner is False and world.args.tailscale == MAC_TAILSCALE
    assert world.args.origin == ORIGIN, 'the logical key comes from native Self, not a frame'
    assert [state for state, _ in world.states] == ['pairing_ready', 'workspace_ready', 'preview_launch_ready']
    assert all(data['transport'] == 'mac_openssh' and data['origin'] == ORIGIN for _, data in world.states)
    assert result.data == {'state': 'launch_handoff', 'request_id': 'req', 'config_fingerprint': 'plan',
                           'generation': world.grant.generation, 'source_revision': 'a' * 40, 'transport': 'mac_openssh'}
    assert 'No owner service is enrolled' in result.lines[-1]


def test_ssh_guide_never_repairs_an_existing_policy_that_enrolls_the_owner_service(monkeypatch, tmp_path):
    from claudlobby.command_result import CommandFailure
    world = GuideWorld(monkeypatch, tmp_path, enroll=True)
    with pytest.raises(CommandFailure, match='explicit --managed-owner handover'):
        world.run()
    assert '_initialize_first_owner' not in world.calls and world.states == []


def test_ssh_guide_stops_when_the_setup_approval_lapses_instead_of_waiting_forever(monkeypatch, tmp_path):
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import host_owner_setup
    world = GuideWorld(monkeypatch, tmp_path)
    monkeypatch.setattr('claudlobby.commands.host_owner_setup.asdict', lambda value: {})
    world.launch_after = 10**6
    original = host_owner_setup.OwnerSetup.retained_state
    def lapsed(self):
        value = original(self)
        value.effective_active = world.polls < 4
        return value
    monkeypatch.setattr(host_owner_setup.OwnerSetup, 'retained_state', lapsed)
    with pytest.raises(CommandFailure, match='expired or changed'):
        world.run()


def test_ssh_guide_requires_the_managed_mac_profile_and_the_typed_channel(monkeypatch, tmp_path):
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import host_owner_setup
    world = GuideWorld(monkeypatch, tmp_path)
    world.profile.runtime = None
    with pytest.raises(CommandFailure, match='managed Mac runtime'):
        world.run()
    assert world.calls == []
    with pytest.raises(CommandFailure, match='typed guide only'):
        host_owner_setup.run(SimpleNamespace(finish_setup=None, template='revenue-web-app'), world.paths,
                             world.store, None, ssh='captured-source')


def test_mac_guide_captures_the_server_source_before_the_profile_environment(darwin_fixed_profile, monkeypatch):
    """dispatch refuses a missing tuple, and hands the captured one to run()."""
    from claudlobby.commands import owner_stdio, host_owner_setup
    from claudlobby.command_result import CommandOutput
    paths, request, *_ = darwin_fixed_profile
    seen = []
    def run(args, admitted, store, terminal, **options):
        seen.append((options.get('ssh'), os.environ.get('SSH_CONNECTION')))
        return CommandOutput({'state': 'fixture'})
    monkeypatch.setattr(host_owner_setup, 'run', run)
    monkeypatch.setattr(owner_stdio, '_selectors', lambda args: None)
    monkeypatch.setattr(owner_stdio, '_admit', lambda args, value: paths)
    for raw, expected in (('100.64.0.42 4242 100.64.0.7 22', 'result'), (None, 'error')):
        incoming, client = os.pipe()
        observer, outgoing = os.pipe()
        class Channel(owner_stdio.OwnerStdio):
            def __init__(self, _in, _out): super().__init__(incoming, outgoing)
            def start(self): return dict(request)
        monkeypatch.setattr(owner_stdio, 'OwnerStdio', Channel)
        if raw is None: monkeypatch.delenv('SSH_CONNECTION', raising=False)
        else: monkeypatch.setenv('SSH_CONNECTION', raw)
        try:
            code = owner_stdio.dispatch(SimpleNamespace(stdio_status=False, root=str(paths.root)))
            os.close(outgoing); outgoing = None
            frames = [json.loads(line) for line in os.read(observer, 65536).splitlines()]
        finally:
            for fd in (incoming, client, observer, outgoing):
                if fd is not None: os.close(fd)
        assert frames[-1]['type'] == expected
        if expected == 'error':
            assert code == 4 and frames[-1]['code'] == 'owner_ssh_source_refused'
    assert len(seen) == 1 and str(seen[0][0].client_ip) == '100.64.0.42'
    assert seen[0][1] is None, 'the tuple was captured before the fixed environment replaced it'
