"""Real maintained Ed25519 verification; no issuer, native identity or grant fixture."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
import base64
import json
import subprocess
import sys
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from claudlobby.plane.workspace_assertion import (
    AssertionRefused, AssertionUnavailable, ExpectedWorkspaceAssertion, PURPOSE,
    verify_workspace_assertion,
)
from tests.conftest import constructed_env


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture
def profile(monkeypatch):
    now = 1800000000

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(now, tz=tz)

    monkeypatch.setattr(jwt.api_jwt, "datetime", Clock)
    private = Ed25519PrivateKey.generate()
    key = {"kty": "OKP", "crv": "Ed25519", "kid": "test-key",
           "x": b64(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)),
           "alg": "EdDSA", "use": "sig"}
    expected = ExpectedWorkspaceAssertion("https://shell.example", "host_" + "a" * 32,
                                         b64(bytes(range(32))), "https://plane.example")
    claims = {"iss": expected.issuer, "sub": str(uuid4()), "workspace_id": str(uuid4()),
              "host_uid": expected.host_uid, "nonce": expected.nonce,
              "operational_origin": expected.operational_origin,
              "aud": f"urn:claudfather:host:{expected.host_uid}:workspace-read-bootstrap:v1",
              "purpose": PURPOSE, "iat": now, "nbf": now, "exp": now + 120, "jti": str(uuid4())}
    return private, key, expected, claims


def verify(profile, *, claims=None, header=None, keys=None, expected=None, signer=None):
    private, key, bound, original = profile
    token = jwt.encode(original if claims is None else claims, signer or private,
                       algorithm="EdDSA", headers={"kid": key["kid"], **(header or {})})
    return verify_workspace_assertion(token, json.dumps({"keys": keys or [key]}).encode(),
                                      expected=expected or bound)


@pytest.mark.parametrize("typ", [None, "JWT"])
def test_real_signature_returns_only_frozen_attestation(profile, typ):
    result = verify(profile, header={"typ": typ})
    assert result.subject == profile[3]["sub"]
    assert result.workspace_id == profile[3]["workspace_id"]
    assert result.host_uid == profile[2].host_uid
    assert result.expires_at - result.issued_at == 120
    assert not hasattr(result, "session") and not hasattr(result, "grant")
    with pytest.raises(FrozenInstanceError):
        result.workspace_id = str(uuid4())


def test_wrong_real_signer_is_refused(profile):
    with pytest.raises(AssertionRefused, match=r"^Workspace assertion refused\.$"):
        verify(profile, signer=Ed25519PrivateKey.generate())


@pytest.mark.parametrize("field", ["iss", "sub", "workspace_id", "host_uid", "nonce",
    "operational_origin", "aud", "purpose", "iat", "nbf", "exp", "jti"])
def test_every_claim_is_required(profile, field):
    claims = dict(profile[3]); del claims[field]
    with pytest.raises(AssertionRefused):
        verify(profile, claims=claims)


@pytest.mark.parametrize("field,value", [
    ("iss", "https://other.example"), ("aud", "urn:claudfather:workspace-bootstrap-unbound"),
    ("aud", ["urn:claudfather:workspace-bootstrap-unbound"]),
    ("purpose", "ordinary-user"), ("host_uid", "host_" + "b" * 32),
    ("nonce", b64(b"x" * 32)), ("nonce", b64(bytes(range(32))) + "="),
    ("operational_origin", "https://other.example"), ("sub", "person@example.invalid"),
    ("workspace_id", "not-a-uuid"), ("jti", "not-a-uuid"),
])
def test_signed_wrong_identity_or_tuple_is_refused(profile, field, value):
    with pytest.raises(AssertionRefused):
        verify(profile, claims={**profile[3], field: value})


def test_signed_ordinary_user_jwt_and_extra_claim_are_refused(profile):
    with pytest.raises(AssertionRefused):
        verify(profile, claims={"sub": profile[3]["sub"], "iat": profile[3]["iat"], "exp": profile[3]["exp"]})
    with pytest.raises(AssertionRefused):
        verify(profile, claims={**profile[3], "email": "person@example.invalid"})


@pytest.mark.parametrize("field", ["iat", "nbf", "exp"])
@pytest.mark.parametrize("kind", ["string", "float", "boolean", "infinity", "nan"])
def test_time_fields_are_integers_not_coercions(profile, field, kind):
    value = profile[3][field]
    value = {"string": str(value), "float": float(value), "boolean": True,
             "infinity": float("inf"), "nan": float("nan")}[kind]
    with pytest.raises(AssertionRefused):
        verify(profile, claims={**profile[3], field: value})


@pytest.mark.parametrize("changes", [
    {"exp": 1800000121}, {"exp": 1800000000}, {"nbf": 1799999999},
    {"iat": 1800000006, "nbf": 1800000006, "exp": 1800000120},
    {"iat": 1799999900, "nbf": 1799999900, "exp": 1799999994},
])
def test_lifetime_not_before_future_and_expiry_are_refused(profile, changes):
    with pytest.raises(AssertionRefused):
        verify(profile, claims={**profile[3], **changes})


def test_library_leeway_is_bounded_and_short_session_lifetime_works(profile):
    assert verify(profile, claims={**profile[3], "exp": 1800000001}).expires_at == 1800000001
    assert verify(profile, claims={**profile[3], "iat": 1799999900, "nbf": 1799999900,
                                  "exp": 1799999996}).expires_at == 1799999996


@pytest.mark.parametrize("header", [
    {"jku": "https://keys.example"}, {"x5u": "https://keys.example"}, {"jwk": {}},
    {"x5c": []}, {"crit": []}, {"typ": "workspace"}, {"kid": ""}, {"kid": "x" * 129},
])
def test_untrusted_header_cannot_supply_keys_or_extensions(profile, header):
    with pytest.raises(AssertionRefused):
        verify(profile, header=header)


def test_algorithm_is_fixed_even_for_valid_other_signature(profile):
    token = jwt.encode(profile[3], "a" * 32, algorithm="HS256", headers={"kid": "test-key"})
    with pytest.raises(AssertionRefused):
        verify_workspace_assertion(token, json.dumps({"keys": [profile[1]]}).encode(), expected=profile[2])


@pytest.mark.parametrize("change", [
    {"kty": "RSA"}, {"crv": "Ed448"}, {"alg": "HS256"}, {"use": "enc"},
    {"key_ops": ["sign"]}, {"d": "private-material"}, {"jku": "https://keys.example"},
    {"x": "bad"}, {"kid": "x" * 129},
])
def test_malformed_or_nonpublic_configured_key_set_is_unavailable(profile, change):
    with pytest.raises(AssertionUnavailable):
        verify(profile, keys=[{**profile[1], **change}])


def test_unknown_rotation_key_is_unavailable_and_duplicate_kids_never_select(profile):
    with pytest.raises(AssertionUnavailable):
        verify(profile, header={"kid": "new-key"})
    with pytest.raises(AssertionUnavailable):
        verify(profile, keys=[profile[1], profile[1]])
    with pytest.raises(AssertionUnavailable):
        verify(profile, keys=[profile[1], {**profile[1], "kid": "bad-key", "x": "bad"}])


def test_rotation_set_accepts_each_known_key_and_refuses_ninth(profile):
    rotated = Ed25519PrivateKey.generate()
    new = {**profile[1], "kid": "new-key",
           "x": b64(rotated.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))}
    assert verify(profile, keys=[profile[1], new])
    assert verify(profile, signer=rotated, header={"kid": "new-key"}, keys=[profile[1], new])
    with pytest.raises(AssertionUnavailable):
        verify(profile, keys=[{**profile[1], "kid": str(i)} for i in range(9)])


@pytest.mark.parametrize("raw", [b"", b"x" * 65537, b"[]", b'{"keys":[]}',
    b'{"keys":[],"keys":[]}', b'{"keys":[42]}', b'{"keys":{},"private":true}', b"\xff"])
def test_bounded_jwks_json_failures_are_fixed_unavailable(profile, raw):
    with pytest.raises(AssertionUnavailable, match=r"^Workspace assertion verification unavailable\.$"):
        verify_workspace_assertion("token", raw, expected=profile[2])


@pytest.mark.parametrize("token", ["", "x" * 8193, "not.a.jwt", "é"])
def test_bounded_token_failures_are_fixed_refused(profile, token):
    with pytest.raises(AssertionRefused):
        verify_workspace_assertion(token, json.dumps({"keys": [profile[1]]}).encode(), expected=profile[2])


@pytest.mark.parametrize("origin", ["http://shell.example", "https://shell.example/", "https://SHELL.example",
    "https://shell.example:443", "https://user@shell.example", "https://shell.example?x=1"])
def test_configured_origins_are_canonical_https(profile, origin):
    with pytest.raises(AssertionUnavailable):
        verify(profile, expected=replace(profile[2], issuer=origin))


def test_loopback_http_requires_explicit_fixture_flag(profile):
    expected = replace(profile[2], issuer="http://localhost:9001", operational_origin="http://127.0.0.1:9002")
    claims = {**profile[3], "iss": expected.issuer, "operational_origin": expected.operational_origin}
    with pytest.raises(AssertionUnavailable):
        verify(profile, expected=expected, claims=claims)
    assert verify(profile, expected=replace(expected, allow_loopback_http=True), claims=claims)
    with pytest.raises(AssertionUnavailable):
        verify(profile, expected=replace(expected, issuer="http://remote.example", allow_loopback_http=True))


def test_nonce_roundtrip_rejects_noncanonical_pad_bits(profile):
    nonce = b64(bytes(32))
    assert nonce.endswith("A")
    with pytest.raises(AssertionUnavailable):
        verify(profile, expected=replace(profile[2], nonce=nonce[:-1] + "B"))


def test_core_and_pure_module_import_without_optional_jwt(tmp_path):
    script = '''
import sys
class DenyJWT:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'jwt' or fullname.startswith('jwt.'):
            raise ModuleNotFoundError('optional JWT intentionally absent')
sys.meta_path.insert(0, DenyJWT())
from claudlobby.plane import db
from claudlobby.plane.workspace_assertion import *
assert not any(name == 'jwt' or name.startswith('jwt.') for name in sys.modules)
expected = ExpectedWorkspaceAssertion('https://shell.example', 'host_'+'a'*32, 'A'*43, 'https://plane.example')
try:
    verify_workspace_assertion('token', b'{}', expected=expected)
except AssertionUnavailable:
    pass
else:
    raise AssertionError('missing optional verifier must be unavailable')
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=constructed_env(),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
