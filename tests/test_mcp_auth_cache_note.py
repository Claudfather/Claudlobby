"""Tests for `mcp_auth_cache_path` / `mcp_auth_cache_note` (lib-common.sh, #1358).

Claude Code records an MCP server whose auth failed in a HOST-GLOBAL
`mcp-needs-auth-cache.json` and then SKIPS SPAWNING that server for every
session started afterwards. One bot therefore disables an MCP estate-wide, and
the result is restart-immune by design. The cost is not the outage -- it is that
every instrument reads "poller dead" while none reads "poller never attempted".

`mcp_auth_cache_note` exists to break that silence and nothing else. Two
properties carry the whole design and both are pinned here:

  FAIL-SILENT, NEVER FAIL-LOUD.  It runs on an already-failing boot path under
  `set -euo pipefail`. Absent, empty, malformed, non-dict and unreadable all
  print nothing at rc 0. A diagnostic that aborts a boot is worse than the
  silence it replaces.

  NEVER DROP A KEY.  The KEY is the finding; the timestamp is context. An entry
  whose value does not parse reports `recorded unknown` and is still listed,
  because a key withheld on a parse failure re-creates the exact silence the
  line exists to break.

On the negatives: each asserts an ABSENCE, which is only evidence when the
instrument has been shown able to speak. Every negative here shares one function
with the positives in this same file, so the silences are breakable silences --
`test_armed_cache_is_named_in_full` is the positive control for all of them.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

LIB_COMMON = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"

# Copied byte-for-byte from a cache observed armed on a live host at
# 2026-09-20T09:49:01-04:00, rather than invented: the parse is then exercised
# against the shape the defect actually produces.
ARMED = '{"plugin:telegram:telegram":{"timestamp":1789912141541,"id":"3eaf116ce58465c5"}}'


def _call(func: str, home: Path, bot_dir: Path | None = None, config_dir: str | None = None):
    """Source lib-common.sh and run one of the two helpers; return (stdout, rc).

    HOME is always pinned to a throwaway so a passing run can never be a read of
    the developer's real ~/.claude cache. CLAUDE_CONFIG_DIR is explicitly removed
    unless the case is about it -- an exported one in the developer environment
    would otherwise silently redirect every case here.
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    env["HOME"] = str(home)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    proc = subprocess.run(
        ["bash", "-c", f'. "$1"; {func} "$2"', "_", str(LIB_COMMON), str(bot_dir or "")],
        capture_output=True, text=True, env=env, timeout=20,
    )
    return proc.stdout.strip(), proc.returncode


def _note(home: Path, bot_dir: Path | None = None, config_dir: str | None = None):
    return _call("mcp_auth_cache_note", home, bot_dir, config_dir)


def _arm(home: Path, content: str) -> Path:
    cfg = home / ".claude"
    cfg.mkdir(parents=True, exist_ok=True)
    f = cfg / "mcp-needs-auth-cache.json"
    f.write_text(content)
    return f


# --- the positive control ----------------------------------------------------

def test_armed_cache_is_named_in_full(tmp_path):
    """The one line an operator reads instead of a bare TIMEOUT.

    Asserts every fact #1358 says was missing: that a listed server is SKIPPED
    rather than started, WHICH server, WHEN the entry was recorded, that the
    cache is host-global and so says nothing about this bot's own credential,
    and the remedy -- addressed at the file actually read, not a generic path.
    """
    _arm(tmp_path, ARMED)
    out, rc = _note(tmp_path)
    assert rc == 0
    assert "AUTH_CACHE_ARMED" in out
    assert "SKIPPED at spawn" in out
    assert "plugin:telegram:telegram" in out
    assert "recorded 2026-" in out          # year only: the render is host-local
    assert "NOT evidence about this bot" in out
    assert f"printf '{{}}' > {tmp_path}/.claude/mcp-needs-auth-cache.json" in out
    assert out.count("\n") == 0, "one greppable line, never a paragraph"


# --- never fail the caller, and never publish a cannot-look as a nothing-found -
#
# These assert NON-FATAL, not SILENT. The two are different claims and only the
# first is the contract: the helper must never break a boot, but a condition it
# could not evaluate has to say so. An earlier revision asserted silence here and
# so certified the collapse -- a cannot-look reported as a nothing-found, which
# start-bot then promoted to `auth_cache_armed: false` on the plane.

@pytest.mark.parametrize(
    "label,content",
    [
        ("absent", None),
        ("empty object", "{}"),
        ("zero bytes", ""),
        ("whitespace only", "   \n"),
    ],
)
def test_a_cache_that_was_read_and_holds_nothing_is_silent(tmp_path, label, content):
    """Silence is reserved for a cache actually READ that lists nothing."""
    if content is not None:
        _arm(tmp_path, content)
    out, rc = _note(tmp_path)
    assert out == "", f"{label} must say nothing, got: {out!r}"
    assert rc == 0, f"{label} must not fail its caller (rc={rc})"


