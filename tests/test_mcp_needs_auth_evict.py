"""Gates for lib/mcp-needs-auth-evict.py.

The module's whole job is one discriminator — local stdio versus remote — so the
load-bearing tests are the NEGATIVE ones. A version that evicted everything would
pass any test that only checks the telegram entry disappears, and it would clear a
real OAuth verdict and loop an auth prompt forever. So every "does evict" case here
is paired with a "must not evict" case that the over-eager version fails.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "lib" / "mcp-needs-auth-evict.py"


def _load():
    spec = importlib.util.spec_from_file_location("mcp_needs_auth_evict", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _plugin(tmp_path: Path, name: str, servers: dict) -> Path:
    root = tmp_path / "cache" / "mkt" / name / "0.0.7"
    root.mkdir(parents=True)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    return root


def _installed(tmp_path: Path, mapping: dict[str, Path]) -> Path:
    path = tmp_path / "installed_plugins.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": {
                    q: [{"scope": "user", "installPath": str(p)}]
                    for q, p in mapping.items()
                },
            }
        )
    )
    return path


def _cache(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "mcp-needs-auth-cache.json"
    path.write_text(json.dumps(payload, separators=(",", ":")))
    return path


# --------------------------------------------------------------------------
# The discriminator itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decl",
    [
        {"command": "bun", "args": ["run", "start"]},
        {"command": "node", "args": []},
        {"command": "python3", "type": "stdio"},
    ],
)
def test_local_stdio_declarations_are_evictable(decl):
    assert mod.is_local_stdio(decl) is True


@pytest.mark.parametrize(
    "decl",
    [
        {"url": "https://mcp.example.com/sse"},
        {"command": "x", "url": "https://mcp.example.com"},  # both -> remote wins
        {"command": "x", "type": "http"},
        {"command": "x", "type": "SSE"},  # case-insensitive
        {"command": "x", "transport": "sse"},
        {"command": ""},  # empty command is not a command
        {"command": "   "},
        {},  # unrecognised shape
        None,
        "bun",
    ],
)
def test_anything_not_unambiguously_stdio_is_kept(decl):
    """The safety half. An over-eager version fails every row here."""
    assert mod.is_local_stdio(decl) is False


# --------------------------------------------------------------------------
# End to end, on the shape the real incident had
# --------------------------------------------------------------------------


def test_evicts_the_false_telegram_verdict_and_leaves_the_oauth_one(tmp_path):
    """The 2026-08-25 incident, reproduced.

    A local-stdio plugin server and a genuine OAuth server sit in the same cache.
    Exactly one is false.
    """
    tg = _plugin(
        tmp_path, "telegram", {"telegram": {"command": "bun", "args": ["run"]}}
    )
    installed = _installed(tmp_path, {"telegram@claudfather-plugins": tg})
    cache = _cache(
        tmp_path,
        {
            "claude.ai Google Drive": {"timestamp": 1787680061179, "id": "mcpsrv_01"},
            "plugin:telegram:telegram": {"timestamp": 1787680190667, "id": "3eaf116"},
        },
    )

    rc = mod.main(["--cache", str(cache), "--installed", str(installed)])

    assert rc == 0
    after = json.loads(cache.read_text())
    assert "plugin:telegram:telegram" not in after
    assert after["claude.ai Google Drive"] == {
        "timestamp": 1787680061179,
        "id": "mcpsrv_01",
    }


def test_a_remote_plugin_server_keeps_its_verdict(tmp_path):
    """The case that must never regress: evicting this loops a real auth prompt."""
    remote = _plugin(tmp_path, "acme", {"api": {"url": "https://mcp.acme.test/sse"}})
    installed = _installed(tmp_path, {"acme@mkt": remote})
    cache = _cache(tmp_path, {"plugin:acme:api": {"timestamp": 1}})

    mod.main(["--cache", str(cache), "--installed", str(installed)])

    assert "plugin:acme:api" in json.loads(cache.read_text())


def test_non_plugin_keys_are_never_touched(tmp_path):
    installed = _installed(tmp_path, {})
    cache = _cache(tmp_path, {"claude.ai Google Drive": {"timestamp": 1}})

    mod.main(["--cache", str(cache), "--installed", str(installed)])

    assert json.loads(cache.read_text()) == {"claude.ai Google Drive": {"timestamp": 1}}


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------


def test_unresolvable_plugin_is_kept(tmp_path):
    """No install path -> no declaration -> no verdict of our own. Keep it."""
    installed = _installed(tmp_path, {})
    cache = _cache(tmp_path, {"plugin:ghost:server": {"timestamp": 1}})

    mod.main(["--cache", str(cache), "--installed", str(installed)])

    assert "plugin:ghost:server" in json.loads(cache.read_text())


def test_a_plugin_name_under_two_marketplaces_is_ambiguous_and_kept(tmp_path):
    """`telegram` really does exist under two marketplaces on the live host.

    Resolving to the wrong one reads the wrong manifest, so ambiguity is dropped
    rather than guessed.
    """
    a = _plugin(tmp_path / "a", "telegram", {"telegram": {"command": "bun"}})
    b = _plugin(tmp_path / "b", "telegram", {"telegram": {"url": "https://x.test"}})
    installed = _installed(
        tmp_path,
        {"telegram@claudfather-plugins": a, "telegram@claude-plugins-official": b},
    )
    cache = _cache(tmp_path, {"plugin:telegram:telegram": {"timestamp": 1}})

    mod.main(["--cache", str(cache), "--installed", str(installed)])

    assert "plugin:telegram:telegram" in json.loads(cache.read_text())


def test_absent_cache_is_a_noop_and_creates_nothing(tmp_path):
    cache = tmp_path / "mcp-needs-auth-cache.json"
    installed = _installed(tmp_path, {})

    assert mod.main(["--cache", str(cache), "--installed", str(installed)]) == 0
    assert not cache.exists()


def test_malformed_cache_is_left_alone(tmp_path):
    cache = tmp_path / "mcp-needs-auth-cache.json"
    cache.write_text("{not json")
    installed = _installed(tmp_path, {})

    assert mod.main(["--cache", str(cache), "--installed", str(installed)]) == 0
    assert cache.read_text() == "{not json"


def test_missing_installed_manifest_evicts_nothing(tmp_path):
    cache = _cache(tmp_path, {"plugin:telegram:telegram": {"timestamp": 1}})

    mod.main(["--cache", str(cache), "--installed", str(tmp_path / "nope.json")])

    assert "plugin:telegram:telegram" in json.loads(cache.read_text())


# --------------------------------------------------------------------------
# It must not disturb a host-global file it shares with 22 bots
# --------------------------------------------------------------------------


def test_on_disk_form_stays_compact(tmp_path):
    tg = _plugin(tmp_path, "telegram", {"telegram": {"command": "bun"}})
    installed = _installed(tmp_path, {"telegram@mkt": tg})
    cache = _cache(
        tmp_path,
        {"keep": {"a": 1}, "plugin:telegram:telegram": {"timestamp": 1}},
    )

    mod.main(["--cache", str(cache), "--installed", str(installed)])

    raw = cache.read_text()
    assert raw == '{"keep":{"a":1}}'
    assert "\n" not in raw and ": " not in raw


def test_dry_run_writes_nothing(tmp_path):
    tg = _plugin(tmp_path, "telegram", {"telegram": {"command": "bun"}})
    installed = _installed(tmp_path, {"telegram@mkt": tg})
    cache = _cache(tmp_path, {"plugin:telegram:telegram": {"timestamp": 1}})
    before = cache.read_text()

    rc = mod.main(["--cache", str(cache), "--installed", str(installed), "--dry-run"])

    assert rc == 0
    assert cache.read_text() == before


def test_no_temp_files_are_left_behind(tmp_path):
    tg = _plugin(tmp_path, "telegram", {"telegram": {"command": "bun"}})
    installed = _installed(tmp_path, {"telegram@mkt": tg})
    cache = _cache(tmp_path, {"plugin:telegram:telegram": {"timestamp": 1}})

    mod.main(["--cache", str(cache), "--installed", str(installed)])

    assert not list(cache.parent.glob(".mcpauth-*"))
