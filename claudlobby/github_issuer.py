"""Managed GitHub issuance, with issuer-owned grants and no operational replica.

Only the local operator can approve/revoke grants. The HTTP door can mint for an
exact approved binding; a customer's configuration is never its own authority.
The SQLite write transaction serializes issuance with confirmed revocation.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .github_connections import canonical_github_repo, REPOSITORY_PERMISSIONS


class IssuanceDenied(ValueError):
    """No current issuer-side authorization for this request."""


class IssuerUnavailable(RuntimeError):
    """Issuance could not be confirmed; no broader credential is substituted."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _positive(value):
    return type(value) is int and value > 0


def _binding(value):
    """Accept the composed wire shape, including its exact revision and identity."""
    required = {"connection_id", "attachment_id", "assignment_id", "bot_id", "project",
                "repo", "mode", "app_id", "installation_id", "permissions",
                "credential_refs", "revision", "slug", "bot_user_id"}
    if not isinstance(value, dict) or set(value) != required:
        raise IssuanceDenied("invalid managed binding")
    if value["mode"] != "managed" or not all(_positive(value[k]) for k in ("app_id", "installation_id")):
        raise IssuanceDenied("invalid managed binding")
    if not isinstance(value["revision"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["revision"]):
        raise IssuanceDenied("invalid managed revision")
    for key in ("connection_id", "attachment_id", "assignment_id", "bot_id", "project"):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 256:
            raise IssuanceDenied("invalid managed identity")
    if canonical_github_repo(value["repo"]) != value["repo"]:
        raise IssuanceDenied("repository must be canonical")
    perms = value["permissions"]
    if not isinstance(perms, dict) or not perms or any(
        k not in REPOSITORY_PERMISSIONS or v not in ("read", "write")
        or k == "metadata" and v != "read" for k, v in perms.items()
    ):
        raise IssuanceDenied("invalid repository permissions")
    refs = value["credential_refs"]
    if not isinstance(refs, dict) or set(refs) != {"issuer_url_env", "issuer_token_env"} or any(
        not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in refs.values()
    ):
        raise IssuanceDenied("invalid credential references")
    return value


def _credential_digest(secret):
    if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,256}", secret):
        raise IssuanceDenied("invalid issuer credential")
    return hashlib.sha256(secret.encode()).hexdigest()


