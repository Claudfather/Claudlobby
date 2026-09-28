"""Tests for the seed fleet (fleet.yaml.seed) and --seed path resolution."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path


from claudlobby.config import load_fleet
from tests.package_fixtures import source_package
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
        fleet, _md = load_fleet(SEED_FLEET_YAML)
        assert fleet.name == "seed"
        assert fleet.service_prefix == "com.claudlobby.seed"

    def test_seed_fleet_single_bot(self):
        """Exactly one bot named claudfather."""
        fleet, _md = load_fleet(SEED_FLEET_YAML)
        assert list(fleet.bots.keys()) == ["claudfather"]
        assert fleet.bots["claudfather"].name == "Claudfather"

    def test_seed_bot_model_opus(self):
        """Claudfather uses opus per seed fleet config."""
        fleet, _md = load_fleet(SEED_FLEET_YAML)
        assert fleet.bots["claudfather"].model == "opus"

    def test_seed_bot_no_skip_permissions(self):
        """Standard permissions — no dangerously_skip_permissions."""
        fleet, _md = load_fleet(SEED_FLEET_YAML)
        assert fleet.bots["claudfather"].dangerously_skip_permissions is False

    def test_seed_bot_expertise(self):
        fleet, _md = load_fleet(SEED_FLEET_YAML)
        assert "setup-assistant" in fleet.bots["claudfather"].expertise

    def test_seed_bot_skills(self):
        fleet, _md = load_fleet(SEED_FLEET_YAML)
        skills = fleet.bots["claudfather"].skills
        assert "bootstrap" in skills
        assert "doctor" in skills
        assert "fleet-status" in skills
        assert "add-bot" in skills

    def test_seed_bot_voice(self):
        fleet, _md = load_fleet(SEED_FLEET_YAML)
        assert fleet.bots["claudfather"].voice == "vito-corleone.md"

    def test_seed_bot_telegram(self):
        fleet, _md = load_fleet(SEED_FLEET_YAML)
        tg = fleet.bots["claudfather"].telegram
        assert tg.handle == "REPLACE_ME"
        assert tg.token_env == "TELEGRAM_TOKEN_CLAUDFATHER"
        assert tg.require_mention is False

    def test_seed_bot_scope(self):
        fleet, _md = load_fleet(SEED_FLEET_YAML)
        scope = fleet.bots["claudfather"].scope
        assert scope is not None
        assert scope.org == "Claudfather"
        assert "Claudlobby" in scope.repos

    def test_seed_fleet_no_teams(self):
        """Seed fleet has no teams — single-bot, no dispatch hierarchy."""
        fleet, _md = load_fleet(SEED_FLEET_YAML)
        assert len(fleet.teams) == 0


# ---------------------------------------------------------------------------
# Path-resolution tests
# ---------------------------------------------------------------------------


class TestSeedPaths:
    def test_seed_paths_resolution(self, tmp_path):
        """The package supplies the seed; runtime stays under the data root."""
        paths = Paths(root=tmp_path, seed=True, package=source_package())
        assert paths.fleet_yaml == source_package().seeds / "fleet.yaml.seed"
        assert paths.runtime == tmp_path / "runtime" / "seed"
        assert paths.runtime_bots == tmp_path / "runtime" / "seed" / "bots"
        assert paths.fleet_dir is None

    def test_seed_source_overlay_stays_under_data_root(self, tmp_path):
        """Selecting a seed does not move writable source into the package."""
        paths = Paths(root=tmp_path, seed=True, package=source_package())
        assert paths.overlay_library == tmp_path / "library"
        assert paths.overlay_voices == tmp_path / "voices"
        assert paths.base_library == source_package().library
        assert paths.base_voices == source_package().voices

    def test_seed_env_file_at_root(self, tmp_path):
        """Seed fleet credentials belong to the data root."""
        paths = Paths(root=tmp_path, seed=True, package=source_package())
        assert paths.env_file == tmp_path / ".env"

    def test_seed_shared_docs_none(self, tmp_path):
        """Seed fleet has no shared docs."""
        paths = Paths(root=tmp_path, seed=True, package=source_package())
        assert paths.shared_docs is None

    def test_seed_string_root_coerced_to_path(self, tmp_path):
        """A string data root does not redirect the selected package seed."""
        paths = Paths(root=str(tmp_path), seed=True, package=source_package())
        assert paths.fleet_yaml == source_package().seeds / "fleet.yaml.seed"
        assert paths.root == tmp_path
        assert isinstance(paths.root, Path)

    def test_seed_bot_runtime_path(self, tmp_path):
        paths = Paths(root=tmp_path, seed=True, package=source_package())
        assert paths.bot_runtime("claudfather") == (
            tmp_path / "runtime" / "seed" / "bots" / "claudfather"
        )


# ---------------------------------------------------------------------------
# Validation test (needs library stubs)
# ---------------------------------------------------------------------------


def _setup_seed_tree(tmp_path: Path) -> Paths:
    """Create a private package fixture and a separate writable data root."""
    root = tmp_path / "package"
    (root / "seeds").mkdir(parents=True)

    # Copy real fleet.yaml.seed
    shutil.copy2(SEED_FLEET_YAML, root / "seeds" / "fleet.yaml.seed")

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
    for s in ("bootstrap", "doctor", "fleet-status", "add-bot"):
        skill_dir = root / "library" / "skills" / s
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\ntitle: {s}\ndescription: Skill\n---\n\n# {s}\n\nSkill.\n"
        )

    # Lesson stubs
    tg_lessons_dir = root / "library" / "lessons" / "telegram"
    tg_lessons_dir.mkdir(parents=True)
    for lesson in ("mcp-drops", "plain-text-escape-incident"):
        (tg_lessons_dir / f"{lesson}.md").write_text(
            f"---\ntitle: {lesson}\ndescription: Telegram lesson\n---\n\n"
            f"# {lesson}\n\nLesson content.\n"
        )

    # MCP fragment
    mcp_frag = {
        "github": {"command": "gh", "args": ["mcp"]},
        "_env_contract": {
            # `secret` is required on every entry since #1214 Phase 1.
            "GITHUB_PAT": {
                "description": "GitHub PAT",
                "default_tier": "fleet",
                "secret": True,
            },
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

    # Voices dir + voice stub
    (root / "voices").mkdir()
    (root / "voices" / "vito-corleone.md").write_text(
        "---\nname: Vito Corleone\n---\n\nVoice stub.\n"
    )

    package = replace(
        source_package(), library=root / "library", voices=root / "voices",
        templates=root / "templates", seeds=root / "seeds",
    )
    data_root = tmp_path / "data"
    (data_root / "runtime" / "seed" / "bots").mkdir(parents=True)
    return Paths(root=data_root, seed=True, package=package)


def _fill_in_placeholders(seed: Path) -> None:
    """Replace the three REPLACE_ME values the seed ships with, as /setup does."""
    seed.write_text(
        seed.read_text()
        .replace(
            'telegram_group_chat_id: "REPLACE_ME"',
            'telegram_group_chat_id: "-1001234567890"',
        )
        .replace('human_telegram_id: "REPLACE_ME"', 'human_telegram_id: "1234567890"')
        .replace("handle: REPLACE_ME", "handle: my_claudfather_bot")
    )


class TestSeedFleetValidation:
    def test_shipped_seed_hard_fails_on_unreplaced_placeholders(self, tmp_path):
        """The seed ships three REPLACE_ME values — validate must ERROR on them.

        Regression guard for the cold-start trap: these used to validate clean at
        exit 0, and getting-started tells the user a warnings-only run is a
        success. So copying the seed and skipping the edit produced a bot that
        composed fine, booted fine, and then failed at runtime trying to post to
        chat id REPLACE_ME — a symptom arbitrarily far from its cause.
        """
        from claudlobby.validator import validate

        paths = _setup_seed_tree(tmp_path)
        fleet, _md = load_fleet(paths.fleet_yaml)
        report = validate(fleet, paths)

        assert report.has_errors, "unreplaced placeholders must be a hard error"
        joined = "\n".join(report.errors)
        assert "telegram_group_chat_id" in joined
        assert "human_telegram_id" in joined
        assert "telegram.handle" in joined

    def test_seed_fleet_validates_once_filled_in(self, tmp_path):
        """Seed fleet passes validate with no errors once the placeholders are real."""
        from claudlobby.validator import validate

        paths = _setup_seed_tree(tmp_path)
        _fill_in_placeholders(paths.fleet_yaml)
        fleet, _md = load_fleet(paths.fleet_yaml)
        report = validate(fleet, paths)
        assert not report.has_errors, f"Validation errors: {report.errors}"


# ---------------------------------------------------------------------------
# Composition test
# ---------------------------------------------------------------------------


class TestComposeSeedBot:
    def test_compose_seed_bot(self, tmp_path):
        """compose_bot produces a CLAUDE.md for claudfather."""
        from claudlobby.composer import compose_bot

        paths = _setup_seed_tree(tmp_path)
        fleet, _md = load_fleet(paths.fleet_yaml)
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

        paths = _setup_seed_tree(tmp_path)
        fleet, _md = load_fleet(paths.fleet_yaml)
        bot = fleet.bots["claudfather"]

        compose_bot(bot, fleet, paths)

        bot_conf = paths.bot_runtime("claudfather") / "bot.conf"
        assert bot_conf.is_file()
        conf_text = bot_conf.read_text()
        assert "--dangerously-skip-permissions" not in conf_text
        assert "opus" in conf_text
