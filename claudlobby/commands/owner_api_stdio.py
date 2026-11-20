"""One finite typed owner operation over an admitted ordinary SSH exec channel.

There is no HTTP/command proxy on the wire. Policy remains the existing ASGI
app; principal and invariant authority originate outside request frames.

Profile ``owner-signin-v1`` keeps the same process, app and lifespan for one
bounded provider sign-in conversation (``converse``); every call in it is still
one compiled operation through the same canonical route.
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import json
import os
import re
import select
import threading
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
# URL and code hand-off are ProviderSignin instance state, and the retained record
# admits one current attempt (another ID only after an ending it reports as
# retryable, in a later conversation). A one-operation exec would start that
# attempt and cancel it in its own mandatory drain, or deliver a code to a
# process that never owned the child. These stay refused on owner-api-v1, where
# signin-status remains a read-only observation; the one owner of an attempt is
# an owner-signin-v1 conversation.
_PROCESS_BOUND = frozenset({'setup-signin-begin', 'setup-signin-code', 'setup-signin-cancel'})
SIGNIN_PROFILE = 'owner-signin-v1'
_CALL = {'call_id', 'operation', 'input', 'token'}
_SIGNIN_CALLS = frozenset({'setup-signin-status', 'setup-signin-code', 'setup-signin-cancel'})
# Admission and observation bounds for one conversation. None of them proves
# that a process or thread stopped: every expiry still runs the full drain.
CALL_LIMIT = 512             # begin is call 1; room for a status check every 2 s through 630 s
REPLY_BYTES = 16384          # one complete decoded body
CALL_SECONDS = 20            # carrier observation; the canonical handler keeps its own 15 s
IDLE_SECONDS = 45            # between a reply and the next call
CONVERSATION_SECONDS = 330   # from the admitted start; no frame renews it
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
        self.admitted = None  # monotonic time of an admitted conversation start
        # The one ordering owner: each output write step (and, after a reply's
        # final byte, its commit) and each input read step run under it, one
        # short step at a time. Waiting for readiness happens outside it.
        self.io_lock = threading.Lock()
        self.commits = 0  # replies whose final byte was written and committed
        self.observed_commits = 0  # commits already counted when the last bytes were read
        self.torn = False  # a frame was left half written

    def _step(self, fd, action):
        """One syscall under the I/O lock, nonblocking: the descriptor's own flag is
        set for the step and put back to what it was, so a shared description
        (a duplex socket as 0 and 1) is never left changed."""
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if flags & os.O_NONBLOCK:
            return action()
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        try:
            return action()
        finally:
            fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)

    def emit(self, kind, _commit=False, _halt=None, **values):
        """Write one frame; the commit is counted under the I/O lock right after its final byte.

        Each step is a nonblocking write; would-block and short writes return to a
        readiness wait outside the lock. ``_halt`` stops a write in progress (the
        frame is then torn and nothing further is written on this channel)."""
        if self.torn:
            raise StdioRefused('output_unavailable')
        self.output_seq += 1
        raw = _wire({**values, 'version': 1, 'type': kind, 'seq': self.output_seq, 'session_id': self.session_id})
        if len(raw) > 32768 or self.output_seq > 4096:
            raise StdioRefused('output_limit')
        total = len(raw)
        deadline = time.monotonic() + 15
        try:
            while raw:
                if _halt is not None and _halt.is_set():
                    raise StdioRefused('output_aborted')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StdioRefused('output_unavailable')
                if not select.select([], [self.outgoing], [], min(remaining, .1))[1]:
                    continue
                with self.io_lock:
                    try:
                        count = self._step(self.outgoing, lambda: os.write(self.outgoing, raw[:4096]))
                    except BlockingIOError:
                        continue
                    if not count:
                        raise StdioRefused('output_unavailable')
                    raw = raw[count:]
                    if not raw and _commit:
                        self.commits += 1
        finally:
            if raw and len(raw) < total:
                self.torn = True  # a partial frame is on the wire; nothing may follow it

    def read(self, allowed, timeout=.25):
        if self.interrupted:
            raise StdioRefused('interrupted')
        if b'\n' not in self.buffer:
            if not select.select([self.incoming], [], [], timeout)[0]:
                if self.partial_since is not None and time.monotonic() - self.partial_since >= 15:
                    raise StdioRefused('expired')
                return None
            with self.io_lock:
                try:
                    part = self._step(self.incoming, lambda: os.read(self.incoming, 4096))
                except BlockingIOError:
                    return None
                self.observed_commits = self.commits
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
            extra = (_START if kind == 'start' else {'chunk_id'} if kind == 'ack'
                     else _CALL if kind == 'call' else set())
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
            if (value['profile'] not in ('owner-api-v1', SIGNIN_PROFILE) or type(value['expected_uid']) is not int
                    or not 0 < value['expected_uid'] < 2**32 - 1
                    or not isinstance(value['source_revision'], str) or re.fullmatch('[a-f0-9]{40}', value['source_revision']) is None
                    or not isinstance(value['binding_sha256'], str) or re.fullmatch('[a-f0-9]{64}', value['binding_sha256']) is None):
                raise StdioRefused('invalid_request')
            try:
                operation_request(value['operation'], value['input'], value['token'], value['last_event_id'])
            except ValueError:
                raise StdioRefused('invalid_request') from None
            if value['profile'] == SIGNIN_PROFILE:
                # A conversation starts only with begin; nothing else selects it.
                if value['operation'] != 'setup-signin-begin' or value['last_event_id'] is not None:
                    raise StdioRefused('invalid_request')
                self.admitted = time.monotonic()
                return value
            if value['operation'] in _PROCESS_BOUND:
                # Refused before any engine entry: the one retained attempt is untouched.
                raise StdioRefused('operation_requires_persistent_owner')
            return value
        raise StdioRefused('expired')

    def end_cancelled(self):
        """The published normal cancellation before headers: healthy release."""
        self.emit('end', status=0, bytes_sent=0, cancelled=True)


async def shutdown_app(app):
    """Attempt every owned shutdown; raise the first failure only after all ran."""
    from ..plane.view import begin_shutdown
    failures = []

    async def attempt(step):
        try:
            await step()
        except Exception as exc:
            failures.append(exc)

    try:
        begin_shutdown(app)
    except Exception as exc:
        failures.append(exc)
    method = getattr(app, 'shutdown', None)
    if method is not None:
        await attempt(method)
    for name in ('pairing_bootstrap', 'connected_setup', 'connected_actions', 'connected_reads', 'workspace_bootstrap'):
        component = getattr(app, name, None)
        if component is not None:
            await attempt(component.shutdown)
    tasks = list(getattr(app, '_inflight', ()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if failures:
        raise failures[0]


def _scope(operation, payload, token, last_event_id, origin):
    """The fixed internal ASGI scope and body of one compiled operation."""
    method, path, query, body, headers = operation_request(operation, payload, token, last_event_id)
    authority = origin.removeprefix('https://')
    headers = [(b'host', authority.encode('ascii')), *headers]
    if operation != 'status':
        headers.append((b'origin', OPERATIONAL_ORIGIN.encode('ascii')))
    return {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
        'scheme': 'https', 'path': path, 'raw_path': path.encode('ascii'), 'query_string': query,
        'headers': headers, 'client': ('ssh-session', 0), 'server': ('ssh-carrier', 0)}, body


def _response_start(message, started):
    """Status and the closed response header allowlist of one response start."""
    if started or type(message.get('status')) is not int or not 100 <= message['status'] <= 599:
        raise StdioRefused('invalid_response')
    selected = {}
    for key, value in message.get('headers', ()):
        name = key.decode('ascii').lower()
        if name in {'content-type', 'cache-control', 'retry-after'}:
            if name in selected or len(value) > 1024:
                raise StdioRefused('invalid_response')
            selected[name] = value.decode('ascii')
    return message['status'], selected


async def run_operation(channel, app, operation, payload, token, last_event_id, *, origin, emit_end=True):
    """Carry one compiled operation and return its (status, bytes, cancelled) outcome.

    The end frame follows the complete owned component drain. ``serve`` passes
    ``emit_end=False`` and emits it only after the application lifespan has also
    shut down successfully.
    """
    scope, body = _scope(operation, payload, token, last_event_id, origin)
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
                code, selected = _response_start(message, status)
                channel.emit('headers', status=code, headers=selected)
                status = code
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
    failed = cleanup_failure = None
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
        try:
            await shutdown_app(app)
        except Exception as exc:
            cleanup_failure = exc  # never replaces an earlier outcome; the join below still runs
        finally:
            stop.set()
            if reader is not None:
                await reader  # its bounded polling read joins before end/process exit
    if carrier_error:
        raise carrier_error[0]
    if failed is not None and not isinstance(failed, asyncio.CancelledError):
        raise failed
    # The operation outcome is derived before cleanup is considered: an
    # incomplete response keeps its own identity when cleanup also failed.
    if request is None or caller_cancelled.is_set():
        # Completion and caller cancellation are independent; this never implies safe replay.
        was_cancelled = True  # the published normal cancellation, even if the handler unwound normally
    elif complete.is_set():
        was_cancelled = False
    else:
        # A returned task is not a response: a started or absent body that nobody
        # cancelled stays unknown, never a successful end.
        incomplete = StdioRefused('response_incomplete')
        incomplete.__context__ = cleanup_failure
        raise incomplete
    if cleanup_failure is not None:
        raise cleanup_failure  # no normal end, cancelled or complete, after a failed cleanup
    if emit_end:
        channel.emit('end', status=status, bytes_sent=sent, cancelled=was_cancelled)
    return status, sent, was_cancelled


async def converse(channel, app, request, *, origin):
    """Own one provider sign-in attempt for one bounded conversation.

    Begin is call 1; later ``call`` frames (status, code, cancel) reach the same
    app instance, each compiled by ``operation_request`` and admitted afresh by
    the canonical route. Nothing is retained as authority between calls. A reply
    is a call receipt only. The (status, bytes, cancelled) outcome is returned
    after the full owned drain; ``serve`` emits ``end`` once the lifespan has
    also shut down. A fault, an expiry or a failed drain returns no outcome.
    """
    from ..plane.provider_signin import _TERMINAL
    from ..plane.view import begin_shutdown
    identity = {name: request['input'][name] for name in ('generation', 'attempt_id')}
    clock = (channel.admitted if channel.admitted is not None else time.monotonic()) + CONVERSATION_SECONDS
    calls = asyncio.Queue(maxsize=1)
    abort, stop, caller_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    carrier_error = []
    halt = threading.Event()  # stops a reply write in progress in its own thread
    writing = None
    # A call is outstanding until its reply's final byte is written and committed.
    # Bytes are read under the channel's one I/O lock, which also covers the final
    # write and its commit; the commit count seen by that read is the frame's
    # permanent classification. Nothing is re-evaluated after a wait.
    state = {'admitted': 1, 'next': 2, 'open': True}
    status = sent = 0
    current = None

    def carrier(code):
        error = StdioRefused(code)
        if not carrier_error:
            carrier_error.append(error)
        trip()
        return error

    def trip():
        abort.set()
        halt.set()

    def read_one():
        frame = channel.read({'call', 'cancel'})
        if frame is None:
            return None, False
        return frame, channel.observed_commits < state['admitted']

    async def controls():
        try:
            while not stop.is_set():
                frame, outstanding = await asyncio.to_thread(read_one)
                if frame is None:
                    continue
                if frame['type'] == 'cancel':
                    caller_cancelled.set()
                    trip()
                    return
                # Refused here, before any canonical entry: pipelined, duplicate,
                # skipped, late, foreign-operation or changed-identity calls.
                if (outstanding or not state['open'] or type(frame['call_id']) is not int
                        or frame['call_id'] != state['next'] or frame['call_id'] > CALL_LIMIT
                        or not isinstance(frame['operation'], str) or frame['operation'] not in _SIGNIN_CALLS
                        or type(frame['input']) is not dict
                        or {name: frame['input'].get(name) for name in identity} != identity):
                    raise StdioRefused('invalid_request')
                try:
                    operation_request(frame['operation'], frame['input'], frame['token'], None)
                except ValueError:
                    raise StdioRefused('invalid_request') from None
                state['admitted'] += 1
                state['next'] += 1
                calls.put_nowait(frame)
        except StdioRefused as exc:
            carrier(exc.code)
        except OSError:
            carrier('disconnected')

    async def first(waiting, seconds):
        """Wait for one task, the abort or the smaller of a bound and the clock."""
        aborted = asyncio.create_task(abort.wait())
        try:
            done, _ = await asyncio.wait({waiting, aborted}, return_when=asyncio.FIRST_COMPLETED,
                timeout=max(0, min(seconds, clock - time.monotonic())))
        finally:
            aborted.cancel()
            await asyncio.gather(aborted, return_exceptions=True)
        if abort.is_set():
            return False
        if waiting not in done:
            carrier('expired')  # observation only; the drain below still joins everything
            return False
        return True

    async def perform(operation, payload, token):
        """One canonical invocation: (status, headers, body), or None once aborted."""
        nonlocal current
        scope, body = _scope(operation, payload, token, None, origin)
        code, selected, data = 0, {}, bytearray()
        received = complete = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            await abort.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            nonlocal code, selected, complete
            if abort.is_set():
                raise asyncio.CancelledError
            try:
                if message['type'] == 'http.response.start':
                    code, selected = _response_start(message, code)
                elif message['type'] == 'http.response.body' and code and not complete:
                    data.extend(message.get('body', b''))
                    if len(data) > REPLY_BYTES:
                        raise StdioRefused('output_limit')
                    complete = not message.get('more_body', False)
                else:
                    raise StdioRefused('invalid_response')
            except StdioRefused as exc:
                raise carrier(exc.code) from None
            except UnicodeError:
                raise carrier('output_unavailable') from None

        current = asyncio.create_task(app(scope, receive, send))
        if not await first(current, CALL_SECONDS):
            return None
        if not current.cancelled() and current.exception() is not None:
            raise current.exception()
        if current.cancelled() or not complete:
            raise carrier('response_incomplete')  # a returned handler is not a response
        return code, selected, bytes(data)

    reader = None
    failed = cleanup_failure = None
    try:
        # A cancel that arrived during admission never reaches a policy handler.
        if await asyncio.to_thread(_pending_cancel, channel, carrier):
            if not carrier_error:
                caller_cancelled.set()
        else:
            reader = asyncio.create_task(controls())
            call_id, operation, payload, token = 1, request['operation'], request['input'], request['token']
            while True:
                reply = await perform(operation, payload, token)
                if reply is None:
                    break
                code, selected, data = reply
                closing = operation != 'setup-signin-code'  # a refused code is the one retry
                if code == 200:
                    # Only the canonical body says whether the pinned attempt is
                    # terminal; worker liveness never ends a conversation.
                    try:
                        value = json.loads(data)
                        named = (type(value) is dict and isinstance(value['state'], str)
                                 and {name: value[name] for name in identity} == identity)
                    except (ValueError, KeyError, RecursionError):
                        named = False
                    if not named:
                        raise carrier('invalid_response')
                    closing = operation == 'setup-signin-cancel' or value['state'] in _TERMINAL
                if closing:
                    state['open'] = False  # no later call is admitted
                try:
                    # The next call is admissible only once the reply's final byte is written.
                    # Off the event loop and halted by the abort: output backpressure must not
                    # stop a cancellation or fault from starting the drain. The thread is joined
                    # below however this ends.
                    writing = asyncio.ensure_future(asyncio.to_thread(
                        channel.emit, 'reply', _commit=True, _halt=halt, call_id=call_id, status=code,
                        headers=selected, data=base64.b64encode(data).decode('ascii')))
                    await asyncio.shield(writing)
                except StdioRefused as exc:
                    # A halted write is a torn reply: unknown, never a successful end.
                    raise carrier('response_incomplete' if exc.code == 'output_aborted' else exc.code) from None
                except OSError:
                    raise carrier('output_unavailable') from None
                status, sent = code, sent + len(data)
                if closing:
                    break
                waiting = asyncio.create_task(calls.get())
                try:
                    arrived = await first(waiting, IDLE_SECONDS)
                finally:
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)
                if not arrived:
                    break
                frame = waiting.result()
                call_id, operation, payload, token = (frame['call_id'], frame['operation'],
                                                      frame['input'], frame['token'])
    except asyncio.CancelledError:
        trip()
        raise
    except BaseException as exc:
        failed = exc
    finally:
        trip()  # wakes a handler waiting in receive(); no further send is accepted
        state['open'] = False
        begin_shutdown(app)
        if writing is not None:
            await asyncio.gather(writing, return_exceptions=True)  # the writer thread has returned
        if current is not None:
            if not current.done():
                # The canonical handler shields and drains work it already started.
                current.cancel()
            await asyncio.gather(current, return_exceptions=True)
        try:
            # Cancels and joins the owned attempt, however long that takes: no
            # timer abandons its thread or lets this process release early.
            await shutdown_app(app)
        except Exception as exc:
            cleanup_failure = exc
        finally:
            stop.set()
            if reader is not None:
                await reader
    if carrier_error:
        raise carrier_error[0]
    if failed is not None and not isinstance(failed, asyncio.CancelledError):
        raise failed
    if cleanup_failure is not None:
        raise cleanup_failure
    return status, sent, caller_cancelled.is_set()


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
    """Own the app lifespan around exactly one operation or one sign-in conversation.

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
        if request['profile'] == SIGNIN_PROFILE:
            outcome = await converse(channel, app, request, origin=origin)
        else:
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
    """Own stdout completely: one operation or conversation, then exit after its drain."""
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
