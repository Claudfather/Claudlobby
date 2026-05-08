"""Tests for composer.py — scaffold_env_files merge behavior."""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby.config import load_fleet
from claudlobby.composer import scaffold_env_files
from claudlobby.paths import Paths


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=root)


class TestScaffoldEnvMerge:
    """scaffold_env_files preserves existing values and appends new stubs."""

    def _setup_fleet(self, tmp_path: Path) -> tuple[Path, Paths]:
        """Minimal fleet layout with MCP env contract."""
        root = tmp_path / "claudlobby"
        root.mkdir()

        # Fleet.yaml with one bot using github MCP (needs GITHUB_PAT)
        (root / "fleet.yaml").write_text(dedent("""\
            fleet:
              name: test-fleet
              service_prefix: com.test
              bots:
                worker:
                  expertise: [eng]
                  mcp: [github, shopify]
                  telegram:
                    handle: w_bot
                    token_env: TG_TOKEN_W
        """))

        # Library dirs
        (root / "library" / "expertise").mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")

        (root / "library" / "mcp").mkdir(parents=True)
        (root / "library" / "mcp" / "github.json").write_text(json.dumps({
            "github": {"command": "gh", "args": ["mcp"]},
            "_env_contract": {
                "GITHUB_PAT": {"description": "GitHub PAT", "tier": "fleet"},
            },
        }))
        (root / "library" / "mcp" / "shopify.json").write_text(json.dumps({
            "shopify": {"command": "npx", "args": ["-y", "@ajackus/shopify-mcp-server"]},
            "_env_contract": {
                "SHOPIFY_ACCESS_TOKEN": {"description": "Shopify token", "tier": "fleet"},
                "SHOPIFY_STORE_DOMAIN": {"description": "Shopify domain", "tier": "fleet"},
            },
        }))

        # Runtime dir (scaffold_env_files runs after compose_bot which creates these)
        (root / "runtime" / "bots" / "worker").mkdir(parents=True)

        paths = _make_paths(root)
        return root, paths

    def test_creates_new_env_file(self, tmp_path):
        root, paths = self._setup_fleet(tmp_path)
        fleet = load_fleet(root / "fleet.yaml")

        scaffold_env_files(fleet, paths, log=lambda m: None)

        env_path = root / ".env"
        assert env_path.is_file()
        content = env_path.read_text()
        assert "GITHUB_PAT" in content
        assert "SHOPIFY_ACCESS_TOKEN" in content

    def test_preserves_existing_values_and_appends_new(self, tmp_path):
        root, paths = self._setup_fleet(tmp_path)
        fleet = load_fleet(root / "fleet.yaml")

        # Pre-populate with one var already set
        env_path = root / ".env"
        env_path.write_text(dedent("""\
            # Fleet environment for: test-fleet
            export GITHUB_PAT="ghp_realtoken123"
        """))

        messages: list[str] = []
        scaffold_env_files(fleet, paths, log=messages.append)

        content = env_path.read_text()
        # Existing value preserved verbatim
        assert 'export GITHUB_PAT="ghp_realtoken123"' in content
        # New vars appended as stubs
        assert "export SHOPIFY_ACCESS_TOKEN=" in content
        assert "export SHOPIFY_STORE_DOMAIN=" in content

        # Log message mentions what was added
        assert any("SHOPIFY_ACCESS_TOKEN" in m for m in messages)

    def test_idempotent_no_duplicates(self, tmp_path):
        root, paths = self._setup_fleet(tmp_path)
        fleet = load_fleet(root / "fleet.yaml")

        # Run scaffold twice
        scaffold_env_files(fleet, paths, log=lambda m: None)
        first_content = (root / ".env").read_text()

        scaffold_env_files(fleet, paths, log=lambda m: None)
        second_content = (root / ".env").read_text()

        assert first_content == second_content
