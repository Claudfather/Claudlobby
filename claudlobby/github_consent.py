"""Browser GitHub consent for an explicitly staged deployment grant.

An operator or the account enrollment owner stages the requested scope.
GitHub separately verifies the consenting repository admin.
OAuth tokens stay in memory and are never used to perform repository writes.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import base64
from email.utils import formatdate
import hashlib
from html import escape
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .github_issuer import (
    GrantConflict,
    GrantStore,
    IssuanceDenied,
    IssuerUnavailable,
    _credential_digest,
    _json,
    _positive,
    _private_file,
)
from .plane.workspace_assertion import _origin

COOKIE = "__Host-claudlobby-github"
CONNECT = "/v1/github/connect"
CALLBACK = "/v1/github/callback"
SELECT = "/v1/github/repositories"


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise IssuanceDenied("invalid consent reference")
    return hashlib.sha256(value.encode()).hexdigest()


def _cookie_name(ticket):
    # Several bot/host invitations can be open in the same browser. Each
    # OAuth state still requires its own invitation's HttpOnly browser secret.
    return COOKIE + "-" + _digest(ticket)


class GithubConsentProvider:
    def __init__(self, config):
        if not isinstance(config, dict) or set(config) != {
            "app_id",
            "client_id",
            "client_secret_path",
            "origin",
        }:
            raise ValueError("invalid consent configuration")
        if (
            not _positive(config["app_id"])
            or not isinstance(config["client_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", config["client_id"])
            or not _origin(config["origin"], False)
        ):
            raise ValueError("invalid consent configuration")
        self.config = dict(config)
        self.origin = config["origin"]
        self.app_id = config["app_id"]
        _private_file(config["client_secret_path"])

    def authorize_url(self, state, verifier):
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return "https://github.com/login/oauth/authorize?" + urlencode(
            {
                "client_id": self.config["client_id"],
                "redirect_uri": self.origin + CALLBACK,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
                "allow_signup": "false",
            }
        )

    @contextmanager
    def user(self, code, verifier, repository_id=None):
        """A temporary provider session; no access or refresh token is retained."""
        import httpx

        try:
            secret = _private_file(self.config["client_secret_path"]).read_text().strip()
            if not secret or len(secret) > 1024 or any(c.isspace() for c in secret):
                raise ValueError()
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=15) as client:
                data = {"client_id": self.config["client_id"], "client_secret": secret,
                        "redirect_uri": self.origin + CALLBACK, "code": code, "code_verifier": verifier}
                if repository_id is not None:
                    data["repository_id"] = repository_id
                response = client.post("https://github.com/login/oauth/access_token",
                    headers={"Accept": "application/json"}, data=data)
                response.raise_for_status()
                token = response.json().get("access_token")
                if not isinstance(token, str) or not token.startswith("ghu_") or any(c.isspace() for c in token):
                    raise IssuanceDenied("GitHub consent was not confirmed")
                headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                           "X-GitHub-Api-Version": "2022-11-28"}
                deadline = time.monotonic() + 60
                def get(path):
                    if time.monotonic() >= deadline:
                        raise IssuerUnavailable("GitHub lookup timed out")
                    result = client.get("https://api.github.com" + path, headers=headers)
                    result.raise_for_status()
                    if len(result.content) > 2 * 1024 * 1024:
                        raise IssuerUnavailable("GitHub response is oversized")
                    return result.json()
                yield get
        except IssuanceDenied:
            raise
        except Exception as exc:
            raise IssuerUnavailable("GitHub access could not be verified") from exc

    @staticmethod
    def rows(get, path, key):
        # Fixed API paths/pages, never provider-supplied next URLs.
        for page in range(1, 101):
            rows = get(f"{path}?per_page=100&page={page}")[key]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise IssuerUnavailable("invalid GitHub listing")
            yield from rows
            if len(rows) < 100:
                return
        raise IssuerUnavailable("GitHub listing exceeded the lookup limit")

    def find(self, get, path, key, identifier):
        for row in self.rows(get, path, key):
            if type(row.get("id")) is int and row["id"] == identifier:
                return row
        raise IssuanceDenied("selected GitHub resource unavailable")

    def verify(self, code, verifier, grant):
        binding = grant["binding"]
        if binding["app_id"] != self.app_id:
            raise IssuanceDenied("different managed App")
        with self.user(code, verifier, grant["repository_id"]) as get:
            user_id = get("/user").get("id")
            installation = self.find(get, "/user/installations", "installations", binding["installation_id"])
            if (type(installation.get("app_id")) is not int or installation["app_id"] != self.app_id
                    or installation.get("suspended_at") is not None):
                raise IssuanceDenied("installation unavailable")
            permissions = installation.get("permissions", {})
            if any(permissions.get(key) not in (("write",) if level == "write" else ("read", "write"))
                   for key, level in binding["permissions"].items()):
                raise IssuanceDenied("installation permissions are insufficient")
            repository = self.find(get, f"/user/installations/{binding['installation_id']}/repositories",
                                   "repositories", grant["repository_id"])
            current = get("/repos/" + binding["repo"])
            if (not _positive(user_id) or type(current.get("id")) is not int
                    or current["id"] != grant["repository_id"]
                    or repository.get("full_name", "").lower() != binding["repo"]
                    or current.get("full_name", "").lower() != binding["repo"]
                    or current.get("permissions", {}).get("admin") is not True):
                raise IssuanceDenied("repository administrator approval required")
            return user_id

    def resolve_repository(self, code, verifier, repository):
        from .github_selection import repository_input, selection, PERMISSIONS
        repository = repository_input(repository)
        with self.user(code, verifier) as get:
            current = get("/repos/" + repository)
            if (not _positive(current.get("id")) or current.get("full_name", "").lower() != repository
                    or current.get("permissions", {}).get("admin") is not True):
                raise IssuanceDenied("repository administrator approval required")
            for installation in self.rows(get, "/user/installations", "installations"):
                if (installation.get("app_id") != self.app_id or not _positive(installation.get("id"))
                        or installation.get("suspended_at") is not None
                        or any(installation.get("permissions", {}).get(k) not in
                               (("write",) if level == "write" else ("read", "write")) for k,level in PERMISSIONS.items())):
                    continue
                for row in self.rows(get, f"/user/installations/{installation['id']}/repositories", "repositories"):
                    if row.get("id") == current["id"] and row.get("full_name", "").lower() == repository:
                        return selection({"version":1,"issuer_origin":self.origin,"app_id":self.app_id,
                            "installation_id":installation["id"],"repository":repository,
                            "repository_id":current["id"],"permissions":dict(PERMISSIONS)})
            raise IssuanceDenied("install the managed App on this repository with the required permissions")


class ConsentFlow:
    def __init__(self, store, provider):
        self.store, self.provider = store, provider

    def invite_selection(self):
        """Public discovery starts no grant and retains no provider credential."""
        ticket = secrets.token_urlsafe(32)
        with self.store.transaction() as db:
            db.execute("DELETE FROM github_consent WHERE json_extract(grant_json,'$.kind')='repository_selection' AND expires_at<=?", (time.time(),))
            if db.execute("SELECT count(*) FROM github_consent WHERE json_extract(grant_json,'$.kind')='repository_selection'").fetchone()[0] >= 128:
                raise IssuerUnavailable("repository selection is busy")
            db.execute("INSERT INTO github_consent (ticket,grant_json,credential_digest,expires_at,status) VALUES (?,?,?,?, 'invited')",
                (ticket, _json({"kind":"repository_selection"}), "selection-only", time.time()+600))
        return self.provider.origin + CONNECT + "?ticket=" + ticket

    def selection_result(self, ticket, cookie):
        _digest(ticket)
        with self.store.transaction() as db:
            row = db.execute("SELECT grant_json FROM github_consent WHERE ticket=? AND browser_digest=? AND status='selected' AND expires_at>?",
                (ticket, _digest(cookie), time.time())).fetchone()
            if row is None:
                raise IssuanceDenied("selection unavailable")
            from .github_selection import selection
            return selection(json.loads(row["grant_json"])["result"])

    def invite(self, grant, credential):
        grant = self.store.validate_grant(grant)
        if grant["binding"]["app_id"] != self.provider.app_id:
            raise IssuanceDenied("different managed App")
        digest = _credential_digest(credential)
        self.store.initialize()
        with self.store.transaction() as db:
            return self.invite_in_transaction(db, grant, digest)

    def invite_in_transaction(self, db, grant, digest):
        """Shared staging only; callers establish operator/account authority."""
        ticket = secrets.token_urlsafe(32)
        self.store.check_grant_available(db, grant, digest)
        db.execute(
            "INSERT INTO github_consent (ticket,grant_json,credential_digest,expires_at,status) VALUES (?,?,?,?, 'invited')",
            (ticket, _json(grant), digest, min(time.time() + 900, grant["expires_at"])),
        )
        return {
            "authorization_url": self.provider.origin + CONNECT + "?ticket=" + ticket,
            "ticket": ticket,
            "grant_id": grant["grant_id"],
            "new_issuance": "not_allowed",
        }

    def cancel(self, ticket):
        _digest(ticket)
        with self.store.transaction() as db:
            result = db.execute(
                "UPDATE github_consent SET status='cancelled',browser_digest=NULL,state_digest=NULL,verifier=NULL,grant_json=json_remove(grant_json,'$.result') WHERE ticket=? AND status!='approved'",
                (ticket,),
            )
            if result.rowcount != 1:
                raise IssuanceDenied(
                    "unknown invitation or grant already approved; revoke the grant"
                )
        return {"ticket": ticket, "consent": "cancelled"}

    def page(self, ticket, browser_cookie=None):
        _digest(ticket)
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM github_consent WHERE ticket=?", (ticket,)
            ).fetchone()
            if row is None or row["expires_at"] <= time.time():
                raise IssuanceDenied("invitation unavailable or expired")
            row = dict(row)
            if row["status"] == "selected" and row["browser_digest"] != _digest(browser_cookie):
                raise IssuanceDenied("selection belongs to another browser")
            if row["status"] == "approved":
                grant_id = json.loads(row["grant_json"])["grant_id"]
                approved = db.execute(
                    "SELECT revoked,expires_at FROM github_grants WHERE grant_id=?",
                    (grant_id,),
                ).fetchone()
                if (
                    approved is None
                    or approved["revoked"]
                    or approved["expires_at"] <= time.time()
                ):
                    row["status"] = "revoked"
            cookie = None
            if row["status"] == "invited":
                cookie = secrets.token_urlsafe(32)
                db.execute(
                    "UPDATE github_consent SET browser_digest=? WHERE ticket=?",
                    (_digest(cookie), ticket),
                )
            return row, cookie

    def begin(self, ticket, cookie, repository=None):
        _digest(ticket)
        digest = _digest(cookie)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.store.transaction() as db:
            row = db.execute("SELECT grant_json FROM github_consent WHERE ticket=? AND browser_digest=? AND status IN ('invited','authorizing') AND expires_at>?",
                (ticket, digest, time.time())).fetchone()
            if row is None:
                raise IssuanceDenied("consent attempt unavailable")
            payload = json.loads(row["grant_json"])
            if payload.get("kind") == "repository_selection":
                from .github_selection import repository_input
                payload = {"kind":"repository_selection", "repository":repository_input(repository)}
                db.execute("UPDATE github_consent SET grant_json=? WHERE ticket=?", (_json(payload), ticket))
            elif repository is not None:
                raise IssuanceDenied("consent scope cannot change")
            result = db.execute(
                "UPDATE github_consent SET status='authorizing',state_digest=?,verifier=?,revocation_seq=(SELECT coalesce(max(seq),0) FROM github_revocations) WHERE ticket=? AND browser_digest=? AND status IN ('invited','authorizing') AND expires_at>?",
                (_digest(state), verifier, ticket, digest, time.time()),
            )
            if result.rowcount != 1:
                raise IssuanceDenied("consent attempt unavailable")
        return self.provider.authorize_url(state, verifier)

    def complete(self, state, cookie, code):
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM github_consent WHERE state_digest=? AND browser_digest=? AND status='authorizing' AND expires_at>?",
                (_digest(state), _digest(cookie), time.time()),
            ).fetchone()
            if row is None:
                raise IssuanceDenied("consent attempt unavailable")
            db.execute(
                "UPDATE github_consent SET status='checking',state_digest=NULL,verifier=NULL WHERE ticket=?",
                (row["ticket"],),
            )
        try:
            if not isinstance(code, str) or not 1 <= len(code) <= 2048:
                raise IssuanceDenied("GitHub authorization cancelled")
            payload = json.loads(row["grant_json"])
            if payload.get("kind") == "repository_selection":
                result = self.provider.resolve_repository(code, row["verifier"], payload["repository"])
                from .github_selection import selection
                result = selection(result)
                if result["repository"] != payload["repository"] or result["issuer_origin"] != self.provider.origin or result["app_id"] != self.provider.app_id:
                    raise IssuanceDenied("selection changed")
                with self.store.transaction() as db:
                    changed = db.execute("UPDATE github_consent SET status='selected',grant_json=? WHERE ticket=? AND status='checking' AND expires_at>?",
                        (_json({**payload,"result":result}), row["ticket"], time.time()))
                    if changed.rowcount != 1:
                        raise IssuanceDenied("selection expired or cancelled")
                return self.provider.origin + CONNECT + "?ticket=" + row["ticket"]
            grant = self.store.validate_grant(payload)
            user_id = self.provider.verify(code, row["verifier"], grant)
            if not _positive(user_id):
                raise IssuanceDenied("GitHub user unavailable")
            with self.store.transaction() as db:
                current = db.execute(
                    "SELECT status,expires_at FROM github_consent WHERE ticket=?",
                    (row["ticket"],),
                ).fetchone()
                if (
                    current["status"] != "checking"
                    or current["expires_at"] <= time.time()
                    or grant["expires_at"] <= time.time()
                ):
                    raise IssuanceDenied("consent attempt expired or cancelled")
                self.store.check_provider_revocations(db, grant, user_id, row["revocation_seq"])
                self.store.approve_in_transaction(db, grant, row["credential_digest"])
                db.execute(
                    "UPDATE github_consent SET status='approved',browser_digest=NULL,github_user_id=? WHERE ticket=?",
                    (user_id, row["ticket"]),
                )
        except (IssuanceDenied, IssuerUnavailable) as exc:
            # A retry begins a new OAuth attempt, never re-exchanges a code.
            # Conflicts need a corrected invitation, not another GitHub login.
            status = "needs_operator" if isinstance(exc, GrantConflict) else "invited"
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE github_consent SET status=?,browser_digest=NULL WHERE ticket=? AND status='checking'",
                    (status, row["ticket"]),
                )
        return self.provider.origin + CONNECT + "?ticket=" + row["ticket"]

    def callback_cookie_name(self, state):
        return self.callback_identity(state)[0]

    def callback_identity(self, state):
        with self.store.transaction() as db:
            row = db.execute("SELECT ticket,grant_json FROM github_consent WHERE state_digest=? AND status='authorizing' AND expires_at>?",
                (_digest(state),time.time())).fetchone()
            if row is None: raise IssuanceDenied("consent attempt unavailable")
            return _cookie_name(row["ticket"]), json.loads(row["grant_json"]).get("kind") == "repository_selection"

    def routes(self):
        from starlette.concurrency import run_in_threadpool
        from starlette.responses import HTMLResponse, RedirectResponse, JSONResponse
        from starlette.routing import Route

        def headers():
            return {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "Date": formatdate(usegmt=True),
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://github.com; frame-ancestors 'none'; base-uri 'none'",
            }

        async def handle(request):
            try:
                if request.headers.get("host") != urlsplit(self.provider.origin).netloc:
                    raise IssuanceDenied("wrong consent origin")
                if request.url.path == SELECT:
                    if request.url.query:
                        raise IssuanceDenied("unexpected selection arguments")
                    location = await run_in_threadpool(self.invite_selection)
                    return RedirectResponse(location, status_code=303, headers=headers())
                if request.url.path == CALLBACK:
                    query = parse_qs(request.url.query, keep_blank_values=True)
                    if any(len(v) != 1 for v in query.values()) or not set(query) <= {
                        "state",
                        "code",
                        "error",
                        "error_description",
                        "error_uri",
                    }:
                        raise IssuanceDenied("invalid callback")
                    cookie_name, selecting = await run_in_threadpool(self.callback_identity,query.get("state",[None])[0])
                    location = await run_in_threadpool(
                        self.complete,
                        query.get("state", [None])[0],
                        request.cookies.get(cookie_name),
                        None if "error" in query else query.get("code", [None])[0],
                    )
                    response = RedirectResponse(
                        location, status_code=303, headers=headers()
                    )
                    if not selecting:
                        response.delete_cookie(cookie_name, secure=True, httponly=True, samesite="lax")
                    return response
                if request.method == "POST":
                    if (
                        request.headers.get("origin") != self.provider.origin
                        or request.headers.get("content-type", "").split(";")[0]
                        != "application/x-www-form-urlencoded"
                    ):
                        raise IssuanceDenied("consent form required")

                    async def body():
                        raw = b""
                        async for chunk in request.stream():
                            raw += chunk
                            if len(raw) > 1024:
                                raise IssuanceDenied("invalid consent form")
                        return raw

                    values = parse_qs(
                        (await asyncio.wait_for(body(), 15)).decode(),
                        keep_blank_values=True,
                    )
                    if set(values) not in ({"ticket"}, {"ticket","repository"}) or any(len(v)!=1 for v in values.values()):
                        raise IssuanceDenied("invalid consent form")
                    location = await run_in_threadpool(
                        self.begin, values["ticket"][0], request.cookies.get(_cookie_name(values["ticket"][0])),
                        values.get("repository",[None])[0]
                    )
                    return RedirectResponse(
                        location, status_code=303, headers=headers()
                    )
                if (
                    set(request.query_params) not in ({"ticket"}, {"ticket","download"})
                    or len(request.query_params.getlist("ticket")) != 1
                    or "download" in request.query_params and request.query_params.getlist("download") != ["1"]
                ):
                    raise IssuanceDenied("invalid invitation")
                cookie = request.cookies.get(_cookie_name(request.query_params["ticket"]))
                if "download" in request.query_params:
                    selected = await run_in_threadpool(self.selection_result,request.query_params["ticket"],cookie)
                    return JSONResponse(selected, headers={**headers(),"Content-Disposition":'attachment; filename="github-selection.json"'})
                row, cookie = await run_in_threadpool(
                    self.page, request.query_params["ticket"], cookie
                )
                response = HTMLResponse(_page(row), headers=headers())
                if cookie:
                    response.set_cookie(
                        _cookie_name(request.query_params["ticket"]),
                        cookie,
                        max_age=900,
                        secure=True,
                        httponly=True,
                        samesite="lax",
                    )
                return response
            except (IssuanceDenied, ValueError, TypeError):
                return HTMLResponse(
                    _layout(
                        "Connection needs attention",
                        "This invitation is unavailable, expired or belongs to another browser. Return to team setup to start a new connection.",
                    ),
                    status_code=403,
                    headers=headers(),
                )
            except (IssuerUnavailable, OSError, sqlite3.Error, asyncio.TimeoutError):
                return HTMLResponse(
                    _layout(
                        "Connection unavailable",
                        "We could not confirm GitHub access. Return to team setup and try again. No repository access is claimed.",
                    ),
                    status_code=503,
                    headers=headers(),
                )

        return [
            Route(CONNECT, handle, methods=["GET", "POST"]),
            Route(CALLBACK, handle, methods=["GET"]),
            Route(SELECT, handle, methods=["GET"]),
        ]


def _layout(title, content):
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Claudfather</title>
<style>body{{margin:0;background:#f7f5ef;color:#302d27;font:17px/1.6 system-ui}}header{{background:#282925;color:#faf2df;padding:18px 7%;border-top:3px solid #df7627}}main{{max-width:660px;margin:56px auto;padding:0 24px}}h1{{font:38px/1.2 Georgia,serif}}section{{background:#fffdf7;border:1px solid #d9d6cd;border-radius:12px;padding:24px}}button{{font:inherit;border:0;border-radius:7px;background:#da7128;color:#171611;padding:12px 20px;cursor:pointer}}input:not([type=hidden]){{display:block;box-sizing:border-box;width:100%;margin-top:8px;padding:12px;font:inherit;color:inherit;background:#fff;border:1px solid #bcb8ad;border-radius:7px}}dt{{font-weight:650}}dd{{margin:0 0 12px;overflow-wrap:anywhere}}small{{color:#625f55}}a{{color:#805020}}</style>
<header><strong>Claudfather</strong> · Rise of the machines</header><main><h1>{escape(title)}</h1>{content}</main></html>"""


