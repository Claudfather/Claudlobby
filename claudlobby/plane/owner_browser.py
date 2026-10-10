"""Direct-host browser session transport with an explicit trusted verifier.

Requires a trusted identity verifier and configured external HTTPS origin.
Neither this factory nor its cookies authenticate Tailscale identity. The
backend/proxy trust boundary and local confirmation remain caller-owned. Only
the fixed owner entry shell and its two assets are public; Plane reads stay gated.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import sqlite3
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from ..activation_state import ActivationError
from ..operation_context import OperationContextError, OperationContextUnavailableError
from .owner_actions import ActionNotStarted, OwnerActions
from .ids import read_host_uid
from .owner_access import AccessDenied, AccessUnavailable, PrincipalRef, PAIRING_SECONDS, SESSION_SECONDS
from .owner_view import VerifiedReader, create_owner_app

COOKIE_NAME = "__Host-claudlobby-owner"
_COOKIE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_MAX_BODY = 1024
_MAX_ACTION_BODY = 32768
_BODY_SECONDS = 5
_MAX_ACTION_WORKERS = 8
_PREFIX = "/api/owner/"
_METHODS = {"actions/context": "POST", "actions/send": "POST", "actions/receipt": "POST", "status": "GET", "pair": "POST", "login": "POST",
            "renew": "POST", "logout": "POST"}
PrincipalVerifier = Callable[[Scope], Awaitable[PrincipalRef]]
_ENTRY_ASSETS = {
    "/owner": ("owner-entry.html", "text/html"),
    "/owner-entry.js": ("owner-entry.js", "text/javascript"),
    "/owner-entry.css": ("owner-entry.css", "text/css"),
}
_ENTRY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; base-uri 'none'; object-src 'none'; form-action 'none'; "
        "frame-ancestors 'none'",
}


def _entry_response(scope: Scope) -> Response:
    """Exact public allowlist, independent of identity and authority state."""
    if scope.get("query_string"):
        return _response({"state": "denied"}, 403)
    if scope["method"] not in {"GET", "HEAD"}:
        response = _response({"state": "denied"}, 405)
        response.headers["Allow"] = "GET, HEAD"
        return response
    filename, media_type = _ENTRY_ASSETS[scope["path"]]
    body = (Path(__file__).with_name("ui") / filename).read_bytes()
    response = Response(body if scope["method"] == "GET" else b"",
                        media_type=media_type, headers=_ENTRY_HEADERS)
    response.headers["Content-Length"] = str(len(body))
    return response


def _origin(value: str) -> tuple[str, str]:
    """Require canonical configuration; never normalize a request into trust."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (not isinstance(value, str) or parsed.scheme != "https" or not host
                or len(host) > 253 or not re.fullmatch(r"[a-z0-9.-]+", host)
                or any(not label or len(label) > 63 or label.startswith("-")
                       or label.endswith("-") for label in host.split("."))
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError
        port = parsed.port
        if port == 0:
            raise ValueError
        authority = host + (f":{port}" if port is not None and port != 443 else "")
        if value != "https://" + authority:
            raise ValueError
        return value, authority
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("a canonical external HTTPS origin is required") from exc


def _header(scope: Scope, name: bytes) -> str | None:
    values = [value for key, value in scope.get("headers", []) if key.lower() == name]
    if len(values) > 1:
        raise AccessDenied("ambiguous_browser_headers")
    try:
        return values[0].decode("ascii") if values else None
    except UnicodeError as exc:
        raise AccessDenied("invalid_browser_header") from exc


def _cookie(scope: Scope, *, required: bool = True) -> str | None:
    # HTTP/2 may split the Cookie field. Recombine only that field; duplicate
    # session names still fail below, and other security headers stay singular.
    try:
        raw = "; ".join(value.decode("ascii") for key, value in scope.get("headers", [])
                        if key.lower() == b"cookie")
    except UnicodeError as exc:
        raise AccessDenied("invalid_browser_header") from exc
    values = []
    for item in raw.split(";"):
        name, _, value = item.strip().partition("=")
        if name == COOKIE_NAME:
            values.append(value)
    if not values and not required:
        return None
    if len(values) != 1 or not _COOKIE_TOKEN.fullmatch(values[0]):
        raise AccessDenied("invalid_session_cookie")
    return values[0]


