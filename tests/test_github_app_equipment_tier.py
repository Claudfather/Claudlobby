"""The GitHub App contract scaffolds where the equipment is (#252 seam / #1226 stage 3).

Two suites already exist and they do not meet. ``test_equipment_scaffolding``
proves ``scaffold_tier`` against a SYNTHETIC fragment written into ``tmp_path``;
``test_github_app_fragment`` proves the real ``library/mcp/github-app.json``
parses, declares three vars and composes into ``.mcp.json``. Neither runs the
shipped fragment THROUGH the tier machinery, so "a GitHub App given to one bot
lands in that bot's ``.env``" — the case the scaffolder's own docstring names —
has never been exercised by a real consumer.

That gap has the shape this repo keeps re-learning: a harness that exercises a
stand-in certifies the stand-in. The fragment could change its ``default_tier``,
lose a var, or be renamed, and every existing test would stay green.

So the fixture COPIES the shipped bytes and asserts the copy is byte-identical
(``test_the_fixture_uses_the_shipped_fragment``). A drifted fixture fails loudly
here rather than quietly certifying a file nobody ships.

Boundary, stated because it is invisible from a green run: this proves
PLACEMENT and COLLECTION only. It does not prove a real App authenticates —
that needs credentials only the operator holds. The helper chain itself was
already proven end to end against a throwaway RSA key (App-auth P1).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from claudlobby.composer import collect_env_contracts, scaffold_env_files
from claudlobby.config import load_fleet
from claudlobby.credentials import declared_for_fleet
from claudlobby.mcp_resolve import required_vars
from claudlobby.paths import Paths

REPO = Path(__file__).resolve().parent.parent
SHIPPED = REPO / "library" / "mcp" / "github-app.json"

APP_VARS = (
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
)

_HEAD = (
    "fleet:\n"
    "  name: acme\n"
    "  service_prefix: com.acme\n"
    "  defaults:\n"
    "    expertise: [x]\n"
    "  bots:\n"
)

ONE_ATTACHES = (
    "    alpha:\n      expertise: [x]\n      mcp: [github-app]\n"
    "    beta:\n      expertise: [x]\n"
)
BOTH_ATTACH = (
    "    alpha:\n      expertise: [x]\n      mcp: [github-app]\n"
    "    beta:\n      expertise: [x]\n      mcp: [github-app]\n"
)
NONE_ATTACH = "    alpha:\n      expertise: [x]\n    beta:\n      expertise: [x]\n"


def _build(tmp_path: Path, bots: str, *, fragment: str | None = None):
    """Stand up a throwaway fleet carrying the SHIPPED github-app fragment.

    ``fragment`` overrides the copied bytes and exists only for the
    can-this-fail control; every real test takes the shipped file.
    """
    for kind in ("mcp", "integrations", "expertise", "skills"):
        (tmp_path / "library" / kind).mkdir(parents=True, exist_ok=True)
    (tmp_path / "lib").mkdir(exist_ok=True)
    (tmp_path / "library" / "expertise" / "x.md").write_text("---\ntitle: x\n---\n# x\n")

    dest = tmp_path / "library" / "mcp" / "github-app.json"
    if fragment is None:
        shutil.copy(SHIPPED, dest)
    else:
        dest.write_text(fragment)

    (tmp_path / "fleet.yaml").write_text(_HEAD + bots)
    fleet, _ = load_fleet(tmp_path / "fleet.yaml")
    paths = Paths(root=tmp_path, fleet_dir=tmp_path)
    for name in fleet.bots:
        paths.bot_runtime(name).mkdir(parents=True, exist_ok=True)
    return fleet, paths


def _env_text(path: Path) -> str:
    return path.read_text() if path.is_file() else ""


def _tiers(fleet, paths) -> dict[str, str]:
    return {
        ev.name: ev.scaffold_tier()
        for ev in collect_env_contracts(fleet, paths)
        if ev.name in APP_VARS
    }


# --- the fixture must be the real thing -------------------------------------


def test_the_fixture_uses_the_shipped_fragment(tmp_path: Path) -> None:
    """A stand-in would make every assertion below vacuous."""
    _build(tmp_path, ONE_ATTACHES)
    copied = tmp_path / "library" / "mcp" / "github-app.json"
    assert copied.read_bytes() == SHIPPED.read_bytes()


def test_the_shipped_fragment_declares_exactly_the_three_app_vars() -> None:
    contract = json.loads(SHIPPED.read_text())["_env_contract"]
    assert tuple(contract) == APP_VARS


def test_the_private_key_is_declared_as_a_PATH_not_key_material() -> None:
    """Design property, pinned so a future edit cannot quietly drop it.

    Key material must never be a composed value: the contract names a path, so
    ``fleet.yaml`` and every scaffolded ``.env`` carry a filename and the .pem
    stays 0600 on disk. A var renamed to hold the key itself would put a
    private key into files the compositor writes.
    """
    contract = json.loads(SHIPPED.read_text())["_env_contract"]
    key_vars = [v for v in contract if "PRIVATE_KEY" in v]
    assert key_vars == ["GITHUB_APP_PRIVATE_KEY_PATH"]
    assert all(v.endswith("_PATH") for v in key_vars)


# --- placement ---------------------------------------------------------------


def test_app_given_to_one_bot_scaffolds_into_that_bots_env(tmp_path: Path) -> None:
    """#1226's claim, exercised by its first real consumer."""
    fleet, paths = _build(tmp_path, ONE_ATTACHES)
    assert _tiers(fleet, paths) == dict.fromkeys(APP_VARS, "bot")

    scaffold_env_files(fleet, paths)
    alpha = _env_text(paths.bot_runtime("alpha") / ".env")
    for var in APP_VARS:
        assert f"export {var}=" in alpha


