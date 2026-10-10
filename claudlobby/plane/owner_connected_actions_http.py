"""Finite connected action carrier; effects stay with the canonical owner engines."""
import asyncio
import time

from starlette.responses import JSONResponse, Response

from ..operation_context import OperationContextError, OperationContextUnavailableError
from .owner_access import AccessDenied, AccessUnavailable, VerifiedReader
from .owner_action_authorization import ConnectedActionAuthorization
from .owner_actions import ActionNotStarted, OwnerActions
from .owner_connected_http import _token
from .owner_workspace_http import WorkspaceBootstrapHTTP, InvalidRequest, _header, _json, _DEADLINE

PREFIX = "/api/owner/connected/actions/"
_OPERATIONS = frozenset({"context", "prepare", "send", "receipt"})


class ConnectedActionsHTTP(WorkspaceBootstrapHTTP):
    """Separate eight-job queue; started synchronous work is always drained."""
    def __init__(self, access, *, config, authority, verify_principal, package=None):
        super().__init__(access, config=config, authority=authority, verify_principal=verify_principal)
        authorization = ConnectedActionAuthorization(access, config.issuer, config.operational_origin)
        self.actions = OwnerActions(access.root, package=package, authorization=authorization)

    def response_seconds(self, scope):
        return 45 if scope["path"].endswith("/send") else 15

    def _boundary(self, scope):
        if (_header(scope, b"host") != self.authority
                or _header(scope, b"origin") != self.config.operational_origin
                or _header(scope, b"cookie") is not None or scope.get("query_string")):
            raise AccessDenied("connected_session_denied")

    async def _admit(self, scope):
        token = _token(scope)  # Reject malformed/missing bearer before native lookup.
        principal = await self._principal(scope, _DEADLINE.get())
        host = await self._blocking(self._source)
        await self._blocking(self.actions.authorization.authorize_read, token, principal, host_uid=host)
        return VerifiedReader(principal, token)

    async def _perform_action(self, scope, receive, operation, progress):
        if scope["method"] == "OPTIONS":
            if _header(scope, b"authorization") is not None:
                raise AccessDenied("connected_session_denied")
            requested = (_header(scope, b"access-control-request-headers") or "").lower().split(",")
            requested = [field.strip() for field in requested]
            if (_header(scope, b"access-control-request-method") != "POST"
                    or len(requested) != 3 or set(requested) != {"authorization", "content-type", "x-claudlobby-owner"}):
                raise AccessDenied("connected_session_denied")
            principal = await self._principal(scope, _DEADLINE.get())
            await self._blocking(self._source)
            binding = await self._blocking(self.workspace.active_binding, principal)
            if binding.issuer != self.config.issuer or binding.operational_origin != self.config.operational_origin:
                raise AccessDenied("connected_session_denied")
            return None, 204
        if (_header(scope, b"x-claudlobby-owner") != "1"
                or (_header(scope, b"content-type") or "").split(";", 1)[0].lower() != "application/json"):
            raise AccessDenied("connected_session_denied")
        reader = await self._admit(scope)
        payload = await asyncio.wait_for(_json(receive, 32768), 5)
        fn = self.actions.context if operation == "context" else self.actions.prepare if operation == "prepare" else self.actions.operation
        args = (reader, payload) if operation in {"context", "prepare"} else (operation, reader, payload)
        # Conservative entry fence. Even queued/failed engine calls are unknown
        # unless the engine proves its own ActionNotStarted pre-adapter refusal.
        progress[0] = True
        result = await self._blocking(fn, *args)
        # This closing check is part of the owned job and original deadline.
        # A changed native principal, workspace, source or grant hides the result.
        current = await self._admit(scope)
        if current.principal != reader.principal:
            raise AccessDenied("connected_session_denied")
        await self._blocking(self.actions.admit_response, operation, current, result)
        return result, 200

    async def _serve(self, scope, receive, send, operation, progress, headers):
        try:
            body, status = await self._perform_action(scope, receive, operation, progress)
        except ActionNotStarted as exc:
            status = 503 if isinstance(exc.refusal, (AccessUnavailable, OperationContextUnavailableError)) else 403
            body = {"state": "unavailable" if status == 503 else "denied", "effect": "not_started"}
        except (AccessDenied, OperationContextError, InvalidRequest) as exc:
            status = 503 if isinstance(exc, OperationContextUnavailableError) else 403
            body = {"state": "unavailable" if status == 503 else "denied"}
        except (Exception, asyncio.CancelledError):
            # Cancellation is owned by this job's timer/shutdown. _blocking has
            # drained any started thread first; no claim that its effect stopped.
            body, status = {"state": "unavailable"}, 503
        if operation == "send" and status in (403, 503) and not progress[0]:
            body["effect"] = "not_started"
        if status == 204:
            await Response(status_code=204, headers={**headers,
                "Access-Control-Allow-Methods": "POST",
                "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Claudlobby-Owner",
                "Vary": "Origin, Access-Control-Request-Method, Access-Control-Request-Headers"})(scope, receive, send)
        else:
            await JSONResponse(body, status, headers=headers)(scope, receive, send)

    async def __call__(self, scope, receive, send):
        headers = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
                   "Vary": "Origin", "X-Content-Type-Options": "nosniff"}
        operation = scope["path"][len(PREFIX):]
        try:
            self._boundary(scope)
        except AccessDenied:
            await JSONResponse({"state": "denied"}, 403, headers=headers)(scope, receive, send)
            return
        headers["Access-Control-Allow-Origin"] = self.config.operational_origin
        if operation not in _OPERATIONS:
            await JSONResponse({"state": "denied"}, 404, headers=headers)(scope, receive, send)
            return
        if scope["method"] not in {"POST", "OPTIONS"}:
            await JSONResponse({"state": "denied"}, 405, headers={**headers, "Allow": "POST, OPTIONS"})(scope, receive, send)
            return
        if self._stopping or len(self._workers) >= 8:
            body = {"state": "unavailable"}
            if operation == "send" and scope["method"] == "POST":
                body["effect"] = "not_started"
            await JSONResponse(body, 503, headers=headers)(scope, receive, send)
            return
        active, emitted = True, False
        progress = [False]
        async def guarded_send(message):
            nonlocal emitted
            if active:
                emitted = True
                await send(message)
        seconds = self.response_seconds(scope)
        deadline = time.monotonic() + seconds
        token = _DEADLINE.set(deadline)
        try:
            job = asyncio.create_task(self._serve(scope, receive, guarded_send, operation, progress, headers))
        finally:
            _DEADLINE.reset(token)
        self._workers.add(job)
        self._operations[job] = False
        timer = asyncio.get_running_loop().call_later(seconds, self._cancel_operation, job)
        def finished(done):
            timer.cancel()
            self._workers.discard(done)
            self._operations.pop(done, None)
            if not done.cancelled():
                done.exception()
        job.add_done_callback(finished)
        try:
            completed, _ = await asyncio.wait({job}, timeout=seconds)
            if completed:
                if not job.cancelled():
                    job.result()
                elif not emitted:
                    await JSONResponse({"state": "unavailable"}, 503, headers=headers)(scope, receive, send)
            else:
                active = False
                if not emitted:
                    body = {"state": "unavailable"}
                    if operation == "send" and not progress[0]:
                        body["effect"] = "not_started"
                    await JSONResponse(body, 503, headers=headers)(scope, receive, send)
        finally:
            # A disconnected caller drops only response ownership. The job stays
            # counted until its deadline/cancellation drains started local work.
            active = False
