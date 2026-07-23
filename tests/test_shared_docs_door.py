"""L3 — §Shared Documentation door-stamp (templates/claude.md.j2).

A vault-wired bot (has ``claudron_vault_path``) gets the Claudron knowledge door
stamped into its §Shared Documentation; a non-vault bot keeps the legacy raw-tree
text, byte-identical to before this change.
"""

from __future__ import annotations

from pathlib import Path

from claudlobby.composer import compose_claude_md
from claudlobby.config import BotConfig, FleetConfig
from claudlobby.paths import Paths
from tests.conftest import install_real_template


def _setup(tmp_path: Path) -> Paths:
    root = tmp_path / "claudlobby"
    root.mkdir()
    (root / "library" / "expertise").mkdir(parents=True)
    (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
    install_real_template(root)
    (root / "runtime" / "bots").mkdir(parents=True)
    (root / "voices").mkdir()
    return Paths(root=root, fleet_dir=root)


# The exact legacy block — the non-vault branch must reproduce it verbatim.
def _expected_raw_tree_block(shared_docs_path: str) -> str:
    return (
        "## Shared Documentation\n\n"
        f"Fleet-shared docs at `{shared_docs_path}`:\n\n"
        "- `planning/active/` — in-flight plans. Check before starting related work.\n"
        "- `planning/completed/` — archived after merge/close.\n"
        "- `decisions/` — ratified ADRs. Follow unless human overrides.\n"
        "- `knowledge/<repo>/` — repo-specific learnings. Check before deep-diving.\n"
        "- `runbooks/` — operational procedures.\n\n"
        "Each doc uses frontmatter (title, type, status, owner, tags). Each subdirectory "
        "has an INDEX.md — scan it before creating new files to avoid duplication.\n\n"
        "When you learn something reusable, write to `knowledge/<repo>/`. When a plan "
        "completes, move from `active/` to `completed/`."
    )


class TestNonVaultRawTree:
    def test_raw_tree_block_is_byte_identical(self, tmp_path):
        paths = _setup(tmp_path)
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        md = compose_claude_md(bot, fleet, paths)
        assert _expected_raw_tree_block(str(paths.shared_docs)) in md

    def test_no_door_language(self, tmp_path):
        paths = _setup(tmp_path)
        bot = BotConfig(bot_id="worker", name="worker", expertise=["eng"])
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        md = compose_claude_md(bot, fleet, paths)
        assert "Navigate for config, query for knowledge" not in md
        assert "CLAUDRON_VAULT_PATH" not in md
        assert "claudron recall" not in md


class TestVaultWiredDoor:
    def _compose(self, tmp_path):
        paths = _setup(tmp_path)
        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            claudron_vault_path="/opt/vaults/acme",
        )
        fleet = FleetConfig(name="t", service_prefix="p", bots={"worker": bot})
        return compose_claude_md(bot, fleet, paths)

    def test_door_named(self, tmp_path):
        md = self._compose(tmp_path)
        assert "## Shared Documentation" in md
        assert "Navigate for config, query for knowledge" in md
        assert "$CLAUDRON_VAULT_PATH" in md
        assert "claudron recall" in md
        assert "claudron capture" in md

    def test_raw_tree_replaced(self, tmp_path):
        md = self._compose(tmp_path)
        # The legacy raw-tree stamp is gone for a vault-wired bot.
        assert "`planning/active/` — in-flight plans" not in md
        assert "ratified ADRs. Follow unless human overrides" not in md

    def test_nesting_aware_cites_contract_not_flat_tiers(self, tmp_path):
        # Forward-compat with the vault-nesting workstream: the door cites the
        # Claudron contract for tier layout instead of enumerating flat tiers.
        md = self._compose(tmp_path)
        assert "VAULT-STRUCTURE.md" in md
        assert "do **not** hardcode tier paths" in md.lower()
