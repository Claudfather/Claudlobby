"""Authenticated GitHub revocation events for the managed issuer.

Events only remove grants. Restoration requires a new operator approval or
browser consent; an added/unsuspended event never restores historical authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import sqlite3
import uuid

from .github_issuer import IssuanceDenied, IssuerUnavailable, _positive, _private_file


def _selector(app_id, event, payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid webhook object")
    action = payload.get("action")
    if event == "github_app_authorization" and action == "revoked":
        user = payload.get("sender", {}).get("id")
        if not _positive(user):
            raise ValueError("missing GitHub user")
        return {"kind": "user", "id": user}
    if (event, action) not in {
        ("installation", "deleted"), ("installation", "suspend"),
        ("installation_repositories", "removed"),
    }:
        return None
    installation = payload.get("installation", {})
    if not _positive(installation.get("id")) or installation.get("app_id") != app_id:
        raise ValueError("wrong GitHub installation")
    selected = {"kind": "installation", "id": installation["id"]}
    if event == "installation_repositories":
        removed = payload.get("repositories_removed")
        if not isinstance(removed, list) or any(
            not isinstance(repo, dict) or not _positive(repo.get("id")) for repo in removed
        ):
            raise ValueError("missing removed repositories")
        # GitHub sends an empty removal list when all -> selected narrows access.
        # Require fresh consent for that installation rather than guess survivors.
        if removed:
            selected.update(kind="repositories", repository_ids=sorted({repo["id"] for repo in removed}))
    return selected


class GithubWebhooks:
    def __init__(self, store, secrets):
        self.store = store
        self.secrets = {app_id: _private_file(path) for app_id, path in secrets.items()}
        if not self.secrets or any(not _positive(app_id) for app_id in self.secrets):
            raise ValueError("invalid webhook Apps")
        values = [path.read_bytes().strip() for path in self.secrets.values()]
        if len(set(values)) != len(values) or any(not 32 <= len(value) <= 1024 for value in values):
            raise ValueError("each App needs a distinct webhook secret of 32 to 1024 bytes")

    def receive(self, app_id, event, delivery_id, signature, raw):
        path = self.secrets.get(app_id)
        if path is None or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
            raise IssuanceDenied("webhook signature required")
        secret = _private_file(path).read_bytes().strip()
        if not 32 <= len(secret) <= 1024:
            raise IssuerUnavailable("webhook secret must contain 32 to 1024 bytes")
        expected = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise IssuanceDenied("webhook signature mismatch")
        if str(uuid.UUID(delivery_id)) != delivery_id:
            raise ValueError("invalid webhook delivery ID")
        # Header values are not signed; the signed body's schema must agree too.
        try:
            selector = _selector(app_id, event, json.loads(raw))
        except (AttributeError, TypeError, ValueError, RecursionError) as exc:
            raise ValueError("invalid GitHub event") from exc
        if selector is not None:
            digest = hashlib.sha256(event.encode() + b"\0" + raw).hexdigest()
            self.store.revoke_provider(app_id, delivery_id, digest, selector)

    def routes(self):
        from starlette.concurrency import run_in_threadpool
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def receive(request):
            headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
            try:
                def single(name):
                    values = request.headers.getlist(name)
                    if len(values) != 1:
                        raise IssuanceDenied("webhook header required")
                    return values[0]

                event = single("x-github-event")
                signature = single("x-hub-signature-256")
                delivery_id = single("x-github-delivery")
                if request.headers.get("origin") or single("content-type").split(";")[0] != "application/json":
                    raise IssuanceDenied("GitHub JSON delivery required")

                async def body():
                    raw = bytearray()
                    async for chunk in request.stream():
                        raw.extend(chunk)
                        if len(raw) > 2 * 1024 * 1024:
                            raise ValueError("webhook body too large")
                    return bytes(raw)

                raw = await asyncio.wait_for(body(), timeout=15)
                await run_in_threadpool(self.receive, request.path_params["app_id"], event, delivery_id, signature, raw)
                # Acknowledge only after durable commit; disclose no grant inventory.
                return JSONResponse({"received": True}, headers=headers)
            except IssuanceDenied:
                return JSONResponse({"error": "webhook_denied"}, status_code=403, headers=headers)
            except ValueError:
                return JSONResponse({"error": "invalid_webhook"}, status_code=400, headers=headers)
            except (IssuerUnavailable, OSError, sqlite3.Error, asyncio.TimeoutError):
                return JSONResponse({"error": "webhook_unavailable"}, status_code=503, headers=headers)

        return [Route("/v1/github/webhook/{app_id:int}", receive, methods=["POST"])]