@pytest.mark.parametrize(
    "label,content",
    [
        ("malformed json", "not json {{{"),
        ("json array", "[1,2,3]"),
        ("json null", "null"),
        ("json string", '"plugin:telegram:telegram"'),
    ],
)
def test_a_cache_that_could_not_be_read_says_unknown(tmp_path, label, content):
    """A shape we cannot parse is UNDETERMINED, never reported as clear.

    The likeliest trigger is not exotic: the cache is host-global and written by
    Claude Code at arbitrary moments, so a read concurrent with a write yields
    invalid JSON. Reporting that as silence tells an operator mid-stall that this
    is not the #1358 signature, when it may be exactly that.
    """
    _arm(tmp_path, content)
    out, rc = _note(tmp_path)
    assert out.startswith("AUTH_CACHE_UNKNOWN"), f"{label} must say UNKNOWN, got: {out!r}"
    assert "NOT evidence the cache is clear" in out, out
    assert rc == 0, f"{label} must not fail its caller (rc={rc})"


def test_unreadable_cache_says_unknown_rather_than_failing_or_lying(tmp_path):
    f = _arm(tmp_path, ARMED)
    f.chmod(0o000)
    try:
        out, rc = _note(tmp_path)
    finally:
        f.chmod(0o644)
    if os.geteuid() == 0:
        pytest.skip("root bypasses the permission bit; the branch is unreachable as root")
    assert out.startswith("AUTH_CACHE_UNKNOWN"), out
    assert rc == 0


def test_an_absent_cache_is_silent_but_an_unreadable_one_is_not(tmp_path):
    """The one distinction the whole split exists to hold, asserted directly.

    Without this, a regression that returned 0-and-silent for BOTH would still
    satisfy every other case in this file.
    """
    absent_out, absent_rc = _note(tmp_path)
    f = _arm(tmp_path, ARMED)
    f.chmod(0o000)
    try:
        unreadable_out, unreadable_rc = _note(tmp_path)
    finally:
        f.chmod(0o644)
    if os.geteuid() == 0:
        pytest.skip("root bypasses the permission bit; the branch is unreachable as root")
    assert absent_out == "", absent_out
    assert unreadable_out.startswith("AUTH_CACHE_UNKNOWN"), unreadable_out
    assert absent_out != unreadable_out, "absent and unreadable must not share an answer"
    assert (absent_rc, unreadable_rc) == (0, 0)


def test_a_broken_python3_says_unknown_without_failing_the_caller(tmp_path):
    """The parse is a subprocess, so it can fail on a healthy host too.

    Emptying PATH cannot isolate this -- lib-common.sh needs `uname` at source
    time and dies first, which would test the harness rather than the helper. So
    shadow python3 with one that exits nonzero and emits nothing: the guard is
    passed, the parse runs, and the `|| true` beside it has to absorb the
    failure. This is also the likelier real shape (a present but broken
    interpreter) than an interpreter that is absent altogether.
    """
    _arm(tmp_path, ARMED)
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "python3").write_text("#!/bin/sh\nexit 9\n")
    (shim / "python3").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    env["HOME"] = str(tmp_path)
    env["PATH"] = f"{shim}:{env.get('PATH', '')}"
    proc = subprocess.run(
        ["/bin/bash", "-c", 'set -euo pipefail; . "$1"; mcp_auth_cache_note; echo "rc=$?"',
         "_", str(LIB_COMMON)],
        capture_output=True, text=True, env=env, timeout=20,
    )
    assert proc.stdout.startswith("AUTH_CACHE_UNKNOWN"), proc.stdout
    assert proc.stdout.strip().endswith("rc=0"), proc.stdout
    assert proc.returncode == 0, proc.stderr


# --- never drop a key --------------------------------------------------------

@pytest.mark.parametrize(
    "label,value",
    [
        ("value is a bare epoch-ms number", "1789912141541"),
        ("object without a timestamp field", '{"id":"abc"}'),
        ("timestamp is a string", '{"timestamp":"1789912141541"}'),
        ("timestamp is a bool", '{"timestamp":true}'),
        ("timestamp overflows a date", '{"timestamp":1e400}'),
        ("timestamp is null", '{"timestamp":null}'),
    ],
)
def test_the_key_is_listed_whatever_its_value(tmp_path, label, value):
    """An unparseable value costs the timestamp, never the finding."""
    _arm(tmp_path, '{"plugin:telegram:telegram":%s}' % value)
    out, rc = _note(tmp_path)
    assert rc == 0
    assert "plugin:telegram:telegram" in out, label


def test_a_bare_epoch_value_still_renders_a_time(tmp_path):
    _arm(tmp_path, '{"plugin:telegram:telegram":1789912141541}')
    out, _ = _note(tmp_path)
    assert "recorded 2026-" in out
    assert "recorded unknown" not in out


def test_an_unparseable_value_says_unknown_rather_than_guessing(tmp_path):
    _arm(tmp_path, '{"plugin:telegram:telegram":{"id":"abc"}}')
    out, _ = _note(tmp_path)
    assert "recorded unknown" in out


