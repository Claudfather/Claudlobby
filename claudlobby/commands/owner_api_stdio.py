"""One finite typed owner operation over an admitted ordinary SSH exec channel.

There is no HTTP/command proxy on the wire. Policy remains the existing ASGI
app; principal and invariant authority originate outside request frames.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import re
import select
import time
from urllib.parse import urlencode
from uuid import uuid4

from .owner_stdio import StdioRefused, _unique, _wire

_BASE = {'version', 'type', 'seq', 'session_id'}
_START = {'profile', 'expected_uid', 'source_revision', 'binding_sha256',
          'operation', 'input', 'token', 'last_event_id'}
_PUBLIC = {'status', 'pair', 'workspace-challenge', 'workspace-stage', 'session-challenge', 'session-issue'}
_FIELDS = {
    'status': (), 'pair': (), 'workspace-challenge': (), 'workspace-stage': ('nonce', 'assertion'),
    'session-challenge': (), 'session-issue': ('nonce', 'assertion'),
    'session-status': (), 'session-renew': (), 'session-logout': (), 'setup-context': (),
    'setup-signin-begin': ('generation', 'attempt_id'), 'setup-signin-status': ('generation', 'attempt_id'),
    'setup-signin-cancel': ('generation', 'attempt_id'), 'setup-signin-code': ('generation', 'attempt_id', 'code'),
    'setup-preview': ('generation', 'goal'),
    'setup-launch': ('generation', 'request_id', 'config_fingerprint', 'source_revision'),
    'setup-receipt': ('generation', 'request_id', 'config_fingerprint', 'source_revision'),
    'action-context': ('room',), 'action-send': ('request_id', 'kind', 'scope', 'target', 'submitted_at', 'body'),
    'action-receipt': ('request_id', 'kind', 'scope', 'target', 'submitted_at'),
    'read-summary': (), 'read-overview': (), 'read-fleets': (), 'read-trust': (),
    'read-channel': ('fleet', 'limit'), 'read-tasks': ('fleet', 'cursor'),
    'read-task': ('task_id', 'fleet'), 'read-identities': ('fleet',), 'read-inventory': ('fleet',),
    'read-org': ('fleet',), 'read-utilization': ('fleet',), 'read-presence': ('fleet',),
    'read-equipment': ('alias',), 'read-search': ('q', 'fleet', 'limit'),
    'read-grid': ('fleet', 'focus'), 'read-stream': ('cursor', 'once'),
}
_SECRET = re.compile(r'[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]')
_UUID = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[1-8][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}')


def operation_request(operation, payload, token, last_event_id):
    """Compile only the existing finite operation schema into an internal route."""
    if (not isinstance(operation, str) or operation not in _FIELDS or type(payload) is not dict
            or set(payload) - set(_FIELDS[operation])
            or not operation.startswith('read-') and set(payload) != set(_FIELDS[operation])
            or operation == 'read-task' and 'task_id' not in payload):
        raise ValueError('invalid owner operation')
    if (operation in _PUBLIC and token is not None or operation not in _PUBLIC
            and (not isinstance(token, str) or _SECRET.fullmatch(token) is None)):
        raise ValueError('invalid owner token')
    if last_event_id is not None and (operation != 'read-stream' or not isinstance(last_event_id, str)
            or re.fullmatch(r'0|[1-9][0-9]{0,15}', last_event_id) is None):
        raise ValueError('invalid owner stream cursor')
    for key, value in payload.items():
        if operation.startswith('action-'):
            continue  # Canonical action DTO validation remains in its policy handler.
        if not isinstance(value, str) or '\0' in value:
            raise ValueError('invalid owner input')
        if len(value.encode('utf-8')) > (8192 if operation.startswith('read-') else 32768):
            raise ValueError('owner input too large')
        if key in {'generation', 'attempt_id', 'request_id'} and _UUID.fullmatch(value) is None:
            raise ValueError('invalid owner identifier')
        if key == 'nonce' and _SECRET.fullmatch(value) is None:
            raise ValueError('invalid owner nonce')
        if key == 'assertion' and re.fullmatch(r'[\x20-\x7e]{1,8192}', value) is None:
            raise ValueError('invalid owner assertion')
        if key == 'config_fingerprint' and re.fullmatch('[a-f0-9]{64}', value) is None:
            raise ValueError('invalid owner fingerprint')
        if key == 'source_revision' and re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', value) is None:
            raise ValueError('invalid owner revision')
        if key == 'task_id' and re.fullmatch('wi_[a-f0-9]{32}', value) is None:
            raise ValueError('invalid owner task')
        if key == 'goal' and len(value) > 2000 or key == 'code' and not 1 <= len(value) <= 2305:
            raise ValueError('invalid owner text')
        if key == 'limit' or key == 'cursor' and operation == 'read-stream':
            if re.fullmatch(r'0|[1-9][0-9]{0,15}', value) is None:
                raise ValueError('invalid owner decimal')
        if key == 'once' and value not in {'0', '1'}:
            raise ValueError('invalid stream mode')
    if operation.startswith('action-'):
        if operation == 'action-context':
            if not isinstance(payload['room'], str) or not 1 <= len(payload['room']) <= 240:
                raise ValueError('invalid room')
        elif (payload['kind'] != 'message' or _UUID.fullmatch(str(payload['request_id'])) is None
                or type(payload['scope']) is not dict or set(payload['scope']) != {'workspace', 'host', 'fleet', 'viewer'}
                or type(payload['target']) is not dict or set(payload['target']) != {'recipient', 'task_id'}
                or payload['target']['task_id'] is not None
                or any(not isinstance(x, str) or not 1 <= len(x) <= 240
                       for x in [*payload['scope'].values(), payload['target']['recipient'], payload['submitted_at']])
                or operation == 'action-send' and (not isinstance(payload['body'], str)
                    or not payload['body'].strip() or not 1 <= len(payload['body']) <= 2000)):
            raise ValueError('invalid ordinary action')
    headers = [] if token is None else [(b'authorization', b'Bearer ' + token.encode('ascii'))]
    if last_event_id is not None:
        headers.append((b'last-event-id', last_event_id.encode('ascii')))
    get = operation.startswith('read-') or operation in {'status', 'session-status'}
    if operation == 'status':
        path = '/api/owner/status'
    elif operation == 'pair':
        path = '/api/owner/bootstrap/pair'
    elif operation.startswith('workspace-'):
        path = '/api/owner/workspace/' + operation[10:]
    elif operation.startswith('session-'):
        path = '/api/owner/connected/' + operation[8:]
    elif operation.startswith(('setup-', 'action-')):
        family, suffix = operation.split('-', 1)
        path = '/api/owner/connected/' + ('actions' if family == 'action' else 'setup') + '/' + suffix
    else:
        suffix = operation[5:]
        path = '/api/owner/connected/read/' + ('tasks/' + payload['task_id'] if suffix == 'task' else suffix)
    query = urlencode({k: v for k, v in payload.items() if k != 'task_id'}).encode() if operation.startswith('read-') else b''
    body = b'' if get else _wire(payload).rstrip(b'\n')
    if len(body) > 32768:
        raise ValueError('owner body too large')
    if not get:
        headers += [(b'content-type', b'application/json'), (b'x-claudlobby-owner', b'1')]
    return ('GET' if get else 'POST'), path, query, body, headers


class APIChannel:
    """Independent finite framing; stream limits are not the guide's128frames."""
    def __init__(self, incoming, outgoing):
        self.incoming, self.outgoing = incoming, outgoing
        self.session_id = uuid4().hex
        self.input_seq = self.output_seq = self.input_bytes = 0
        self.buffer = bytearray()
        self.partial_since = None

    def emit(self, kind, **values):
        self.output_seq += 1
        raw = _wire({**values, 'version': 1, 'type': kind, 'seq': self.output_seq, 'session_id': self.session_id})
        if len(raw) > 32768 or self.output_seq > 4096:
            raise StdioRefused('output_limit')
        deadline = time.monotonic() + 15
        while raw:
            if not select.select([], [self.outgoing], [], max(0, deadline - time.monotonic()))[1]:
                raise StdioRefused('output_unavailable')
            count = os.write(self.outgoing, raw[:4096])
            if not count:
                raise StdioRefused('output_unavailable')
            raw = raw[count:]

    def read(self, allowed, timeout=.25):
        if b'\n' not in self.buffer:
            if not select.select([self.incoming], [], [], timeout)[0]:
                if self.partial_since is not None and time.monotonic() - self.partial_since >= 15:
                    raise StdioRefused('expired')
                return None
            part = os.read(self.incoming, 4096)
            if not part:
                raise StdioRefused('disconnected')
            self.buffer.extend(part)
            if self.partial_since is None:
                self.partial_since = time.monotonic()
        if len(self.buffer) > 65536:
            raise StdioRefused('invalid_request')
        if b'\n' not in self.buffer:
            return None
        raw, _, rest = self.buffer.partition(b'\n')
        self.buffer = bytearray(rest)
        self.partial_since = time.monotonic() if rest else None
        self.input_bytes += len(raw) + 1
        try:
            value = json.loads(raw, object_pairs_hook=_unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            kind = value.get('type')
            extra = _START if kind == 'start' else {'chunk_id'} if kind == 'ack' else set()
            if (kind not in allowed or set(value) != _BASE | extra or _wire(value) != raw + b'\n'
                    or value['session_id'] != self.session_id or type(value['version']) is not int or value['version'] != 1
                    or type(value['seq']) is not int or value['seq'] != self.input_seq + 1
                    or value['seq'] > 4096 or self.input_bytes > 262144):
                raise ValueError
            self.input_seq = value['seq']
            return value
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise StdioRefused('invalid_request') from None

    async def start(self):
        self.emit('hello', profile='owner-api-v1')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = await asyncio.to_thread(self.read, {'start', 'cancel'})
            if value is None:
                continue
            if value['type'] == 'cancel':
                raise StdioRefused('cancelled')
            if (value['profile'] != 'owner-api-v1' or type(value['expected_uid']) is not int
                    or not 0 < value['expected_uid'] < 2**32 - 1
                    or not isinstance(value['source_revision'], str) or re.fullmatch('[a-f0-9]{40}', value['source_revision']) is None
                    or not isinstance(value['binding_sha256'], str) or re.fullmatch('[a-f0-9]{64}', value['binding_sha256']) is None):
                raise StdioRefused('invalid_request')
            operation_request(value['operation'], value['input'], value['token'], value['last_event_id'])
            return value
        raise StdioRefused('expired')


async def shutdown_app(app):
    from ..plane.view import begin_shutdown
    begin_shutdown(app)
    method = getattr(app, 'shutdown', None)
    if method is not None:
        await method()
    for name in ('pairing_bootstrap', 'connected_setup', 'connected_actions', 'connected_reads', 'workspace_bootstrap'):
        component = getattr(app, name, None)
        if component is not None:
            await component.shutdown()
    tasks = list(getattr(app, '_inflight', ()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_operation(channel, app, operation, payload, token, last_event_id, *, origin):
    method, path, query, body, headers = operation_request(operation, payload, token, last_event_id)
    authority = origin.removeprefix('https://')
    headers = [(b'host', authority.encode('ascii')), *headers]
    if operation != 'status':
        headers.append((b'origin', b'https://plane.claudfather.ai'))
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
        'scheme': 'https', 'path': path, 'raw_path': path.encode('ascii'), 'query_string': query,
        'headers': headers, 'client': ('ssh-session', 0), 'server': ('ssh-carrier', 0)}
    ack = asyncio.Queue(maxsize=1)
    cancelled, stop = asyncio.Event(), asyncio.Event()
    control_error = []
    status = sent = chunk_id = 0
    received = False
    async def controls():
        try:
            while not stop.is_set():
                frame = await asyncio.to_thread(channel.read, {'ack', 'cancel'})
                if frame is None:
                    continue
                if frame['type'] == 'cancel':
                    cancelled.set()
                    return
                if type(frame['chunk_id']) is not int or ack.full():
                    raise StdioRefused('invalid_request')
                ack.put_nowait(frame['chunk_id'])
        except (StdioRefused, OSError) as exc:
            control_error.append(exc)
            cancelled.set()
    async def receive():
        nonlocal received
        if not received:
            received = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        await cancelled.wait()
        return {'type': 'http.disconnect'}
    async def send(message):
        nonlocal status, sent, chunk_id
        if cancelled.is_set():
            raise asyncio.CancelledError
        if message['type'] == 'http.response.start':
            if status:
                raise StdioRefused('invalid_response')
            status = message['status']
            selected = {}
            for key, value in message.get('headers', ()):
                name = key.decode('ascii').lower()
                if name in {'content-type', 'cache-control', 'retry-after'}:
                    if name in selected or len(value) > 1024:
                        raise StdioRefused('invalid_response')
                    selected[name] = value.decode('ascii')
            channel.emit('headers', status=status, headers=selected)
        elif message['type'] == 'http.response.body':
            if not status:
                raise StdioRefused('invalid_response')
            value = message.get('body', b'')
            for offset in range(0, len(value), 16384):
                part = value[offset:offset + 16384]
                sent += len(part)
                if sent > (8388608 if operation == 'read-stream' else 2097152):
                    raise StdioRefused('output_limit')
                chunk_id += 1
                channel.emit('chunk', chunk_id=chunk_id, data=base64.b64encode(part).decode('ascii'))
                if await asyncio.wait_for(ack.get(), 15) != chunk_id:
                    raise StdioRefused('invalid_request')
        else:
            raise StdioRefused('invalid_response')
    reader = asyncio.create_task(controls())
    request = asyncio.create_task(app(scope, receive, send))
    cancellation = asyncio.create_task(cancelled.wait())
    failed = None
    try:
        done, _ = await asyncio.wait({request, cancellation},
            timeout=300 if operation == 'read-stream' else 45, return_when=asyncio.FIRST_COMPLETED)
        if request in done:
            await request
        elif cancellation in done:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        else:
            raise StdioRefused('expired')
    except BaseException as exc:
        failed = exc
    finally:
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await shutdown_app(app)
        stop.set()
        await reader  # its bounded polling read joins before end/process exit
        cancellation.cancel()
        await asyncio.gather(cancellation, return_exceptions=True)
    if control_error:
        raise control_error[0]
    if failed is not None:
        raise failed
    channel.emit('end', status=status, bytes_sent=sent, cancelled=cancelled.is_set())


async def _dispatch(args, channel):
    from .owner_stdio import _admit, _selectors
    from .owner_setup_consent import FixedOwnerProfile
    from ..plane.owner_ssh import SSHSessionSource, SSHSessionVerifier
    from ..plane.owner_ingress import NativePrincipalResolver
    from ..plane.owner_access import OwnerAccess
    from ..plane.owner_source import inspect_owner_setup
    from ..plane.owner_browser import create_owner_browser_app
    from ..plane.owner_workspace_http import WorkspaceBootstrapConfig
    _selectors(args)
    source = SSHSessionSource.capture()  # before any profile/environment scrub
    request = await channel.start()
    request['finish_setup'] = None  # internal existing profile consistency field, never a wire escape
    paths = _admit(args, request)
    profile = FixedOwnerProfile(paths, request)
    if profile.runtime is None:
        raise StdioRefused('unsupported_ssh_profile')
    with profile.environment():
        resolver = NativePrincipalResolver(tailscale_binary=Path('/Applications/Tailscale.app/Contents/MacOS/Tailscale'))
        verifier = SSHSessionVerifier(source, resolver)
        origin = await verifier.origin()
        await verifier({})  # even the public status op must prove the captured native principal
        access = OwnerAccess(paths.root)
        inspect_owner_setup(paths.root)
        owner = access.current_grant()
        options = ({'workspace_bootstrap': WorkspaceBootstrapConfig('https://claudfather.ai', 'https://plane.claudfather.ai')}
                   if owner is not None and owner.active else {'pairing_origin': 'https://plane.claudfather.ai'})
        app = create_owner_browser_app(paths.root, expected_origin=origin, verify_principal=verifier,
                                      package=paths.package, **options)
        # Lifespan owns existing finite-read/stream state. It is always closed
        # before this stable-CLI process can release its runtime-use reader.
        incoming, started, stopped = asyncio.Queue(), asyncio.Event(), asyncio.Event()
        async def lifecycle_send(message):
            if message['type'] == 'lifespan.startup.complete': started.set()
            elif message['type'] == 'lifespan.shutdown.complete': stopped.set()
            else: raise StdioRefused('unavailable')
        lifecycle = asyncio.create_task(app({'type': 'lifespan', 'asgi': {'version': '3.0'}}, incoming.get, lifecycle_send))
        try:
            await incoming.put({'type': 'lifespan.startup'})
            await asyncio.wait_for(started.wait(), 10)
            await run_operation(channel, app, request['operation'], request['input'], request['token'],
                                request['last_event_id'], origin=origin)
        finally:
            await incoming.put({'type': 'lifespan.shutdown'})
            try:
                await asyncio.wait_for(stopped.wait(), 10)
            finally:
                if not lifecycle.done(): lifecycle.cancel()
                await asyncio.gather(lifecycle, return_exceptions=True)


def dispatch(args):
    channel = APIChannel(0, 1)
    try:
        asyncio.run(_dispatch(args, channel))
        return 0
    except Exception as exc:
        from ..plane.owner_access import AccessDenied
        code = exc.code if isinstance(exc, (StdioRefused, AccessDenied)) else 'unavailable'
        try: channel.emit('error', code=code, retry=False)
        except (OSError, StdioRefused): pass
        return 4
