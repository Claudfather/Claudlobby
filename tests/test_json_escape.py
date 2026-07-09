"""json_escape (lib-common) — JSON string escaping for the JSONL ledgers (#530).

The escaper must produce content that round-trips ``json.loads`` when wrapped
in double quotes — including control characters, which would otherwise split
single-line JSONL rows that the line-oriented rotation then truncates into
permanently invalid JSON (the #528-review Major generalized: any ledger
writer, any caller-supplied text).
"""

from __future__ import annotations

import json

from tests.conftest import call_lib_fn
from tests.test_claudron_query_wedge import _run_dispatch, _wedge_env


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


def test_dispatch_ledger_survives_newline_in_task(tmp_path):
    # End-to-end: operator-supplied task text with an embedded newline must
    # land as one valid JSON ledger row (the wedge fixed the claudron-supplied
    # vector in #529; this is the caller-supplied one).
    env = _wedge_env(tmp_path, json.dumps({"query": "q", "results": []}))
    env.pop("CLAUDRON_QUERY_BEFORE")  # wedge off — this is about json_escape
    r, _, row = _run_dispatch(tmp_path, env, task="line one\nline two")
    assert r.returncode == 0, r.stderr
    # _run_dispatch json.loads the last ledger line — reaching here means the
    # row round-tripped despite the newline.
    assert row["task"] == "line one\nline two"
