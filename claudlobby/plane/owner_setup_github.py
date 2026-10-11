"""Managed first-team credentials and consent, behind explicit local setup approval.

The private owner database retains the credential before any network effect.
Only an exact, newly owned fleet .env is installed; existing operator files are
never parsed or rewritten. Website traffic carries digests, never this secret.
"""
import json
import os
import secrets
import stat
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from uuid import uuid4

from ..dotenv import format_file
from ..github_connections import parse_github_policy, canonical_github_repo
from ..github_enrollment import prepare_request, request_digest, EnrollmentRestartRequired
from ..github_issuer import _token_result, IssuanceDenied
from .owner_access import AccessDenied, AccessUnavailable
from .workspace_assertion import _origin


def configuration(policy, enrollment):
    if enrollment is None:
        return None
    if (not isinstance(enrollment, dict) or set(enrollment) != {"issuer_origin", "repository_id"}
            or not _origin(enrollment["issuer_origin"], False)
            or type(enrollment["repository_id"]) is not int or enrollment["repository_id"] <= 0
            or policy is None):
        raise AccessDenied("setup_github_configuration")
    connections, _ = parse_github_policy(json.loads(policy))
    if len(connections) != 1:
        raise AccessDenied("setup_github_configuration")
    connection = next(iter(connections.values()))
    refs = dict(connection.credential_refs)
    if (connection.mode != "managed" or len(set(refs.values())) != 2
            or any(name.startswith(("GIT_", "LD_", "DYLD_", "PYTHON", "CLAUDLOBBY_"))
                or name in {"PATH", "HOME", "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"}
                for name in refs.values())):
        raise AccessDenied("setup_github_configuration")
    return connection


def initialize(conn, grant, *, previous=None):
    if grant.github_enrollment is None:
        return
    configuration(grant.github_policy, grant.github_enrollment)
    conn.execute("""CREATE TABLE IF NOT EXISTS setup_github (
        generation TEXT PRIMARY KEY, credential TEXT NOT NULL, requests TEXT NOT NULL)""")
    retained = conn.execute("SELECT credential,requests FROM setup_github WHERE generation=?",
        (previous.generation,)).fetchone() if previous else None
    if previous and retained is None:
        raise AccessUnavailable()
    conn.execute("INSERT OR REPLACE INTO setup_github VALUES(?,?,?)", (grant.generation,
        retained["credential"] if retained else secrets.token_urlsafe(32), retained["requests"] if retained else "{}"))


def post(origin, path, payload, credential=None):
    """Fixed local-approved HTTPS destination, finite response, no redirect/proxy."""
    import httpx
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if credential is not None:
        headers["Authorization"] = "Bearer " + credential
    try:
        started = int(time.time()) - 1
        deadline = time.monotonic() + 10
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
            with client.stream("POST", origin + path, json=payload, headers=headers) as response:
                body = bytearray()
                for chunk in response.iter_raw():
                    body.extend(chunk)
                    if len(body) > 32768 or time.monotonic() >= deadline:
                        raise ValueError()
                if response.status_code == 409 and json.loads(body) == {"error":"enrollment_restart_required"}:
                    raise EnrollmentRestartRequired()
                if response.status_code == 403:
                    raise IssuanceDenied()
                if response.status_code != 200:
                    raise AccessUnavailable()
                result = json.loads(body)
                if credential is not None:
                    # Translate the TLS-authenticated issuer clock, as the
                    # existing token consumer does; host clock skew is harmless.
                    expiry = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
                    issued = parsedate_to_datetime(response.headers["Date"])
                    remaining = (expiry - issued).total_seconds()
                    if expiry.tzinfo is None or issued.tzinfo is None or not 0 < remaining <= 3600:
                        raise ValueError()
                    result["expires_at"] = datetime.fromtimestamp(started + remaining, timezone.utc).isoformat()
                return result
    except IssuanceDenied:
        raise
    except Exception:
        raise AccessUnavailable() from None


