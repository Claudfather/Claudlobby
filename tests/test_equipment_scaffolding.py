"""A var's stub follows its equipment (#1214 F3a / #1226 stage 3).

The rule under test is SCOPE, not spelling: equipment held by some bots and not
others is that bot's equipment and its credential belongs with it; equipment
every bot holds is fleet infrastructure however it was declared.

The load-bearing negative is the last section. Routing a var to a bot's .env
moves its stub to the MOST specific tier there is, where a bare ``export VAR=``
outranks every other assignment of that key on the machine. The scaffolder
already guards that (``provided_upstream``); this file exists partly to prove
stage 3 did not quietly walk around it.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby.composer import collect_env_contracts, scaffold_env_files
from claudlobby.config import load_fleet
from claudlobby.paths import Paths

FLEET = dedent("""\
    fleet:
      name: acme
      service_prefix: com.acme
      defaults:
        expertise: [x]
    {defaults}
      bots:
    {bots}
    """)


def _build(tmp_path: Path, *, defaults: str = "", bots: str) -> tuple:
    for kind in ("mcp", "integrations", "expertise", "skills"):
        (tmp_path / "library" / kind).mkdir(parents=True, exist_ok=True)
    (tmp_path / "lib").mkdir(exist_ok=True)
    (tmp_path / "library" / "expertise" / "x.md").write_text("---\ntitle: x\n---\n# x\n")
    (tmp_path / "library" / "mcp" / "github.json").write_text(
        '{"mcpServers": {"github": {"command": "x", "env": {"T": "${GITHUB_PAT}"}}},'
        ' "_env_contract": {"GITHUB_PAT": {"description": "PAT",'
        ' "default_tier": "fleet", "secret": true}}}'
    )
    (tmp_path / "library" / "mcp" / "notion.json").write_text(
        '{"mcpServers": {"notion": {"command": "x", "env": {"T": "${NOTION_KEY}"}}},'
        ' "_env_contract": {"NOTION_KEY": {"description": "Notion",'
        ' "default_tier": "fleet", "secret": true}}}'
    )
    (tmp_path / "fleet.yaml").write_text(FLEET.format(defaults=defaults, bots=bots))
    fleet, _ = load_fleet(tmp_path / "fleet.yaml")
    paths = Paths(root=tmp_path, fleet_dir=tmp_path)
    for name in fleet.bots:
        paths.bot_runtime(name).mkdir(parents=True, exist_ok=True)
    return fleet, paths


TWO_BOTS_ONE_ATTACHES = """\
    alpha:
      expertise: [x]
      mcp: [notion]
    beta:
      expertise: [x]
"""

TWO_BOTS_BOTH_ATTACH = """\
    alpha:
      expertise: [x]
      mcp: [github]
    beta:
      expertise: [x]
      mcp: [github]
