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
        bot.tool_permissions.allow.append("mcp__shopify__createProduct")
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
    # The default heuristic: an auto-granted tool must LOOK like a read
    # (get_*/list_*/how_to_use). A server whose naming convention doesn't fit the
    # heuristic registers its verified reads in VERIFIED_NONSTANDARD_READS below —
    # a deliberate, human-curated exception per fragment. That review friction is
    # the point: a genuinely new read must be typed in by a human who confirmed it
    # does not mutate.
    READ_NAME_RE = re.compile(r"^(get|list)[_A-Z]|^how_to_use$")

    # Writes each split server exposes that must stay prompt-gated (never
    # auto-granted). An empty list = a verified PURE-READ server: the fragment
    # exposes no write tool, because the server is read-only (e.g. the GA4 Data
    # API) or the fragment drops writes server-side (posthog's readonly=true).
    # Every split fragment needs an entry — the curated ledger the write-guard
    # enforces.
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
        "google-analytics": [],
        "posthog": [],
        "google-search-console": [
            "add_site",
            "delete_site",
            "submit_sitemap",
            "delete_sitemap",
            "manage_sitemaps",
        ],
        # meta-ads: pure-read as shipped. meta-ads-mcp-server is write-capable
        # (19 create/update/delete/pause campaign+adset+ad, budget-schedule,
        # creative, and image-upload tools), but the fragment sets
        # META_ADS_ENABLE_WRITE_TOOLS=false, so the server never registers them —
        # the same server-side write-drop as posthog's readonly=true. Empty
        # list = a verified pure-read fragment.
        "meta-ads": [],
    }

    # Verified read-only tools whose names don't match READ_NAME_RE's get_*/list_*
    # heuristic. Each is confirmed side-effect-free in the pinned server source;
    # listing it here is the deliberate human sign-off. shopify/printify need no
    # entry — their reads all fit the heuristic.
    VERIFIED_NONSTANDARD_READS = {
        # GA4: keyword schema search over dimension/metric names (read).
        "google-analytics": {"search_schema"},
        # posthog uses <resource>-<action> naming; none fit get_*/list_*.
        "posthog": {
            "organizations-get",
            "organization-details-get",
            "projects-get",
            "insights-get-all",
            "insight-get",
            "insight-query",
            "query-run",
            "query-generate-hogql-from-question",
            "dashboards-get-all",
            "dashboard-get",
            "event-definitions-list",
            "property-definitions",
            "properties-list",
        },
        # GSC: period comparison, URL index inspection, batched inspection, and
        # indexing-issue checks — all read-only in the pinned server source.
        "google-search-console": {
            "compare_search_periods",
            "inspect_url_enhanced",
            "batch_url_inspection",
            "check_indexing_issues",
        },
        # meta-ads uses meta_ads_<verb>_<object> naming; none fit get_*/list_*.
        # All 35 are confirmed side-effect-free reads in the pinned
        # meta-ads-mcp-server@1.5.1 source (its 19 write tools are gated off —
        # see KNOWN_WRITES above).
        "meta-ads": {
            "meta_ads_list_ad_accounts",
            "meta_ads_get_ad_account_details",
            "meta_ads_get_activities_by_adaccount",
            "meta_ads_get_activities_by_adset",
            "meta_ads_get_ad_by_id",
            "meta_ads_get_ads_by_adaccount",
            "meta_ads_get_ads_by_campaign",
            "meta_ads_get_ads_by_adset",
            "meta_ads_get_adset_by_id",
            "meta_ads_get_adsets_by_ids",
            "meta_ads_get_adsets_by_adaccount",
            "meta_ads_get_adsets_by_campaign",
            "meta_ads_get_campaign_by_id",
            "meta_ads_get_campaigns_by_adaccount",
            "meta_ads_get_adcreatives_by_adaccount",
            "meta_ads_get_ad_creative_by_id",
            "meta_ads_get_ad_creatives_by_ad_id",
            "meta_ads_compute_image_crops",
            "meta_ads_get_adaccount_insights",
            "meta_ads_get_campaign_insights",
            "meta_ads_get_adset_insights",
            "meta_ads_get_ad_insights",
            "meta_ads_get_ad_images",
            "meta_ads_get_ad_previews",
            "meta_ads_get_ad_video",
            "meta_ads_get_image_by_hash",
            "meta_ads_get_account_pages",
            "meta_ads_search_pages_by_name",
            "meta_ads_fetch_pagination_url",
            "meta_ads_search_interests",
            "meta_ads_get_interest_suggestions",
            "meta_ads_search_behaviors",
            "meta_ads_search_demographics",
            "meta_ads_search_geo_locations",
            "meta_ads_estimate_audience_size",
        },
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
    def test_read_only_is_subset_of_tools(self, name):
        contract = self._contract(name)
        read_only = set(contract["read_only_tools"])
        tools = set(contract["tools"])
        # <= not <: a PURE-READ server (KNOWN_WRITES[name] == []) legitimately has
        # read_only == tools. A write-bearing server must keep its writes out of
        # the read set, so for those the subset is strict.
        assert read_only <= tools, "read set must be within the declared tool universe"
        if self.KNOWN_WRITES[name]:
            assert read_only < tools, "write-bearing server: reads must exclude writes"

    @pytest.mark.parametrize("name", SPLIT_FRAGMENTS)
    def test_read_only_names_match_read_patterns(self, name):
        """Every auto-granted tool must look like a read — a canary against a
        mutation ever slipping into the curated list. Reads that don't fit the
        get_*/list_* heuristic must be explicitly vetted in
        VERIFIED_NONSTANDARD_READS[name]."""
        allowed = self.VERIFIED_NONSTANDARD_READS.get(name, set())
        for tool in self._contract(name)["read_only_tools"]:
            assert self.READ_NAME_RE.match(tool) or tool in allowed, (
                f"{tool} is not get_*/list_* and is not vetted in "
                f"VERIFIED_NONSTANDARD_READS[{name!r}]"
            )

    @pytest.mark.parametrize("name", SPLIT_FRAGMENTS)
    def test_integration_grants_mirror_read_only_exactly(self, name):
        contract = self._contract(name)
        expected = {f"mcp__{name}__{t}" for t in contract["read_only_tools"]}
        assert set(self._tool_grants(name)) == expected

    @pytest.mark.parametrize("name", SPLIT_FRAGMENTS)
    def test_known_writes_stay_prompt_gated(self, name):
        writes = self.KNOWN_WRITES.get(name)
        assert writes is not None, (
            f"add {name} to KNOWN_WRITES — every split fragment needs an entry "
            "([] for a verified pure-read server with no write tools)"
        )
        contract = self._contract(name)
        grants = self._tool_grants(name)
        for write in writes:
            assert write in contract["tools"], (
                "write must stay in the declared universe"
            )
            assert write not in contract["read_only_tools"]
            assert f"mcp__{name}__{write}" not in grants
