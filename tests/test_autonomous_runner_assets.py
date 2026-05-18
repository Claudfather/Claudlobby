"""Sanity tests for the autonomous-runner skill assets.

Schema parsing/validation is covered by test_config.py, test_validator.py, and
test_composer.py (Part A of Phase 4). These tests guard the Part B markdown
assets: file presence, valid SKILL.md frontmatter, and absence of placeholder
strings that would indicate an incomplete edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "library" / "skills" / "autonomous-runner"


@pytest.fixture(scope="module")
def skill_md_text() -> str:
    return (SKILL_DIR / "SKILL.md").read_text()


class TestAssetsExist:
    def test_skill_dir_exists(self):
        assert SKILL_DIR.is_dir()

    def test_skill_md_present(self):
        assert (SKILL_DIR / "SKILL.md").is_file()

    def test_risk_classifier_prompt_present(self):
        path = SKILL_DIR / "risk-classifier-prompt.md"
        assert path.is_file()
        assert path.read_text().strip(), "risk-classifier-prompt.md is empty"

    def test_archetype_present(self):
        path = SKILL_DIR / "archetype.md"
        assert path.is_file()
        assert path.read_text().strip(), "archetype.md is empty"


class TestSkillFrontmatter:
    def test_frontmatter_parseable(self, skill_md_text):
        assert skill_md_text.startswith("---\n"), "SKILL.md missing frontmatter fence"
        _, frontmatter, _ = skill_md_text.split("---", 2)
        parsed = yaml.safe_load(frontmatter)
        assert isinstance(parsed, dict)

    def test_frontmatter_has_name(self, skill_md_text):
        _, frontmatter, _ = skill_md_text.split("---", 2)
        parsed = yaml.safe_load(frontmatter)
        assert parsed.get("name") == "autonomous-runner"

    def test_frontmatter_has_description(self, skill_md_text):
        _, frontmatter, _ = skill_md_text.split("---", 2)
        parsed = yaml.safe_load(frontmatter)
        desc = parsed.get("description")
        assert isinstance(desc, str) and desc.strip()


class TestNoPlaceholders:
    @pytest.mark.parametrize("placeholder", ["<CLAUDNA_PATH>", "TBD", "XXX", "FIXME"])
    def test_skill_md_has_no_placeholder(self, skill_md_text, placeholder):
        assert placeholder not in skill_md_text, (
            f"SKILL.md still contains placeholder {placeholder!r}"
        )

    def test_skill_md_invokes_via_skill_tool(self, skill_md_text):
        assert "/claudna:" in skill_md_text, (
            "SKILL.md should invoke clauDNA skills via /claudna:<name>"
        )
