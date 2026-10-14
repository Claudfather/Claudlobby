"""Local workspace proposals in the canonical owner authority transaction.

Only trusted callers may stage a WorkspaceAttestation returned by the pure
verifier and a freshly verified native principal. Dataclass shape is not proof.
No method here issues a browser credential or a fleet action grant.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
import secrets

from .owner_access import (AccessDenied, AccessUnavailable, MAX_PENDING,
                           OwnerAccess, OwnerGrant, PrincipalRef, _digest)
from .workspace_assertion import (ExpectedWorkspaceAssertion, WorkspaceAttestation,
                                  _canonical_uuid, _origin)

_NATIVE = "tailscale:controlplane.tailscale.com"
_CHALLENGE_SECONDS = 300
_ASSERTION_SECONDS = 120
_SCHEMA = (
    "CREATE TABLE workspace_metadata(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)",
    "INSERT INTO workspace_metadata VALUES(1,1)",
    """CREATE TABLE workspace_challenges(digest TEXT PRIMARY KEY, owner_revision INTEGER NOT NULL,
       namespace TEXT NOT NULL, subject TEXT NOT NULL, created_at REAL NOT NULL,
       expires_at REAL NOT NULL, issuer TEXT NOT NULL, operational_origin TEXT NOT NULL,
       binding_revision INTEGER NOT NULL, proposal TEXT)""",
    """CREATE TABLE workspace_bindings(revision INTEGER PRIMARY KEY, owner_revision INTEGER NOT NULL,
       generation TEXT NOT NULL, issuer TEXT NOT NULL, subject TEXT NOT NULL, workspace_id TEXT NOT NULL,
       operational_origin TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)), changed_at REAL NOT NULL)""",
)


@dataclass(frozen=True)
class WorkspaceChallenge:
    nonce: str = field(repr=False)
    owner: OwnerGrant
    issuer: str
    operational_origin: str
    expires_at: float


@dataclass(frozen=True)
class WorkspaceProposal:
    nonce: str = field(repr=False)
    owner: OwnerGrant
    attestation: WorkspaceAttestation = field(repr=False)
    expires_at: float
    created_at: float
    previous_state: str
    previous_binding: WorkspaceBinding | None


@dataclass(frozen=True)
class WorkspaceBinding:
    owner: OwnerGrant
    revision: int
    generation: str
    issuer: str
    subject: str
    workspace_id: str
    operational_origin: str
    active: bool
    changed_at: float


class OwnerWorkspace:
    """Optional schema, sharing OwnerAccess's private-file and transaction owner."""
    def __init__(self, access: OwnerAccess):
        self.access = access

    @staticmethod
    def _schema(conn):
        rows = conn.execute("SELECT singleton,version FROM workspace_metadata").fetchall()
        if len(rows) != 1 or tuple(rows[0]) != (1, 1):
            raise AccessUnavailable()
        # Missing tables/columns are unavailable, never silently initialized.
        conn.execute("SELECT digest,owner_revision,namespace,subject,created_at,expires_at,issuer,"
                     "operational_origin,binding_revision,proposal FROM workspace_challenges LIMIT 0")
        conn.execute("SELECT revision,owner_revision,generation,issuer,subject,workspace_id,"
                     "operational_origin,active,changed_at FROM workspace_bindings LIMIT 0")

    def _owner(self, conn, principal):
        owner = self.access._owner(conn, principal)
        if (principal.namespace != _NATIVE or not principal.subject.isascii()
                or not principal.subject.isdecimal() or int(principal.subject) <= 0
                or str(int(principal.subject)) != principal.subject):
            raise AccessDenied("workspace_native_owner_required")
        return owner

    def initialize(self, *, expected_owner):
        with self.access._connection(write=True) as conn:
            self.access._expected_owner(conn, expected_owner)
            self._owner(conn, expected_owner.principal)
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            names = {"workspace_metadata", "workspace_challenges", "workspace_bindings"}
            if not tables & names:
                for statement in _SCHEMA:
                    conn.execute(statement)
            self._schema(conn)  # partial/unsupported schemas refuse; no repair

    def _binding(self, conn):
        row = conn.execute("SELECT * FROM workspace_bindings ORDER BY revision DESC LIMIT 1").fetchone()
        if row is None:
            return None
        if (type(row["revision"]) is not int or row["revision"] <= 0
                or type(row["owner_revision"]) is not int or row["owner_revision"] <= 0
                or row["active"] not in (0, 1) or not _canonical_uuid(row["generation"])
                or not _canonical_uuid(row["subject"]) or not _canonical_uuid(row["workspace_id"])
                or not _origin(row["issuer"], False) or not _origin(row["operational_origin"], False)
                or not isinstance(row["changed_at"], (int, float))
                or not math.isfinite(row["changed_at"]) or row["changed_at"] < 0):
            raise AccessUnavailable()
        owner_row = conn.execute("SELECT * FROM grants WHERE revision=?", (row["owner_revision"],)).fetchone()
        if owner_row is None or owner_row["active"] != 1:
            raise AccessUnavailable()
        owner = OwnerGrant(self.access._grant(conn).host_uid,
                           PrincipalRef(owner_row["namespace"], owner_row["subject"]),
                           owner_row["revision"], True)
        return WorkspaceBinding(owner, row["revision"], row["generation"], row["issuer"],
                                row["subject"], row["workspace_id"], row["operational_origin"],
                                bool(row["active"]), row["changed_at"])

    @staticmethod
    def _state(binding, owner):
        return ("absent" if binding is None else "revoked" if not binding.active else
                "active" if owner == binding.owner else "stale-owner")

    def status(self):
        """Local retained metadata only; a stale owner never becomes active again."""
        with self.access._connection() as conn:
            self._schema(conn)
            binding = self._binding(conn)
            owner = self.access._grant(conn)
            state = self._state(binding, owner)
            return state, binding

    def _active_binding(self, conn, principal):
        """Current native owner and effective binding in the caller's transaction."""
        self._schema(conn)
        owner = self._owner(conn, principal)
        binding = self._binding(conn)
        if binding is None or self._state(binding, owner) != "active":
            raise AccessDenied("workspace_binding_required")
        return binding

    def active_binding(self, principal):
        """Fresh native owner admission, not retained status flags."""
        with self.access._connection() as conn:
            return self._active_binding(conn, principal)

    def begin(self, principal: PrincipalRef, *, issuer: str, operational_origin: str):
        if not _origin(issuer, False) or not _origin(operational_origin, False):
            raise AccessDenied("invalid_workspace_configuration")
        with self.access._connection(write=True) as conn:
            self._schema(conn)
            owner = self._owner(conn, principal)
            binding = self._binding(conn)
            if binding is not None and binding.active:
                raise AccessDenied("workspace_already_bound")
            now = self.access._now()
            conn.execute("DELETE FROM workspace_challenges WHERE expires_at<=? OR created_at>? OR owner_revision<>?",
                         (now, now, owner.revision))
            if conn.execute("SELECT count(*) FROM workspace_challenges").fetchone()[0] >= MAX_PENDING:
                raise AccessDenied("too_many_workspace_requests")
            nonce = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO workspace_challenges VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                         (_digest(nonce), owner.revision, principal.namespace, principal.subject,
                          now, now + _CHALLENGE_SECONDS, issuer, operational_origin, binding.revision if binding else 0))
            return WorkspaceChallenge(nonce, owner, issuer, operational_origin, now + _CHALLENGE_SECONDS)

    def _challenge(self, conn, nonce, principal=None):
        self._schema(conn)
        row = conn.execute("SELECT * FROM workspace_challenges WHERE digest=?", (_digest(nonce),)).fetchone()
        now = self.access._now()
        if row is not None and (not all(isinstance(row[name], (int, float)) and math.isfinite(row[name])
                for name in ("created_at", "expires_at"))
                or not 0 <= row["created_at"] < row["expires_at"] <= row["created_at"] + _CHALLENGE_SECONDS
                or type(row["owner_revision"]) is not int or row["owner_revision"] <= 0
                or type(row["binding_revision"]) is not int or row["binding_revision"] < 0):
            raise AccessUnavailable()
        if row is None or not row["created_at"] <= now < row["expires_at"]:
            raise AccessDenied("workspace_request_expired")
        stored_principal = PrincipalRef(row["namespace"], row["subject"])
        owner = self._owner(conn, stored_principal if principal is None else principal)
        binding = self._binding(conn)
        if (stored_principal != owner.principal or row["owner_revision"] != owner.revision
                or row["binding_revision"] != (binding.revision if binding else 0)
                or binding is not None and binding.active):
            raise AccessDenied("workspace_binding_changed")
        if not _origin(row["issuer"], False) or not _origin(row["operational_origin"], False):
            raise AccessUnavailable()
        return row, owner

    def expected(self, nonce, principal):
        """Trusted verifier inputs read from an actual outstanding local challenge."""
        with self.access._connection() as conn:
            row, owner = self._challenge(conn, nonce, principal)
            return ExpectedWorkspaceAssertion(row["issuer"], owner.host_uid, nonce, row["operational_origin"])

    @staticmethod
    def _attestation(value):
        if (not isinstance(value, WorkspaceAttestation)
                or not all(_canonical_uuid(getattr(value, name)) for name in ("subject", "workspace_id", "jti"))
                or type(value.issued_at) is not int or type(value.expires_at) is not int
                or not 0 < value.issued_at < value.expires_at <= 2**53 - 1
                or value.expires_at - value.issued_at > _ASSERTION_SECONDS):
            raise AccessDenied("invalid_workspace_attestation")
        metadata = asdict(value)
        del metadata["nonce"]  # retain only its digest in the challenge key
        return json.dumps(metadata, sort_keys=True, separators=(",", ":"))

    def stage(self, attestation: WorkspaceAttestation, principal: PrincipalRef):
        """Internal verified-evidence door, not a public token/JSON authenticator."""
        proposal = self._attestation(attestation)
        with self.access._connection(write=True) as conn:
            row, owner = self._challenge(conn, attestation.nonce, principal)
            now = self.access._now()
            if (attestation.host_uid != owner.host_uid or attestation.issuer != row["issuer"]
                    or attestation.operational_origin != row["operational_origin"]
                    or not int(row["created_at"]) <= attestation.issued_at <= now < attestation.expires_at):
                raise AccessDenied("workspace_attestation_mismatch")
            if row["proposal"] is not None and row["proposal"] != proposal:
                raise AccessDenied("workspace_proposal_changed")
            conn.execute("UPDATE workspace_challenges SET proposal=?,expires_at=? WHERE digest=?",
                         (proposal, min(row["expires_at"], attestation.expires_at), _digest(attestation.nonce)))
            binding = self._binding(conn)
            return WorkspaceProposal(attestation.nonce, owner, attestation,
                                     min(row["expires_at"], attestation.expires_at),
                                     row["created_at"], self._state(binding, owner), binding)

    def admit_challenge(self, expected_challenge: WorkspaceChallenge, principal: PrincipalRef):
        """Readonly exact issued-state closing admission with fresh native identity."""
        if not isinstance(expected_challenge, WorkspaceChallenge):
            raise AccessDenied("workspace_request_changed")
        with self.access._connection() as conn:
            row, owner = self._challenge(conn, expected_challenge.nonce, principal)
            current = WorkspaceChallenge(expected_challenge.nonce, owner, row["issuer"],
                                         row["operational_origin"], row["expires_at"])
            if current != expected_challenge or row["proposal"] is not None:
                raise AccessDenied("workspace_request_changed")

    def admit_proposal(self, expected_proposal: WorkspaceProposal, principal: PrincipalRef):
        """Readonly full retained-proposal closing admission, never confirmation."""
        if not isinstance(expected_proposal, WorkspaceProposal):
            raise AccessDenied("workspace_proposal_changed")
        with self.access._connection() as conn:
            if self._proposal(conn, expected_proposal.nonce, principal) != expected_proposal:
                raise AccessDenied("workspace_proposal_changed")

    def _proposal(self, conn, nonce, principal=None):
        row, owner = self._challenge(conn, nonce, principal)
        if row["proposal"] is None:
            raise AccessDenied("workspace_proposal_missing")
        try:
            value = WorkspaceAttestation(nonce=nonce, **json.loads(row["proposal"]))
            encoded = self._attestation(value)
        except (TypeError, ValueError, AccessDenied):
            raise AccessUnavailable() from None
        if (encoded != row["proposal"] or value.host_uid != owner.host_uid
                or value.issuer != row["issuer"] or value.operational_origin != row["operational_origin"]
                or not int(row["created_at"]) <= value.issued_at <= self.access._now() < value.expires_at
                or row["expires_at"] > value.expires_at):
            raise AccessUnavailable()
        binding = self._binding(conn)
        return WorkspaceProposal(nonce, owner, value, row["expires_at"], row["created_at"],
                                 self._state(binding, owner), binding)

    def inspect(self, nonce):
        with self.access._connection() as conn:
            return self._proposal(conn, nonce)

    def confirm(self, *, expected_proposal: WorkspaceProposal):
        from uuid import uuid4
        if not isinstance(expected_proposal, WorkspaceProposal):
            raise AccessDenied("workspace_proposal_changed")
        with self.access._connection(write=True) as conn:
            proposal = self._proposal(conn, expected_proposal.nonce)
            if proposal != expected_proposal:
                raise AccessDenied("workspace_proposal_changed")
            previous = proposal.previous_binding
            a = proposal.attestation
            revision = previous.revision + 1 if previous else 1
            generation = str(uuid4())
            now = self.access._now()
            if not max(proposal.created_at, a.issued_at) <= now < proposal.expires_at:
                raise AccessDenied("workspace_request_expired")
            conn.execute("INSERT INTO workspace_bindings VALUES(?,?,?,?,?,?,?,1,?)",
                         (revision, proposal.owner.revision, generation, a.issuer, a.subject,
                          a.workspace_id, a.operational_origin, now))
            conn.execute("DELETE FROM workspace_challenges")
            return self._binding(conn)

    def revoke(self, *, expected_binding: WorkspaceBinding, expected_owner: OwnerGrant):
        with self.access._connection(write=True) as conn:
            self._schema(conn)
            binding = self._binding(conn)
            if (not isinstance(expected_binding, WorkspaceBinding) or binding != expected_binding
                    or not binding.active):
                raise AccessDenied("workspace_binding_changed")
            self.access._expected_owner(conn, expected_owner)
            self._owner(conn, expected_owner.principal)
            conn.execute("INSERT INTO workspace_bindings VALUES(?,?,?,?,?,?,?,0,?)",
                         (binding.revision + 1, binding.owner.revision, binding.generation, binding.issuer,
                          binding.subject, binding.workspace_id, binding.operational_origin, self.access._now()))
            conn.execute("DELETE FROM workspace_challenges")
            return self._binding(conn)
