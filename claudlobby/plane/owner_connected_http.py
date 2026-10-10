"""Opt-in direct connected reads; no cookie, action route or private-data proxy."""
import asyncio
import math
import time
from urllib.parse import parse_qsl

from starlette.responses import JSONResponse

from .owner_access import AccessDenied, AccessUnavailable, VerifiedReader
from .owner_workspace_http import WorkspaceBootstrapHTTP, InvalidRequest, _header, _json, _DEADLINE
from .owner_view import _ReadStopped
from .session_assertion import verify_session_assertion
from .workspace_assertion import _canonical_32_bytes

PREFIX = "/api/owner/connected/"
READ_SECONDS = 15
_METHODS = {"challenge": "POST", "issue": "POST", "status": "GET", "renew": "POST", "logout": "POST"}
_READS = {
    "summary": (), "overview": (), "fleets": (), "channel": ("fleet", "limit"),
    "tasks": ("fleet",), "identities": ("fleet",), "inventory": ("fleet",),
    "org": ("fleet",), "utilization": ("fleet",), "equipment": ("alias",),
    "search": ("q", "fleet", "limit"), "grid": ("fleet", "focus"),
    "presence": ("fleet",), "trust": (), "stream": ("cursor", "once"),
}


def _token(scope):
    value = _header(scope, b"authorization")
    if not value or not value.startswith("Bearer ") or not _canonical_32_bytes(value[7:]):
        raise AccessDenied("connected_session_denied")
    return value[7:]