class SetupGithub:
    def __init__(self, store):
        self.store, self.access = store, store.access

    def bindings(self, grant):
        from ..commands.setup import _template_config
        from ..config import load_fleet_snapshot
        from ..composer import compose_github_bindings
        configuration(grant.github_policy, grant.github_enrollment)
        content = _template_config(grant.template, grant.fleet, grant.repository,
            "Connect the approved repository", github_policy=grant.github_policy)
        fleet, _ = load_fleet_snapshot(self.access.root / "local" / grant.fleet / "fleet.yaml", content, None)
        bindings = {}
        for bot,config in fleet.bots.items():
            rows = json.loads(compose_github_bindings(config, fleet)[0])["bindings"]
            if len(rows)!=1 or rows[0]["repo"]!=canonical_github_repo(grant.repository):
                raise AccessDenied("setup_github_binding")
            bindings[bot] = rows[0]
        return bindings

    def _row(self, conn, reader, grant):
        self.store._admit(conn, reader, expected=grant, effects=True)
        if grant.github_enrollment is None:
            raise AccessDenied("setup_github_not_allowed")
        row = conn.execute("SELECT * FROM setup_github WHERE generation=?", (grant.generation,)).fetchone()
        if row is None:
            raise AccessUnavailable()
        return row

    def _env(self, grant, credential):
        connection = configuration(grant.github_policy, grant.github_enrollment)
        refs = dict(connection.credential_refs)
        return format_file("# Managed GitHub credentials for this approved first-team setup.", {
            refs["issuer_url_env"]: grant.github_enrollment["issuer_origin"] + "/v1/github/token",
            refs["issuer_token_env"]: credential,
        }).encode()

    @staticmethod
    def _owned_file(path, content):
        try:
            info = path.lstat()
            return (path.resolve() == path and stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600
                and path.read_bytes() == content)
        except OSError:
            return False

    def owns_env(self, grant):
        if grant.github_enrollment is None:
            return False
        with self.access._connection() as conn:
            row = conn.execute("SELECT credential FROM setup_github WHERE generation=?", (grant.generation,)).fetchone()
        path = self.access.root / "local" / grant.fleet / ".env"
        return bool(row and self._owned_file(path,self._env(grant,row["credential"])))

    def prepare(self, reader, grant, bot):
        bindings = self.bindings(grant)
        if bot not in bindings:
            raise AccessDenied("setup_github_bot")
        with self.access._connection(write=True) as conn:
            row = self._row(conn, reader, grant)
            binding = self.access.workspace()._active_binding(conn, reader.principal)
            requests = json.loads(row["requests"])
            if bot not in requests:
                requests[bot] = prepare_request(request_id=str(uuid4()), workspace_id=binding.workspace_id,
                    host_uid=binding.owner.host_uid, nonce=secrets.token_urlsafe(32), binding=bindings[bot],
                    repository_id=grant.github_enrollment["repository_id"], credential=row["credential"])
                conn.execute("UPDATE setup_github SET requests=? WHERE generation=?", (json.dumps(requests), grant.generation))
            request = requests[bot]
            if request["binding"]!=bindings[bot]: raise AccessDenied("setup_github_binding_changed")
            return {"version": 1, "generation": grant.generation, "bot": bot,
                "request_id": request["request_id"], "request_digest": request_digest(request),
                "credential_digest": request["credential_digest"], "nonce": request["nonce"],
                "host_uid": request["host_uid"], "workspace_id": request["workspace_id"],
                "binding_revision": binding.revision, "binding_generation": binding.generation,
                "issuer_origin": grant.github_enrollment["issuer_origin"]}

    def submit(self, reader, grant, request_id, assertion):
        if not isinstance(assertion, str) or not 1 <= len(assertion) <= 8192:
            raise AccessDenied("setup_github_assertion")
        with self.access._connection() as conn:
            row = self._row(conn, reader, grant)
            request = next((r for r in json.loads(row["requests"]).values() if r["request_id"] == request_id), None)
            if request is None:
                raise AccessDenied("setup_github_request_unknown")
        try:
            result = post(grant.github_enrollment["issuer_origin"], "/v1/github/enroll", {"request": request, "assertion": assertion})
        except EnrollmentRestartRequired:
            # Only an authenticated terminal issuer response retires a request.
            # An uncertain network outcome always keeps the original identity.
            with self.access._connection(write=True) as conn:
                row = self._row(conn, reader, grant)
                requests = json.loads(row["requests"])
                bot = request["binding"]["bot_id"]
                if requests.get(bot) != request: raise AccessDenied("setup_github_request_unknown")
                requests.pop(bot)
                conn.execute("UPDATE setup_github SET requests=? WHERE generation=?", (json.dumps(requests), grant.generation))
            return {"version":1,"generation":grant.generation,"request_id":request_id,
                    "state":"restart_required","bot":bot}
        self.store.authorize(reader, expected=grant, effects=True)
        if (not isinstance(result, dict) or set(result) != {"authorization_url", "ticket", "grant_id", "new_issuance"}
                or not isinstance(result["ticket"], str) or not result["ticket"].isascii()
                or len(result["ticket"]) != 43 or not all(c.isalnum() or c in "_-" for c in result["ticket"])
                or result["authorization_url"] != grant.github_enrollment["issuer_origin"] + "/v1/github/connect?ticket=" + result["ticket"]
                or result["grant_id"] != "enrollment-" + request_id or result["new_issuance"] != "not_allowed"):
            raise AccessUnavailable()
        return {"version": 1, "generation": grant.generation, "request_id": request_id,
                "state":"awaiting_consent","authorization_url": result["authorization_url"], "bot": request["binding"]["bot_id"]}

    def check(self, reader, grant):
        bindings = self.bindings(grant)
        with self.access._connection() as conn:
            credential = self._row(conn, reader, grant)["credential"]
        statuses = []
        for bot,binding in bindings.items():
            self.store.authorize(reader, expected=grant, effects=True)
            try:
                result = post(grant.github_enrollment["issuer_origin"], "/v1/github/token",
                    {"schema": 1, "binding": binding, "revision": binding["revision"]}, credential)
                if not isinstance(result,dict) or result.get("revision") != binding["revision"]:
                    raise AccessUnavailable()
                _token_result(binding, grant.github_enrollment["repository_id"], result)
                state = "approved"
            except IssuanceDenied:
                state = "needs_consent"
            except (AccessUnavailable, RuntimeError):
                state = "unavailable"
            statuses.append({"bot":bot,"state":state})
        self.store.authorize(reader,expected=grant,effects=True)
        if any(row["state"]!="approved" for row in statuses):
            return {"version":1,"generation":grant.generation,"state":"needs_authorization","bots":statuses}
        content = self._env(grant, credential)
        path = self.access.root / "local" / grant.fleet / ".env"
        # Re-admit before installing. The durable private row is a recovery
        # intent; only its exact bytes may be reused after a lost response.
        with self.access._connection(write=True) as conn:
            self._row(conn, reader, grant)
            for directory in (self.access.root / "local", path.parent):
                if directory.is_symlink() or directory.resolve() != directory:
                    raise AccessDenied("setup_github_env_conflict")
                directory.mkdir(mode=0o700, exist_ok=True)
            if not self._owned_file(path,content):
                if path.exists() or path.is_symlink(): raise AccessDenied("setup_github_env_conflict")
                from ..commands.setup import _write_config
                _write_config(path,content,replace=False)
        return {"version": 1, "generation": grant.generation, "state": "credentials_ready", "bots": statuses}
