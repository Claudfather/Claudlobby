"""Purpose-separated connected reads, sharing the canonical owner transaction.

Only trusted callers supply fresh native principals and verified session
attestations. These objects are not proofs by construction. Action admission
requires existing explicit local grants; no grants, cookie sessions, network
or automatic schema initialization are created here.
"""
from dataclasses import asdict, dataclass, field
import json
import math
import secrets

from .owner_access import AccessDenied, AccessUnavailable, OwnerGrant, SESSION_SECONDS, MAX_SESSIONS, MAX_PENDING, _canonical_uid, _digest
from .owner_workspace import WorkspaceBinding
from .session_assertion import ExpectedSessionAssertion, SessionAttestation
from .workspace_assertion import _canonical_32_bytes

CHALLENGE_SECONDS = 120
_SCHEMA = (
    "CREATE TABLE connected_metadata(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL)",
    "INSERT INTO connected_metadata VALUES(1,1)",
    """CREATE TABLE connected_challenges(digest TEXT PRIMARY KEY,owner_revision INTEGER NOT NULL,
       binding_revision INTEGER NOT NULL,binding_generation TEXT NOT NULL,
       binding_snapshot TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL)""",
    """CREATE TABLE connected_sessions(digest TEXT PRIMARY KEY,owner_revision INTEGER NOT NULL,
       binding_revision INTEGER NOT NULL,binding_generation TEXT NOT NULL,
       binding_snapshot TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL)""",
)


@dataclass(frozen=True)
class ConnectedChallenge:
    expected: ExpectedSessionAssertion = field(repr=False)
    owner_revision: int
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class ConnectedAdmission:
    owner: OwnerGrant
    binding: WorkspaceBinding
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class ConnectedSession:
    token: str = field(repr=False)
    admission: ConnectedAdmission


def _times(row, lifetime):
    start, end = row["created_at"], row["expires_at"]
    if (not all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in (start, end))
            or end - start != lifetime):
        raise AccessUnavailable()
    return start, end