def _page(row):
    grant = json.loads(row["grant_json"])
    if grant.get("kind") == "repository_selection":
        if row["status"] == "selected":
            result = grant["result"]
            return _layout("Repository selected", f'''<p>GitHub confirmed administrator access to <strong>{escape(result["repository"])}</strong> through this App installation.</p>
<p><a href="{CONNECT}?ticket={escape(row["ticket"])}&amp;download=1">Download repository selection</a></p>
<p>Save this nonsecret file on the computer that will run your team, then use it with <code>host owner allow-setup --github-selection</code>. Verify the issuer address and repository during local approval.</p>
<p><small>This file grants no access. Each bot still needs its own authorization in team setup. No fleet was started or repository changed.</small></p>''')
        if row["status"] in {"invited","authorizing"}:
            return _layout("Choose your repository", f'''<p>Paste an existing GitHub repository. Sign in as its administrator; the managed App must already be installed there with contents and pull-request write access.</p>
<section><form action="{CONNECT}" method="post"><input type="hidden" name="ticket" value="{escape(row["ticket"])}"><label>Repository URL or owner/repo <input name="repository" required maxlength="256" value="{escape(grant.get("repository",""))}" placeholder="https://github.com/your-org/your-project"></label><p><button type="submit">Find repository with GitHub →</button></p></form></section>
<p><small>This step reads repository details to fill in setup. It grants no bot access. If access was not confirmed, check App installation, organization approval and administrator permissions before retrying.</small></p>''')
        return _layout("Repository selection needs attention", "<p>Finish GitHub login in the original browser, or start a new repository selection. No access is granted by this step.</p>")
    binding = grant["binding"]
    if row["status"] == "approved":
        return _layout(
            "GitHub is connected.",
            "<p>The selected bot’s repository grant is approved. Return to team setup to prepare its working copy. No agents were started here.</p>",
        )
    if row["status"] == "revoked":
        return _layout(
            "Connection removed",
            "<p>This repository grant is no longer active. Return to team setup if you want to reconnect it.</p>",
        )
    if row["status"] == "needs_operator":
        return _layout(
            "Connection needs a new invitation",
            "<p>This invitation conflicts with an existing connection. Ask the person managing setup for a corrected invitation. Signing in to GitHub again will not fix it.</p>",
        )
    if row["status"] not in {"invited", "authorizing"}:
        return _layout(
            "Connection in progress"
            if row["status"] == "checking"
            else "Connection cancelled",
            "<p>Complete the GitHub step in the browser where you started, or return to team setup for a new invitation.</p>",
        )
    fields = [
        ("Repository", binding["repo"]),
        ("Bot", binding["bot_id"]),
        (
            "Access",
            ", ".join(
                k.replace("_", " ") + ": " + v
                for k, v in binding["permissions"].items()
            ),
        ),
        ("Workspace", grant["workspace_id"]),
        ("Computer", grant["deployment_id"]),
    ]
    details = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in fields)
    return _layout(
        "Connect GitHub to your team",
        f'''<p>Review this connection, then sign in as a repository administrator. GitHub must already have the managed App installed on this repository with the listed access.</p>
<section><dl>{details}</dl><form action="{CONNECT}" method="post"><input type="hidden" name="ticket" value="{escape(row["ticket"])}"><button type="submit">Authorize with GitHub →</button></form></section>
<p><small>If you cancelled or access was not confirmed, you can try again. Check App installation, repository permissions and any organization approval or SSO requirement first. This approves repository access for this bot; it does not launch the team or perform work.</small></p>''',
    )