class ConnectedReadHTTP(WorkspaceBootstrapHTTP):
    """Reuse bounded bootstrap work/key owners; delegate one canonical read app."""
    methods = frozenset({"GET", "POST", "OPTIONS"})

    def response_seconds(self, scope):
        return 35 if scope["method"] == "POST" and scope["path"] == PREFIX + "issue" else 15
    def __init__(self, access, *, config, authority, verify_principal, read_app):
        super().__init__(access, config=config, authority=authority, verify_principal=verify_principal)
        self.connected = access.connected()
        self.read_app = read_app.with_admission(self._reader, self._admit_reader)
        self._readers = set()

    async def shutdown(self):
        # The corrected bootstrap owns cancellation/drain of mutation workers.
        await super().shutdown()
        readers = tuple(self._readers)
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)

    def _boundary(self, scope):
        if (_header(scope, b"host") != self.authority
                or _header(scope, b"origin") != self.config.operational_origin
                or _header(scope, b"cookie") is not None):
            raise AccessDenied("connected_session_denied")

    def _binding_config(self, binding):
        if (binding.issuer != self.config.issuer
                or binding.operational_origin != self.config.operational_origin):
            raise AccessDenied("connected_session_denied")

    async def _reader(self, scope):
        token = _token(scope)
        principal = await self._read_principal(scope)
        return VerifiedReader(principal, token)

    async def _read_principal(self, scope):
        try:
            return await self._principal(scope, time.monotonic() + READ_SECONDS)
        except (AccessDenied, AccessUnavailable):
            raise
        except Exception:
            raise AccessUnavailable() from None

    async def _admit_reader(self, scope, reader, host_uid):
        token = _DEADLINE.set(time.monotonic() + READ_SECONDS)
        try:
            principal = await self._read_principal(scope)
            if principal != reader.principal:
                raise AccessDenied("connected_session_denied")
            admission = await self._blocking(self.connected.authorize, reader.token, principal,
                host_uid=host_uid, operational_origin=self.config.operational_origin)
            self._binding_config(admission.binding)
        except asyncio.TimeoutError:
            raise AccessUnavailable() from None
        finally:
            _DEADLINE.reset(token)

    def _session_source(self,principal):
        from .owner_setup import OwnerSetup
        try: return self._setup_source()
        except (AccessDenied,AccessUnavailable):
            # A partially applied canonical activation may leave an unbound
            # recorder. Only the locally granted original activation is recoverable;
            # read/action routes continue to require their recorder admission.
            return OwnerSetup(self.access).recovery_host(principal)

    async def _admission(self, token, principal):
        # A fresh source preflight precedes any session mutation.
        host_uid = await self._blocking(self._session_source,principal)
        admission = await self._blocking(self.connected.authorize, token, principal,
            host_uid=host_uid, operational_origin=self.config.operational_origin)
        self._binding_config(admission.binding)
        return admission

    def _ready(self, admission):
        binding = admission.binding
        now = self.access._now()
        if not admission.created_at <= now < admission.expires_at:
            raise AccessDenied("connected_session_denied")
        try:
            self._source()
            setup_only=False
        except (AccessDenied,AccessUnavailable):
            setup_only=True
        return {"version": 1, "state": "setup_only" if setup_only else "ready", "expires_in": max(0, math.floor(admission.expires_at - now)),
            ("setup_profile" if setup_only else "read_profile"): {"version": 1, "profile": "first-team-setup-v1" if setup_only else "direct-owner-read-v1", "host_uid": admission.owner.host_uid},
            "session_context": {"issuer": binding.issuer, "subject": binding.subject,
                "workspace_id": binding.workspace_id, "binding_revision": binding.revision,
                "binding_generation": binding.generation, "operational_origin": binding.operational_origin}}

    async def _perform(self, scope, receive):
        deadline = _DEADLINE.get()
        action = scope["path"][len(PREFIX):]
        if scope["method"] == "OPTIONS":
            method, allowed = self._preflight_fields(scope, action)
            await self._preflight(scope, method, allowed)
            return None, 204
        if action in {"challenge", "issue"}:
            if _header(scope, b"authorization") is not None:
                raise AccessDenied("connected_session_denied")
        else:
            token = _token(scope)
        principal = await self._principal(scope, deadline)
        host_uid = await self._blocking(self._session_source,principal)
        if action != "status":
            if (_header(scope, b"x-claudlobby-owner") != "1"
                    or (_header(scope, b"content-type") or "").split(";", 1)[0].lower() != "application/json"):
                raise AccessDenied("connected_session_denied")
            payload = await asyncio.wait_for(_json(receive, 16384 if action == "issue" else 1024), 5)
            if action == "issue":
                if (set(payload) != {"nonce", "assertion"} or not _canonical_32_bytes(payload["nonce"])
                        or not isinstance(payload["assertion"], str) or not payload["assertion"].isascii()
                        or not 1 <= len(payload["assertion"]) <= 8192):
                    raise InvalidRequest
            elif payload:
                raise InvalidRequest
        if action == "challenge":
            # Refuse mismatched configuration before inserting a challenge.
            with_binding = await self._blocking(self.workspace.active_binding, principal)
            self._binding_config(with_binding)
            challenge = await self._blocking(self.connected.begin, principal)
            e = challenge.expected
            self._binding_config(e)
            body = {"version": 1, "state": "awaiting_session_assertion", "nonce": e.nonce,
                "issuer": e.issuer, "host_uid": e.host_uid, "operational_origin": e.operational_origin,
                "binding_revision": e.binding_revision, "binding_generation": e.binding_generation,
                "expires_at": math.floor(challenge.expires_at)}
            principal = await self._principal(scope, deadline)
            await self._blocking(self._session_source,principal)
            await self._blocking(self.connected.admit_challenge, challenge, principal)
            return body, 202
        if action == "issue":
            expected = await self._blocking(self.connected.expected, payload["nonce"], principal)
            self._binding_config(expected)
            keys = await self.keys.get(payload["assertion"])
            attestation = await self._blocking(verify_session_assertion, payload["assertion"], keys, expected=expected)
            principal = await self._principal(scope, deadline)
            await self._blocking(self._session_source,principal)
            session = await self._blocking(self.connected.issue, attestation, principal)
            token = session.token
        else:
            admission = await self._admission(token, principal)
            if action == "logout":
                await self._blocking(self.connected.end, token, principal, host_uid=host_uid,
                                     operational_origin=self.config.operational_origin)
                # Final current owner/binding check, without requiring the deleted token.
                principal = await self._principal(scope, deadline)
                await self._blocking(self._session_source,principal)
                binding = await self._blocking(self.workspace.active_binding, principal)
                if binding != admission.binding:
                    raise AccessDenied("connected_session_denied")
                return {"version": 1, "state": "signed_out"}, 200
            if action == "renew":
                session = await self._blocking(self.connected.renew, token, principal, host_uid=host_uid,
                                              operational_origin=self.config.operational_origin)
                token = session.token
        principal = await self._principal(scope, deadline)
        admission = await self._admission(token, principal)
        body = await self._blocking(self._ready,admission)
        if action in {"issue", "renew"}:
            body["token"] = token
        return body, 200

    async def _preflight(self, scope, method, headers):
        requested = _header(scope, b"access-control-request-headers") or ""
        fields = [x.strip().lower() for x in requested.split(",") if x.strip()]
        if (_header(scope, b"authorization") is not None
                or _header(scope, b"access-control-request-method") != method
                or len(fields) != len(set(fields)) or set(fields) != set(headers)):
            raise AccessDenied("connected_session_denied")
        token = _DEADLINE.set(time.monotonic() + READ_SECONDS)
        try:
            principal = await self._read_principal(scope)
            if scope["path"].startswith(PREFIX+"read/"):
                await self._blocking(self._source)
            else:
                await self._blocking(self._session_source,principal)
            binding = await self._blocking(self.workspace.active_binding, principal)
            self._binding_config(binding)
        except asyncio.TimeoutError:
            raise AccessUnavailable() from None
        finally:
            _DEADLINE.reset(token)

    @staticmethod
    def _preflight_fields(scope, action):
        is_read = action.startswith("read/")
        suffix = action[5:] if is_read else None
        method = "GET" if is_read else _METHODS[action]
        allowed = (["authorization"] if is_read or action == "status" else
            ["content-type", "x-claudlobby-owner"] + ([] if action in {"challenge", "issue"} else ["authorization"]))
        if suffix == "stream" and "last-event-id" in (_header(scope, b"access-control-request-headers") or "").lower():
            allowed += ["last-event-id"]
        if is_read and _header(scope, b"access-control-request-method") == "HEAD":
            method = "HEAD"
        return method, allowed

    @staticmethod
    def _read_route(scope, suffix):
        if suffix.startswith("tasks/"):
            from .ids import ID_PATTERNS
            import re
            if not re.fullmatch(ID_PATTERNS["work_item"], suffix[6:]):
                raise InvalidRequest
            fields = ("fleet",)
        elif suffix in _READS:
            fields = _READS[suffix]
        else:
            raise InvalidRequest
        try:
            raw = scope.get("query_string", b"").decode("ascii")
            query = parse_qsl(raw, keep_blank_values=True, strict_parsing=True) if raw else []
        except (ValueError, UnicodeError):
            raise InvalidRequest from None
        if len(query) != len({k for k, _ in query}) or any(k not in fields for k, _ in query):
            raise InvalidRequest

    async def __call__(self, scope, receive, send):
        if scope["method"] == "HEAD":
            downstream = send
            async def send(message):
                if message["type"] == "http.response.body":
                    message = {**message, "body": b""}
                await downstream(message)
        headers = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
                   "Vary": "Origin", "X-Content-Type-Options": "nosniff"}
        try:
            self._boundary(scope)
        except AccessDenied:
            await JSONResponse({"state": "denied"}, 403, headers=headers)(scope, receive, send)
            return
        headers["Access-Control-Allow-Origin"] = self.config.operational_origin
        action = scope["path"][len(PREFIX):]
        is_read = action.startswith("read/")
        suffix = action[5:] if is_read else None
        method = "GET" if is_read else _METHODS.get(action)
        try:
            if method is None or (not is_read and scope.get("query_string")):
                raise InvalidRequest
            if is_read:
                self._read_route(scope, suffix)
            if self._stopping:
                await JSONResponse({"state": "unavailable"}, 503, headers=headers)(scope, receive, send)
                return
            started = asyncio.Event()
            head = is_read and scope["method"] == "HEAD"
            async def cors_send(message):
                if message["type"] == "http.response.start":
                    names = {key.lower().encode() for key in headers}
                    message = {**message, "headers": [(k, v) for k, v in message.get("headers", [])
                        if k.lower() not in names] + [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
                    started.set()
                if head and suffix == "stream" and message["type"] == "http.response.body" and message.get("more_body"):
                    # The canonical gate has already admitted this first chunk.
                    # Finish HEAD now and unwind its producer through that gate,
                    # preserving original GET query validation without an idle tail.
                    await send({**message, "body": b"", "more_body": False})
                    raise _ReadStopped()
                await send(message)
            if scope["method"] == "OPTIONS":
                method, allowed = self._preflight_fields(scope, action)
                headers.update({"Access-Control-Allow-Methods": method,
                    "Access-Control-Allow-Headers": ", ".join(allowed),
                    "Vary": "Origin, Access-Control-Request-Method, Access-Control-Request-Headers"})
                # Read query shape was already checked; no query enters OPTIONS
                # authority. Reuse the same owned deadline/cancel/drain machinery.
                await super().__call__({**scope, "query_string": b""}, receive, cors_send)
            elif scope["method"] != method and not (is_read and scope["method"] == "HEAD"):
                await JSONResponse({"state": "denied"}, 405,
                    headers={**headers, "Allow": "GET, HEAD, OPTIONS" if is_read else method + ", OPTIONS"})(scope, receive, send)
            elif is_read:
                # Canonical API routes are GET-only. HEAD traverses their exact
                # query/source/response gates, with only the outgoing body removed.
                inner = {**scope, "method": "GET", "path": "/api/" + suffix,
                         "raw_path": ("/api/" + suffix).encode()}
                job = asyncio.create_task(self.read_app(inner, receive, cors_send))
                self._readers.add(job)
                try:
                    if suffix == "stream":
                        ready = asyncio.create_task(started.wait())
                        try:
                            done, _ = await asyncio.wait({job, ready}, timeout=READ_SECONDS,
                                                         return_when=asyncio.FIRST_COMPLETED)
                            if not done:
                                raise asyncio.TimeoutError
                        finally:
                            ready.cancel()
                            await asyncio.gather(ready, return_exceptions=True)
                        # Observe child completion without forwarding cancellation
                        # from the request into it. A healthy stream has no idle deadline.
                        await asyncio.wait({job})
                    else:
                        done, _ = await asyncio.wait({job}, timeout=READ_SECONDS)
                        if not done:
                            raise asyncio.TimeoutError
                    if job.cancelled():
                        if not self._stopping:
                            raise asyncio.CancelledError
                        if not started.is_set():
                            await JSONResponse({"state": "unavailable"}, 503, headers=headers)(scope, receive, send)
                    else:
                        job.result()
                except asyncio.TimeoutError:
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
                    if not started.is_set():
                        await JSONResponse({"state": "unavailable"}, 503, headers=headers)(scope, receive, send)
                    # Started finite responses remain incomplete, never replaced.
                finally:
                    if not job.done():
                        job.cancel()
                        await asyncio.gather(job, return_exceptions=True)
                    self._readers.discard(job)
            else:
                # Delegate the corrected bootstrap deadline/drain machinery.
                await super().__call__(scope, receive, send)
        except AccessDenied:
            await JSONResponse({"state": "denied"}, 403, headers=headers)(scope, receive, send)
        except InvalidRequest:
            await JSONResponse({"state": "invalid_request"}, 400, headers=headers)(scope, receive, send)
        except AccessUnavailable:
            await JSONResponse({"state": "unavailable"}, 503, headers=headers)(scope, receive, send)
