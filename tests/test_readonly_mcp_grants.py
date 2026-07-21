"""#661 read-only MCP auto-grants — reads compose, writes stay prompt-gated.

A fragment ``_permissions_contract`` that declares ``read_only_tools`` splits
its tool universe: the read subset composes into ``settings.local.json`` as
exact per-tool allows (so headless bots never wedge on a read prompt), while
every other tool keeps prompting. The server wildcard is never emitted for
such a server, the paired integration's ``tool_grants`` must mirror the read
set exactly, and no library-derived layer (integration, skill, expertise,
guardrail) may cover a write — both directions hard-fail compose. fleet.yaml
``tools.allow`` stays the operator's explicit escape hatch.

The shipped split-contract library content is pinned here too — the mechanism
guarding writes is only as good as the curated lists it enforces.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from claudlobby.composer import (
    _resolve_integration_grants,
    _resolve_mcp_permissions,
    compose_settings_local,
)
from claudlobby.config import BotConfig, FleetConfig, McpEntry
from claudlobby.loader import integration_tool_grants
from claudlobby.paths import Paths

REPO_DIR = Path(__file__).resolve().parent.parent

READS = ["getProduct", "listOrders", "listProducts"]
WRITES = ["createProduct", "updateProduct"]


def _build_library(
    root: Path,
    *,
    read_only: list[str] | None = READS,
    tools: list[str] | None = None,
    integration_grants: list[str] | None = None,
    skill_grants: list[str] | None = None,
) -> Paths:
    """Stage a split-contract shopify fragment + optional integration/skill files."""
    (root / "runtime" / "bots").mkdir(parents=True)
    mcp = root / "library" / "mcp"
    mcp.mkdir(parents=True)
    contract: dict = {"tools": READS + WRITES if tools is None else tools}
    if read_only is not None:
        contract["read_only_tools"] = read_only
    (mcp / "shopify.json").write_text(
        json.dumps(
            {
                "_permissions_contract": contract,
                "shopify": {"command": "npx", "args": ["-y", "shopify-mcp"]},
            }
        )
    )
    if integration_grants is not None:
        integ = root / "library" / "integrations"
        integ.mkdir(parents=True)
        grants_yaml = "".join(f'  - "{g}"\n' for g in integration_grants)
        (integ / "shopify.md").write_text(
            f"---\ntitle: shopify\ntool_grants:\n{grants_yaml}---\n\n# shopify\n"
        )
    if skill_grants is not None:
        skill_dir = root / "library" / "skills" / "catalog"
        skill_dir.mkdir(parents=True)
        grants_yaml = "".join(f'  - "{g}"\n' for g in skill_grants)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: catalog\ntool_grants:\n{grants_yaml}---\n\n# catalog\n\nbody\n"
        )
    return Paths(root=root, fleet_dir=root)


def _bot(
    instances: list[str] | None = None, skills: list[str] | None = None
) -> BotConfig:
    return BotConfig(
        bot_id="worker",
        name="worker",
        expertise=["eng"],
        skills=skills or [],
        mcp=[McpEntry(name="shopify", instances=instances or ["default"])],
    )


def _single_bot_fleet(bot: BotConfig) -> FleetConfig:
    return FleetConfig(name="t", service_prefix="p", bots={bot.bot_id: bot})


class TestReadOnlySubsetEmission:
    """_resolve_mcp_permissions: read_only_tools → per-tool entries, never a wildcard."""

    def test_read_only_emits_exact_tools_no_wildcard(self, tmp_path):
        paths = _build_library(tmp_path / "r")
        result = _resolve_mcp_permissions(_bot(), paths)
        assert sorted(result) == [
            "mcp__shopify__getProduct",
            "mcp__shopify__listOrders",
            "mcp__shopify__listProducts",
        ]

    def test_write_tools_never_emitted(self, tmp_path):
        paths = _build_library(tmp_path / "r")
        result = _resolve_mcp_permissions(_bot(), paths)
        for write in WRITES:
            assert f"mcp__shopify__{write}" not in result

    def test_multi_instance_expands_read_only(self, tmp_path):
        paths = _build_library(tmp_path / "r")
        result = _resolve_mcp_permissions(_bot(instances=["prod", "staging"]), paths)
        assert "mcp__shopify-prod__getProduct" in result
        assert "mcp__shopify-staging__getProduct" in result
        assert len(result) == 2 * len(READS)

    def test_empty_read_only_list_emits_nothing(self, tmp_path):
        """Declared-but-empty read set = a fully prompt-gated server."""
        paths = _build_library(tmp_path / "r", read_only=[])
        assert _resolve_mcp_permissions(_bot(), paths) == []

    def test_read_only_outside_tools_raises(self, tmp_path):
        """A typo'd or renamed tool must fail compose, not silently grant nothing."""
        paths = _build_library(tmp_path / "r", read_only=["getProdcut"])
        with pytest.raises(ValueError, match="getProdcut"):
            _resolve_mcp_permissions(_bot(), paths)

    def test_read_only_without_tools_universe_raises(self, tmp_path):
        paths = _build_library(tmp_path / "r", tools=[], read_only=["getProduct"])
        with pytest.raises(ValueError, match="tool universe"):
            _resolve_mcp_permissions(_bot(), paths)

    def test_no_read_only_keeps_wildcard(self, tmp_path):
        """github/notion posture unchanged: full contract without a split → wildcard."""
        paths = _build_library(tmp_path / "r", read_only=None)
        assert _resolve_mcp_permissions(_bot(), paths) == ["mcp__shopify__*"]


