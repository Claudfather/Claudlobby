"""CANON_V1 — the one definition of canonical bytes (design v2 §9).

The hash gate and cross-host payload comparison both depend on identical
states producing identical bytes. Rules, in full:

  encoding      UTF-8, ensure_ascii=False
  unicode       every str (key or value) NFC-normalized before serialization
  ordering      dict keys sorted by post-normalization code point
  whitespace    none (separators ",", ":")
  numbers       int and bool only; float raises (platform-dependent repr
                would silently fork hashes — registry payloads never need it)
  null          None is serialized as null and always included
  containers    dict and list only; anything else raises
  paths         producer's duty: absolute POSIX form before handing over —
                canonicalization never rewrites values
  hash          sha256 over the canonical bytes, rendered "sha256:<hex>"

Any change to these rules is a new CANON_VERSION and a new golden-fixture
set — never an in-place edit.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

CANON_VERSION = "canon-1"


class CanonicalizationError(ValueError):
    """The object violates the CANON_V1 value contract."""


def _normalize(obj: object) -> object:
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, bool) or obj is None or isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        raise CanonicalizationError("floats are not canonicalizable (CANON_V1)")
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CanonicalizationError(f"non-string key: {k!r}")
            nk = unicodedata.normalize("NFC", k)
            if nk in out:
                # Two distinct input keys normalizing to one NFC key would
                # silently drop a value — round-2 F9 input hardening.
                raise CanonicalizationError(f"NFC key collision: {k!r}")
            out[nk] = _normalize(v)
        return out
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    raise CanonicalizationError(f"unsupported type: {type(obj).__name__}")


def canonical_bytes(obj: object) -> bytes:
    return json.dumps(
        _normalize(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def canonical_hash(obj: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(obj)).hexdigest()
