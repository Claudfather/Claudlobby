"""F2 grant-superset gate — the migration safety net, not advisory.

Composing any bot must prove the new integration ``tool_grants`` resolver is a
**superset** of the legacy ``_resolve_mcp_permissions`` output, so cutting
``_permissions_contract`` in a later phase cannot silently drop a live grant.

This module sweeps a representative synthetic fleet that exercises every grant
shape (fragment-backed default + multi-instance, connector, CLI, empty-contract,
and the explicit-integrations-would-strip case). The gate itself lives inside
``compose_settings_local``; composing each bot here is what fires it.

The real production fleets are validated empirically at generate time (the gate
runs on every ``generate``) and in the PR's empirical proof — they live in
gitignored ``local/`` overlays and cannot be referenced from a committed test.
"""

from __future__ import annotations

import json
from pathlib import Path

from claudlobby.composer import (
    _resolve_integration_grants,
    _resolve_mcp_permissions,
    compose_settings_local,
)
from claudlobby.config import BotConfig, FleetConfig, McpEntry
from claudlobby.paths import Paths


def _build_library(root: Path) -> None:
    """Stage fragments + integration files covering every grant shape."""
    (root / "runtime" / "bots").mkdir(parents=True)
    mcp = root / "library" / "mcp"
    mcp.mkdir(parents=True)
    integ = root / "library" / "integrations"
    integ.mkdir(parents=True)

    # Fragment-backed servers (non-empty contracts) + an empty-contract one.
    contracts = {
        "github": ["search_code", "create_pull_request"],
        "notion": ["API-post-page"],
        "gws": ["search_gmail_messages", "get_events"],
        "shopify": [],  # empty contract → grants nothing (printify/shopify shape)
    }
    for name, tools in contracts.items():
        (mcp / f"{name}.json").write_text(
            json.dumps(
                {
                    "_permissions_contract": {"tools": tools},
                    name: {"command": "npx", "args": ["-y", f"{name}-mcp"]},
                }
            )
        )

    def write_integration(name: str, body: str) -> None:
        (integ / f"{name}.md").write_text(body)

    # Fragment-backed integration files carry the server wildcard (empty for shopify).
    for name in ("github", "notion", "gws"):
        write_integration(
            name,
            f'---\ntitle: {name}\ntool_grants:\n  - "mcp__{name}__*"\n---\n\n# {name}\n',
        )
    write_integration("shopify", "---\ntitle: shopify\n---\n\n# shopify\n")
    # CLI-backed — no tool_grants.
    write_integration("neon", "---\ntitle: neon\ntype: cli\n---\n\n# neon\n")
    # Connector-backed — literal claude_ai grants, no fragment.
    write_integration(
        "gmail",
        '---\ntitle: gmail\ntype: connector\ntool_grants:\n  - "mcp__claude_ai_Gmail__*"\n---\n\n# gmail\n',
    )
    write_integration(
        "google-calendar",
        '---\ntitle: google-calendar\ntype: connector\ntool_grants:\n  - "mcp__claude_ai_Google_Calendar__*"\n---\n\n# google-calendar\n',
    )


def _representative_bots() -> dict[str, BotConfig]:
    """One bot per grant shape, including the strip case the union resolver fixes."""
    return {
        "auto-pair": BotConfig(
            bot_id="auto-pair",
            name="auto-pair",
            expertise=["eng"],
            mcp=[McpEntry(name="github"), McpEntry(name="notion")],
        ),
        "multi-instance": BotConfig(
            bot_id="multi-instance",
            name="multi-instance",
            expertise=["eng"],
            mcp=[McpEntry(name="gws", instances=["personal", "work"])],
        ),
        # Explicit CLI integration + mcp servers: the pre-fix verbatim resolver
        # returned [neon] and stripped github/notion grants. Union must cover them.
        "strip-case": BotConfig(
            bot_id="strip-case",
            name="strip-case",
            expertise=["eng"],
            integrations=["neon"],
            mcp=[McpEntry(name="github"), McpEntry(name="notion")],
        ),
        "connector-equipped": BotConfig(
            bot_id="connector-equipped",
            name="connector-equipped",
            expertise=["eng"],
            integrations=["gmail", "google-calendar"],
            mcp=[McpEntry(name="github")],
        ),
        "empty-contract": BotConfig(
            bot_id="empty-contract",
            name="empty-contract",
            expertise=["eng"],
            mcp=[McpEntry(name="shopify")],
        ),
    }


