"""PR-B T7: the SessionStart hook (spec §19.6 / F12) — session identity
derived + published, bash byte-identical to the python derivation."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from claudlobby.plane.ids import ID_PATTERNS, derive_session_uid, mint_uid

HOOK = Path(__file__).resolve().parent.parent / "lib" / "plane-session-start.sh"


def _run(payload: str, env: dict):
    return subprocess.run(
        ["bash", str(HOOK)], input=payload, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", **env}, timeout=30,
    )


def _armed_env(tmp_path: Path) -> dict:
    bot = tmp_path / "bots" / "b1"
    bot.mkdir(parents=True)
    return {"PLANE_EMIT_ENABLED": "1", "BOT_DIR": str(bot)}


def test_bash_derivation_matches_python_byte_for_byte(tmp_path):
    """THE parity pin — now including the #1372-review F8 counterexamples:
    \\uXXXX escapes (café), escaped quotes, and raw non-ASCII, which the old
    sed parse derived DIFFERENTLY from derive_session_uid."""
    env = _armed_env(tmp_path)
    for sid in ("8ad2aa7e-bade-4c55-b3c3-000000000000", "abc", "UPPER-and-123",
                "café", 'quo"ted', "emoji-🎯"):
        r = _run(json.dumps({"session_id": sid}), env)
        assert r.returncode == 0, r.stderr
        out = json.loads((Path(env["BOT_DIR"]) / "data" / ".plane-session").read_text())
        assert out["session_uid"] == derive_session_uid(sid), sid
    # the \u-escaped wire form of café must land on the same uid as the char
    r = _run('{"session_id":"caf\\u00e9"}', env)
    assert r.returncode == 0, r.stderr
    out = json.loads((Path(env["BOT_DIR"]) / "data" / ".plane-session").read_text())
    assert out["session_uid"] == derive_session_uid("café")


def test_refused_start_invalidates_stale_identity(tmp_path):
    """#1372 review F8: a later {} retained the previous session's identity,
    attributing the new session's work to the old one. A refusal DELETES."""
    env = _armed_env(tmp_path)
    _run(json.dumps({"session_id": "old-transcript"}), env)
    f = Path(env["BOT_DIR"]) / "data" / ".plane-session"
    assert f.exists()
    r = _run("{}", env)
    assert r.returncode == 0 and "refusing to derive" in r.stderr
    assert not f.exists(), "stale identity must be invalidated on refusal"


def test_whitespace_only_id_refused(tmp_path):
    env = _armed_env(tmp_path)
    r = _run(json.dumps({"session_id": "   "}), env)
    assert r.returncode == 0 and "refusing to derive" in r.stderr
    assert not (Path(env["BOT_DIR"]) / "data" / ".plane-session").exists()


def test_process_uid_fresh_per_invocation_and_well_formed(tmp_path):
    env = _armed_env(tmp_path)
    uids = []
    for _ in range(2):
        r = _run(json.dumps({"session_id": "same-transcript"}), env)
        assert r.returncode == 0, r.stderr
        out = json.loads((Path(env["BOT_DIR"]) / "data" / ".plane-session").read_text())
        assert re.match(ID_PATTERNS["process"], out["process_uid"])
        uids.append(out["process_uid"])
    assert uids[0] != uids[1], (
        "F12: process_uid distinguishes concurrent resumes — must be fresh")
    # session_uid stayed stable across the two processes of one transcript
    assert derive_session_uid("same-transcript")


def test_empty_platform_id_rejected_with_disclosure(tmp_path):
    env = _armed_env(tmp_path)
    for payload in ("{}", json.dumps({"session_id": ""}), "not json"):
        r = _run(payload, env)
        assert r.returncode == 0, "a hook must never break a boot"
        assert "refusing to derive" in r.stderr, payload
        assert not (Path(env["BOT_DIR"]) / "data" / ".plane-session").exists()


def test_unarmed_hook_is_a_silent_noop(tmp_path):
    env = {**_armed_env(tmp_path), "PLANE_EMIT_ENABLED": "0"}
    r = _run(json.dumps({"session_id": "x"}), env)
    assert r.returncode == 0 and r.stderr == ""
    assert not (Path(env["BOT_DIR"]) / "data" / ".plane-session").exists()


def test_file_mode_is_0600(tmp_path):
    import os
    import stat

    env = _armed_env(tmp_path)
    _run(json.dumps({"session_id": "modecheck"}), env)
    f = Path(env["BOT_DIR"]) / "data" / ".plane-session"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_python_derive_rejects_empty():
    with pytest.raises(ValueError):
        derive_session_uid("  ")
    assert mint_uid("process").startswith("proc_")