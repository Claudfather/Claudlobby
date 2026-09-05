"""json_escape (lib-common) — JSON string escaping for the JSONL ledgers (#530).

The escaper must produce content that round-trips ``json.loads`` when wrapped
in double quotes — including control characters, which would otherwise split
single-line JSONL rows that the line-oriented rotation then truncates into
permanently invalid JSON (the #528-review Major generalized: any ledger
writer, any caller-supplied text).
"""

from __future__ import annotations

import json

import subprocess

from tests.conftest import call_lib_fn
from tests.plane_fixtures import ro as _ro
from tests.test_task_id_dispatch import _fake_lib


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


def test_dispatch_record_survives_newline_in_task(tmp_path):
    # End-to-end: operator-supplied task text with an embedded newline must
    # land as one valid record — on the plane since F18 R1 (the work item's
    # title is the task text the door escaped; the wedge fixed the
    # claudron-supplied vector in #529; this is the caller-supplied one).
    libdir, env = _fake_lib(tmp_path, "#!/bin/bash\nexit 0\n")
    r = subprocess.run(
        ["bash", "-c", f'"{libdir}/dispatch-task.sh" --repo kev worker-1 "line one\nline two"'],
        capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    with _ro(tmp_path) as conn:
        titles = [row[0] for row in conn.execute("SELECT title FROM work_items")]
    # the batch validated and landed — reaching a row means the text
    # round-tripped despite the newline
    assert titles == ["line one\nline two"], (titles, r.stderr)
