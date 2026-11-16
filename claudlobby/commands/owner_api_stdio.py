"""One finite typed owner operation over an admitted ordinary SSH exec channel.

There is no HTTP/command proxy on the wire. Policy remains the existing ASGI
app; principal and invariant authority originate outside request frames.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import select
import time
from urllib.parse import urlencode
from uuid import uuid4

from .owner_stdio import StdioRefused, _unique, _wire

WEBSITE_ISSUER = 'https://claudfather.ai'
OPERATIONAL_ORIGIN = 'https://plane.claudfather.ai'

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
# The provider sign-in attempt lives in one process: its worker thread, PTY child,
# URL and code hand-off are ProviderSignin instance state, and the retained row
# admits exactly one original attempt. A one-operation exec would start that
# attempt and cancel it in its own mandatory drain, or deliver a code to a
# process that never owned the child. These stay refused here until a reviewed
# owner for that attempt exists; signin-status remains a read-only observation.
_PROCESS_BOUND = frozenset({'setup-signin-begin', 'setup-signin-code', 'setup-signin-cancel'})
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
        self.interrupted = False

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
        if self.interrupted:
            raise StdioRefused('interrupted')
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

    def interrupt(self):
        """A stop signal joins the same owned drain as a lost channel."""
        self.interrupted = True

    def pending_cancel(self):
        """One non-blocking look for a cancel that arrived during admission."""
        value = self.read({'cancel'}, 0)
        return value is not None

    async def start(self):
        """Return the admitted start frame, or None for a normal early cancel."""
        self.emit('hello', profile='owner-api-v1')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = await asyncio.to_thread(self.read, {'start', 'cancel'})
            if value is None:
                continue
            if value['type'] == 'cancel':
                return None
            if (value['profile'] != 'owner-api-v1' or type(value['expected_uid']) is not int
                    or not 0 < value['expected_uid'] < 2**32 - 1
                    or not isinstance(value['source_revision'], str) or re.fullmatch('[a-f0-9]{40}', value['source_revision']) is None
                    or not isinstance(value['binding_sha256'], str) or re.fullmatch('[a-f0-9]{64}', value['binding_sha256']) is None):
                raise StdioRefused('invalid_request')
            try:
                operation_request(value['operation'], value['input'], value['token'], value['last_event_id'])
            except ValueError:
                raise StdioRefused('invalid_request') from None
            if value['operation'] in _PROCESS_BOUND:
                # Refused before any engine entry: the one retained attempt is untouched.
                raise StdioRefused('operation_requires_persistent_owner')
            return value
        raise StdioRefused('expired')

    def end_cancelled(self):
        """The published normal cancellation before headers: healthy release."""
        self.emit('end', status=0, bytes_sent=0, cancelled=True)


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


async def run_operation(channel, app, operation, payload, token, last_event_id, *, origin, emit_end=True):
    """Carry one compiled operation and return its (status, bytes, cancelled) outcome.

    The end frame follows the complete owned component drain. ``serve`` passes
    ``emit_end=False`` and emits it only after the application lifespan has also
    shut down successfully.
    """
    method, path, query, body, headers = operation_request(operation, payload, token, last_event_id)
    authority = origin.removeprefix('https://')
    headers = [(b'host', authority.encode('ascii')), *headers]
    if operation != 'status':
        headers.append((b'origin', OPERATIONAL_ORIGIN.encode('ascii')))
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
        'scheme': 'https', 'path': path, 'raw_path': path.encode('ascii'), 'query_string': query,
        'headers': headers, 'client': ('ssh-session', 0), 'server': ('ssh-carrier', 0)}
    ack = asyncio.Queue(maxsize=1)
    cancelled, stop = asyncio.Event(), asyncio.Event()
    # Explicit caller cancellation and a complete final body are tracked apart
    # from task cancellation/return: a handler may unwind normally in either
    # case, or abandon a started response (canonical finite reads do).
    caller_cancelled, complete = asyncio.Event(), asyncio.Event()
    # Carrier faults are kept apart from policy outcomes: a handler that turns
    # an exception into a response cannot hide a broken or expired channel.
    carrier_error = []
    status = sent = chunk_id = 0
    received = False

    def carrier(code):
        error = StdioRefused(code)
        if not carrier_error:
            carrier_error.append(error)
        cancelled.set()
        return error

    async def controls():
        try:
            while not stop.is_set():
                frame = await asyncio.to_thread(channel.read, {'ack', 'cancel'})
                if frame is None:
                    continue
                if frame['type'] == 'cancel':
                    caller_cancelled.set()
                    cancelled.set()
                    return
                if type(frame['chunk_id']) is not int or ack.full():
                    raise StdioRefused('invalid_request')
                ack.put_nowait(frame['chunk_id'])
        except StdioRefused as exc:
            carrier(exc.code)
        except OSError:
            carrier('disconnected')

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
        try:
            if message['type'] == 'http.response.start':
                if status or type(message.get('status')) is not int or not 100 <= message['status'] <= 599:
                    raise carrier('invalid_response')
                selected = {}
                for key, value in message.get('headers', ()):
                    name = key.decode('ascii').lower()
                    if name in {'content-type', 'cache-control', 'retry-after'}:
                        if name in selected or len(value) > 1024:
                            raise carrier('invalid_response')
                        selected[name] = value.decode('ascii')
                channel.emit('headers', status=message['status'], headers=selected)
                status = message['status']
            elif message['type'] == 'http.response.body':
                if not status:
                    raise carrier('invalid_response')
                value = message.get('body', b'')
                for offset in range(0, len(value), 16384):
                    part = value[offset:offset + 16384]
                    if sent + len(part) > (8388608 if operation == 'read-stream' else 2097152):
                        raise carrier('output_limit')
                    sent += len(part)
                    chunk_id += 1
                    channel.emit('chunk', chunk_id=chunk_id, data=base64.b64encode(part).decode('ascii'))
                    try:
                        acknowledged = await asyncio.wait_for(ack.get(), 15)
                    except asyncio.TimeoutError:
                        raise carrier('expired') from None
                    if acknowledged != chunk_id:
                        raise carrier('invalid_request')
                if not message.get('more_body', False):
                    complete.set()  # only after every chunk of the final body was acknowledged
            else:
                raise carrier('invalid_response')
        except StdioRefused as exc:
            raise carrier(exc.code) from None
        except (OSError, UnicodeError):
            raise carrier('output_unavailable') from None

    reader = request = None
    failed = None
    try:
        # A cancel that arrived during admission never reaches a policy handler.
        # This single look completes before the control reader owns the channel.
        if not await asyncio.to_thread(_pending_cancel, channel, carrier):
            reader = asyncio.create_task(controls())
            request = asyncio.create_task(app(scope, receive, send))
            cancellation = asyncio.create_task(cancelled.wait())
            try:
                done, _ = await asyncio.wait({request, cancellation},
                    timeout=300 if operation == 'read-stream' else 45, return_when=asyncio.FIRST_COMPLETED)
                if request in done:
                    # Read the outcome without awaiting it: a handler unwound by
                    # the caller's own cancel is a normal end, not an interruption.
                    if not request.cancelled() and request.exception() is not None:
                        failed = request.exception()
                elif cancellation not in done:
                    carrier('expired')
            finally:
                cancellation.cancel()
                await asyncio.gather(cancellation, return_exceptions=True)
        else:
            if not carrier_error:
                caller_cancelled.set()
            cancelled.set()
    except asyncio.CancelledError:
        cancelled.set()
        raise
    except BaseException as exc:
        failed = exc
    finally:
        cancelled.set()  # wakes a handler waiting in receive(); no further send is accepted
        from ..plane.view import begin_shutdown
        begin_shutdown(app)  # held streams finish through their own gate first
        if request is not None:
            if not request.done() and operation == 'read-stream':
                await asyncio.wait({request}, timeout=1)  # its own shutdown gate, briefly
            if not request.done():
                # Handlers shield and drain work they already started; this only
                # stops waiting for a response nobody will read.
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        await shutdown_app(app)
        stop.set()
        if reader is not None:
            await reader  # its bounded polling read joins before end/process exit
    if carrier_error:
        raise carrier_error[0]
    if failed is not None and not isinstance(failed, asyncio.CancelledError):
        raise failed
    if request is None or not complete.is_set() and caller_cancelled.is_set():
        was_cancelled = True  # the published normal cancellation, even if the handler unwound normally
    elif complete.is_set():
        was_cancelled = False
    else:
        # A returned task is not a response: a started or absent body that nobody
        # cancelled stays unknown, never a successful end.
        raise StdioRefused('response_incomplete')
    if emit_end:
        channel.emit('end', status=status, bytes_sent=sent, cancelled=was_cancelled)
    return status, sent, was_cancelled


def _pending_cancel(channel, carrier):
    try:
        return channel.pending_cancel()
    except StdioRefused as exc:
        carrier(exc.code)
        return True
    except OSError:
        carrier('disconnected')
        return True


def _selectors(args):
    """Reject inherited selectors before input, environment scrubbing or reads."""
    from .host_owner import _GENERATED
    if any(name in os.environ for name in _GENERATED):
        raise StdioRefused('operator_required')
    if (args.root is None or getattr(args, 'fleet', None) or getattr(args, 'seed', None)
            or getattr(args, 'json', False) or getattr(args, 'stdio', False) is not True
            or os.isatty(0) or os.isatty(1)):
        raise StdioRefused('invalid_request')


def admit(args, request, source):
    """Account/root/source admission shared with the guide; SSH is not a grant."""
    from types import SimpleNamespace
    from .owner_stdio import _admit
    from .owner_setup_consent import FixedOwnerProfile
    from ..plane.owner_ssh import SSHSessionSource
    if type(source) is not SSHSessionSource:
        raise StdioRefused('owner_ssh_source_refused')
    # The guide's selector check covers its own flags; this route has none.
    guide = SimpleNamespace(root=args.root, fleet=None, seed=None, json=False, template='revenue-web-app')
    # No finish tuple crosses this wire. Current-source binding is still exact:
    # the profile compares the installed revision, preparation receipt and preflight.
    paths = _admit(guide, request)
    profile = FixedOwnerProfile(paths, {**request, 'finish_setup': None}, updated_source=True)
    if profile.runtime is None:
        raise StdioRefused('unsupported_ssh_profile')
    return paths, profile


def owner_app(paths, verifier, origin):
    """The existing policy app; pairing before an owner, workspace doors after."""
    from ..plane.owner_access import OwnerAccess, AccessUnavailable
    from ..plane.owner_source import inspect_owner_setup
    from ..plane.owner_browser import create_owner_browser_app
    from ..plane.owner_workspace_http import WorkspaceBootstrapConfig
    access = OwnerAccess(paths.root)
    owner = access.current_grant()  # validates existing authority; never initializes
    inspect_owner_setup(paths.root)
    common = {'expected_origin': origin, 'verify_principal': verifier, 'package': paths.package}
    if owner is None or not owner.active:
        return create_owner_browser_app(paths.root, pairing_origin=OPERATIONAL_ORIGIN, **common)
    try:
        return create_owner_browser_app(paths.root, **common,
            workspace_bootstrap=WorkspaceBootstrapConfig(WEBSITE_ISSUER, OPERATIONAL_ORIGIN))
    except AccessUnavailable:
        # Workspace authority is not prepared yet: only the status observation
        # is reachable; every connected route stays behind the existing gate.
        return create_owner_browser_app(paths.root, **common)


async def serve(channel, app, request, *, origin):
    """Own the app lifespan around exactly one operation.

    End is emitted only after the operation drain and a successful lifespan
    shutdown. It still never releases the runtime lease: process exit does.
    """
    incoming, started, stopped = asyncio.Queue(), asyncio.Event(), asyncio.Event()
    async def lifecycle_send(message):
        if message['type'] == 'lifespan.startup.complete': started.set()
        elif message['type'] == 'lifespan.shutdown.complete': stopped.set()
        else: raise StdioRefused('unavailable')
    lifecycle = asyncio.create_task(app({'type': 'lifespan', 'asgi': {'version': '3.0'}}, incoming.get, lifecycle_send))
    outcome = original = None
    try:
        await incoming.put({'type': 'lifespan.startup'})
        ready = asyncio.create_task(started.wait())
        try:
            await asyncio.wait({ready, lifecycle}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)
        if not started.is_set():
            raise StdioRefused('unavailable')
        outcome = await run_operation(channel, app, request['operation'], request['input'], request['token'],
                                      request['last_event_id'], origin=origin, emit_end=False)
    except BaseException as exc:
        original = exc  # the meaningful outcome is kept; shutdown still runs below
    # Lifespan owns finite-read/stream state. It is always closed before this
    # stable-CLI process can release its runtime-use reader.
    shutdown_failure = None
    if not lifecycle.done():
        await incoming.put({'type': 'lifespan.shutdown'})
        await asyncio.wait({lifecycle}, timeout=10)
    if not lifecycle.done():
        lifecycle.cancel()
        shutdown_failure = StdioRefused('shutdown_timeout')
    result, = await asyncio.gather(lifecycle, return_exceptions=True)
    if shutdown_failure is None and (isinstance(result, BaseException) or not stopped.is_set()):
        shutdown_failure = StdioRefused('shutdown_failed')
    if original is not None:
        if shutdown_failure is not None and original.__context__ is None:
            original.__context__ = shutdown_failure
        raise original
    if shutdown_failure is not None:
        raise shutdown_failure
    channel.emit('end', status=outcome[0], bytes_sent=outcome[1], cancelled=outcome[2])


async def _dispatch(args, channel):
    from ..plane.owner_ssh import SSHSessionSource, SSHSessionVerifier, MAC_TAILSCALE
    from ..plane.owner_ingress import NativePrincipalResolver
    import signal
    _selectors(args)
    source = SSHSessionSource.capture()  # before any profile/environment replacement
    loop = asyncio.get_running_loop()
    for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        loop.add_signal_handler(number, channel.interrupt)
    request = await channel.start()
    if request is None:
        channel.end_cancelled()
        return 0
    paths, profile = admit(args, request, source)
    with profile.environment():
        verifier = SSHSessionVerifier(source, NativePrincipalResolver(tailscale_binary=MAC_TAILSCALE))
        origin = await verifier.origin()
        # Even the public status operation must prove the captured native principal.
        await verifier.resolver.resolve(source.client_ip)
        app = owner_app(paths, verifier, origin)
        await serve(channel, app, request, origin=origin)
    return 0


def dispatch(args):
    """Own stdout completely: one operation, then exit after its drain."""
    channel = APIChannel(0, 1)
    try:
        return asyncio.run(_dispatch(args, channel))
    except BaseException as exc:
        from ..plane.owner_access import AccessDenied
        from ..command_result import CommandFailure
        code = (exc.code if isinstance(exc, (StdioRefused, AccessDenied)) else
                exc.error.code if isinstance(exc, CommandFailure) else
                'interrupted' if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else 'unavailable')
        if not isinstance(code, str) or re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code) is None:
            code = 'unavailable'
        try: channel.emit('error', code=code, retry=False)
        except (OSError, StdioRefused): pass
        return 4
