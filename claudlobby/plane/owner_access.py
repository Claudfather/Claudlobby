"""Host-owned direct-reader pairing and sessions.

This is a policy/state primitive, NOT an identity verifier. A future trusted
ingress must establish a human PrincipalRef; the host owner CLI separately
authorizes local pairing/revocation. Neither is supplied by this module.
The internal owner_view factory exercises this policy with an explicitly
injected verifier. No runtime service enables the network factory. See the owner
access foundation section of documentation/architecture/observable-plane.md.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
import time
from typing import Callable, Iterator

from .ids import ID_PATTERNS, read_host_uid

PAIRING_SECONDS = 300
SESSION_SECONDS = 900
MAX_PENDING = 32
MAX_SESSIONS = 32
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_HUMAN_ALIAS = re.compile(r"human:[^\s:/]+")
_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                       version INTEGER NOT NULL, host_uid TEXT NOT NULL);
CREATE TABLE grants (revision INTEGER PRIMARY KEY, namespace TEXT NOT NULL,
                     subject TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0, 1)),
                     changed_at REAL NOT NULL);
CREATE TABLE challenges (digest TEXT PRIMARY KEY, namespace TEXT NOT NULL,
                         subject TEXT NOT NULL, revision INTEGER NOT NULL,
                         created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE sessions (digest TEXT PRIMARY KEY, revision INTEGER NOT NULL REFERENCES grants,
                       created_at REAL NOT NULL, expires_at REAL NOT NULL);
"""
_MESSAGE_GRANTS_SCHEMA = """
CREATE TABLE message_grants (
    owner_revision INTEGER NOT NULL REFERENCES grants(revision),
    fleet_uid TEXT NOT NULL,
    actor_uid TEXT NOT NULL,
    actor_alias TEXT NOT NULL,
    PRIMARY KEY (owner_revision, fleet_uid)
)"""


