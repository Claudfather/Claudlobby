"""Internal direct-host browser session transport; no runtime entry point.

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
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from .ids import read_host_uid
from .owner_access import AccessDenied, AccessUnavailable, PrincipalRef, PAIRING_SECONDS, SESSION_SECONDS
from .owner_view import VerifiedReader, create_owner_app

COOKIE_NAME = "__Host-claudlobby-owner"
_COOKIE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_MAX_BODY = 1024
_BODY_SECONDS = 5
_PREFIX = "/api/owner/"
_METHODS = {"status": "GET", "pair": "POST", "login": "POST",
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


async def _empty_json(receive: Receive) -> None:
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise AccessDenied("invalid_browser_body")
        chunk = message.get("body", b"")
        if len(body) + len(chunk) > _MAX_BODY:
            raise AccessDenied("invalid_browser_body")
        body.extend(chunk)
        if not message.get("more_body", False):
            break
    try:
        if json.loads(body) != {}:
            raise ValueError
    except (ValueError, RecursionError) as exc:
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
        self.state = self.read_app.state
        self.access = self.read_app.access

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
            # Preserve the existing gate's streaming/error lifecycle, including
            # failures after headers. Never turn those into a second response.
            await self.read_app(scope, receive, send)
            return
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
                    await asyncio.wait_for(_empty_json(receive), timeout=_BODY_SECONDS)
                principal = await self._principal(scope)
                try:
                    token = _cookie(scope, required=False)
                except AccessDenied:
                    if action != "login":
                        raise AccessDenied("sign_in_required") from None
                    token = None
                if action == "status":
                    response = await run_in_threadpool(self._status, principal, token)
                else:
                    response = await run_in_threadpool(self._mutate, action, principal, token)
        except AccessDenied as exc:
            # A delayed denial must not erase a newer cookie from a concurrent
            # successful sign-in or renewal. Only explicit logout deletes it.
            state = "sign_in_required" if exc.code == "sign_in_required" else "denied"
            response = _response({"state": state}, 403)
        except (AccessUnavailable, OSError, ValueError, asyncio.TimeoutError):
            response = _response({"state": "unavailable"}, 503)
        await response(scope, receive, send)


def create_owner_browser_app(root: Path, *, verify_principal: PrincipalVerifier,
                             expected_origin: str, sampler=None, package=None):
    """Internal same-origin lifecycle plus protected canonical reads.

    Never initializes authority or exposes local pairing confirmation, grants,
    revocation, or bot actions. The verifier must establish a PrincipalRef from
    trusted ingress, not cookie/query/header claims accepted here. The external
    HTTPS origin is explicit even when the trusted proxy forwards local HTTP.
    """
    return _OwnerBrowser(root, verify_principal, expected_origin, sampler=sampler, package=package)
