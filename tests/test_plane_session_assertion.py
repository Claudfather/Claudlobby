"""Actual maintained signature verification; no website interop claim."""
from dataclasses import replace
import json

import jwt
import pytest

from claudlobby.plane.session_assertion import PURPOSE, ExpectedSessionAssertion, verify_session_assertion
from claudlobby.plane.workspace_assertion import AssertionRefused, AssertionUnavailable
from tests.test_plane_workspace_assertion import profile


def signed(profile, **changes):
    _, _, old, values = profile
    expected = ExpectedSessionAssertion(old.issuer, values["sub"], values["workspace_id"], old.host_uid,
        old.nonce, old.operational_origin, 1, "00000000-0000-4000-8000-000000000001")
    claims = {**values, "purpose": PURPOSE,
        "aud": f"urn:claudfather:host:{old.host_uid}:workspace-read-session:v1",
        "binding_revision": expected.binding_revision, "binding_generation": expected.binding_generation,
        **changes}
    token = jwt.encode(claims, profile[0], algorithm="EdDSA", headers={"kid": profile[1]["kid"]})
    return token, json.dumps({"keys": [profile[1]]}).encode(), expected


def test_real_signature_finite_session_evidence_only(profile):
    token, keys, expected = signed(profile)
    result = verify_session_assertion(token, keys, expected=expected)
    assert result.expected == expected and result.expires_at - result.issued_at == 120
    assert not hasattr(result, "token") and not hasattr(result, "reader")


@pytest.mark.parametrize("change", [
    {"purpose": "claudfather-workspace-host-bootstrap-v1"},
    {"purpose": "claudfather-plane-document-v1"}, {"purpose": "ordinary-user"},
    {"binding_generation": "00000000-0000-4000-8000-000000000002"},
    {"binding_revision": True}, {"binding_revision": 2},
    {"sub": "00000000-0000-4000-8000-000000000002"},
    {"workspace_id": "00000000-0000-4000-8000-000000000002"},
    {"exp": 1800000121}, {"iat": True}, {"nbf": 1800000001},
    {"jti": "invalid"}, {"extra": 1}, {"aud": "unbound"},
])
def test_wrong_purpose_context_shape_or_time_refused(profile, change):
    token, keys, expected = signed(profile, **change)
    with pytest.raises(AssertionRefused):
        verify_session_assertion(token, keys, expected=expected)


def test_unusable_configuration_keys_unknown_key_distinct(profile):
    token, keys, expected = signed(profile)
    with pytest.raises(AssertionUnavailable):
        verify_session_assertion(token, b"{}", expected=expected)
    with pytest.raises(AssertionUnavailable):
        verify_session_assertion(token, keys, expected=replace(expected, binding_revision=0))
    key = {**profile[1], "kid": "other"}
    with pytest.raises(AssertionUnavailable):
        verify_session_assertion(token, json.dumps({"keys": [key]}).encode(), expected=expected)
