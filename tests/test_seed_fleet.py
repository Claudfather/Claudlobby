"""Tests for the seed fleet (fleet.yaml.seed) and --seed path resolution."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


from claudlobby.config import load_fleet
from claudlobby.paths import Paths


# Repo root — tests run from the repo root or via pytest discovery.
REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_FLEET_YAML = REPO_ROOT / "fleet.yaml.seed"


# ---------------------------------------------------------------------------
# Config-layer tests (parse fleet.yaml.seed directly)
# ---------------------------------------------------------------------------


class TestSeedFleetConfig:
    def test_seed_fleet_loads(self):
        """fleet.yaml.seed parses without error."""
        fleet = load_fleet(SEED_FLEET_YAML)
        assert fleet.name == "seed"
        assert fleet.service_prefix == "com.claudlobby.seed"

    def test_seed_fleet_single_bot(self):
        """Exactly one bot named claudfather."""
        fleet = load_fleet(SEED_FLEET_YAML)
        assert list(fleet.bots.keys()) == ["claudfather"]
        assert fleet.bots["claudfather"].name == "Claudfather"

    def test_seed_bot_model_sonnet(self):
        """Claudfather uses sonnet for cost efficiency."""
        fleet = load_fleet(SEED_FLEET_YAML)
        assert fleet.bots["claudfather"].model == "sonnet"

    def test_seed_bot_no_skip_permissions(self):
        """Standard permissions — no dangerously_skip_permissions."""
        fleet = load_fleet(SEED_FLEET_YAML)
        assert fleet.bots["claudfather"].dangerously_skip_permissions is False

    def test_seed_bot_expertise(self):
        fleet = load_fleet(SEED_FLEET_YAML)
        assert "setup-assistant" in fleet.bots["claudfather"].expertise

    def test_seed_bot_skills(self):
        fleet = load_fleet(SEED_FLEET_YAML)
        skills = fleet.bots["claudfather"].skills
        assert "bootstrap" in skills
        assert "doctor" in skills
        assert "fleet-status" in skills

    def test_seed_bot_telegram(self):
        fleet = load_fleet(SEED_FLEET_YAML)
        tg = fleet.bots["claudfather"].telegram
        assert tg.handle == "REPLACE_ME"
        assert tg.token_env == "TELEGRAM_TOKEN_CLAUDFATHER"
        assert tg.require_mention is False

    def test_seed_bot_scope(self):
        fleet = load_fleet(SEED_FLEET_YAML)
        scope = fleet.bots["claudfather"].scope
        assert scope is not None
        assert scope.org == "Claudfather"
        assert "Claudlobby" in scope.repos

    def test_seed_fleet_no_teams(self):
        """Seed fleet has no teams — single-bot, no dispatch hierarchy."""
        fleet = load_fleet(SEED_FLEET_YAML)
        assert len(fleet.teams) == 0


# ---------------------------------------------------------------------------
# Path-resolution tests
# ---------------------------------------------------------------------------


class TestSeedPaths:
    def test_seed_paths_resolution(self):
        """--seed flag resolves correct paths."""
        paths = Paths(root=REPO_ROOT, seed=True)
        assert paths.fleet_yaml == REPO_ROOT / "fleet.yaml.seed"
        assert paths.runtime == REPO_ROOT / "runtime" / "seed"
        assert paths.runtime_bots == REPO_ROOT / "runtime" / "seed" / "bots"
        assert paths.fleet_dir is None

    def test_seed_no_overlay(self):
        """Seed mode uses base library only — no overlay."""
        paths = Paths(root=REPO_ROOT, seed=True)
        assert paths.overlay_library is None
        assert paths.overlay_voices is None

    def test_seed_env_file_at_root(self):
        """Seed fleet uses the repo-root .env."""
        paths = Paths(root=REPO_ROOT, seed=True)
        assert paths.env_file == REPO_ROOT / ".env"

    def test_seed_shared_docs_none(self):
        """Seed fleet has no shared docs."""
        paths = Paths(root=REPO_ROOT, seed=True)
        assert paths.shared_docs is None

    def test_seed_bot_runtime_path(self):
        paths = Paths(root=REPO_ROOT, seed=True)
        assert paths.bot_runtime("claudfather") == (
            REPO_ROOT / "runtime" / "seed" / "bots" / "claudfather"
        )


# ---------------------------------------------------------------------------
# Validation test (needs library stubs)
# ---------------------------------------------------------------------------


def _setup_seed_tree(tmp_path: Path) -> Path:
    """Create a minimal seed fleet directory tree with library stubs."""
    root = tmp_path / "claudlobby"
    root.mkdir()

    # Copy real fleet.yaml.seed
    shutil.copy2(SEED_FLEET_YAML, root / "fleet.yaml.seed")

    # Minimal library stubs for validation
    for kind in (
        "expertise",
        "guardrails",
        "protocols",
        "integrations",
        "mcp",
        "skills",
        "resources",
        "lessons",
    ):
        (root / "library" / kind).mkdir(parents=True)

    # Expertise stub
    (root / "library" / "expertise" / "setup-assistant.md").write_text(
        "---\ntitle: Setup Assistant\ndescription: Fleet bootstrap\n"
        "title_label: Setup Assistant\n---\n\n"
        "# Setup Assistant\n\nYou are the fleet setup assistant.\n"
    )

    # Guardrail stubs
    for g in ("no-push-main", "no-destructive-git", "pii-protection", "no-fabrication"):
        (root / "library" / "guardrails" / f"{g}.md").write_text(
            f"---\ntitle: {g}\ndescription: Guardrail\n---\n\n# {g}\n\nGuardrail.\n"
        )

    # Protocol stubs
    for p in ("context-management", "telegram-routing", "telegram-formatting"):
        (root / "library" / "protocols" / f"{p}.md").write_text(
            f"---\ntitle: {p}\ndescription: Protocol\n---\n\n# {p}\n\nProtocol.\n"
        )

    # Skill stubs
    for s in ("bootstrap", "doctor", "fleet-status"):
        skill_dir = root / "library" / "skills" / s
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\ntitle: {s}\ndescription: Skill\n---\n\n# {s}\n\nSkill.\n"
        )

    # MCP fragment
    mcp_frag = {
        "github": {"command": "gh", "args": ["mcp"]},
        "_env_contract": {
            "GITHUB_PAT": {"description": "GitHub PAT", "tier": "fleet"},
        },
    }
    (root / "library" / "mcp" / "github.json").write_text(json.dumps(mcp_frag))

    # Template (minimal for compose)
    (root / "templates").mkdir()
    (root / "templates" / "claude.md.j2").write_text(
        "# {{ bot.name }}"
        "{% if title_label %} — {{ title_label }}{% endif %}\n\n"
        "{{ expertise_body }}\n"
    )

    # Voices dir
    (root / "voices").mkdir()

    # Runtime seed dir
    (root / "runtime" / "seed" / "bots").mkdir(parents=True)

    # lib/ directory (for Paths.detect() marker)
    (root / "lib").mkdir()

    return root


class TestSeedFleetValidation:
    def test_seed_fleet_validates(self, tmp_path):
        """Seed fleet passes validate with no errors."""
        from claudlobby.validator import validate

        root = _setup_seed_tree(tmp_path)
        paths = Paths(root=root, seed=True)
        fleet = load_fleet(paths.fleet_yaml)
        report = validate(fleet, paths)
        assert not report.has_errors, f"Validation errors: {report.errors}"


# ---------------------------------------------------------------------------
# Composition test
# ---------------------------------------------------------------------------


class TestComposeSeedBot:
    def test_compose_seed_bot(self, tmp_path):
        """compose_bot produces a CLAUDE.md for claudfather."""
        from claudlobby.composer import compose_bot

        root = _setup_seed_tree(tmp_path)
        paths = Paths(root=root, seed=True)
        fleet = load_fleet(paths.fleet_yaml)
        bot = fleet.bots["claudfather"]

        bot_dir = compose_bot(bot, fleet, paths)

        assert bot_dir.is_dir()
        claude_md = bot_dir / "CLAUDE.md"
        assert claude_md.is_file()
        content = claude_md.read_text()
        assert "Claudfather" in content
        assert "Setup Assistant" in content

    def test_compose_seed_bot_conf(self, tmp_path):
        """compose_bot produces bot.conf without --dangerously-skip-permissions."""
        from claudlobby.composer import compose_bot

        root = _setup_seed_tree(tmp_path)
        paths = Paths(root=root, seed=True)
        fleet = load_fleet(paths.fleet_yaml)
        bot = fleet.bots["claudfather"]

        compose_bot(bot, fleet, paths)

        bot_conf = paths.bot_runtime("claudfather") / "bot.conf"
        assert bot_conf.is_file()
        conf_text = bot_conf.read_text()
        assert "--dangerously-skip-permissions" not in conf_text
        assert "sonnet" in conf_text