"""


def _tier_of(fleet, paths, var: str) -> str:
    for ev in collect_env_contracts(fleet, paths):
        if ev.name == var:
            return ev.scaffold_tier()
    raise AssertionError(f"{var} not collected")


def test_equipment_only_one_bot_holds_scaffolds_at_that_bot(tmp_path: Path) -> None:
    """The GitHub-App-given-to-a-bot case, which is the point of F3a."""
    fleet, paths = _build(tmp_path, bots=TWO_BOTS_ONE_ATTACHES)
    assert _tier_of(fleet, paths, "NOTION_KEY") == "bot"

    scaffold_env_files(fleet, paths)
    assert "NOTION_KEY" in (paths.bot_runtime("alpha") / ".env").read_text()
    beta = paths.bot_runtime("beta") / ".env"
    assert not beta.is_file() or "NOTION_KEY" not in beta.read_text()
    assert "NOTION_KEY" not in (tmp_path / ".env").read_text()


def test_equipment_every_bot_holds_stays_fleet_tier(tmp_path: Path) -> None:
    """Declared per-bot, but held by all — identical scope to fleet.defaults.

    Keying on the spelling would turn one fleet stub into N bot stubs at the
    most specific tier, multiplying exposure to the shadowing this phase is
    meant to contain.
    """
    fleet, paths = _build(tmp_path, bots=TWO_BOTS_BOTH_ATTACH)
    assert _tier_of(fleet, paths, "GITHUB_PAT") == "fleet"

    scaffold_env_files(fleet, paths)
    assert "GITHUB_PAT" in (tmp_path / ".env").read_text()
    for name in ("alpha", "beta"):
        bot_env = paths.bot_runtime(name) / ".env"
        assert not bot_env.is_file() or "GITHUB_PAT" not in bot_env.read_text()


def test_fleet_defaults_equipment_stays_fleet_tier(tmp_path: Path) -> None:
    fleet, paths = _build(
        tmp_path,
        defaults="    mcp: [github]\n",
        bots="    alpha:\n      expertise: [x]\n    beta:\n      expertise: [x]\n",
    )
    assert _tier_of(fleet, paths, "GITHUB_PAT") == "fleet"


def test_a_one_bot_fleet_is_fleet_tier_not_bot_tier(tmp_path: Path) -> None:
    """All-of-one is still all. The degenerate case must not invert the rule."""
    fleet, paths = _build(tmp_path, bots="    solo:\n      expertise: [x]\n      mcp: [github]\n")
    assert _tier_of(fleet, paths, "GITHUB_PAT") == "fleet"


@pytest.mark.parametrize("tier", ["host", "root"])
def test_a_host_tier_var_gets_no_stub_at_all(tmp_path: Path, tier: str) -> None:
    """Withholding the stub is the feature, not a gap.

    The composer owns the fleet and bot .env files. Writing `export VAR=` at the
    fleet tier for a var whose declared home is ~/.env would BLANK the value
    that declaration points at — manufacturing #1213 out of helpfulness.
    """
    fleet, paths = _build(tmp_path, bots=TWO_BOTS_BOTH_ATTACH)
    (tmp_path / "library" / "mcp" / "github.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {"command": "x", "env": {"T": "${GITHUB_PAT}"}}
                },
                "_env_contract": {
                    "GITHUB_PAT": {
                        "description": "PAT",
                        "default_tier": tier,
                        "secret": True,
                    }
                },
            }
        )
    )
    fleet, _ = load_fleet(tmp_path / "fleet.yaml")
    assert _tier_of(fleet, paths, "GITHUB_PAT") == tier

    logged: list[str] = []
    scaffold_env_files(fleet, paths, log=logged.append)
    fleet_env = tmp_path / ".env"
    body = fleet_env.read_text() if fleet_env.is_file() else ""
    assert "GITHUB_PAT" not in body, "a stub here would blank the declared tier"
    assert any("no stub written" in line and "GITHUB_PAT" in line for line in logged), (
        "withholding silently is its own defect — the operator must be told "
        f"where the value goes: {logged}"
    )


# --------------------------------------------------------------------------
# The guard stage 3 must not regress
# --------------------------------------------------------------------------


def test_a_bot_routed_var_with_an_upstream_value_is_commented_not_live(
    tmp_path: Path,
) -> None:
    """Routing to the bot tier must not resurrect the blanking bug.

    ``provided_upstream`` already refuses to emit a live stub for a var carrying
    a real value at a higher tier. Stage 3 sends MORE vars to the bot tier, so
    this is the test that stage 3 walked through that guard rather than around
    it. A live `export NOTION_KEY=` here would blank the fleet value on the
    bot's next boot, silently.
    """
    fleet, paths = _build(tmp_path, bots=TWO_BOTS_ONE_ATTACHES)
    (tmp_path / ".env").write_text("export NOTION_KEY=a_real_value\n")

    scaffold_env_files(fleet, paths)
    body = (paths.bot_runtime("alpha") / ".env").read_text()
    assert "NOTION_KEY" in body
    assert "\nexport NOTION_KEY=\n" not in body, (
        f"live blanking stub emitted at the most specific tier:\n{body}"
    )
    assert "# export NOTION_KEY=" in body or "higher .env tier" in body, body


def test_the_guard_test_can_fail(tmp_path: Path) -> None:
    """Positive control: with NO upstream value the stub IS live.

    Without this, the assertion above would pass just as happily against a
    scaffolder that had stopped emitting anything at all.
    """
    fleet, paths = _build(tmp_path, bots=TWO_BOTS_ONE_ATTACHES)
    scaffold_env_files(fleet, paths)
    body = (paths.bot_runtime("alpha") / ".env").read_text()
    assert "\nexport NOTION_KEY=\n" in body, body
