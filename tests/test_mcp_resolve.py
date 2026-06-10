"""Tests for mcp_resolve.py — shared MCP env-var resolution.

The round-trip test verifies that composer (resolve_placeholders) and
validator (required_vars / _bot_required_env_vars) agree on canonical
var names for both shared and instance-scoped vars.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from claudlobby.config import McpEntry, load_fleet
from claudlobby.mcp_resolve import (
    canonical_var_name,
    required_vars,
    resolve_placeholders,
)
from claudlobby.paths import Paths


class TestCanonicalVarName:
    def test_shared_scope_unchanged(self):
        contract = {"TOKEN": {"scope": "shared", "tier": "fleet"}}
        entry = McpEntry(name="github")
        assert canonical_var_name("TOKEN", contract, entry, "default") == "TOKEN"

    def test_instance_scope_gets_prefix(self):
        contract = {"TOKEN": {"scope": "instance", "tier": "bot"}}
        entry = McpEntry(name="notion", instances=["work", "personal"])
        assert (
            canonical_var_name("TOKEN", contract, entry, "work") == "NOTION_WORK_TOKEN"
        )

    def test_instance_scope_default_instance(self):
        contract = {"TOKEN": {"scope": "instance", "tier": "bot"}}
        entry = McpEntry(name="notion", instances=["default"])
        assert canonical_var_name("TOKEN", contract, entry, "default") == "NOTION_TOKEN"

    def test_unknown_var_treated_as_shared(self):
        contract = {}
        entry = McpEntry(name="github")
        assert canonical_var_name("MYSTERY", contract, entry, "default") == "MYSTERY"

    def test_dash_in_name_becomes_underscore(self):
        contract = {"API_KEY": {"scope": "instance", "tier": "fleet"}}
        entry = McpEntry(name="my-server", instances=["prod"])
        assert (
            canonical_var_name("API_KEY", contract, entry, "prod")
            == "MY_SERVER_PROD_API_KEY"
        )


class TestResolvePlaceholders:
    def test_string_shared(self):
        contract = {"PAT": {"scope": "shared"}}
        entry = McpEntry(name="github")
        assert resolve_placeholders("${PAT}", contract, entry, "default") == "${PAT}"

    def test_string_instance(self):
        contract = {"TOKEN": {"scope": "instance"}}
        entry = McpEntry(name="notion", instances=["work"])
        assert (
            resolve_placeholders("Bearer ${TOKEN}", contract, entry, "work")
            == "Bearer ${NOTION_WORK_TOKEN}"
        )

    def test_dict(self):
        contract = {"TOKEN": {"scope": "instance"}}
        entry = McpEntry(name="notion", instances=["work"])
        val = {"Authorization": "Bearer ${TOKEN}", "X-Custom": "static"}
        result = resolve_placeholders(val, contract, entry, "work")
        assert result == {
            "Authorization": "Bearer ${NOTION_WORK_TOKEN}",
            "X-Custom": "static",
        }

    def test_list(self):
        contract = {"URL": {"scope": "instance"}}
        entry = McpEntry(name="svc", instances=["prod"])
        result = resolve_placeholders(["--url", "${URL}"], contract, entry, "prod")
        assert result == ["--url", "${SVC_PROD_URL}"]

    def test_nested(self):
        contract = {"KEY": {"scope": "instance"}}
        entry = McpEntry(name="svc", instances=["dev"])
        val = [{"headers": {"X-Key": "${KEY}"}}, "plain"]
        result = resolve_placeholders(val, contract, entry, "dev")
        assert result == [{"headers": {"X-Key": "${SVC_DEV_KEY}"}}, "plain"]

    def test_non_string_passthrough(self):
        contract = {}
        entry = McpEntry(name="x")
        assert resolve_placeholders(42, contract, entry, "default") == 42
        assert resolve_placeholders(True, contract, entry, "default") is True


class TestRoundTrip:
    """Composer's resolve_placeholders and validator's required_vars must
    agree on canonical var names — this is the whole point of the extraction."""

    def _make_fleet_dir(self, tmp_path: Path) -> tuple[Path, Paths]:
        root = tmp_path / "claudlobby"
        root.mkdir()

        (root / "fleet.yaml").write_text(
            dedent("""\
            fleet:
              name: test-fleet
              service_prefix: com.test
              bots:
                worker:
                  expertise: [eng]
                  mcp:
                    - notion:
                        instances: [work, personal]
                  telegram:
                    handle: w_bot
                    token_env: TG_W
        """)
        )

        for kind in (
            "expertise",
            "mcp",
            "integrations",
            "guardrails",
            "protocols",
            "skills",
            "resources",
            "lessons",
        ):
            (root / "library" / kind).mkdir(parents=True)

        (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")

        frag = {
            "notion-server": {
                "command": "npx",
                "args": ["-y", "notion-mcp"],
                "env": {
                    "NOTION_TOKEN": "${TOKEN}",
                    "OAUTH_ID": "${OAUTH_CLIENT_ID}",
                },
            },
            "_env_contract": {
                "TOKEN": {
                    "scope": "instance",
                    "tier": "bot",
                    "description": "API token",
                },
                "OAUTH_CLIENT_ID": {
                    "scope": "shared",
                    "tier": "fleet",
                    "description": "OAuth ID",
                },
            },
        }
        (root / "library" / "mcp" / "notion.json").write_text(json.dumps(frag))

        (root / "templates").mkdir()
        (root / "templates" / "claude.md.j2").write_text("# {{ bot.name }}\n")
        (root / "voices").mkdir()
        (root / "runtime" / "bots").mkdir(parents=True)

        return root, Paths(root=root, fleet_dir=None)

    def test_composer_and_validator_agree(self, tmp_path):
        root, paths = self._make_fleet_dir(tmp_path)
        fleet, _md = load_fleet(root / "fleet.yaml")
        bot = fleet.bots["worker"]

        # --- Validator side: canonical var names ---
        req = required_vars(bot, paths)
        validator_vars = {(name, inst) for name, _tier, _src, inst in req}

        # --- Composer side: resolve the same fragment ---
        frag = json.loads((root / "library" / "mcp" / "notion.json").read_text())
        contract = frag.get("_env_contract", {})
        entry = bot.mcp[0]

        # Collect the canonical var names the composer produces per instance
        import re as _re

        composer_names: set[str] = set()
        for instance in entry.instances:
            resolved_env = resolve_placeholders(
                frag["notion-server"]["env"], contract, entry, instance
            )
            for env_val in resolved_env.values():
                if isinstance(env_val, str):
                    for m in _re.finditer(r"\$\{([A-Z_][A-Z0-9_]*)\}", env_val):
                        composer_names.add(m.group(1))

        # Validator canonical names (the first element of each tuple)
        validator_names = {name for name, _tier, _src, _inst in req}

        # Both must agree on the full set of canonical var names
        assert composer_names == validator_names
