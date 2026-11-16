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
    resolver = NativePrincipalResolver(tailscale_binary=__import__('pathlib').Path('/bin/true'))
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