class OwnerConnected:
    def __init__(self, access):
        self.access, self.workspace = access, access.workspace()

    @staticmethod
    def _schema(conn):
        rows = conn.execute("SELECT singleton,version FROM connected_metadata").fetchall()
        if len(rows) != 1 or tuple(rows[0]) != (1, 1):
            raise AccessUnavailable()
        for table in ("connected_challenges", "connected_sessions"):
            conn.execute(f"SELECT digest,owner_revision,binding_revision,binding_generation,binding_snapshot,created_at,expires_at FROM {table} LIMIT 0")

    def initialize(self, *, expected_owner):
        with self.access._connection(write=True) as conn:
            self.access._expected_owner(conn, expected_owner)
            self.workspace._schema(conn)
            self.workspace._owner(conn, expected_owner.principal)
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not names & {"connected_metadata", "connected_challenges", "connected_sessions"}:
                for statement in _SCHEMA:
                    conn.execute(statement)
            self._schema(conn)  # partial/unsupported schema refuses, never repairs

    def _context(self, conn, principal):
        self._schema(conn)
        binding = self.workspace._active_binding(conn, principal)
        return binding.owner, binding

    def _now(self):
        now = self.access._now()
        if now < 0:
            raise AccessUnavailable()
        return now

    @staticmethod
    def _matches(row, owner, binding):
        return (row["owner_revision"] == owner.revision and row["binding_revision"] == binding.revision
                and row["binding_generation"] == binding.generation
                and row["binding_snapshot"] == OwnerConnected._snapshot(binding))

    @staticmethod
    def _snapshot(binding):
        return json.dumps(asdict(binding), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @staticmethod
    def _expected(nonce, binding):
        return ExpectedSessionAssertion(binding.issuer, binding.subject, binding.workspace_id,
            binding.owner.host_uid, nonce, binding.operational_origin, binding.revision, binding.generation)

    @staticmethod
    def _cleanup(conn, table, now, owner, binding):
        conn.execute(f"DELETE FROM {table} WHERE expires_at<=? OR created_at>? OR owner_revision!=? "
                     "OR binding_revision!=? OR binding_generation!=? OR binding_snapshot!=?",
                     (now, now, owner.revision, binding.revision, binding.generation, OwnerConnected._snapshot(binding)))

    def begin(self, principal):
        with self.access._connection(write=True) as conn:
            owner, binding = self._context(conn, principal)
            now = self._now()
            self._cleanup(conn, "connected_challenges", now, owner, binding)
            if conn.execute("SELECT count(*) FROM connected_challenges").fetchone()[0] >= MAX_PENDING:
                raise AccessUnavailable()
            nonce = secrets.token_urlsafe(32)
            expires = now + CHALLENGE_SECONDS
            conn.execute("INSERT INTO connected_challenges VALUES(?,?,?,?,?,?,?)",
                         (_digest(nonce), owner.revision, binding.revision, binding.generation, self._snapshot(binding), now, expires))
            return ConnectedChallenge(self._expected(nonce, binding), owner.revision, now, expires)

    def _challenge(self, conn, nonce, principal):
        if not _canonical_32_bytes(nonce):
            raise AccessDenied("connected_session_denied")
        owner, binding = self._context(conn, principal)
        row = conn.execute("SELECT * FROM connected_challenges WHERE digest=?", (_digest(nonce),)).fetchone()
        if row is None:
            raise AccessDenied("connected_session_denied")
        start, end = _times(row, CHALLENGE_SECONDS)
        if not self._matches(row, owner, binding) or not start <= self._now() < end:
            raise AccessDenied("connected_session_denied")
        return ConnectedChallenge(self._expected(nonce, binding), owner.revision, start, end), owner, binding

    def expected(self, nonce, principal):
        with self.access._connection() as conn:
            return self._challenge(conn, nonce, principal)[0].expected

    def admit_challenge(self, challenge, principal):
        if type(challenge) is not ConnectedChallenge:
            raise AccessDenied("connected_session_denied")
        with self.access._connection() as conn:
            if self._challenge(conn, challenge.expected.nonce, principal)[0] != challenge:
                raise AccessDenied("connected_session_denied")

    def _issue(self, conn, owner, binding, now, valid_from, valid_until):
        self._cleanup(conn, "connected_sessions", now, owner, binding)
        if conn.execute("SELECT count(*) FROM connected_sessions").fetchone()[0] >= MAX_SESSIONS:
            raise AccessUnavailable()
        now = self._now()  # final write sample, after cleanup/capacity work
        if not valid_from <= now < valid_until:
            raise AccessDenied("connected_session_denied")
        token = secrets.token_urlsafe(32)
        end = now + SESSION_SECONDS
        conn.execute("INSERT INTO connected_sessions VALUES(?,?,?,?,?,?,?)",
                     (_digest(token), owner.revision, binding.revision, binding.generation, self._snapshot(binding), now, end))
        return ConnectedSession(token, ConnectedAdmission(owner, binding, now, end))

    def issue(self, attestation, principal):
        if type(attestation) is not SessionAttestation:
            raise AccessDenied("connected_session_denied")
        with self.access._connection(write=True) as conn:
            challenge, owner, binding = self._challenge(conn, attestation.expected.nonce, principal)
            now = self._now()
            if (challenge.expected != attestation.expected
                    or type(attestation.issued_at) is not int or type(attestation.expires_at) is not int
                    or not 0 < attestation.expires_at - attestation.issued_at <= CHALLENGE_SECONDS
                    or not max(challenge.created_at, attestation.issued_at) <= now
                    < min(challenge.expires_at, attestation.expires_at)):
                raise AccessDenied("connected_session_denied")
            session = self._issue(conn, owner, binding, now,
                max(challenge.created_at, attestation.issued_at), min(challenge.expires_at, attestation.expires_at))
            conn.execute("DELETE FROM connected_challenges WHERE digest=?", (_digest(challenge.expected.nonce),))
            return session

    def _admit(self, conn, token, principal, host_uid, operational_origin):
        if not _canonical_32_bytes(token):
            raise AccessDenied("connected_session_denied")
        owner, binding = self._context(conn, principal)
        row = conn.execute("SELECT * FROM connected_sessions WHERE digest=?", (_digest(token),)).fetchone()
        if row is None:
            raise AccessDenied("connected_session_denied")
        start, end = _times(row, SESSION_SECONDS)
        if (not self._matches(row, owner, binding) or not start <= self._now() < end
                or host_uid != owner.host_uid or operational_origin != binding.operational_origin):
            raise AccessDenied("connected_session_denied")
        return ConnectedAdmission(owner, binding, start, end)

    def authorize(self, token, principal, *, host_uid, operational_origin):
        with self.access._connection() as conn:
            return self._admit(conn, token, principal, host_uid, operational_origin)

    def authorize_action(self, kind, token, principal, *, host_uid, fleet_uid, operational_origin):
        """Connected session AND explicit local grant in one read snapshot.

        Fixed kinds only. This never grants, registers, or issues a direct session.
        Grants remain native-principal/fleet specific, not workspace specific.
        """
        if kind not in ("message", "nudge", "feedback", "permission"):
            raise AccessDenied("unsupported_owner_action")
        fleet_uid = _canonical_uid(fleet_uid, "fleet", action=kind)
        with self.access._connection() as conn:
            admission = self._admit(conn, token, principal, host_uid, operational_origin)
            grant = self.access._action_grant(conn, admission.owner, fleet_uid, kind)
            return admission, grant

    def renew(self, token, principal, *, host_uid, operational_origin):
        with self.access._connection(write=True) as conn:
            admission = self._admit(conn, token, principal, host_uid, operational_origin)
            now = self._now()
            if not admission.created_at <= now < admission.expires_at:
                raise AccessDenied("connected_session_denied")
            conn.execute("DELETE FROM connected_sessions WHERE digest=?", (_digest(token),))
            return self._issue(conn, admission.owner, admission.binding, now,
                               admission.created_at, admission.expires_at)

    def end(self, token, principal, *, host_uid, operational_origin):
        with self.access._connection(write=True) as conn:
            self._admit(conn, token, principal, host_uid, operational_origin)
            conn.execute("DELETE FROM connected_sessions WHERE digest=?", (_digest(token),))
