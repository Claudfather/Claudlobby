"""Account-bound staging of managed GitHub consent; never direct grant approval.

The host retains its random issuer credential. Only its digest and the exact
requested binding reach this service. Account proof and GitHub consent are
separate checks; neither claims physical ownership of a caller-named host.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time

from .github_issuer import IssuanceDenied, IssuerUnavailable, _json, _credential_digest
from .plane.workspace_assertion import (
    AssertionRefused, AssertionUnavailable, _canonical_uuid, _canonical_32_bytes,
    _HOST, _KID, _origin, _public_keys, _unique_object,
)
from .plane.owner_workspace_jwks import WorkspaceKeyset

PURPOSE = "claudfather-github-enrollment-v1"
CLAIMS = frozenset({"iss", "sub", "workspace_id", "host_uid", "nonce", "operational_origin",
                    "binding_revision", "binding_generation", "aud", "purpose", "iat", "nbf",
                    "exp", "jti", "request_digest", "credential_digest"})


class EnrollmentRestartRequired(IssuanceDenied):
    """The retained invitation cannot be resumed; start a new request."""


def prepare_request(*, request_id, workspace_id, host_uid, nonce, binding, repository_id, credential):
    """Nonsecret enrollment input for the locally approved host setup owner."""
    return {"request_id": request_id, "workspace_id": workspace_id, "host_uid": host_uid,
            "nonce": nonce, "binding": binding, "repository_id": repository_id,
            "credential_digest": _credential_digest(credential)}


def request_digest(request):
    return hashlib.sha256(_json(request).encode()).hexdigest()


class GithubEnrollment:
    def __init__(self, consent, config):
        if not isinstance(config, dict) or set(config) != {"website_origin", "operational_origin"} or any(
            not _origin(value, False) for value in config.values()
        ):
            raise ValueError("invalid enrollment configuration")
        self.consent, self.store = consent, consent.store
        self.config = dict(config)
        self.keys = WorkspaceKeyset(config["website_origin"])

    def verify(self, assertion, jwks, request):
        if not isinstance(assertion, str) or not assertion.isascii() or not 1 <= len(assertion) <= 8192:
            raise AssertionRefused()
        try:
            import jwt
        except ImportError:
            raise AssertionUnavailable() from None
        keys = _public_keys(jwks, jwt)
        try:
            header = jwt.get_unverified_header(assertion)
            if (set(header) - {"alg", "kid", "typ"} or header.get("alg") != "EdDSA"
                    or header.get("typ", "JWT") != "JWT" or not isinstance(header.get("kid"), str)
                    or not _KID.fullmatch(header["kid"])):
                raise AssertionRefused()
            key = keys.get(header["kid"])
            if key is None:
                raise AssertionUnavailable()
            claims = jwt.decode_complete(assertion, key, algorithms=["EdDSA"],
                issuer=self.config["website_origin"],
                audience=f"urn:claudfather:github-enrollment:{self.consent.provider.origin}:v1",
                options={"require": sorted(CLAIMS), "strict_aud": True}, leeway=5)["payload"]
            expected = {"purpose": PURPOSE, "workspace_id": request["workspace_id"],
                        "host_uid": request["host_uid"], "nonce": request["nonce"],
                        "operational_origin": self.config["operational_origin"],
                        "request_digest": request_digest(request), "credential_digest": request["credential_digest"]}
            if (set(claims) != CLAIMS or any(claims[k] != v for k, v in expected.items())
                    or not all(_canonical_uuid(claims[k]) for k in ("sub", "jti", "binding_generation"))
                    or type(claims["binding_revision"]) is not int or not 0 < claims["binding_revision"] < 2**53
                    or not all(type(claims[k]) is int and 0 < claims[k] < 2**53 for k in ("iat", "nbf", "exp"))
                    or claims["nbf"] != claims["iat"] or not 0 < claims["exp"] - claims["iat"] <= 120):
                raise AssertionRefused()
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, RecursionError):
            raise AssertionRefused() from None

    def enroll(self, request, assertion, jwks):
        fields = {"request_id", "workspace_id", "host_uid", "nonce", "binding", "repository_id", "credential_digest"}
        if (not isinstance(request, dict) or set(request) != fields
                or not all(_canonical_uuid(request[k]) for k in ("request_id", "workspace_id"))
                or not isinstance(request["host_uid"], str) or not _HOST.fullmatch(request["host_uid"])
                or not _canonical_32_bytes(request["nonce"])
                or not isinstance(request["credential_digest"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", request["credential_digest"])):
            raise IssuanceDenied("invalid enrollment request")
        request = json.loads(_json(request))
        grant = self.store.validate_grant({"grant_id": "enrollment-" + request["request_id"],
            "workspace_id": request["workspace_id"], "deployment_id": request["host_uid"],
            "binding": request["binding"], "repository_id": request["repository_id"],
            "expires_at": time.time() + 30 * 86400})
        if grant["binding"]["app_id"] != self.consent.provider.app_id:
            raise IssuanceDenied("different managed App")
        claims = self.verify(assertion, jwks, request)
        digest = request_digest(request)
        with self.store.transaction() as db:
            # Signature checking may wait behind an in-flight mint; readmit time
            # while holding the same transaction that reserves this request.
            if time.time() >= claims["exp"]:
                raise AssertionRefused()
            prior = db.execute("SELECT * FROM github_enrollments WHERE request_id=?", (request["request_id"],)).fetchone()
            if prior is not None:
                if (prior["request_digest"] != digest or prior["subject"] != claims["sub"]
                        or prior["issuer"] != claims["iss"]):
                    raise IssuanceDenied("enrollment request changed")
                result = json.loads(prior["result"])
                invitation = db.execute("SELECT status,expires_at FROM github_consent WHERE ticket=?", (result["ticket"],)).fetchone()
                if (invitation is None or invitation["expires_at"] <= time.time()
                        or invitation["status"] in {"cancelled", "needs_operator"}):
                    raise EnrollmentRestartRequired("start a new enrollment")
                return result
            pending = db.execute("""SELECT count(*) FROM github_consent WHERE expires_at>?
                AND status IN ('invited','authorizing','checking')
                AND json_extract(grant_json,'$.workspace_id')=?""", (time.time(), request["workspace_id"])).fetchone()[0]
            if pending >= 16:
                raise IssuerUnavailable("too many pending enrollments")
            result = self.consent.invite_in_transaction(db, grant, request["credential_digest"])
            db.execute("INSERT INTO github_enrollments (request_id,request_digest,issuer,subject,result) VALUES (?,?,?,?,?)",
                       (request["request_id"], digest, claims["iss"], claims["sub"], _json(result)))
            return result

    def routes(self):
        from starlette.concurrency import run_in_threadpool
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def enroll(request):
            headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
            try:
                if request.headers.get("origin") or request.headers.get("content-type", "").split(";")[0] != "application/json":
                    raise IssuanceDenied("host JSON request required")
                async def body():
                    raw = bytearray()
                    async for chunk in request.stream():
                        raw.extend(chunk)
                        if len(raw) > 32768:
                            raise ValueError("enrollment too large")
                    return bytes(raw)
                payload = json.loads(await asyncio.wait_for(body(), 5), object_pairs_hook=_unique_object)
                if not isinstance(payload, dict) or set(payload) != {"request", "assertion"}:
                    raise ValueError("invalid enrollment")
                keys = await self.keys.get(payload["assertion"])
                result = await run_in_threadpool(self.enroll, payload["request"], payload["assertion"], keys)
                return JSONResponse(result, headers=headers)
            except EnrollmentRestartRequired:
                return JSONResponse({"error": "enrollment_restart_required"}, status_code=409, headers=headers)
            except (IssuanceDenied, AssertionRefused, ValueError, TypeError, RecursionError):
                return JSONResponse({"error": "enrollment_denied"}, status_code=403, headers=headers)
            except (IssuerUnavailable, AssertionUnavailable, OSError, sqlite3.Error, asyncio.TimeoutError):
                return JSONResponse({"error": "enrollment_unavailable"}, status_code=503, headers=headers)

        return [Route("/v1/github/enroll", enroll, methods=["POST"])]