class TestReadOnlyGrantGuardrail:
    """Integration tool_grants for a split server must mirror the read set exactly."""

    def test_wildcard_grant_fails_compose(self, tmp_path):
        paths = _build_library(tmp_path / "r", integration_grants=["mcp__shopify__*"])
        with pytest.raises(ValueError, match="read_only_tools"):
            _resolve_integration_grants(_bot(), paths)

    def test_write_tool_grant_fails_compose(self, tmp_path):
        paths = _build_library(
            tmp_path / "r",
            integration_grants=[
                *(f"mcp__shopify__{t}" for t in READS),
                "mcp__shopify__createProduct",
            ],
        )
        with pytest.raises(ValueError, match="createProduct"):
            _resolve_integration_grants(_bot(), paths)

    def test_instance_qualified_write_grant_fails_compose(self, tmp_path):
        """A pre-qualified instance grant can't smuggle a write past the prefix rewrite."""
        paths = _build_library(
            tmp_path / "r",
            integration_grants=[
                *(f"mcp__shopify__{t}" for t in READS),
                "mcp__shopify-prod__createProduct",
            ],
        )
        with pytest.raises(ValueError, match="read_only_tools"):
            _resolve_integration_grants(_bot(), paths)

    def test_partial_read_mirror_fails_with_directional_error(self, tmp_path):
        """Forgetting to mirror a read tool names the exact fix — the net that
        keeps reads composing once the legacy resolver's superset gate is cut."""
        paths = _build_library(
            tmp_path / "r", integration_grants=["mcp__shopify__getProduct"]
        )
        with pytest.raises(ValueError, match="missing read entries"):
            _resolve_integration_grants(_bot(), paths)

    def test_exact_read_grants_pass_and_rewrite_per_instance(self, tmp_path):
        paths = _build_library(
            tmp_path / "r",
            integration_grants=[f"mcp__shopify__{t}" for t in READS],
        )
        result = _resolve_integration_grants(_bot(instances=["prod"]), paths)
        assert sorted(result) == sorted(f"mcp__shopify-prod__{t}" for t in READS)

    def test_contract_less_integration_unaffected(self, tmp_path):
        """No read_only_tools declared → wildcard grants stay legal (github posture)."""
        paths = _build_library(
            tmp_path / "r", read_only=None, integration_grants=["mcp__shopify__*"]
        )
        assert _resolve_integration_grants(_bot(), paths) == ["mcp__shopify__*"]


class TestComposeEndToEnd:
    """compose_settings_local lands reads in allow and keeps writes/wildcards out."""

    def _compose(self, tmp_path, bot: BotConfig | None = None, **kwargs):
        paths = _build_library(tmp_path / "r", **kwargs)
        bot = bot or _bot()
        return compose_settings_local(bot, _single_bot_fleet(bot), paths)

    def test_reads_allowed_writes_absent(self, tmp_path):
        settings = self._compose(
            tmp_path, integration_grants=[f"mcp__shopify__{t}" for t in READS]
        )
        allow = settings["permissions"]["allow"]
        for read in READS:
            assert f"mcp__shopify__{read}" in allow
        assert "mcp__shopify__*" not in allow
        for write in WRITES:
            assert f"mcp__shopify__{write}" not in allow

    def test_wildcard_integration_fails_generation(self, tmp_path):
        with pytest.raises(ValueError, match="read_only_tools"):
            self._compose(tmp_path, integration_grants=["mcp__shopify__*"])

    def test_missing_integration_grants_trips_superset_gate(self, tmp_path):
        """Split contract without a grants-bearing integration file trips the
        migration-window superset gate (the no-file case; the partial-mirror
        case fails earlier with the directional error)."""
        with pytest.raises(ValueError, match="drop legacy MCP grants"):
            self._compose(tmp_path)


