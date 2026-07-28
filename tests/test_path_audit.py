"""Tests for path_audit.py — the compositor path-ownership guard.

Extends the fresh-box self-containment contract to cover PATHS: no emitted wiring
file may carry a flat/dangling/cross-fleet absolute path. A path written against a
composer anchor (FLEET_ROOT / BOT_DIR / CLAUDLOBBY_ROOT) resolves to the fleet's
real nested location and passes; a hand-typed flat husk fails.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from claudlobby.config import BotConfig, FleetConfig, TelegramConfig
from claudlobby.path_audit import (
    COMPOSER_PROVIDED_PATH_ANCHORS,
    PathFinding,
    assert_bot_paths,
    audit_bot_paths,
    improper_fleet_paths,
)
from claudlobby.paths import Paths


def _paths(tmp_path):
    root = tmp_path / "claudlobby"
    fleet_dir = root / "local" / "home" / "tl"
    (fleet_dir / "runtime" / "bots" / "kev").mkdir(parents=True)
    (root / "lib").mkdir(parents=True)
    return Paths(root=root, fleet_dir=fleet_dir)


def _bot(**overrides):
    base = dict(
        bot_id="kev",
        name="kev",
        expertise=["eng"],
        telegram=TelegramConfig(handle="kev_bot"),
    )
    base.update(overrides)
    return BotConfig(**base)


def _fleet():
    return FleetConfig(name="tl", service_prefix="com.crog.tl")


class TestImproperFleetPaths:
    def test_flat_husk_is_flagged(self, tmp_path):
        paths = _paths(tmp_path)
        flat = f"{paths.root}/local/tl/.secrets/ga4.json"  # local/tl not local/home/tl
        bad = improper_fleet_paths(f'"{flat}"', _bot(), paths)
        assert [p for p, _ in bad] == [flat]

    def test_nested_correct_absolute_is_ok(self, tmp_path):
        paths = _paths(tmp_path)
        good = f"{paths.root}/local/home/tl/.secrets/ga4.json"
        assert improper_fleet_paths(good, _bot(), paths) == []

    def test_fleet_root_token_is_ok(self, tmp_path):
        paths = _paths(tmp_path)
        txt = "${FLEET_ROOT}/runtime/bots/kev/data/printify-mcp/dist/index.js"
        assert improper_fleet_paths(txt, _bot(), paths) == []

    def test_claudlobby_root_anchored_bot_dir_is_ok(self, tmp_path):
        paths = _paths(tmp_path)
        txt = 'BOT_DIR="$CLAUDLOBBY_ROOT/local/home/tl/runtime/bots/kev"'
        assert improper_fleet_paths(txt, _bot(), paths) == []

    def test_bot_dir_token_is_ok(self, tmp_path):
        paths = _paths(tmp_path)
        txt = "${BOT_DIR}/data/printify-mcp/dist/index.js"
        assert improper_fleet_paths(txt, _bot(), paths) == []

    def test_system_and_home_paths_ignored(self, tmp_path):
        paths = _paths(tmp_path)
        txt = "/usr/bin/node --flag /tmp/scratch $HOME/.claude/settings.json"
        assert improper_fleet_paths(txt, _bot(), paths) == []

    def test_cross_fleet_path_flagged(self, tmp_path):
        paths = _paths(tmp_path)
        other = f"{paths.root}/local/home/other-fleet/runtime/bots/z/x.js"
        bad = improper_fleet_paths(other, _bot(), paths)
        assert [p for p, _ in bad] == [other]

    def test_fleet_state_path_style_root_anchor_is_ok(self, tmp_path):
        # $CLAUDLOBBY_ROOT/state/... resolves outside local/ entirely — never flagged.
        paths = _paths(tmp_path)
        txt = 'export FLEET_STATE_PATH="$CLAUDLOBBY_ROOT/state/fleet-state.json"'
        assert improper_fleet_paths(txt, _bot(), paths) == []

    def test_foreign_host_flat_absolute_is_flagged(self, tmp_path):
        # A stale absolute copied from a prior host: under neither this install's
        # local/ nor a resolving vault path, yet it traverses this fleet's own
        # flat overlay layout (local/<fleet>/…) — a dangling fleet path, not a
        # system path. This is the seam-1 gap (PR #690 review).
        paths = _paths(tmp_path)
        foreign = (
            "/Users/olduser/old-mac-mini-install/claudlobby/local/tl/.secrets/ga4.json"
        )
        bad = improper_fleet_paths(f'"{foreign}"', _bot(), paths)
        assert [p for p, _ in bad] == [foreign]

    def test_foreign_host_nested_absolute_is_flagged(self, tmp_path):
        # Same, but the stale path carries the nested overlay layout at a foreign root.
        paths = _paths(tmp_path)
        foreign = (
            "/Users/olduser/old-install/claudlobby/local/home/tl/.secrets/ga4.json"
        )
        bad = improper_fleet_paths(foreign, _bot(), paths)
        assert [p for p, _ in bad] == [foreign]

    def test_foreign_bot_runtime_absolute_is_flagged(self, tmp_path):
        # A bot-runtime tree (runtime/bots/…) anchored at a foreign root is a
        # transplanted fleet path — recognized by the runtime-tree layout marker.
        paths = _paths(tmp_path)
        foreign = "/mnt/old/claudlobby/local/home/tl/runtime/bots/kev/data/x.js"
        bad = improper_fleet_paths(foreign, _bot(), paths)
        assert [p for p, _ in bad] == [foreign]

    # --- false-positive locks: real absolutes that live in emitted wiring today ---
    # (bot.conf / .mcp.json / unit files). The broadened recognizer must leave
    # every one of these clean, or it breaks legit generates.

    def test_usr_local_bin_is_not_flagged(self, tmp_path):
        # /usr/local/... contains a `local` segment but is not a fleet overlay.
        paths = _paths(tmp_path)
        txt = "/usr/local/bin/node /usr/local/lib/thing"
        assert improper_fleet_paths(txt, _bot(), paths) == []

    def test_npm_scope_fragment_in_mcp_json_is_not_flagged(self, tmp_path):
        # `@notionhq/notion-mcp-server@2.2.1` — the abs-token regex extracts
        # `/notion-mcp-server@2.2.1`; it is not a filesystem path at all.
        paths = _paths(tmp_path)
        txt = '"args": ["-y", "@notionhq/notion-mcp-server@2.2.1", "@org/server-github@2025.4.8"]'
        assert improper_fleet_paths(txt, _bot(), paths) == []

    def test_cross_platform_homebrew_path_is_not_flagged(self, tmp_path):
        # A launchd plist emitted on Linux carries a macOS PATH whose entries
        # (/opt/homebrew/...) do not resolve on the generating host — legit, must pass.
        paths = _paths(tmp_path)
        txt = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin"
        assert improper_fleet_paths(txt, _bot(), paths) == []

    def test_plist_xml_fragments_are_not_flagged(self, tmp_path):
        # plist is XML; the abs-token regex extracts `/array>`, `/dict>`,
        # `/www.apple.com/DTDs/PropertyList-1.0.dtd` from closing tags / the DTD URL.
        paths = _paths(tmp_path)
        txt = "</array></dict> http://www.apple.com/DTDs/PropertyList-1.0.dtd </plist>"
        assert improper_fleet_paths(txt, _bot(), paths) == []


class TestAuditBotPaths:
    def _seed_bot_dir(self, paths):
        bot_dir = paths.bot_runtime("kev")
        (bot_dir / ".claude").mkdir(parents=True, exist_ok=True)
        return bot_dir

    def test_clean_bot_dir_no_findings(self, tmp_path):
        paths = _paths(tmp_path)
        bot_dir = self._seed_bot_dir(paths)
        (bot_dir / "bot.conf").write_text(
            'export FLEET_ROOT="$CLAUDLOBBY_ROOT/local/home/tl"\n'
            f"export FLEET_MISSION_FILE={paths.root}/local/home/tl/missions/f.md\n"
        )
        (bot_dir / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "printify": {
                            "args": [
                                "${FLEET_ROOT}/runtime/bots/kev/data/printify-mcp/dist/index.js"
                            ]
                        }
                    }
                }
            )
        )
        assert audit_bot_paths(_bot(), _fleet(), paths) == []
        assert_bot_paths(_bot(), _fleet(), paths)  # does not raise

    def test_flat_path_in_mcp_json_is_a_finding_and_raises(self, tmp_path):
        paths = _paths(tmp_path)
        bot_dir = self._seed_bot_dir(paths)
        flat = f"{paths.root}/local/tl/runtime/bots/kev/dist/index.js"  # flat husk
        (bot_dir / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"printify": {"args": [flat]}}})
        )
        findings = audit_bot_paths(_bot(), _fleet(), paths)
        assert len(findings) == 1
        assert isinstance(findings[0], PathFinding)
        assert findings[0].file == ".mcp.json"
        assert findings[0].path == flat
        with pytest.raises(ValueError, match="improper absolute fleet path"):
            assert_bot_paths(_bot(), _fleet(), paths)

    def test_flat_path_in_bot_conf_is_flagged(self, tmp_path):
        paths = _paths(tmp_path)
        bot_dir = self._seed_bot_dir(paths)
        (bot_dir / "bot.conf").write_text(
            f"export GA4_SA_KEY_PATH={paths.root}/local/tl/.secrets/ga4.json\n"
        )
        findings = audit_bot_paths(_bot(), _fleet(), paths)
        assert [f.file for f in findings] == ["bot.conf"]

    def test_missing_wiring_files_are_skipped(self, tmp_path):
        # A bot with no .mcp.json / units on disk audits clean, not crashing.
        paths = _paths(tmp_path)
        self._seed_bot_dir(paths)
        assert audit_bot_paths(_bot(), _fleet(), paths) == []


def test_anchor_ssot_is_the_three_blessed_anchors():
    assert set(COMPOSER_PROVIDED_PATH_ANCHORS) == {
        "CLAUDLOBBY_ROOT",
        "FLEET_ROOT",
        "BOT_DIR",
    }


def _write_nested_fleet(root, env_path_value):
    """A nested-overlay fleet (local/home/tl) reusing the fleet_dir fixture's
    root-level library, with one bot carrying a hand-typed env path."""
    fleet_dir = root / "local" / "home" / "tl"
    fleet_dir.mkdir(parents=True)
    (fleet_dir / "fleet.yaml").write_text(
        "fleet:\n"
        "  name: tl\n"
        "  service_prefix: com.crog.tl\n"
        '  telegram_group_chat_id: "-100999"\n'
        "  accounts:\n"
        "    default: ~/.claude\n"
        "  bots:\n"
        "    kev:\n"
        "      expertise: [software-engineering]\n"
        "      env:\n"
        f'        DANGLING_PATH: "{env_path_value}"\n'
        "      telegram:\n"
        "        handle: kev_bot\n"
        "        token_env: T\n"
    )
    return fleet_dir


class TestComposeBotFiresPathGuard:
    """End-to-end: compose_bot invokes a path guard, so a hand-typed fleet path in
    the composed wiring fails generate loudly.

    Since #702, the L1 source guard fronts compose_bot (before any write), so a
    raw absolute in a *source* value (here bot.env) is denied at the source —
    stricter than L2, which alone accepted a nested-correct raw absolute. L2's own
    emitted-path coverage (a nested-correct absolute is fine, a flat/foreign husk
    is flagged) lives untouched in TestImproperFleetPaths / TestAuditBotPaths;
    here the in-fleet path must be *anchored* to compose clean."""

    def test_flat_env_path_makes_compose_bot_raise(self, fleet_dir):
        from claudlobby.composer import compose_bot
        from claudlobby.config import load_fleet

        root = fleet_dir
        flat = f"{root}/local/tl/data/x"  # flat husk: local/tl not local/home/tl
        nested = _write_nested_fleet(root, flat)
        paths = Paths(root=root, fleet_dir=nested)
        fleet, _ = load_fleet(nested / "fleet.yaml")
        # L1 denies the unanchored absolute at the source, before emission.
        with pytest.raises(ValueError, match="absolute path"):
            compose_bot(fleet.bots["kev"], fleet, paths)

    def test_anchored_env_path_composes_clean(self, fleet_dir):
        from claudlobby.composer import compose_bot
        from claudlobby.config import load_fleet

        root = fleet_dir
        # An in-fleet path expressed against the FLEET_ROOT anchor: L1 passes it,
        # and the emitted (resolved) path resolves in-fleet so L2 passes too.
        good = "${FLEET_ROOT}/data/x"
        nested = _write_nested_fleet(root, good)
        paths = Paths(root=root, fleet_dir=nested)
        fleet, _ = load_fleet(nested / "fleet.yaml")
        bot_dir = compose_bot(fleet.bots["kev"], fleet, paths)
        assert bot_dir.is_dir()

    def test_foreign_host_env_path_makes_compose_bot_raise(self, fleet_dir):
        # rajan's seam-1 repro at the compose seam: a fully-external hand-typed
        # absolute (stale prior-host path, under neither this install's local/
        # nor a resolving vault path) must fail generate loud, not bake in silent.
        from claudlobby.composer import compose_bot
        from claudlobby.config import load_fleet

        root = fleet_dir
        foreign = (
            "/Users/olduser/old-mac-mini-install/claudlobby/local/tl/.secrets/ga4.json"
        )
        nested = _write_nested_fleet(root, foreign)
        paths = Paths(root=root, fleet_dir=nested)
        fleet, _ = load_fleet(nested / "fleet.yaml")
        with pytest.raises(ValueError, match="absolute path"):
            compose_bot(fleet.bots["kev"], fleet, paths)

    @pytest.mark.parametrize("bridged", [True, False], ids=["bridge", "bridge-less"])
    def test_vault_wired_bot_composes_clean(self, fleet_dir, bridged):
        # A bot pointing CLAUDRON_VAULT_PATH at its own vault root composes clean:
        # the composer emits the absolute vault-root literal, and L2 must treat the
        # fleet's own vault root as a sanctioned reach, not a dangling/foreign husk.
        # Bridge-less (never ran `claudron plug`): paths.vault_root is None and the
        # bot's own claudron_vault_path declaration is the only provenance — the
        # composer's own first-class key must not hard-fail generate.
        # (Regression for the crog-eng-team ari generate-failure, both halves.)
        from claudlobby.composer import compose_bot
        from claudlobby.config import load_fleet

        root = fleet_dir
        vault_root = root / "local"  # the install's local/ overlay IS the vault root
        nested = root / "local" / "home" / "tl"
        nested.mkdir(parents=True)
        (nested / "fleet.yaml").write_text(
            "fleet:\n"
            "  name: tl\n"
            "  service_prefix: com.crog.tl\n"
            '  telegram_group_chat_id: "-100999"\n'
            "  accounts:\n"
            "    default: ~/.claude\n"
            "  bots:\n"
            "    kev:\n"
            "      expertise: [software-engineering]\n"
            f"      claudron_vault_path: {vault_root}\n"
            "      telegram:\n"
            "        handle: kev_bot\n"
            "        token_env: T\n"
        )
        paths = Paths(
            root=root, fleet_dir=nested, vault_root=vault_root if bridged else None
        )
        fleet, _ = load_fleet(nested / "fleet.yaml")
        bot_dir = compose_bot(fleet.bots["kev"], fleet, paths)
        assert bot_dir.is_dir()
        assert "CLAUDRON_VAULT_PATH=" in (bot_dir / "bot.conf").read_text()


class TestVaultModePathAudit:
    """Vault-mode regression coverage (PR #690 review flagged this branch as
    untested): in vault mode ``_fleet_content_roots`` includes ``vault_root``,
    so a cross-fleet leak inside the vault is flagged while the fleet's own vault
    path passes."""

    def _vault_paths(self, tmp_path):
        root = tmp_path / "claudlobby"
        vault = tmp_path / "vault"
        fleet_dir = vault / "tl"  # fleet lives in the vault, not under local/
        (fleet_dir / "runtime" / "bots" / "kev").mkdir(parents=True)
        (fleet_dir / "fleet.yaml").write_text("fleet:\n  name: tl\n")
        (root / "library").mkdir(parents=True)
        (root / "lib").mkdir(parents=True)
        return Paths(root=root, fleet_dir=fleet_dir, vault_root=vault)

    def test_own_vault_fleet_path_is_ok(self, tmp_path):
        paths = self._vault_paths(tmp_path)
        good = f"{paths.vault_root}/tl/.secrets/ga4.json"
        assert improper_fleet_paths(good, _bot(), paths) == []

    def test_cross_fleet_leak_in_vault_is_flagged(self, tmp_path):
        paths = self._vault_paths(tmp_path)
        leak = f"{paths.vault_root}/other-fleet/.secrets/ga4.json"
        bad = improper_fleet_paths(leak, _bot(), paths)
        assert [p for p, _ in bad] == [leak]

    def test_vault_root_itself_is_ok(self, tmp_path):
        # The fleet's OWN vault root — exactly what CLAUDRON_VAULT_PATH points at —
        # is the sanctioned shared parent the fleet belongs to, not a cross-fleet
        # leak. L1 already exempts claudron_vault_path (declared-by-construction);
        # L2 must honor the same sanction for the emitted vault-root value. Only
        # the root itself is blessed — a subtree under it stays a leak, per
        # test_cross_fleet_leak_in_vault_is_flagged.
        paths = self._vault_paths(tmp_path)
        assert improper_fleet_paths(str(paths.vault_root), _bot(), paths) == []


class TestDeclaredVaultPathAudit:
    """The per-bot DECLARED vault root (``claudron_vault_path``) is sanctioned the
    same way as the bridge-derived ``paths.vault_root``: L1 already exempts the
    key as declared-by-construction, and a deployment that never ran ``claudron
    plug`` has no bridge to derive a vault root from — the declaration is the
    only provenance there is. Root-only, exactly like the derived root (the
    crog-eng-team ari generate-failure, second half: #804 blessed the derived
    root, but a bridge-less host still failed on the identical declared value)."""

    def test_declared_vault_root_without_bridge_is_ok(self, tmp_path):
        # No .claudron bridge (vault_root=None): the declared root must still pass.
        paths = _paths(tmp_path)
        declared = paths.root / "local"  # parent of the fleet overlay — the ari shape
        txt = f"export CLAUDRON_VAULT_PATH={declared}"
        bot = _bot(claudron_vault_path=str(declared))
        assert improper_fleet_paths(txt, bot, paths) == []

    def test_declared_vault_root_needs_the_declaration(self, tmp_path):
        # The same value on a bot that does NOT declare it stays denied —
        # provenance is the declaration, not the string.
        paths = _paths(tmp_path)
        undeclared = f"{paths.root}/local"
        bad = improper_fleet_paths(
            f"export CLAUDRON_VAULT_PATH={undeclared}", _bot(), paths
        )
        assert [p for p, _ in bad] == [undeclared]

    def test_subtree_of_declared_vault_stays_flagged(self, tmp_path):
        # Root-only, same stance as the derived root: a subtree is still a leak.
        paths = _paths(tmp_path)
        declared = paths.root / "local"
        leak = f"{declared}/other-fleet/.secrets/ga4.json"
        bad = improper_fleet_paths(leak, _bot(claudron_vault_path=str(declared)), paths)
        assert [p for p, _ in bad] == [leak]

    def test_declared_and_derived_may_differ_both_pass(self, tmp_path):
        # A bridge pointing at one vault and a bot declaring another (per-bot
        # vaults are legal — the key is per-bot, the bridge is per-install):
        # the declared root passes on its own provenance.
        paths = dataclasses.replace(_paths(tmp_path), vault_root=tmp_path / "vault")
        declared = paths.root / "local"
        txt = f"export CLAUDRON_VAULT_PATH={declared}"
        bot = _bot(claudron_vault_path=str(declared))
        assert improper_fleet_paths(txt, bot, paths) == []


class TestClassifiedSourcePaths:
    """classified_source_paths — the pre-bless companion to denied_source_paths (the
    externals report's usage lens: which declaration blesses a live source value)."""

    def test_returns_classified_absolutes_with_provenance(self):
        from claudlobby.path_audit import classified_source_paths

        bot = BotConfig(
            bot_id="kev",
            name="kev",
            expertise=["eng"],
            env={"A": "/opt/x", "OK": "${FLEET_ROOT}/y", "TOKEN": "${GH}"},
        )
        pairs = classified_source_paths(bot)
        by_path = {p: prov for prov, p in pairs}
        assert "/opt/x" in by_path  # raw absolute classified
        assert "${FLEET_ROOT}/y" not in by_path  # anchored value is not a path
        assert not any(p.startswith("${GH}") for p in by_path)  # plain ${VAR} passes
        assert by_path["/opt/x"] == "bots.kev.env.A"

    def test_includes_mcp_fragment_paths(self):
        from claudlobby.path_audit import classified_source_paths

        bot = BotConfig(bot_id="kev", name="kev", expertise=["eng"])
        frags = {"printify": {"command": "node", "args": ["/opt/printify/index.js"]}}
        pairs = classified_source_paths(bot, frags)
        assert any(p == "/opt/printify/index.js" for _prov, p in pairs)

    def test_exempt_fields_are_not_classified(self):
        from claudlobby.path_audit import classified_source_paths

        # mounts are EXEMPT (host targets, resolve/escape-gated elsewhere) — the walk
        # skips them, so they never appear as classified source paths.
        bot = BotConfig(
            bot_id="kev",
            name="kev",
            expertise=["eng"],
            mounts={"data": "/mnt/host/data"},
        )
        assert classified_source_paths(bot) == []


class TestRenderedToolsPathScan:
    """F6 — rendered tools/ scripts fold into the L2 emitted-path scan, so a
    fleet-shaped absolute baked into one fails generate (via assert_bot_paths) and
    surfaces in freshbox, by the SAME shape predicate the wiring files use."""

    def _seed_bot_dir(self, paths):
        bot_dir = paths.bot_runtime("kev")
        (bot_dir / "tools").mkdir(parents=True, exist_ok=True)
        return bot_dir

    def test_flat_path_in_rendered_tool_is_flagged_and_raises(self, tmp_path):
        paths = _paths(tmp_path)
        bot_dir = self._seed_bot_dir(paths)
        flat = f"{paths.root}/local/tl/dist/deploy.sh"  # flat husk
        (bot_dir / "tools" / "deploy.sh").write_text(f"#!/bin/sh\nexec {flat}\n")

        findings = audit_bot_paths(_bot(), _fleet(), paths)
        assert [f.file for f in findings] == ["tools/deploy.sh"]
        assert findings[0].path == flat
        with pytest.raises(ValueError, match="improper absolute fleet path"):
            assert_bot_paths(_bot(), _fleet(), paths)

    def test_legit_tool_script_paths_pass(self, tmp_path):
        paths = _paths(tmp_path)
        bot_dir = self._seed_bot_dir(paths)
        (bot_dir / "tools" / "ok.sh").write_text("#!/usr/bin/env bash\ncat /dev/null\n")
        assert audit_bot_paths(_bot(), _fleet(), paths) == []
