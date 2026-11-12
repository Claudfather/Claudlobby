"""Finite connected-session evidence; never enrollment approval or read authority."""
from dataclasses import dataclass, field

from .workspace_assertion import (AssertionRefused, AssertionUnavailable, _canonical_uuid,
                                  _canonical_32_bytes, _origin, _HOST, _KID, _public_keys)

PURPOSE = "claudfather-workspace-host-session-v1"
_CLAIMS = frozenset({"iss", "sub", "workspace_id", "host_uid", "nonce", "operational_origin",
                     "binding_revision", "binding_generation", "aud", "purpose", "iat", "nbf", "exp", "jti"})


@dataclass(frozen=True)
class ExpectedSessionAssertion:
    issuer: str
    subject: str
    workspace_id: str
    host_uid: str
    nonce: str = field(repr=False)
    operational_origin: str
    binding_revision: int
    binding_generation: str


@dataclass(frozen=True)
class SessionAttestation:
    expected: ExpectedSessionAssertion = field(repr=False)
    issued_at: int
    expires_at: int
    jti: str


def verify_session_assertion(assertion, public_jwks, *, expected):
    """Maintained signature/time checking plus one fixed purpose/claim profile."""
    if (type(expected) is not ExpectedSessionAssertion
            or not _origin(expected.issuer, False) or not _origin(expected.operational_origin, False)
            or not isinstance(expected.host_uid, str) or not _HOST.fullmatch(expected.host_uid)
            or not _canonical_32_bytes(expected.nonce)
            or not all(_canonical_uuid(v) for v in (expected.subject, expected.workspace_id,
                                                     expected.binding_generation))
            or type(expected.binding_revision) is not int or not 0 < expected.binding_revision < 2**53):
        raise AssertionUnavailable()
    if not isinstance(assertion, str) or not assertion.isascii() or not 1 <= len(assertion) <= 8192:
        raise AssertionRefused()
    try:
        import jwt
    except ImportError:
        raise AssertionUnavailable() from None
    keys = _public_keys(public_jwks, jwt)
    try:
        header = jwt.get_unverified_header(assertion)
        if (set(header) - {"alg", "kid", "typ"} or header.get("alg") != "EdDSA"
                or header.get("typ", "JWT") != "JWT" or not isinstance(header.get("kid"), str)
                or not _KID.fullmatch(header["kid"])):
            raise AssertionRefused()
        key = keys.get(header["kid"])
        if key is None:
            raise AssertionUnavailable()
        claims = jwt.decode_complete(assertion, key, algorithms=["EdDSA"], issuer=expected.issuer,
            audience=f"urn:claudfather:host:{expected.host_uid}:workspace-read-session:v1",
            options={"require": sorted(_CLAIMS), "strict_aud": True}, leeway=5)["payload"]
        if (set(claims) != _CLAIMS or claims["purpose"] != PURPOSE
                or any(claims[k] != v for k, v in {
                    "sub": expected.subject, "workspace_id": expected.workspace_id,
                    "host_uid": expected.host_uid, "nonce": expected.nonce,
                    "operational_origin": expected.operational_origin,
                    "binding_revision": expected.binding_revision,
                    "binding_generation": expected.binding_generation}.items())
                or type(claims["binding_revision"]) is not int
                or not _canonical_uuid(claims["jti"])
                or not all(type(claims[k]) is int and 0 < claims[k] < 2**53 for k in ("iat", "nbf", "exp"))
                or claims["iat"] != claims["nbf"] or not 0 < claims["exp"] - claims["iat"] <= 120):
            raise AssertionRefused()
        return SessionAttestation(expected, claims["iat"], claims["exp"], claims["jti"])
    except (jwt.PyJWTError, ValueError, TypeError, RecursionError):
        raise AssertionRefused() from None