class TestUnionLayerWriteGuard:
    """No library-derived layer may cover a write of a split server (#661 F1).

    Skill ``tool_grants`` join the same allow union as integration grants; the
    union-layer assert must catch a write there, while the operator's
    fleet.yaml ``tools.allow`` stays exempt.
    """

    def _compose_with_skill(self, tmp_path, skill_grants: list[str]):
        paths = _build_library(
            tmp_path / "r",
            integration_grants=[f"mcp__shopify__{t}" for t in READS],
            skill_grants=skill_grants,
        )
        bot = _bot(skills=["catalog"])
        return compose_settings_local(bot, _single_bot_fleet(bot), paths)

    def test_skill_wildcard_fails_generation(self, tmp_path):
        with pytest.raises(ValueError, match="non-read tools"):
            self._compose_with_skill(tmp_path, ["mcp__shopify__*"])

    def test_skill_write_grant_fails_generation(self, tmp_path):
        with pytest.raises(ValueError, match="createProduct"):
            self._compose_with_skill(tmp_path, ["mcp__shopify__createProduct"])

    def test_skill_read_grant_passes(self, tmp_path):
        settings = self._compose_with_skill(tmp_path, ["mcp__shopify__getProduct"])
        assert "mcp__shopify__getProduct" in settings["permissions"]["allow"]

    def test_operator_tools_allow_write_stays_exempt(self, tmp_path):
        """fleet.yaml tools.allow is appended after the guard — the escape hatch."""
        paths = _build_library(
            tmp_path / "r", integration_grants=[f"mcp__shopify__{t}" for t in READS]
        )
        bot = _bot()
        bot.tools.allow.append("mcp__shopify__createProduct")
        settings = compose_settings_local(bot, _single_bot_fleet(bot), paths)
        allow = settings["permissions"]["allow"]
        assert "mcp__shopify__createProduct" in allow
        assert "mcp__shopify__updateProduct" not in allow


def _split_fragment_names() -> list[str]:
    """Every shipped fragment declaring a read/write split — self-extending pin."""
    names = []
    for frag_path in sorted((REPO_DIR / "library" / "mcp").glob("*.json")):
        contract = json.loads(frag_path.read_text()).get("_permissions_contract") or {}
        if contract.get("read_only_tools") is not None:
            names.append(frag_path.stem)
    return names


SPLIT_FRAGMENTS = _split_fragment_names()


class TestShippedLibraryContent:
    """Pin every shipped split-contract fragment to the read-only guardrail."""

    # how_to_use is a static documentation reader — verified side-effect-free in
    # the packaged server source, granted alongside the get_*/list_* families.
    # A future split fragment with an oddly-named read must extend this canary
    # deliberately — that review friction is the point.
    READ_NAME_RE = re.compile(r"^(get|list)[_A-Z]|^how_to_use$")

    KNOWN_WRITES = {
        "shopify": [
            "createProduct",
            "updateProduct",
            "createRefund",
            "adjustInventory",
        ],
        "printify": [
            "create_product",
            "update_product",
            "delete_product",
            "publish_product",
            "upload_image",
        ],
    }

    def _contract(self, name: str) -> dict:
        frag = json.loads((REPO_DIR / "library" / "mcp" / f"{name}.json").read_text())
        return frag["_permissions_contract"]

    def _tool_grants(self, name: str) -> list[str]:
        # The production reader — the mirror is pinned through the same code
        # path _resolve_integration_grants composes from.
        return integration_tool_grants(Paths(root=REPO_DIR, fleet_dir=REPO_DIR), name)

    def test_shipped_split_fragments_discovered(self):
        assert {"shopify", "printify"} <= set(SPLIT_FRAGMENTS)

    @pytest.mark.parametrize("name", SPLIT_FRAGMENTS)
    def test_read_only_is_strict_subset_of_tools(self, name):
        contract = self._contract(name)
        read_only = set(contract["read_only_tools"])
        tools = set(contract["tools"])
        assert read_only < tools, "read set must exclude at least the write tools"

    @pytest.mark.parametrize("name", SPLIT_FRAGMENTS)
    def test_read_only_names_match_read_patterns(self, name):
        """Every auto-granted tool must look like a read — a canary against a
        mutation ever slipping into the curated list."""
        for tool in self._contract(name)["read_only_tools"]:
            assert self.READ_NAME_RE.match(tool), f"{tool} does not look read-only"

    @pytest.mark.parametrize("name", SPLIT_FRAGMENTS)
    def test_integration_grants_mirror_read_only_exactly(self, name):
        contract = self._contract(name)
        expected = {f"mcp__{name}__{t}" for t in contract["read_only_tools"]}
        assert set(self._tool_grants(name)) == expected

    @pytest.mark.parametrize("name", SPLIT_FRAGMENTS)
    def test_known_writes_stay_prompt_gated(self, name):
        writes = self.KNOWN_WRITES.get(name)
        assert writes, f"add {name} to KNOWN_WRITES — every split fragment needs one"
        contract = self._contract(name)
        grants = self._tool_grants(name)
        for write in writes:
            assert write in contract["tools"], (
                "write must stay in the declared universe"
            )
            assert write not in contract["read_only_tools"]
            assert f"mcp__{name}__{write}" not in grants
