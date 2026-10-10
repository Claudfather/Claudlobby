"""Pure, optional verification of workspace bootstrap evidence, never host access."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import ipaddress
import json
import re
from urllib.parse import urlsplit
from uuid import UUID

PURPOSE = "claudfather-workspace-host-bootstrap-v1"
_CLAIMS = frozenset({"iss", "sub", "workspace_id", "host_uid", "nonce",
                     "operational_origin", "aud", "purpose", "iat", "nbf", "exp", "jti"})
_HOST = re.compile(r"host_[0-9a-f]{32}\Z")
_KID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_BASE64 = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class AssertionRefused(Exception):
    def __init__(self) -> None:
        super().__init__("Workspace assertion refused.")


class AssertionUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__("Workspace assertion verification unavailable.")


@dataclass(frozen=True)
class ExpectedWorkspaceAssertion:
    """Trusted configuration/local challenge tuple, not values from the token."""
    issuer: str
    host_uid: str
    nonce: str
    operational_origin: str
    allow_loopback_http: bool = False


@dataclass(frozen=True)
class WorkspaceAttestation:
    """Signed statement only: no approval, session, replay consume or read grant."""
    issuer: str
    subject: str
    workspace_id: str
    host_uid: str
    nonce: str
    operational_origin: str
    issued_at: int
    expires_at: int
    jti: str


def _canonical_32_bytes(value: object) -> bool:
    if not isinstance(value, str) or not _BASE64.fullmatch(value):
        return False
    try:
        raw = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        return False
    return len(raw) == 32 and base64.urlsafe_b64encode(raw).decode().rstrip("=") == value


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _origin(value: object, allow_loopback_http: bool) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or not value.isascii():
        return False
    try:
        url = urlsplit(value)
        host, port = url.hostname, url.port
        if not host or url.username is not None or url.password is not None:
            return False
        if url.path or url.query or url.fragment or url.scheme not in {"https", "http"}:
            return False
        try:
            address = ipaddress.ip_address(host)
            loopback = address.is_loopback
        except ValueError:
            if not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host):
                return False
            if any(not part or len(part) > 63 or part.startswith("-") or part.endswith("-")
                   for part in host.split(".")):
                return False
            loopback = host == "localhost"
        if url.scheme == "http" and not (allow_loopback_http and loopback):
            return False
        if port is not None and (port == (443 if url.scheme == "https" else 80) or port == 0):
            return False
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority += f":{port}"
        return value == f"{url.scheme}://{authority}"
    except ValueError:
        return False


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate member")
        result[key] = value
    return result


def _public_keys(public_jwks: bytes, jwt) -> dict:
    """Bound/configure before maintained parsing; do not silently skip bad keys."""
    try:
        if not isinstance(public_jwks, bytes) or not 1 <= len(public_jwks) <= 65536:
            raise ValueError
        obj = json.loads(public_jwks, object_pairs_hook=_unique_object)
        if not isinstance(obj, dict) or set(obj) != {"keys"}:
            raise ValueError
        keys = obj["keys"]
        if not isinstance(keys, list) or not 1 <= len(keys) <= 8:
            raise ValueError
        parsed = {}
        for key in keys:
            if not isinstance(key, dict) or not {"kty", "crv", "x", "kid"} <= set(key):
                raise ValueError
            if set(key) - {"kty", "crv", "x", "kid", "alg", "use", "key_ops"}:
                raise ValueError
            kid = key["kid"]
            if not isinstance(kid, str) or not _KID.fullmatch(kid) or kid in parsed:
                raise ValueError
            if key["kty"] != "OKP" or key["crv"] != "Ed25519" or not _canonical_32_bytes(key["x"]):
                raise ValueError
            if key.get("alg", "EdDSA") != "EdDSA" or key.get("use", "sig") != "sig":
                raise ValueError
            if "key_ops" in key and key["key_ops"] != ["verify"]:
                raise ValueError
            parsed[kid] = jwt.PyJWK.from_dict(key, algorithm="EdDSA")
        return parsed
    except (ValueError, TypeError, UnicodeError, RecursionError, jwt.PyJWTError):
        raise AssertionUnavailable() from None


def verify_workspace_assertion(
    assertion: str, public_jwks: bytes, *, expected: ExpectedWorkspaceAssertion,
) -> WorkspaceAttestation:
    """Verify bounded evidence using current library time and fixed five-second leeway.

    No I/O, discovery, clock override or authority mutation. An unknown key is
    unavailable; a signature/profile mismatch with usable keys is refused.
    """
    if (not isinstance(expected, ExpectedWorkspaceAssertion)
            or type(expected.allow_loopback_http) is not bool
            or not _origin(expected.issuer, expected.allow_loopback_http)
            or not _origin(expected.operational_origin, expected.allow_loopback_http)
            or not isinstance(expected.host_uid, str) or not _HOST.fullmatch(expected.host_uid)
            or not _canonical_32_bytes(expected.nonce)):
        raise AssertionUnavailable()
    if not isinstance(assertion, str) or not 1 <= len(assertion) <= 8192 or not assertion.isascii():
        raise AssertionRefused()
    try:
        import jwt
    except ImportError:
        raise AssertionUnavailable() from None
    keys = _public_keys(public_jwks, jwt)
    try:
        header = jwt.get_unverified_header(assertion)
        if (set(header) - {"alg", "kid", "typ"} or header.get("alg") != "EdDSA"
                or ("typ" in header and header["typ"] != "JWT")
                or not isinstance(header.get("kid"), str) or not _KID.fullmatch(header["kid"])):
            raise AssertionRefused()
        key = keys.get(header["kid"])
        if key is None:
            raise AssertionUnavailable()
        claims = jwt.decode_complete(
            assertion, key, algorithms=["EdDSA"], issuer=expected.issuer,
            audience=f"urn:claudfather:host:{expected.host_uid}:workspace-read-bootstrap:v1",
            options={"require": sorted(_CLAIMS), "strict_aud": True}, leeway=5,
        )["payload"]
        if (set(claims) != _CLAIMS or claims["purpose"] != PURPOSE
                or claims["host_uid"] != expected.host_uid
                or claims["nonce"] != expected.nonce
                or claims["operational_origin"] != expected.operational_origin
                or not all(_canonical_uuid(claims[name]) for name in ("sub", "workspace_id", "jti"))
                or not all(type(claims[name]) is int and 0 < claims[name] <= 2**53 - 1
                           for name in ("iat", "nbf", "exp"))
                or claims["nbf"] != claims["iat"]
                or not 0 < claims["exp"] - claims["iat"] <= 120):
            raise AssertionRefused()
        return WorkspaceAttestation(
            issuer=claims["iss"], subject=claims["sub"], workspace_id=claims["workspace_id"],
            host_uid=claims["host_uid"], nonce=claims["nonce"],
            operational_origin=claims["operational_origin"], issued_at=claims["iat"],
            expires_at=claims["exp"], jti=claims["jti"],
        )
    except (jwt.PyJWTError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AssertionRefused() from None