class AccessDenied(ValueError):
    """A policy refusal. Codes contain no credentials or caller identifiers."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class AccessUnavailable(RuntimeError):
    """Missing, incompatible or unreadable authority is never permission."""


@dataclass(frozen=True)
class PrincipalRef:
    """Namespaced identity supplied by a verifier, never by browser claims.

    Constructing this value does not verify it. Namespace/subject mechanics
    for Tailscale remain an adapter contract; login/email equality is not one.
    """

    namespace: str
    subject: str

    def __post_init__(self):
        for value in (self.namespace, self.subject):
            if (not isinstance(value, str) or not 1 <= len(value) <= 256
                    or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError("principal reference must be nonempty bounded text")


@dataclass(frozen=True)
class VerifiedReader:
    """A verified principal and its session, supplied out of band by ingress.

    Construction does not verify identity. No header, cookie, query string or
    website account identifier is interpreted as authority by this module.
    """

    principal: PrincipalRef
    token: str = field(repr=False)



@dataclass(frozen=True)
class PairingChallenge:
    token: str = field(repr=False)
    principal: PrincipalRef
    expires_at: float


@dataclass(frozen=True)
class OwnerGrant:
    host_uid: str
    principal: PrincipalRef
    revision: int
    active: bool


@dataclass(frozen=True)
class OwnerMessageGrant:
    """Locally approved ordinary-message actor for one fleet and owner revision."""

    owner: OwnerGrant
    fleet_uid: str
    actor_uid: str
    actor_alias: str


@dataclass(frozen=True)
class ReaderSession:
    token: str = field(repr=False)
    grant: OwnerGrant
    expires_at: float


def _digest(token: str) -> str:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise AccessDenied("invalid_credential")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _canonical_uid(value: str, kind: str) -> str:
    if not isinstance(value, str) or re.fullmatch(ID_PATTERNS[kind], value) is None:
        raise AccessDenied("invalid_message_binding")
    return value


def _human_alias(value: str) -> str:
    if not isinstance(value, str) or _HUMAN_ALIAS.fullmatch(value) is None:
        raise AccessDenied("invalid_message_binding")
    return value


class OwnerAccess:
    """Explicit, host-scoped state, separate from the append-only Plane ledger.

    ``authorize_read`` must be called for every request and before each stream
    delivery; never cache its result. Ordinary messages require a separate,
    explicit local grant and ``authorize_message`` admission. This class does
    not send messages or grant other actions, website membership or workspace
    authority.
    Same-UID processes/root can alter its files and are outside this boundary.
    """

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time):
        self.root = Path(root).absolute()
        self.path = self.root / "state/plane/owner-access.db"
        self._clock = clock

    @classmethod
    def initialize(cls, root: Path, *, clock: Callable[[], float] = time.time) -> OwnerAccess:
        """Explicit preparation only; never invoked by a read or runtime startup.

        Requires the existing installation identity. Existing authority is
        validated, never reinitialized, migrated, or rebound here.
        """
        store = cls(root, clock=clock)
        try:
            host_uid = read_host_uid(store.root / "state")
            if store.path.parent.is_symlink():
                raise ValueError("redirected authority directory")
            store.path.parent.mkdir(mode=0o700, exist_ok=True)
            if stat.S_IMODE(store.path.parent.stat().st_mode) != 0o700:
                raise ValueError("authority directory must be private")
            if not os.path.lexists(store.path):
                fd, name = tempfile.mkstemp(prefix=".owner-access-", dir=store.path.parent)
                os.close(fd)
                temporary = Path(name)
                try:
                    conn = sqlite3.connect(temporary, isolation_level=None)
                    try:
                        conn.execute("PRAGMA synchronous = FULL")
                        conn.executescript(_SCHEMA)
                        conn.execute("INSERT INTO metadata VALUES (1, 1, ?)", (host_uid,))
                        conn.commit()
                    finally:
                        conn.close()
                    # Publish complete bytes only; a competing initializer wins
                    # without losing its grants. A crash can leave private temp
                    # litter, never an incomplete authority at the final path.
                    try:
                        os.link(temporary, store.path)
                    except FileExistsError:
                        pass
                finally:
                    temporary.unlink()
            with store._connection():
                pass
            # The plane directory's entry lives in state. Sync it even when
            # another initializer created it, before acknowledging the store.
            for path in (store.path.parent.parent, store.path.parent):
                directory = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise AccessUnavailable("owner authority could not be initialized") from exc
        return store

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = None
        try:
            host_uid = read_host_uid(self.root / "state")
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or self.path.parent.is_symlink()
                    or stat.S_IMODE(self.path.parent.stat().st_mode) != 0o700):
                raise AccessUnavailable("owner authority must be private regular storage")
            mode = "rw" if write else "ro"
            conn = sqlite3.connect(f"{self.path.as_uri()}?mode={mode}", uri=True,
                                   isolation_level=None, timeout=5)
            conn.row_factory = sqlite3.Row
            if write:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA synchronous = FULL")
            else:
                conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            metadata = conn.execute("SELECT * FROM metadata").fetchall()
            if (len(metadata) != 1 or metadata[0]["version"] != 1
                    or metadata[0]["host_uid"] != host_uid):
                raise AccessUnavailable("owner authority version or installation mismatch")
            yield conn
            if write:
                conn.commit()
        except AccessDenied:
            raise
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise AccessUnavailable("owner authority is unavailable") from exc
        finally:
            if conn is not None:
                conn.close()  # uncommitted changes roll back, including policy refusals

    def _now(self) -> float:
        value = self._clock()
        if not math.isfinite(value):
            raise AccessUnavailable("host clock is unavailable")
        return value

    @staticmethod
    def _grant(conn: sqlite3.Connection) -> OwnerGrant | None:
        row = conn.execute("SELECT * FROM grants ORDER BY revision DESC LIMIT 1").fetchone()
        if row is None:
            return None
        host_uid = conn.execute("SELECT host_uid FROM metadata").fetchone()[0]
        return OwnerGrant(host_uid, PrincipalRef(row["namespace"], row["subject"]),
                          row["revision"], bool(row["active"]))

    @staticmethod
    def _owner(conn: sqlite3.Connection, principal: PrincipalRef) -> OwnerGrant:
        grant = OwnerAccess._grant(conn)
        if grant is None or not grant.active or grant.principal != principal:
            raise AccessDenied("owner_not_paired")
        return grant

    @staticmethod
    def _expected_owner(conn: sqlite3.Connection, expected_owner: OwnerGrant) -> OwnerGrant:
        grant = OwnerAccess._grant(conn)
        if (not isinstance(expected_owner, OwnerGrant) or grant is None
                or not grant.active or grant != expected_owner):
            raise AccessDenied("grant_changed")
        return grant

    @staticmethod
    def _has_message_grants(conn: sqlite3.Connection) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'message_grants'"
        ).fetchone() is not None

    @staticmethod
    def _message_grant(conn: sqlite3.Connection, owner: OwnerGrant,
                       fleet_uid: str) -> OwnerMessageGrant:
        if not OwnerAccess._has_message_grants(conn):
            raise AccessDenied("messages_not_allowed")
        row = conn.execute(
            "SELECT actor_uid, actor_alias FROM message_grants "
            "WHERE owner_revision = ? AND fleet_uid = ?",
            (owner.revision, fleet_uid),
        ).fetchone()
        if row is None:
            raise AccessDenied("messages_not_allowed")
        # A damaged binding cannot become authority through a read.
        try:
            actor_uid = _canonical_uid(row["actor_uid"], "actor")
            actor_alias = _human_alias(row["actor_alias"])
        except AccessDenied as exc:
            raise AccessUnavailable("owner message grant is invalid") from exc
        return OwnerMessageGrant(owner, fleet_uid, actor_uid, actor_alias)

    def current_grant(self) -> OwnerGrant | None:
        """Local inspection only. Exposing this result requires its own gate."""
        with self._connection() as conn:
            return self._grant(conn)

    def begin_pairing(self, principal: PrincipalRef) -> PairingChallenge:
        """After trusted human verification, request separate local approval."""
        with self._connection(write=True) as conn:
            grant = self._grant(conn)
            if grant is not None and grant.active:
                raise AccessDenied("owner_already_paired")
            now = self._now()
            conn.execute("DELETE FROM challenges WHERE expires_at <= ? OR created_at > ?", (now, now))
            if conn.execute("SELECT count(*) FROM challenges").fetchone()[0] >= MAX_PENDING:
                raise AccessDenied("pairing_capacity")
            token = secrets.token_urlsafe(32)
            expires = now + PAIRING_SECONDS
            conn.execute("INSERT INTO challenges VALUES (?, ?, ?, ?, ?, ?)",
                         (_digest(token), principal.namespace, principal.subject,
                          grant.revision if grant else 0, now, expires))
            return PairingChallenge(token, principal, expires)

    def confirm_pairing(self, token: str, *, expected_principal: PrincipalRef) -> OwnerGrant:
        """Local approval of the exact displayed challenge AND principal.

        A remote caller must never be able to invoke this method. It does
        not itself prove local console presence or distinguish same-UID bots.
        """
        digest = _digest(token)
        with self._connection(write=True) as conn:
            now = self._now()
            row = conn.execute("SELECT * FROM challenges WHERE digest = ?", (digest,)).fetchone()
            grant = self._grant(conn)
            revision = grant.revision if grant else 0
            if (row is None or not row["created_at"] <= now < row["expires_at"]
                    or row["revision"] != revision or (grant is not None and grant.active)
                    or PrincipalRef(row["namespace"], row["subject"]) != expected_principal):
                raise AccessDenied("pairing_unavailable")
            conn.execute("INSERT INTO grants VALUES (?, ?, ?, 1, ?)",
                         (revision + 1, expected_principal.namespace, expected_principal.subject, now))
            conn.execute("DELETE FROM challenges")
            return self._owner(conn, expected_principal)

    def inspect_pairing(self, token: str) -> PairingChallenge:
        """Local console preview only; never expose this lookup remotely.

        The subsequent confirmation rechecks expiry, revision and principal in
        its write transaction. Previewing a request does not consume or grant it.
        """
        digest = _digest(token)
        with self._connection() as conn:
            now = self._now()
            row = conn.execute("SELECT * FROM challenges WHERE digest = ?", (digest,)).fetchone()
            grant = self._grant(conn)
            revision = grant.revision if grant else 0
            if (row is None or not row["created_at"] <= now < row["expires_at"]
                    or row["revision"] != revision or (grant is not None and grant.active)):
                raise AccessDenied("pairing_unavailable")
            return PairingChallenge(token, PrincipalRef(row["namespace"], row["subject"]),
                                    row["expires_at"])

    @staticmethod
    def _issue(conn: sqlite3.Connection, grant: OwnerGrant, now: float) -> ReaderSession:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ? OR created_at > ?", (now, now))
        if conn.execute("SELECT count(*) FROM sessions").fetchone()[0] >= MAX_SESSIONS:
            raise AccessDenied("session_capacity")
        token = secrets.token_urlsafe(32)
        expires = now + SESSION_SECONDS
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                     (_digest(token), grant.revision, now, expires))
        return ReaderSession(token, grant, expires)

    def open_session(self, principal: PrincipalRef) -> ReaderSession:
        """Fresh direct login after trusted verification; no website dependency."""
        with self._connection(write=True) as conn:
            return self._issue(conn, self._owner(conn, principal), self._now())

    def _admit(self, conn: sqlite3.Connection, digest: str, principal: PrincipalRef) -> OwnerGrant:
        row = conn.execute("SELECT * FROM sessions WHERE digest = ?", (digest,)).fetchone()
        now = self._now()
        if row is None or not row["created_at"] <= now < row["expires_at"]:
            raise AccessDenied("session_unavailable")
        grant = self._owner(conn, principal)
        if row["revision"] != grant.revision:
            raise AccessDenied("session_unavailable")
        return grant

    def authorize_read(self, token: str, principal: PrincipalRef, *, host_uid: str) -> OwnerGrant:
        """Direct whole-deployment read only; no action or workspace admission.

        The caller must derive host_uid from the actual data source, never
        from a browser-supplied resource claim.
        """
        with self._connection() as conn:
            grant = self._admit(conn, _digest(token), principal)
            if grant.host_uid != host_uid:
                raise AccessDenied("wrong_deployment")
            return grant

    def allow_messages(self, *, expected_owner: OwnerGrant, fleet_uid: str,
                       actor_uid: str, actor_alias: str) -> OwnerMessageGrant:
        """Local approval only; caller must bind current registry IDs separately.

        A remote caller must never invoke this method. An active fleet binding
        is immutable; revoke it explicitly before approving a different actor.
        """
        fleet_uid = _canonical_uid(fleet_uid, "fleet")
        actor_uid = _canonical_uid(actor_uid, "actor")
        actor_alias = _human_alias(actor_alias)
        with self._connection(write=True) as conn:
            owner = self._expected_owner(conn, expected_owner)
            if not self._has_message_grants(conn):
                conn.execute(_MESSAGE_GRANTS_SCHEMA)
            row = conn.execute(
                "SELECT actor_uid, actor_alias FROM message_grants "
                "WHERE owner_revision = ? AND fleet_uid = ?",
                (owner.revision, fleet_uid),
            ).fetchone()
            if row is not None:
                if row["actor_uid"] != actor_uid or row["actor_alias"] != actor_alias:
                    raise AccessDenied("message_binding_changed")
            else:
                conn.execute(
                    "INSERT INTO message_grants VALUES (?, ?, ?, ?)",
                    (owner.revision, fleet_uid, actor_uid, actor_alias),
                )
            return OwnerMessageGrant(owner, fleet_uid, actor_uid, actor_alias)

    def revoke_messages(self, *, expected_owner: OwnerGrant, fleet_uid: str) -> None:
        """Locally remove one current owner/fleet ordinary-message grant."""
        fleet_uid = _canonical_uid(fleet_uid, "fleet")
        with self._connection(write=True) as conn:
            owner = self._expected_owner(conn, expected_owner)
            if self._has_message_grants(conn):
                conn.execute(
                    "DELETE FROM message_grants WHERE owner_revision = ? AND fleet_uid = ?",
                    (owner.revision, fleet_uid),
                )

    def authorize_message(self, token: str, principal: PrincipalRef, *,
                          host_uid: str, fleet_uid: str) -> OwnerMessageGrant:
        """Admit one fleet's ordinary messages using one read-only snapshot.

        The caller must derive host/fleet IDs from its actual target, never
        from browser-supplied claims. This does not authorize message content,
        recipients, replies, nudges, or any other operation.
        """
        fleet_uid = _canonical_uid(fleet_uid, "fleet")
        with self._connection() as conn:
            owner = self._admit(conn, _digest(token), principal)
            if owner.host_uid != host_uid:
                raise AccessDenied("wrong_deployment")
            return self._message_grant(conn, owner, fleet_uid)

    def renew_session(self, token: str, principal: PrincipalRef) -> ReaderSession:
        """Atomically rotate a still-valid session; old token cannot be replayed."""
        with self._connection(write=True) as conn:
            digest = _digest(token)
            grant = self._admit(conn, digest, principal)
            conn.execute("DELETE FROM sessions WHERE digest = ?", (digest,))
            return self._issue(conn, grant, self._now())

    def end_session(self, token: str, principal: PrincipalRef) -> None:
        """End this session; a current pairing still permits fresh direct login."""
        with self._connection(write=True) as conn:
            digest = _digest(token)
            self._admit(conn, digest, principal)
            conn.execute("DELETE FROM sessions WHERE digest = ?", (digest,))

    def revoke_owner(self, *, expected_revision: int) -> OwnerGrant:
        """Local removal, guarded against a stale approval revoking a new pairing."""
        if type(expected_revision) is not int or expected_revision < 1:
            raise AccessDenied("grant_changed")
        with self._connection(write=True) as conn:
            grant = self._grant(conn)
            if grant is None or not grant.active or grant.revision != expected_revision:
                raise AccessDenied("grant_changed")
            conn.execute("INSERT INTO grants VALUES (?, ?, ?, 0, ?)",
                         (grant.revision + 1, grant.principal.namespace,
                          grant.principal.subject, self._now()))
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM challenges")
            return self._grant(conn)