def test_every_entry_is_listed_not_only_a_recognised_one(tmp_path):
    """No key matching, deliberately.

    The channel plugin's key is a string Claude Code owns. A matcher against it
    goes SILENT the day it is renamed -- failing closed on the one line whose
    whole job is to break a silence (the #751 string-drift trap). So unrelated
    entries are reported too: at a TIMEOUT, a cache holding only Google Drive
    tells the operator this is NOT the signature, which is also worth knowing.
    """
    _arm(tmp_path, json.dumps({
        "plugin:telegram:telegram": {"timestamp": 1789912141541},
        "plugin:gdrive:gdrive": {"timestamp": 1789912141541},
    }))
    out, _ = _note(tmp_path)
    assert "plugin:telegram:telegram" in out
    assert "plugin:gdrive:gdrive" in out
    assert out.index("plugin:gdrive:gdrive") < out.index("plugin:telegram:telegram"), \
        "sorted, so two runs of the same cache render identically"


def test_the_record_stays_one_line_whatever_the_key_holds(tmp_path):
    """The one-line shape is a contract, not a style choice.

    Every consumer selects this record with grep -- the harnesses, and an
    operator reading startup.log. A key carrying a newline would split it in
    two and leave the second half in the log as an unattributed fragment.
    """
    _arm(tmp_path, json.dumps({"plugin:evil\ntelegram:x\tb": {"timestamp": 1789912141541}}))
    out, rc = _note(tmp_path)
    assert rc == 0
    assert out.count("\n") == 0
    assert "plugin:evil telegram:x b" in out, "collapsed, not dropped"


def test_an_absurd_key_is_cut_visibly_rather_than_silently(tmp_path):
    """A truncation that reads as the whole name is worse than a long line."""
    _arm(tmp_path, json.dumps({"p" * 500: {"timestamp": 1789912141541}}))
    out, _ = _note(tmp_path)
    assert "…(truncated)" in out
    assert "p" * 200 in out
    assert "p" * 201 not in out


# --- which cache, exactly ----------------------------------------------------

def test_path_defaults_to_the_home_config_dir(tmp_path):
    out, rc = _call("mcp_auth_cache_path", tmp_path)
    assert out == f"{tmp_path}/.claude/mcp-needs-auth-cache.json"
    assert rc == 0


def test_ambient_config_dir_overrides_home(tmp_path):
    out, _ = _call("mcp_auth_cache_path", tmp_path, config_dir=str(tmp_path / "amb"))
    assert out == f"{tmp_path}/amb/mcp-needs-auth-cache.json"


def test_the_bots_own_config_dir_wins_over_the_ambient_one(tmp_path):
    """rolling-restart reads at FLEET level, without the bot env sourced.

    A bot pinned to its own CLAUDE_CONFIG_DIR (a canary, a harness throwaway)
    consults a different cache than the operator does, so describing it with the
    ambient one would name a file its session never reads.
    """
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "bot.conf").write_text(f'BOT_ID=b1\nCLAUDE_CONFIG_DIR="{tmp_path}/botcfg"\n')
    out, _ = _call("mcp_auth_cache_path", tmp_path, bot_dir=bot, config_dir=str(tmp_path / "amb"))
    assert out == f"{tmp_path}/botcfg/mcp-needs-auth-cache.json"


def test_a_commented_config_dir_is_not_a_declaration(tmp_path):
    """The composer emits the default commented out; reading it would be wrong."""
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "bot.conf").write_text("BOT_ID=b1\n# CLAUDE_CONFIG_DIR='~/.claude'  # default account\n")
    out, _ = _call("mcp_auth_cache_path", tmp_path, bot_dir=bot)
    assert out == f"{tmp_path}/.claude/mcp-needs-auth-cache.json"


def test_a_tilde_path_is_expanded_rather_than_silencing_the_line(tmp_path):
    """Unexpanded, `~/...` names a path that cannot exist and the helper goes
    quiet -- a silent wrong answer, which is the failure class it exists to
    break. So expand it rather than trust that nobody hand-edits a bot.conf."""
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "bot.conf").write_text('BOT_ID=b1\nCLAUDE_CONFIG_DIR="~/.claude"\n')
    out, _ = _call("mcp_auth_cache_path", tmp_path, bot_dir=bot)
    assert out == f"{tmp_path}/.claude/mcp-needs-auth-cache.json"


def test_an_armed_operator_cache_cannot_leak_into_a_scoped_read(tmp_path):
    """Isolation asserted, not merely arranged.

    A bot pinned elsewhere must stay silent even when the ambient config dir is
    armed -- otherwise every test above could pass by reading the wrong file on
    a host that happened to be armed.
    """
    amb = tmp_path / "amb"
    amb.mkdir()
    (amb / "mcp-needs-auth-cache.json").write_text(ARMED)
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "bot.conf").write_text(f'BOT_ID=b1\nCLAUDE_CONFIG_DIR="{tmp_path}/botcfg"\n')
    (tmp_path / "botcfg").mkdir()
    (tmp_path / "botcfg" / "mcp-needs-auth-cache.json").write_text("{}")
    out, rc = _note(tmp_path, bot_dir=bot, config_dir=str(amb))
    assert out == "", "read the bot's cache, not the ambient armed one"
    assert rc == 0
