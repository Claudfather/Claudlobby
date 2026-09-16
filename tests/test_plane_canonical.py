"""CANON_V1: the exact bytes that get hashed. Golden fixtures pin the contract
across versions — a serializer change that alters any golden output is a
schema-version event, not a refactor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudlobby.plane.canonical import (
    CANON_VERSION,
    CanonicalizationError,
    canonical_bytes,
    canonical_hash,
)

GOLDEN = Path(__file__).parent / "fixtures" / "plane" / "canonical_golden.json"


def test_version_constant():
    assert CANON_VERSION == "canon-1"


def test_sorts_keys_and_compacts():
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_nfc_normalizes_keys_and_values():
    # "é" as NFD (e + combining acute) must serialize identically to NFC "é"
    nfd = "é"
    nfc = "é"
    assert canonical_bytes({nfd: nfd}) == canonical_bytes({nfc: nfc})


def test_none_is_included_not_dropped():
    assert canonical_bytes({"a": None}) == b'{"a":null}'


def test_non_ascii_not_escaped():
    assert canonical_bytes({"k": "émoji 🐋"}) == '{"k":"émoji 🐋"}'.encode("utf-8")


def test_nested_containers():
    obj = {"z": [{"b": 1, "a": [True, False, None]}], "a": "x"}
    assert canonical_bytes(obj) == b'{"a":"x","z":[{"a":[true,false,null],"b":1}]}'


def test_float_rejected():
    with pytest.raises(CanonicalizationError):
        canonical_bytes({"a": 1.5})


def test_unsupported_type_rejected():
    with pytest.raises(CanonicalizationError):
        canonical_bytes({"a": {1, 2}})


def test_nfc_key_collision_rejected():
    nfd, nfc = "e\u0301", "\u00e9"   # both normalize to é
    with pytest.raises(CanonicalizationError):
        canonical_bytes({nfd: 1, nfc: 2})


def test_hash_format():
    h = canonical_hash({"a": 1})
    assert h.startswith("sha256:") and len(h) == 7 + 64


def test_golden_fixtures():
    cases = json.loads(GOLDEN.read_text())
    assert len(cases) >= 5
    for case in cases:
        got_bytes = canonical_bytes(case["input"])
        assert got_bytes.decode("utf-8") == case["canonical"], case["name"]
        assert canonical_hash(case["input"]) == case["hash"], case["name"]
