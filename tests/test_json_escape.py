"""json_escape (lib-common) — JSON string escaping for batch fixtures.

The escaper must produce content that round-trips ``json.loads`` when wrapped
in double quotes — including control characters, which would otherwise make a serialized batch invalid.
"""

from __future__ import annotations

import json


from tests.conftest import call_lib_fn


def _escaped(value: str) -> str:
    return call_lib_fn("json_escape", value)


def _roundtrip(value: str) -> str:
    return json.loads(f'"{_escaped(value)}"')


def test_quotes_and_backslashes():
    assert _roundtrip('say "hi" \\ there') == 'say "hi" \\ there'


def test_plain_text_unchanged():
    assert _escaped("fix the spotify job") == "fix the spotify job"


def test_control_characters_roundtrip():
    # Newline is the ledger-splitting vector; CR and tab ride along.
    value = "line one\nline two\r\twith tab"
    out = _escaped(value)
    assert "\n" not in out and "\r" not in out and "\t" not in out
    assert _roundtrip(value) == value


def test_exotic_control_characters_roundtrip():
    # JSON forbids ALL raw chars below 0x20, not just \n\r\t — a \x0b or
    # \x01 must also route to the escaping path (simplify-pass finding:
    # the original detection pattern let these through the sed fast path).
    for value in ("vert\x0btab", "soh\x01byte", "esc\x1bseq", "ff\x0cfeed"):
        out = _escaped(value)
        assert not any(ord(c) < 0x20 for c in out), (value, out)
        assert _roundtrip(value) == value
