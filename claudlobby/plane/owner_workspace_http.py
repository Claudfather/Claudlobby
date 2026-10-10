"""Opt-in operational-origin bootstrap precursor; never issues read authority."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import time

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from .owner_access import AccessDenied, AccessUnavailable, PrincipalRef
from .owner_source import inspect_source
from .workspace_assertion import (AssertionRefused, AssertionUnavailable,
                                  _canonical_32_bytes, _origin, verify_workspace_assertion)

PATHS = frozenset({"/api/owner/workspace/challenge", "/api/owner/workspace/stage"})


class InvalidRequest(ValueError):
    """Only caller payload parsing/shape failure; never an operation outcome."""


@dataclass(frozen=True)
class WorkspaceBootstrapConfig:
    issuer: str
    operational_origin: str

    def __post_init__(self):
        if not _origin(self.issuer, False) or not _origin(self.operational_origin, False):
            raise ValueError("workspace issuer and operational origin require exact HTTPS origins")


def _header(scope, name):
    values = [value for key, value in scope.get("headers", ()) if key.lower() == name]
    if len(values) > 1:
        raise AccessDenied("workspace_bootstrap_denied")
    try:
        return values[0].decode("ascii") if values else None
    except UnicodeError:
        raise AccessDenied("workspace_bootstrap_denied") from None


async def _json(receive, limit):
    raw = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise InvalidRequest
        chunk = message.get("body", b"")
        if len(raw) + len(chunk) > limit:
            raise InvalidRequest
        raw.extend(chunk)
        if not message.get("more_body", False):
            break
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InvalidRequest
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(InvalidRequest()))
    except (ValueError, RecursionError, UnicodeError):
        raise InvalidRequest from None
    if type(result) is not dict:
        raise InvalidRequest
    return result


class WorkspaceBootstrapHTTP:
    def __init__(self, access, *, config, authority, verify_principal):
        from .owner_workspace_jwks import WorkspaceKeyset
        # Validate optional dependency readiness/schema without startup network
        # or initialization; current direct-owner path never constructs this.
        import jwt
        import httpx
        if type(config) is not WorkspaceBootstrapConfig:
            raise ValueError("explicit workspace bootstrap configuration is required")
        self.access = access
        self.workspace = access.workspace()
        self.workspace.status()
        self.config, self.authority, self.verify_principal = config, authority, verify_principal
        self.keys = WorkspaceKeyset(config.issuer)
        self._workers = set()

    async def _principal(self, scope, deadline):
        principal = await asyncio.wait_for(self.verify_principal(scope),
                                          max(0, deadline - time.monotonic()))
        if type(principal) is not PrincipalRef:
            raise AccessDenied("workspace_bootstrap_denied")
        return principal

    async def _blocking(self, fn, *args, **kwargs):
        # Caller cancellation must not release capacity while a DB thread is
        # still running. Owned workers are shielded below; retain this seam too.
        job = asyncio.create_task(run_in_threadpool(fn, *args, **kwargs))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            await job
            raise

    def _source(self):
        return inspect_source(self.access.root)

    async def _perform(self, scope, receive):
        deadline = time.monotonic() + 35
        is_stage = scope["path"].endswith("/stage")
        if scope["method"] == "OPTIONS":
            requested = _header(scope, b"access-control-request-headers") or ""
            fields = [field.strip().lower() for field in requested.split(",")]
            if (_header(scope, b"access-control-request-method") != "POST"
                    or len(fields) != 2 or set(fields) != {"content-type", "x-claudlobby-owner"}):
                raise AccessDenied("workspace_bootstrap_denied")
            principal = await self._principal(scope, deadline)
            owner = await self._blocking(self.access.current_grant)
            if (owner.principal != principal or principal.namespace != "tailscale:controlplane.tailscale.com"
                    or not principal.subject.isascii() or not principal.subject.isdecimal()
                    or int(principal.subject) <= 0 or str(int(principal.subject)) != principal.subject):
                raise AccessDenied("workspace_bootstrap_denied")
            await self._blocking(self._source)
            return None, 204
        if (_header(scope, b"x-claudlobby-owner") != "1"
                or (_header(scope, b"content-type") or "").split(";", 1)[0].lower() != "application/json"
                or _header(scope, b"cookie") is not None or _header(scope, b"authorization") is not None):
            raise AccessDenied("workspace_bootstrap_denied")
        payload = await asyncio.wait_for(_json(receive, 16384 if is_stage else 1024), 5)
        if is_stage:
            if (set(payload) != {"nonce", "assertion"} or not _canonical_32_bytes(payload["nonce"])
                    or not isinstance(payload["assertion"], str)
                    or not payload["assertion"].isascii() or not 1 <= len(payload["assertion"]) <= 8192):
                raise InvalidRequest
        elif payload:
            raise InvalidRequest
        principal = await self._principal(scope, deadline)
        await self._blocking(self._source)
        if is_stage:
            expected = await self._blocking(self.workspace.expected, payload["nonce"], principal)
            if (expected.issuer != self.config.issuer
                    or expected.operational_origin != self.config.operational_origin):
                raise AccessDenied("workspace_bootstrap_denied")
            keys = await self.keys.get(payload["assertion"])
            attestation = await self._blocking(verify_workspace_assertion, payload["assertion"],
                                               keys, expected=expected)
            principal = await self._principal(scope, deadline)
            await self._blocking(self._source)
            admitted = await self._blocking(self.workspace.stage, attestation, principal)
            body = {"version": 1, "state": "awaiting_local_approval", "expires_at": int(admitted.expires_at)}
            admission = self.workspace.admit_proposal
        else:
            admitted = await self._blocking(self.workspace.begin, principal,
                issuer=self.config.issuer, operational_origin=self.config.operational_origin)
            body = {"version": 1, "state": "awaiting_assertion", "issuer": admitted.issuer,
                    "host_uid": admitted.owner.host_uid, "nonce": admitted.nonce,
                    "operational_origin": admitted.operational_origin, "expires_at": int(admitted.expires_at)}
            admission = self.workspace.admit_challenge
        principal = await self._principal(scope, deadline)
        await self._blocking(self._source)
        await self._blocking(admission, admitted, principal)
        if time.monotonic() >= deadline:
            raise TimeoutError
        return body, 202

    async def _response(self, scope, receive):
        try:
            return await self._perform(scope, receive)
        except (AccessDenied, AssertionRefused):
            return {"state": "denied"}, 403
        except InvalidRequest:
            return {"state": "invalid_request"}, 400
        except Exception:
            # A staged proposal can survive a lost/denied response; this never
            # claims no effect or automatically retries/consumes it.
            return {"state": "unavailable"}, 503

    async def __call__(self, scope, receive, send):
        headers = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
                   "Vary": "Origin", "X-Content-Type-Options": "nosniff"}
        try:
            if (_header(scope, b"host") != self.authority or scope.get("query_string")
                    or _header(scope, b"origin") != self.config.operational_origin):
                raise AccessDenied("workspace_bootstrap_denied")
        except AccessDenied:
            await JSONResponse({"state": "denied"}, 403, headers=headers)(scope, receive, send)
            return
        headers["Access-Control-Allow-Origin"] = self.config.operational_origin
        if scope["method"] not in {"POST", "OPTIONS"}:
            headers["Allow"] = "POST, OPTIONS"
            body, status = {"state": "denied"}, 405
        elif len(self._workers) >= 8:
            body, status = {"state": "unavailable"}, 503
        else:
            # Reserve before body read/task creation. A disconnect/cancel only
            # abandons its response, never its still-running owned work/count.
            job = asyncio.create_task(self._response(scope, receive))
            self._workers.add(job)
            def done(task):
                self._workers.discard(task)
                if not task.cancelled():
                    task.exception()
            job.add_done_callback(done)
            body, status = await asyncio.shield(job)
        if status == 204:
            headers.update({"Access-Control-Allow-Methods": "POST",
                            "Access-Control-Allow-Headers": "Content-Type, X-Claudlobby-Owner",
                            "Vary": "Origin, Access-Control-Request-Method, Access-Control-Request-Headers"})
            from starlette.responses import Response
            await Response(status_code=204, headers=headers)(scope, receive, send)
        else:
            await JSONResponse(body, status, headers=headers)(scope, receive, send)