def _response(data: dict, status: int = 200, *, clear_cookie: bool = False) -> JSONResponse:
    response = JSONResponse(data, status_code=status,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    if clear_cookie:
        response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")
    return response


async def _read_body(receive: Receive, limit: int) -> bytearray:
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise AccessDenied("invalid_browser_body")
        chunk = message.get("body", b"")
        if len(body) + len(chunk) > limit:
            raise AccessDenied("invalid_browser_body")
        body.extend(chunk)
        if not message.get("more_body", False):
            return body


async def _empty_json(receive: Receive) -> None:
    body = await _read_body(receive, _MAX_BODY)
    try:
        if json.loads(body) != {}:
            raise ValueError
    except (ValueError, RecursionError) as exc:
        raise AccessDenied("invalid_browser_body") from exc


async def _action_json(receive: Receive) -> dict:
    body = await _read_body(receive, _MAX_ACTION_BODY)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        value = json.loads(body, object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:
            raise ValueError
        return value
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise AccessDenied("invalid_browser_body") from exc


class _OwnerBrowser:
    def __init__(self, root: Path, verify_principal: PrincipalVerifier, expected_origin: str,
                 *, sampler=None, package=None):
        self.origin, self.authority = _origin(expected_origin)
        if not callable(verify_principal):
            raise ValueError("an explicit trusted principal verifier is required")
        self.verify_principal = verify_principal
        self.read_app = create_owner_app(root, verify_reader=self._reader,
                                         sampler=sampler, package=package)
        # Substitute only this protected module. The existing read gate still
        # verifies identity/session before headers and each response body.
        def owner_transport(request):
            return FileResponse(Path(__file__).with_name("ui") / "owner-api-client.js",
                media_type="text/javascript", headers={"Cache-Control": "no-store"})
        self.read_app.app.router.routes.insert(0, Route("/api-client.js", owner_transport,
                                                     methods=["GET", "HEAD"]))
        self.state = self.read_app.state
        self.access = self.read_app.access
        self.actions = OwnerActions(root, package=package)
        self._inflight = set()
        self._action_workers = 0

    async def _principal(self, scope: Scope) -> PrincipalRef:
        principal = await self.verify_principal(scope)
        if type(principal) is not PrincipalRef:
            raise AccessDenied("unverified_principal")
        return principal

    async def _reader(self, scope: Scope) -> VerifiedReader:
        return VerifiedReader(await self._principal(scope), _cookie(scope))

    def _boundary(self, scope: Scope) -> None:
        if _header(scope, b"host") != self.authority:
            raise AccessDenied("wrong_browser_host")
        origin = _header(scope, b"origin")
        if origin is not None and origin != self.origin:
            raise AccessDenied("wrong_browser_origin")

    def _status(self, principal: PrincipalRef, token: str | None) -> JSONResponse:
        grant = self.access.current_grant()
        if grant is None or not grant.active:
            return _response({"state": "needs_pairing"})
        if grant.principal != principal:
            raise AccessDenied("owner_not_paired")
        if token is not None:
            try:
                self.access.authorize_read(token, principal,
                    host_uid=read_host_uid(self.access.root / "state"))
                return _response({"state": "ready"})
            except AccessDenied:
                pass
        return _response({"state": "sign_in_required"})

    def _mutate(self, action: str, principal: PrincipalRef, token: str | None) -> JSONResponse:
        if action == "pair":
            challenge = self.access.begin_pairing(principal)
            return _response({"state": "awaiting_local_approval", "challenge": challenge.token,
                "principal": {"namespace": principal.namespace, "subject": principal.subject},
                "expires_at": challenge.expires_at, "expires_in": PAIRING_SECONDS}, 202)
        if action == "logout":
            if token is None:
                raise AccessDenied("sign_in_required")
            self.access.end_session(token, principal)
            return _response({"state": "signed_out"}, clear_cookie=True)
        if action == "renew":
            if token is None:
                raise AccessDenied("sign_in_required")
            session = self.access.renew_session(token, principal)
        elif token is not None:
            try:
                session = self.access.renew_session(token, principal)
            except AccessDenied as exc:
                if exc.code != "session_unavailable":
                    raise
                # Explicit sign-in can recover an expired/lost rotation using
                # the freshly verified principal and its current local grant.
                session = self.access.open_session(principal)
        else:
            session = self.access.open_session(principal)
        response = _response({"state": "ready", "expires_at": session.expires_at})
        response.set_cookie(COOKIE_NAME, session.token, max_age=SESSION_SECONDS,
                            path="/", secure=True, httponly=True, samesite="strict")
        return response

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.read_app(scope, receive, send)
            return
        try:
            self._boundary(scope)
        except AccessDenied:
            await _response({"state": "denied"}, 403)(scope, receive, send)
            return
        if scope["path"] in _ENTRY_ASSETS:
            try:
                response = await run_in_threadpool(_entry_response, scope)
            except OSError:
                response = _response({"state": "unavailable"}, 503)
            await response(scope, receive, send)
            return
        action = scope["path"].removeprefix(_PREFIX) if scope["path"].startswith(_PREFIX) else None
        if action not in _METHODS:
            # The read gate remains authoritative. Only its root-page 403
            # becomes a fixed entry redirect; API/asset errors and unavailable
            # authority keep their existing status and response lifecycle.
            if scope["path"] == "/" and scope["method"] in {"GET", "HEAD"}:
                redirected = False

                async def root_send(message):
                    nonlocal redirected
                    if redirected:
                        return  # discard every byte of the refused response
                    if (message["type"] == "http.response.start"
                            and message["status"] == 403):
                        redirected = True
                        await RedirectResponse("/owner", status_code=303, headers={
                            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                        })(scope, receive, send)
                    else:
                        await send(message)

                await self.read_app(scope, receive, root_send)
            else:
                await self.read_app(scope, receive, send)
            return
        action_result = None
        action_worker_started = False
        try:
            if scope["method"] != _METHODS[action]:
                response = _response({"state": "denied"}, 405)
                response.headers["Allow"] = _METHODS[action]
            else:
                if scope.get("query_string"):
                    raise AccessDenied("owner_query_refused")
                if scope["method"] == "POST":
                    if (_header(scope, b"origin") != self.origin
                            or _header(scope, b"x-claudlobby-owner") != "1"
                            or _header(scope, b"sec-fetch-site") not in {None, "same-origin"}
                            or (_header(scope, b"content-type") or "").split(";", 1)[0].lower()
                            != "application/json"):
                        raise AccessDenied("browser_intent_required")
                    payload = await asyncio.wait_for(
                        _action_json(receive) if action.startswith("actions/") else _empty_json(receive),
                        timeout=_BODY_SECONDS)
                principal = await self._principal(scope)
                try:
                    token = _cookie(scope, required=False)
                except AccessDenied:
                    if action != "login":
                        raise AccessDenied("sign_in_required") from None
                    token = None
                if action.startswith("actions/"):
                    if token is None:
                        raise AccessDenied("sign_in_required")
                    reader = VerifiedReader(principal, token)
                    operation = action.split("/", 1)[1]
                    fn = self.actions.context if operation == "context" else self.actions.operation
                    args = (reader, payload) if operation == "context" else (operation, reader, payload)
                    # Shield the worker lifetime: disconnect/cancellation does not
                    # erase a committed or native effect. UUID inspection recovers it.
                    if self._action_workers >= _MAX_ACTION_WORKERS:
                        raise AccessUnavailable("owner actions unavailable")
                    # Reservation and task creation contain no await: concurrent
                    # requests cannot race past the active-plus-queued ceiling.
                    self._action_workers += 1
                    work = run_in_threadpool(fn, *args)
                    try:
                        task = asyncio.create_task(work)
                    except BaseException:
                        work.close()
                        self._action_workers -= 1
                        raise
                    action_worker_started = True
                    self._inflight.add(task)
                    def finished(done):
                        self._inflight.discard(done)
                        self._action_workers -= 1
                        if not done.cancelled():
                            done.exception()
                    task.add_done_callback(finished)
                    action_result = await asyncio.shield(task)
                    response = _response(action_result)
                    response.headers["Cache-Control"] = "no-store, private"
                elif action == "status":
                    response = await run_in_threadpool(self._status, principal, token)
                else:
                    response = await run_in_threadpool(self._mutate, action, principal, token)
        except ActionNotStarted as exc:
            unavailable = isinstance(exc.refusal, (AccessUnavailable, OperationContextUnavailableError))
            response = _response({"state": "unavailable" if unavailable else "denied",
                                  "effect": "not_started"}, 503 if unavailable else 403)
        except AccessDenied as exc:
            # A delayed denial must not erase a newer cookie from a concurrent
            # successful sign-in or renewal. Only explicit logout deletes it.
            state = "sign_in_required" if exc.code == "sign_in_required" else "denied"
            response = _response({"state": state}, 403)
        except OperationContextUnavailableError:
            response = _response({"state": "unavailable"}, 503)
        except OperationContextError:
            response = _response({"state": "denied"}, 403)
        except (AccessUnavailable, ActivationError, OSError, ValueError, sqlite3.Error, asyncio.TimeoutError):
            response = _response({"state": "unavailable"}, 503)
        except Exception:
            if not action.startswith("actions/"):
                raise
            response = _response({"state": "unavailable"}, 503)
        if action_result is None:
            if action == "actions/send" and not action_worker_started and response.status_code in {403, 503}:
                # This invocation never reached an adapter; no claim about a
                # previous use of its UUID. Never mark post-worker failures.
                body = json.loads(response.body)
                body["effect"] = "not_started"
                response = _response(body, response.status_code)
            await response(scope, receive, send)
            return
        # Hold headers until current session/grant/source admission permits the
        # body. A slow or revoked operation cannot leak stale private metadata.
        pending_start = None
        async def action_send(message):
            nonlocal pending_start
            if message["type"] == "http.response.start":
                pending_start = message
                return
            try:
                await run_in_threadpool(self.actions.admit_response, operation, reader, action_result)
            except OperationContextUnavailableError:
                refusal = _response({"state": "unavailable"}, 503)
            except (AccessDenied, OperationContextError):
                refusal = _response({"state": "denied"}, 403)
            except (AccessUnavailable, ActivationError, OSError, ValueError, sqlite3.Error):
                refusal = _response({"state": "unavailable"}, 503)
            except Exception:
                refusal = _response({"state": "unavailable"}, 503)
            else:
                refusal = None
            if refusal is not None:
                await refusal(scope, receive, send)
                return
            await send(pending_start)
            await send(message)
        await response(scope, receive, action_send)


def create_owner_browser_app(root: Path, *, verify_principal: PrincipalVerifier,
                             expected_origin: str, sampler=None, package=None):
    """Internal same-origin lifecycle plus protected canonical reads.

    Never initializes authority or exposes local pairing confirmation, grants,
    revocation, or bot lifecycle actions. Ordinary messages require an explicit
    local grant and use the canonical durable request adapter. The verifier must establish a PrincipalRef from
    trusted ingress, not cookie/query/header claims accepted here. The external
    HTTPS origin is explicit even when the trusted proxy forwards local HTTP.
    """
    return _OwnerBrowser(root, verify_principal, expected_origin, sampler=sampler, package=package)
