"""Opt-in pairing preparation for the temporary, unpaired owner server.

Origin is fixed client routing metadata, not authentication. The configured
native ingress verifier supplies identity; this route only begins the existing
challenge. Inspection and confirmation remain local operator operations.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import threading
import time

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .owner_access import AccessDenied, PrincipalRef, PAIRING_SECONDS
from .owner_source import inspect_owner_setup
from .workspace_assertion import _origin

PATH = "/api/owner/bootstrap/pair"
RESPONSE_SECONDS = 35
_DEADLINE = ContextVar("pairing_bootstrap_deadline")
_NAMESPACE = "tailscale:controlplane.tailscale.com"


class InvalidRequest(ValueError):
    """Only malformed body input, not a canonical operation outcome."""


def _header(scope, name):
    values = [value for key, value in scope.get("headers", ()) if key.lower() == name]
    if len(values) > 1:
        raise AccessDenied("pairing_bootstrap_denied")
    try:
        return values[0].decode("ascii") if values else None
    except UnicodeError:
        raise AccessDenied("pairing_bootstrap_denied") from None


async def _empty_object(receive):
    raw = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise InvalidRequest
        chunk = message.get("body", b"")
        if len(raw) + len(chunk) > 1024:
            raise InvalidRequest
        raw.extend(chunk)
        if not message.get("more_body", False):
            break
    try:
        value = json.loads(raw.decode("utf-8"))
        if type(value) is not dict or value:
            raise InvalidRequest
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidRequest from None


class PairingBootstrapHTTP:
    def __init__(self, access, *, origin, authority, verify_principal):
        if not _origin(origin, False) or not callable(verify_principal):
            raise ValueError("pairing bootstrap requires an exact HTTPS origin and trusted verifier")
        owner = access.current_grant()  # Existing initialized authority only.
        if owner is not None and owner.active:
            raise ValueError("pairing bootstrap refuses an active owner")
        self.access, self.origin, self.authority = access, origin, authority
        self.verify_principal = verify_principal
        self._workers = set()
        self._operations = {}
        self._stopping = False

    def response_seconds(self, scope):
        return RESPONSE_SECONDS

    async def _principal(self, scope):
        principal = await asyncio.wait_for(self.verify_principal(scope),
            max(0, _DEADLINE.get() - time.monotonic()))
        if (type(principal) is not PrincipalRef or principal.namespace != _NAMESPACE
                or not principal.subject.isascii() or not principal.subject.isdecimal()
                or len(principal.subject) > 19
                or not 0 < int(principal.subject) < 2**63
                or str(int(principal.subject)) != principal.subject):
            raise AccessDenied("pairing_bootstrap_denied")
        return principal

    def _state(self):
        host_uid = inspect_owner_setup(self.access.root)
        owner = self.access.current_grant()
        if owner is not None and owner.active:
            raise AccessDenied("pairing_bootstrap_denied")
        return host_uid, owner

    async def _blocking(self, fn, *args):
        deadline = _DEADLINE.get()
        lock = threading.Lock()
        started, abandoned = False, False

        def guarded():
            nonlocal started
            with lock:
                if abandoned or self._stopping or time.monotonic() >= deadline:
                    raise asyncio.TimeoutError
                started = True
            return fn(*args)

        job = asyncio.create_task(run_in_threadpool(guarded))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            with lock:
                abandoned = not started
            if abandoned:
                job.cancel()
            # Started canonical storage work is drained, never abandoned or
            # repeated because its response disappeared.
            await asyncio.gather(job, return_exceptions=True)
            raise

    async def _perform(self, scope, receive):
        await asyncio.wait_for(_empty_object(receive), 5)
        principal = await self._principal(scope)
        before = await self._blocking(self._state)
        challenge = await self._blocking(self.access.begin_pairing, principal)
        current = await self._principal(scope)
        if (current != principal or await self._blocking(self._state) != before
                or not self.access._now() < challenge.expires_at):
            raise AccessDenied("pairing_bootstrap_denied")
        return {"version": 1, "state": "awaiting_local_approval", "challenge": challenge.token,
                "principal": {"namespace": principal.namespace, "subject": principal.subject},
                "expires_at": challenge.expires_at, "expires_in": PAIRING_SECONDS}, 202

    def _cancel(self, operation):
        if operation in self._operations and not self._operations[operation]:
            self._operations[operation] = True
            operation.cancel()

    async def _response(self, scope, receive):
        operation = asyncio.create_task(self._perform(scope, receive))
        self._operations[operation] = False
        timer = asyncio.get_running_loop().call_later(
            max(0, _DEADLINE.get() - time.monotonic()), self._cancel, operation)
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            self._cancel(operation)
            await asyncio.gather(operation, return_exceptions=True)
            return {"state": "unavailable"}, 503
        except AccessDenied:
            return {"state": "denied"}, 403
        except InvalidRequest:
            return {"state": "invalid_request"}, 400
        except Exception:
            # A retained challenge can survive an unavailable response. No
            # authority was granted, and no replacement is minted here.
            return {"state": "unavailable"}, 503
        finally:
            timer.cancel()
            self._operations.pop(operation, None)

    async def shutdown(self):
        self._stopping = True
        for operation in tuple(self._operations):
            self._cancel(operation)
        if self._workers:
            await asyncio.gather(*tuple(self._workers), return_exceptions=True)

    async def __call__(self, scope, receive, send):
        headers = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
                   "Vary": "Origin", "X-Content-Type-Options": "nosniff"}
        try:
            if (scope["path"] != PATH or scope.get("raw_path", PATH.encode()) != PATH.encode()
                    or scope.get("query_string") or _header(scope, b"host") != self.authority
                    or _header(scope, b"origin") != self.origin
                    or _header(scope, b"cookie") is not None
                    or _header(scope, b"authorization") is not None):
                raise AccessDenied("pairing_bootstrap_denied")
            if scope["method"] != "POST":
                headers["Allow"] = "POST"
                await JSONResponse({"state": "denied"}, 405, headers=headers)(scope, receive, send)
                return
            if (_header(scope, b"x-claudlobby-owner") != "1"
                    or (_header(scope, b"content-type") or "").split(";", 1)[0].lower() != "application/json"):
                raise AccessDenied("pairing_bootstrap_denied")
        except AccessDenied:
            await JSONResponse({"state": "denied"}, 403, headers=headers)(scope, receive, send)
            return
        if self._stopping or len(self._workers) >= 8:
            body, status = {"state": "unavailable"}, 503
        else:
            deadline = time.monotonic() + self.response_seconds(scope)
            token = _DEADLINE.set(deadline)
            try:
                job = asyncio.create_task(self._response(scope, receive))
            finally:
                _DEADLINE.reset(token)
            self._workers.add(job)

            def done(task):
                self._workers.discard(task)
                if not task.cancelled():
                    task.exception()

            job.add_done_callback(done)
            completed, _ = await asyncio.wait({job}, timeout=max(0, deadline - time.monotonic()))
            body, status = job.result() if completed else ({"state": "unavailable"}, 503)
        await JSONResponse(body, status, headers=headers)(scope, receive, send)
