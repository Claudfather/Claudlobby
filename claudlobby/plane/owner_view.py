"""Internal protected Plane factory; no CLI/runtime or identity adapter yet.

The injected verifier is trusted server code, not a browser identity claim.
This seam exercises the read boundary with disposable data while ingress,
pairing UI and browser credential transport are still being established.
"""

from pathlib import Path
from typing import Awaitable, Callable

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..context import resolve_paths
from .ids import read_host_uid
from .owner_access import AccessDenied, AccessUnavailable, OwnerAccess, PrincipalRef, VerifiedReader


ReaderVerifier = Callable[[Scope], Awaitable[VerifiedReader]]


class _ReadStopped(Exception):
    """Unwind the response producer after access ends, including held streams."""


def _is_read_stop(exc: BaseException) -> bool:
    # Older Starlette/AnyIO wraps exceptions from streaming task groups.
    if isinstance(exc, _ReadStopped):
        return True
    children = getattr(exc, "exceptions", ())
    return bool(children) and all(_is_read_stop(child) for child in children)


def _refusal(exc: Exception) -> JSONResponse:
    unavailable = isinstance(exc, AccessUnavailable)
    return JSONResponse(
        {"state": "unavailable" if unavailable else "denied",
         "remediation": "Owner access could not be verified." if unavailable
         else "A current owner read session is required."},
        status_code=503 if unavailable else 403,
        headers={"Cache-Control": "no-store"},
    )


class _OwnerReadGate:
    def __init__(self, app: ASGIApp, root: Path, verify_reader: ReaderVerifier):
        self.app = app
        self.state = app.state  # preserve begin_shutdown's existing lifecycle
        self.access = OwnerAccess(root)  # never initialize or repair on a read
        self.verify_reader = verify_reader

    def _admit(self, reader: VerifiedReader) -> None:
        try:
            host_uid = read_host_uid(self.access.root / "state")
        except (OSError, ValueError) as exc:
            raise AccessUnavailable("installation identity is unavailable") from exc
        self.access.authorize_read(reader.token, reader.principal, host_uid=host_uid)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        try:
            reader = await self.verify_reader(scope)
            if not isinstance(reader, VerifiedReader):
                raise AccessDenied("unverified_reader")
            await run_in_threadpool(self._admit, reader)
        except (AccessDenied, AccessUnavailable) as exc:
            await _refusal(exc)(scope, receive, send)
            return
        # This factory grants reads only, including for future added routes.
        if scope["method"] not in {"GET", "HEAD"}:
            await JSONResponse({"state": "denied"}, status_code=405,
                               headers={"Allow": "GET, HEAD", "Cache-Control": "no-store"})(
                                   scope, receive, send)
            return

        pending_start = None
        started = False

        async def guarded_send(message: Message) -> None:
            nonlocal pending_start, started
            if message["type"] == "http.response.start":
                # Hold headers until the first body is admitted. Dropping length
                # permits safe early termination if a later chunk is refused.
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in {b"cache-control", b"content-length"}]
                pending_start = {**message, "headers": headers + [(b"cache-control", b"no-store")]}
                return
            if message["type"] != "http.response.body":
                raise RuntimeError("unsupported protected response message")
            try:
                await run_in_threadpool(self._admit, reader)
            except (AccessDenied, AccessUnavailable) as exc:
                if started:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                else:
                    await _refusal(exc)(scope, receive, send)
                raise _ReadStopped() from None
            if not started:
                await send(pending_start)
                started = True
            await send(message)

        # File responses must traverse guarded_send; no sendfile bypass.
        inner_scope = {**scope, "extensions": {
            key: value for key, value in scope.get("extensions", {}).items()
            if key not in {"http.response.pathsend", "http.response.zerocopysend"}
        }}
        try:
            await self.app(inner_scope, receive, guarded_send)
        except Exception as exc:
            if not _is_read_stop(exc):
                raise


def create_owner_app(root: Path, *, verify_reader: ReaderVerifier,
                     sampler=None, package=None):
    """Guard the canonical view, including static/health/error/stream paths.

    No default verifier and no public login/session route. Runtime callers
    continue to use view.create_app until trusted ingress is implemented and
    validated. The source root selects the authority; a request cannot do so.
    """
    if not callable(verify_reader):
        raise ValueError("an explicit trusted reader verifier is required")
    from .view import create_app

    paths = resolve_paths(root=root, package=package)
    view = create_app(paths.root, sampler=sampler, package=paths.package)
    return _OwnerReadGate(view, paths.root, verify_reader)