class TestGrantSupersetSweep:
    """Every representative bot composes without tripping the gate, and new ⊇ legacy."""

    def _paths_and_fleet(self, tmp_path: Path):
        root = tmp_path / "claudlobby"
        _build_library(root)
        paths = Paths(root=root, fleet_dir=root)
        bots = _representative_bots()
        fleet = FleetConfig(name="t", service_prefix="p", bots=bots)
        return paths, fleet, bots

    def test_every_bot_composes_and_new_is_superset(self, tmp_path):
        paths, fleet, bots = self._paths_and_fleet(tmp_path)
        for name, bot in bots.items():
            # compose runs _assert_grant_superset internally — a strip would raise here
            result = compose_settings_local(bot, fleet, paths)
            allow = set(result.get("permissions", {}).get("allow", []))
            legacy = set(_resolve_mcp_permissions(bot, paths))
            new = set(_resolve_integration_grants(bot, paths))
            assert new >= legacy, f"{name}: new {new} not superset of legacy {legacy}"
            # emitted allow list contains the union (belt-and-suspenders)
            assert legacy <= allow, f"{name}: legacy grants missing from allow list"
            assert new <= allow, f"{name}: new grants missing from allow list"

    def test_strip_case_retains_mcp_grants_and_adds_connectors(self, tmp_path):
        paths, fleet, bots = self._paths_and_fleet(tmp_path)
        # strip-case: github/notion grants survive despite explicit integrations:[neon]
        allow = set(
            compose_settings_local(bots["strip-case"], fleet, paths)["permissions"][
                "allow"
            ]
        )
        assert {"mcp__github__*", "mcp__notion__*"} <= allow

        # connector-equipped: legacy github grant retained + connector additions
        allow2 = set(
            compose_settings_local(bots["connector-equipped"], fleet, paths)[
                "permissions"
            ]["allow"]
        )
        assert "mcp__github__*" in allow2
        assert "mcp__claude_ai_Gmail__*" in allow2
        assert "mcp__claude_ai_Google_Calendar__*" in allow2

    def test_multi_instance_expands_and_covers_legacy(self, tmp_path):
        paths, fleet, bots = self._paths_and_fleet(tmp_path)
        bot = bots["multi-instance"]
        new = set(_resolve_integration_grants(bot, paths))
        assert new == {"mcp__gws-personal__*", "mcp__gws-work__*"}
        assert set(_resolve_mcp_permissions(bot, paths)) <= new

    def test_dir_folder_integration_grants_resolved(self, tmp_path):
        # generate must resolve grants from a dir/ folder-expanded integration —
        # the same bypass the validator closes, kept consistent on the composer
        # side so validate and generate agree. Pre-fix the resolver was
        # folder-blind (find_library_file("integrations", "connectors/") -> None)
        # and silently dropped these grants.
        root = tmp_path / "claudlobby"
        _build_library(root)
        conn = root / "library" / "integrations" / "connectors"
        conn.mkdir()
        conn.joinpath("native.md").write_text(
            "---\ntitle: native\ntype: connector\n"
            'tool_grants:\n  - "mcp__claude_ai_Gmail__*"\n---\n\n# native\n'
        )
        paths = Paths(root=root, fleet_dir=root)
        bot = BotConfig(
            bot_id="folder-bot",
            name="folder-bot",
            expertise=["eng"],
            integrations=["connectors/"],
        )
        assert "mcp__claude_ai_Gmail__*" in set(_resolve_integration_grants(bot, paths))


class TestGrantSupersetGateHasTeeth:
    """A genuine coverage gap hard-fails the gate — the property is enforced, not assumed."""

    def test_uncovered_fragment_grant_raises(self, tmp_path):
        import pytest

        root = tmp_path / "claudlobby"
        (root / "runtime" / "bots").mkdir(parents=True)
        mcp = root / "library" / "mcp"
        mcp.mkdir(parents=True)
        # Contract fragment with a live grant, but NO paired integration tool_grants.
        (mcp / "github.json").write_text(
            json.dumps(
                {
                    "_permissions_contract": {"tools": ["search_code"]},
                    "github": {"command": "npx", "args": []},
                }
            )
        )
        paths = Paths(root=root, fleet_dir=root)
        bot = BotConfig(
            bot_id="w", name="w", expertise=["eng"], mcp=[McpEntry(name="github")]
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"w": bot})
        with pytest.raises(ValueError, match="mcp__github__"):
            compose_settings_local(bot, fleet, paths)