def _private_file(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise IssuerUnavailable("issuer files must be private regular files owned by this user")
    return path


class GrantStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def transaction(self):
        _private_file(self.path)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def initialize(self):
        # The directory is also private: SQLite journals contain grant data.
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise IssuerUnavailable("use a private issuer directory (mode 0700)")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS github_grants (
                grant_id TEXT PRIMARY KEY, credential_digest TEXT NOT NULL,
                workspace_id TEXT NOT NULL, deployment_id TEXT NOT NULL,
                binding TEXT NOT NULL, repository_id INTEGER NOT NULL,
                expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                last_token_expires_at TEXT
            )""")

    def approve(self, grant, credential):
        expected = {"grant_id", "workspace_id", "deployment_id", "binding", "repository_id", "expires_at"}
        if not isinstance(grant, dict) or set(grant) != expected:
            raise IssuanceDenied("invalid issuer grant")
        for key in ("grant_id", "workspace_id", "deployment_id"):
            if not isinstance(grant[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", grant[key]):
                raise IssuanceDenied("invalid grant identity")
        if not _positive(grant["repository_id"]) or type(grant["expires_at"]) not in (int, float) or not time.time() < grant["expires_at"] < time.time() + 366 * 86400:
            raise IssuanceDenied("grant needs a repository ID and an expiry within one year")
        binding = _json(_binding(grant["binding"]))
        digest = _credential_digest(credential)
        self.initialize()
        with self.transaction() as db:
            # An issuer credential represents one deployment/workspace, even
            # when several independently approved bot bindings share it.
            if db.execute("SELECT 1 FROM github_grants WHERE credential_digest=? AND (workspace_id!=? OR deployment_id!=?)",
                          (digest, grant["workspace_id"], grant["deployment_id"])).fetchone():
                raise IssuanceDenied("credential already belongs to another deployment")
            try:
                db.execute("INSERT INTO github_grants (grant_id,credential_digest,workspace_id,deployment_id,binding,repository_id,expires_at) VALUES (?,?,?,?,?,?,?)",
                           (grant["grant_id"], digest, grant["workspace_id"], grant["deployment_id"], binding, grant["repository_id"], grant["expires_at"]))
            except sqlite3.IntegrityError as exc:
                raise IssuanceDenied("grant ID already exists; revoked grants cannot be restored") from exc
        return {"grant_id": grant["grant_id"], "new_issuance": "allowed", "expires_at": grant["expires_at"]}

    def revoke(self, grant_id):
        with self.transaction() as db:
            grant = db.execute("SELECT * FROM github_grants WHERE grant_id=?", (grant_id,)).fetchone()
            if grant is None:
                raise IssuanceDenied("unknown grant")
            db.execute("UPDATE github_grants SET revoked=1 WHERE grant_id=?", (grant_id,))
            result = {"grant_id": grant_id, "new_issuance": "blocked",
                      "host_enforcement": "unconfirmed", "residual_token_expires_at": grant["last_token_expires_at"]}
        return result

    def issue(self, credential, payload, mint):
        digest = _credential_digest(credential)
        if not isinstance(payload, dict) or set(payload) != {"schema", "binding", "revision"} or type(payload["schema"]) is not int or payload["schema"] != 1:
            raise IssuanceDenied("invalid issuance request")
        binding = _binding(payload["binding"])
        if payload["revision"] != binding["revision"]:
            raise IssuanceDenied("invalid issuance revision")
        with self.transaction() as db:
            grants = db.execute("SELECT * FROM github_grants WHERE credential_digest=? AND binding=? AND revoked=0 AND expires_at>?",
                                (digest, _json(binding), time.time())).fetchall()
            if len(grants) != 1:
                raise IssuanceDenied("no unique active grant for this binding")
            grant = grants[0]
            # Only issuer-owned fields reach the mint implementation.
            approved = json.loads(grant["binding"])
            result = mint(approved, grant["repository_id"])
            result = _token_result(approved, grant["repository_id"], result)
            if grant["expires_at"] <= time.time():
                raise IssuanceDenied("grant expired during issuance")
            db.execute("UPDATE github_grants SET last_token_expires_at=max(coalesce(last_token_expires_at,''),?) WHERE grant_id=?",
                       (result["expires_at"], grant["grant_id"]))
        return result


def _token_result(binding, repository_id, result):
    try:
        token, expires = result["token"], result["expires_at"]
        expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        repos, permissions = result["repositories"], result["permissions"]
        allowed = {**binding["permissions"], "metadata": "read"}
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError()
        if expiry.tzinfo is None or not 0 < expiry.timestamp() - time.time() <= 3600:
            raise ValueError()
        if not isinstance(repos, list) or len(repos) != 1 or type(repos[0]["id"]) is not int or repos[0]["id"] != repository_id or repos[0]["full_name"].lower() != binding["repo"]:
            raise ValueError()
        if not isinstance(permissions, dict) or any(allowed.get(k) != v for k, v in permissions.items()) or any(permissions.get(k) != v for k, v in binding["permissions"].items()):
            raise ValueError()
        return {"token": token, "expires_at": expiry.astimezone(timezone.utc).isoformat(),
                "repositories": [{"id": repository_id, "full_name": binding["repo"]}],
                "permissions": permissions, "revision": binding["revision"]}
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError) as exc:
        raise IssuerUnavailable("GitHub did not confirm the exact approved scope and expiry") from exc


class GithubMinter:
    def __init__(self, app_keys):
        self.app_keys = dict(app_keys)

    def __call__(self, binding, repository_id):
        import httpx
        import jwt

        try:
            key = _private_file(self.app_keys[binding["app_id"]]).read_bytes()
            now = int(time.time())
            assertion = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(binding["app_id"])}, key, algorithm="RS256")
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
                response = client.post(
                    f"https://api.github.com/app/installations/{binding['installation_id']}/access_tokens",
                    headers={"Authorization": "Bearer " + assertion, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                    json={"repository_ids": [repository_id], "permissions": binding["permissions"]})
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            # Provider errors can include sensitive context; never reflect them.
            raise IssuerUnavailable("GitHub issuance unavailable; check installation access and issuer configuration") from exc


def create_app(store, mint):
    import asyncio
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.concurrency import run_in_threadpool

    async def issue(request):
        try:
            size = request.headers.get("content-length", "")
            authorization = request.headers.getlist("authorization")
            if not size.isdecimal() or not 0 < int(size) <= 16384 or request.headers.get("transfer-encoding") or request.headers.get("origin") or len(authorization) != 1 or not authorization[0].startswith("Bearer "):
                raise IssuanceDenied("invalid issuance request")
            raw = await asyncio.wait_for(request.body(), timeout=15)
            if len(raw) != int(size):
                raise IssuanceDenied("invalid issuance request")
            result = await run_in_threadpool(store.issue, authorization[0][7:], json.loads(raw), mint)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except (IssuanceDenied, ValueError, TypeError):
            return JSONResponse({"error": "issuance_denied"}, status_code=403, headers={"Cache-Control": "no-store"})
        except (IssuerUnavailable, OSError, sqlite3.Error, asyncio.TimeoutError):
            return JSONResponse({"error": "issuer_unavailable"}, status_code=503, headers={"Cache-Control": "no-store"})

    return Starlette(routes=[Route("/v1/github/token", issue, methods=["POST"])])
