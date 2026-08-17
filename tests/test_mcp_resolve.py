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
    iter_operator_contract_vars,
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

    def test_composer_provided_path_anchors_pass_through(self):
        """FLEET_ROOT / BOT_DIR / CLAUDLOBBY_ROOT are runtime path anchors the
        composer exports to bot.conf; a fragment references them and they survive
        resolve_placeholders verbatim into .mcp.json for runtime expansion — never
        baked to an absolute path here. Covers the printify combination: an anchor
        with a long path suffix as a single args-array element."""
        from claudlobby.path_audit import COMPOSER_PROVIDED_PATH_ANCHORS

        entry = McpEntry(name="printify")
        args = ["${FLEET_ROOT}/runtime/bots/kev/data/printify-mcp/dist/index.js"]
        assert resolve_placeholders(args, {}, entry, "default") == args
        for anchor in COMPOSER_PROVIDED_PATH_ANCHORS:
            token = "${" + anchor + "}"
            assert resolve_placeholders(token, {}, entry, "default") == token
            # anchor + long suffix, both as a string leaf and an args-array element
            suffixed = token + "/data/x/dist/index.js"
            assert resolve_placeholders(suffixed, {}, entry, "default") == suffixed
            assert resolve_placeholders([suffixed], {}, entry, "default") == [suffixed]


class TestRoundTrip:
    """Composer's resolve_placeholders and validator's required_vars must
    agree on canonical var names — this is the whole point of the extraction."""

    def _make_fleet_dir(
        self, tmp_path: Path, *, composer_var: str | None = None
    ) -> tuple[Path, Paths]:
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

        contract = {
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
        }
        if composer_var:
            # provided_by:composer — compositor-supplied, never operator-set.
            # In the contract only (not the server env block) so the
            # composer/validator agreement test still sees matched name sets.
            contract[composer_var] = {
                "scope": "shared",
                "tier": "fleet",
                "provided_by": "composer",
                "description": "compositor-supplied",
            }
        frag = {
            "notion-server": {
                "command": "npx",
                "args": ["-y", "notion-mcp"],
                "env": {
                    "NOTION_TOKEN": "${TOKEN}",
                    "OAUTH_ID": "${OAUTH_CLIENT_ID}",
                },
            },
            "_env_contract": contract,
        }
        (root / "library" / "mcp" / "notion.json").write_text(json.dumps(frag))

        (root / "templates").mkdir()
        (root / "templates" / "claude.md.j2").write_text("# {{ bot.name }}\n")
        (root / "voices").mkdir()
        (root / "runtime" / "bots").mkdir(parents=True)

        return root, Paths(root=root, fleet_dir=None)

    def test_required_vars_skips_provided_by_composer(self, tmp_path):
        """required_vars must exclude provided_by:composer vars — they are
        compositor-supplied, never operator-set, so validate must not false-warn
        'requires X but not set' (#547; mirrors collect_env_contracts)."""
        root, paths = self._make_fleet_dir(tmp_path, composer_var="VAULT_PATH")
        fleet, _md = load_fleet(root / "fleet.yaml")
        names = {name for name, *_ in required_vars(fleet.bots["worker"], paths)}
        assert "OAUTH_CLIENT_ID" in names  # operator var still required
        assert "VAULT_PATH" not in names  # composer-provided var skipped

    def test_composer_and_validator_agree(self, tmp_path):
        root, paths = self._make_fleet_dir(tmp_path)
        fleet, _md = load_fleet(root / "fleet.yaml")
        bot = fleet.bots["worker"]

        # --- Validator side: canonical var names ---
        req = required_vars(bot, paths)
        validator_vars = {(r.name, r.instance) for r in req}

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

        # Validator canonical names
        validator_names = {r.name for r in req}

        # Both must agree on the full set of canonical var names
        assert composer_names == validator_names


class TestSecretAndSourceFields:
    """#1214 Phase 1 — the two new `_env_contract` fields, carried by the walk.

    Every negative here is paired with a positive control. A test asserting a
    field is absent passes identically when the walk returns nothing at all, so
    "found no source" and "enumerated nothing" have to be distinguishable.
    """

    def _walk(self, contract: dict, *, instances: list[str] | None = None):
        entry = McpEntry(name="acme", instances=instances or ["default"])
        return list(iter_operator_contract_vars(contract, entry))

    def test_both_fields_are_carried_through_the_walk(self):
        got = self._walk(
            {
                "ACME_TOKEN": {
                    "tier": "fleet",
                    "secret": True,
                    "source": "cli:gh-token",
                }
            }
        )
        assert len(got) == 1, "positive control: the walk enumerated nothing"
        assert (got[0].secret, got[0].source) == (True, "cli:gh-token")

    def test_absent_source_is_none_while_the_var_is_still_enumerated(self):
        got = self._walk({"ACME_TOKEN": {"tier": "fleet", "secret": True}})
        assert [v.canonical_name for v in got] == ["ACME_TOKEN"]  # positive control
        assert got[0].source is None
        assert got[0].secret is True

    def test_secret_false_is_preserved_not_coerced_to_the_default(self):
        # The failure this guards: reading `secret` with a truthiness test
        # rather than an explicit lookup makes false and absent identical, and
        # the whole point of F4 is that they are different declarations.
        got = self._walk({"PORT": {"tier": "fleet", "secret": False}})
        assert got[0].secret is False

    def test_instance_scoped_vars_each_carry_the_fields(self):
        got = self._walk(
            {"TOKEN": {"tier": "bot", "scope": "instance", "secret": True}},
            instances=["work", "personal"],
        )
        assert {v.canonical_name for v in got} == {
            "ACME_WORK_TOKEN",
            "ACME_PERSONAL_TOKEN",
        }
        assert all(v.secret is True for v in got)

    def test_required_vars_exposes_origin_and_source_as_separate_facts(self, tmp_path):
        """The rename that matters: `origin` is the declaring surface,
        `source` is the resolver. Conflating them is a silent mis-read, so
        assert they are simultaneously present AND different."""
        root = tmp_path / "claudlobby"
        root.mkdir()
        (root / "fleet.yaml").write_text(
            dedent("""\
            fleet:
              name: t
              service_prefix: com.t
              bots:
                w:
                  expertise: [eng]
                  mcp: [acme]
                  telegram: {handle: w, token_env: TG_W}
        """)
        )
        for kind in ("expertise", "mcp", "integrations", "guardrails", "skills"):
            (root / "library" / kind).mkdir(parents=True)
        (root / "library" / "expertise" / "eng.md").write_text("# E\n\nx.\n")
        (root / "library" / "mcp" / "acme.json").write_text(
            json.dumps(
                {
                    "_env_contract": {
                        "ACME_TOKEN": {
                            "tier": "fleet",
                            "secret": True,
                            "source": "cli:gh-token",
                        }
                    },
                    "acme": {"command": "x", "env": {"T": "${ACME_TOKEN}"}},
                }
            )
        )
        fleet, _ = load_fleet(root / "fleet.yaml")
        paths = Paths(root=root)
        (req,) = required_vars(fleet.bots["w"], paths)
        assert req.origin == "mcp/acme"
        assert req.source == "cli:gh-token"
        assert req.origin != req.source