def test_app_given_to_one_bot_never_reaches_the_fleet_env(tmp_path: Path) -> None:
    """The negative half. A fleet-tier stub is sourced by every bot."""
    fleet, paths = _build(tmp_path, ONE_ATTACHES)
    scaffold_env_files(fleet, paths)
    fleet_env = _env_text(tmp_path / ".env")
    for var in APP_VARS:
        assert var not in fleet_env


def test_a_bot_without_the_app_is_untouched(tmp_path: Path) -> None:
    fleet, paths = _build(tmp_path, ONE_ATTACHES)
    scaffold_env_files(fleet, paths)
    beta = _env_text(paths.bot_runtime("beta") / ".env")
    for var in APP_VARS:
        assert var not in beta
    assert [v.name for v in required_vars(fleet.bots["beta"], paths)] == []


def test_app_held_by_every_bot_stays_fleet_tier(tmp_path: Path) -> None:
    """Scope, not spelling: equipment everyone holds is fleet infrastructure."""
    fleet, paths = _build(tmp_path, BOTH_ATTACH)
    assert _tiers(fleet, paths) == dict.fromkeys(APP_VARS, "fleet")

    scaffold_env_files(fleet, paths)
    fleet_env = _env_text(tmp_path / ".env")
    for var in APP_VARS:
        assert f"export {var}=" in fleet_env
        for name in fleet.bots:
            assert var not in _env_text(paths.bot_runtime(name) / ".env")


# --- dormancy ----------------------------------------------------------------


def test_the_fragment_is_dormant_until_a_fleet_declares_it(tmp_path: Path) -> None:
    """A credential contract must not activate because it exists in library/.

    The file is present and loadable; no bot attaches it. Nothing may be
    collected and nothing written, at ANY tier — otherwise a root pull arms a
    credential surface on every fleet on the host.
    """
    fleet, paths = _build(tmp_path, NONE_ATTACH)
    assert _tiers(fleet, paths) == {}

    scaffold_env_files(fleet, paths)
    for var in APP_VARS:
        assert var not in _env_text(tmp_path / ".env")
        for name in fleet.bots:
            assert var not in _env_text(paths.bot_runtime(name) / ".env")


# --- creds-reconcile ---------------------------------------------------------


def test_creds_reconcile_collects_all_three_declarations(tmp_path: Path) -> None:
    """``declared_for_fleet`` is the door ``claudlobby creds-reconcile`` reads.

    Attribution is asserted too, not just presence: a declaration collected but
    credited to the wrong equipment or the wrong bot would report a real gap
    against a bot that never asked for it.
    """
    fleet, paths = _build(tmp_path, ONE_ATTACHES)
    declarations, _ = declared_for_fleet(fleet, paths)
    by_var = {d.var: d for d in declarations}
    for var in APP_VARS:
        assert var in by_var, f"{var} not collected by the creds-reconcile door"
        assert by_var[var].source == "github-app"
        assert by_var[var].kind == "mcp"
        assert by_var[var].equipped_by == ["alpha"]


# --- the control -------------------------------------------------------------


def test_the_placement_assertions_can_fail(tmp_path: Path) -> None:
    """A green suite that cannot go red is not evidence.

    Same fleet shape, one mutation: the contract declares ``fleet`` and nothing
    attaches per-bot, so the bot-tier assertion must break.
    """
    mutated = json.loads(SHIPPED.read_text())
    for var in APP_VARS:
        mutated["_env_contract"][var]["default_tier"] = "fleet"
    fleet, paths = _build(
        tmp_path, BOTH_ATTACH, fragment=json.dumps(mutated)
    )
    assert _tiers(fleet, paths) != dict.fromkeys(APP_VARS, "bot")
