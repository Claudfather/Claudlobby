"""MCP-trust seeding for dev checkouts (lib-common) — #638.

``seed_all_checkouts`` (wired into start-bot.sh) and its per-repo twin in
git-pull-all.sh pre-trust a bot's composed MCP-server allowlist into each
``projects/`` checkout's ``.claude/settings.local.json``, so an interactive
``claude`` session rooted in a dev checkout never stalls on the
MCP-server-trust prompt (which no ``--permission-mode`` answers).

Safety-critical invariants under test:
  - the RIGHT allowlist is seeded — exactly the bot's composed
    ``enabledMcpjsonServers``, propagated verbatim;
  - idempotent + non-destructive — re-runs don't corrupt, existing developer
    keys survive, and a freshly-seeded file leaves the checkout's git clean;
  - the ``[ -d projects ]`` / ``[ -d checkout ]`` guards no-op safely;
  - composer-sole-deriver — the helpers only PROPAGATE the composed allowlist,
    they never re-derive it (fail-closed when there is nothing to trust).

These drive the real bash functions (Python-wrapped, per the
tests/test_json_escape.py + call_lib_fn precedent) rather than a reimplementation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"

# jq is the seeding helpers' only external dependency (settings.local.json merge).
pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="seed helpers require jq"
)


def _run_lib_fn(fn: str, *args: str) -> subprocess.CompletedProcess:
    """Source lib-common.sh and call ``fn`` with positional args.

    Unlike ``call_lib_fn``, returns the CompletedProcess without asserting rc 0,
    so the guard / fail-closed return-1 paths are directly testable. Args travel
    as positionals so the shell never re-interprets them.
    """
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}"; {fn} "$@"', "_", *args],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=15,
    )


def _write_home_settings(home: Path, allowlist) -> Path:
    """A bot home whose composed settings.local.json carries (or omits) the allowlist."""
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    settings: dict = {"spinnerTipsEnabled": False}  # a composed sibling key
    if allowlist is not None:
        settings["enabledMcpjsonServers"] = allowlist
    (home / ".claude" / "settings.local.json").write_text(json.dumps(settings))
    return home


def _git_checkout(parent: Path, name: str = "repo") -> Path:
    co = parent / name
    co.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=co, check=True)
    return co


def _settings(checkout: Path) -> dict:
    return json.loads((checkout / ".claude" / "settings.local.json").read_text())


def _exclude(checkout: Path) -> str:
    p = checkout / ".git" / "info" / "exclude"
    return p.read_text() if p.exists() else ""


# --- seed_checkout_mcp_trust: seeds the RIGHT allowlist ----------------------


def test_seed_writes_exact_allowlist_into_fresh_checkout(tmp_path):
    co = _git_checkout(tmp_path)
    r = _run_lib_fn("seed_checkout_mcp_trust", str(co), '["github","notion"]')
    assert r.returncode == 0, r.stderr
    assert _settings(co)["enabledMcpjsonServers"] == ["github", "notion"]


def test_seed_rewrites_unparseable_existing_file(tmp_path):
    co = _git_checkout(tmp_path)
    (co / ".claude").mkdir()
    (co / ".claude" / "settings.local.json").write_text("{ not valid json")
    r = _run_lib_fn("seed_checkout_mcp_trust", str(co), '["github"]')
    assert r.returncode == 0, r.stderr
    assert _settings(co)["enabledMcpjsonServers"] == ["github"]  # recovered fresh


# --- idempotent + non-destructive (no-clobber) ------------------------------


def test_seed_preserves_existing_developer_keys(tmp_path):
    co = _git_checkout(tmp_path)
    (co / ".claude").mkdir()
    (co / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "devKey": 42})
    )
    r = _run_lib_fn("seed_checkout_mcp_trust", str(co), '["github"]')
    assert r.returncode == 0, r.stderr
    s = _settings(co)
    assert s["enabledMcpjsonServers"] == ["github"]  # added
    assert s["permissions"] == {"allow": ["Bash(ls)"]}  # preserved
    assert s["devKey"] == 42  # preserved


def test_seed_is_idempotent_across_reruns(tmp_path):
    co = _git_checkout(tmp_path)
    for _ in range(3):
        r = _run_lib_fn("seed_checkout_mcp_trust", str(co), '["github","notion"]')
        assert r.returncode == 0, r.stderr
    assert _settings(co)["enabledMcpjsonServers"] == [
        "github",
        "notion",
    ]  # not corrupted
    # the git-exclude line is added once, not appended per run
    assert _exclude(co).splitlines().count(".claude/settings.local.json") == 1


def test_fresh_seed_keeps_checkout_git_clean(tmp_path):
    co = _git_checkout(tmp_path)
    r = _run_lib_fn("seed_checkout_mcp_trust", str(co), '["github"]')
    assert r.returncode == 0, r.stderr
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=co, capture_output=True, text=True
    )
    assert status.stdout.strip() == "", f"checkout not clean: {status.stdout!r}"
    assert ".claude/settings.local.json" in _exclude(co)


def test_seed_into_existing_file_leaves_git_exclude_untouched(tmp_path):
    # An existing settings.local.json was already visible to whoever wrote it —
    # the merge path (created=0) must NOT add the git-exclude line.
    co = _git_checkout(tmp_path)
    (co / ".claude").mkdir()
    (co / ".claude" / "settings.local.json").write_text(json.dumps({"devKey": 1}))
    r = _run_lib_fn("seed_checkout_mcp_trust", str(co), '["github"]')
    assert r.returncode == 0, r.stderr
    assert ".claude/settings.local.json" not in _exclude(co)


# --- guards: safe no-ops ----------------------------------------------------


def test_seed_checkout_noop_on_missing_dir(tmp_path):
    r = _run_lib_fn("seed_checkout_mcp_trust", str(tmp_path / "nope"), '["github"]')
    assert r.returncode == 0  # [ -d checkout ] guard → clean no-op


def test_seed_checkout_noop_on_empty_allowlist(tmp_path):
    co = _git_checkout(tmp_path)
    for empty in ("[]", "null", ""):
        r = _run_lib_fn("seed_checkout_mcp_trust", str(co), empty)
        assert r.returncode == 0, (empty, r.stderr)
    assert not (co / ".claude" / "settings.local.json").exists()  # nothing written


def test_seed_all_checkouts_noop_without_projects_dir(tmp_path):
    # Home has a valid allowlist but no projects/ dir → the [ -d projects ] guard
    # returns before anything is read or written.
    home = _write_home_settings(tmp_path / "bot", ["github"])
    r = _run_lib_fn("seed_all_checkouts", str(home))
    assert r.returncode == 0, r.stderr


def test_seed_all_checkouts_noop_when_home_has_no_allowlist(tmp_path):
    home = tmp_path / "bot"
    _write_home_settings(home, None)  # composed settings, but no enabledMcpjsonServers
    co = _git_checkout(home / "projects", "repo")
    r = _run_lib_fn("seed_all_checkouts", str(home))
    assert r.returncode == 0, r.stderr
    assert not (co / ".claude" / "settings.local.json").exists()  # nothing to trust


# --- composer-sole-deriver: propagate, never re-derive ----------------------


def test_home_allowlist_echoes_composed_value_verbatim(tmp_path):
    home = _write_home_settings(tmp_path / "bot", ["github", "gws-kev", "notion"])
    r = _run_lib_fn("_home_mcp_allowlist", str(home))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == ["github", "gws-kev", "notion"]


def test_home_allowlist_fails_closed_when_nothing_to_trust(tmp_path):
    # missing file, missing key, and empty array all mean "nothing to trust"
    assert _run_lib_fn("_home_mcp_allowlist", str(tmp_path / "missing")).returncode != 0
    assert (
        _run_lib_fn(
            "_home_mcp_allowlist", str(_write_home_settings(tmp_path / "b1", None))
        ).returncode
        != 0
    )
    assert (
        _run_lib_fn(
            "_home_mcp_allowlist", str(_write_home_settings(tmp_path / "b2", []))
        ).returncode
        != 0
    )


def test_seed_all_checkouts_propagates_home_allowlist_to_every_git_checkout(tmp_path):
    # End-to-end: whatever the composer wrote into the bot home is what lands in
    # each checkout — no transformation (sole-deriver) — and non-git dirs skip.
    home = tmp_path / "bot"
    _write_home_settings(home, ["github", "notion"])
    co_a = _git_checkout(home / "projects", "repo-a")
    co_b = _git_checkout(home / "projects", "repo-b")
    (home / "projects" / "not-a-repo").mkdir()  # no .git → skipped
    r = _run_lib_fn("seed_all_checkouts", str(home))
    assert r.returncode == 0, r.stderr
    for co in (co_a, co_b):
        assert _settings(co)["enabledMcpjsonServers"] == ["github", "notion"]
    assert not (home / "projects" / "not-a-repo" / ".claude").exists()
