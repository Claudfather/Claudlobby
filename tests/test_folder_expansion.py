"""Tests for Paths.expand_library_folder and Paths.expand_skill_folder."""

from __future__ import annotations

from pathlib import Path

import pytest

from claudlobby.paths import Paths


@pytest.fixture
def base_tree(tmp_path: Path) -> Paths:
    """Root-mode Paths with a populated library/."""
    (tmp_path / "library").mkdir()
    (tmp_path / "lib").mkdir()

    # guardrails/security/  with two .md files and a README
    sec = tmp_path / "library" / "guardrails" / "security"
    sec.mkdir(parents=True)
    (sec / "no-push-main.md").write_text("---\ntitle: No push main\n---\nbody\n")
    (sec / "no-force-push.md").write_text("---\ntitle: No force push\n---\nbody\n")
    (sec / "README.md").write_text("# Security guardrails\n")

    # skills/claudna/  with two skill dirs and a non-skill dir
    claudna = tmp_path / "library" / "skills" / "claudna"
    claudna.mkdir(parents=True)
    (claudna / "commit").mkdir()
    (claudna / "commit" / "SKILL.md").write_text("commit skill\n")
    (claudna / "review").mkdir()
    (claudna / "review" / "SKILL.md").write_text("review skill\n")
    (claudna / "not-a-skill").mkdir()
    (claudna / "not-a-skill" / "notes.txt").write_text("no SKILL.md here\n")

    return Paths(root=tmp_path)


@pytest.fixture
def overlay_tree(tmp_path: Path) -> Paths:
    """Overlay-mode Paths where overlay overrides base."""
    (tmp_path / "library" / "guardrails" / "security").mkdir(parents=True)
    (tmp_path / "lib").mkdir()

    # Base: one guardrail
    base_sec = tmp_path / "library" / "guardrails" / "security"
    (base_sec / "no-push-main.md").write_text("base version\n")
    (base_sec / "base-only.md").write_text("base only\n")

    # Overlay: overrides no-push-main, adds overlay-only
    fleet = tmp_path / "local" / "myfleet"
    overlay_sec = fleet / "library" / "guardrails" / "security"
    overlay_sec.mkdir(parents=True)
    (overlay_sec / "no-push-main.md").write_text("overlay version\n")
    (overlay_sec / "overlay-only.md").write_text("overlay only\n")

    # Skills: overlay adds a skill, base has one
    base_skills = tmp_path / "library" / "skills" / "ops"
    base_skills.mkdir(parents=True)
    (base_skills / "deploy").mkdir()
    (base_skills / "deploy" / "SKILL.md").write_text("base deploy\n")

    overlay_skills = fleet / "library" / "skills" / "ops"
    overlay_skills.mkdir(parents=True)
    (overlay_skills / "deploy").mkdir()
    (overlay_skills / "deploy" / "SKILL.md").write_text("overlay deploy\n")
    (overlay_skills / "rollback").mkdir()
    (overlay_skills / "rollback" / "SKILL.md").write_text("overlay rollback\n")

    (fleet / "fleet.yaml").write_text("fleet:\n  name: myfleet\n  bots: {}\n")
    return Paths(root=tmp_path, fleet_dir=fleet)


class TestExpandLibraryFolder:
    def test_collects_md_files(self, base_tree: Paths):
        result = base_tree.expand_library_folder("guardrails", "security")
        assert set(result.keys()) == {"no-push-main", "no-force-push"}

    def test_skips_readme(self, base_tree: Paths):
        result = base_tree.expand_library_folder("guardrails", "security")
        assert not any("readme" in k.lower() for k in result)

    def test_sorted_output(self, base_tree: Paths):
        result = base_tree.expand_library_folder("guardrails", "security")
        assert list(result.keys()) == sorted(result.keys())

    def test_empty_dir_returns_empty(self, base_tree: Paths):
        result = base_tree.expand_library_folder("guardrails", "nonexistent")
        assert result == {}

    def test_overlay_wins(self, overlay_tree: Paths):
        result = overlay_tree.expand_library_folder("guardrails", "security")
        # overlay-only + base-only + overlay version of no-push-main
        assert set(result.keys()) == {"no-push-main", "base-only", "overlay-only"}
        # overlay version wins for the shared key
        assert "overlay version" in result["no-push-main"].read_text()


class TestExpandSkillFolder:
    def test_collects_skill_dirs(self, base_tree: Paths):
        result = base_tree.expand_skill_folder("claudna")
        assert set(result.keys()) == {"commit", "review"}

    def test_skips_non_skill_dirs(self, base_tree: Paths):
        result = base_tree.expand_skill_folder("claudna")
        assert "not-a-skill" not in result

    def test_sorted_output(self, base_tree: Paths):
        result = base_tree.expand_skill_folder("claudna")
        assert list(result.keys()) == sorted(result.keys())

    def test_empty_dir_returns_empty(self, base_tree: Paths):
        result = base_tree.expand_skill_folder("nonexistent")
        assert result == {}

    def test_overlay_wins(self, overlay_tree: Paths):
        result = overlay_tree.expand_skill_folder("ops")
        assert set(result.keys()) == {"deploy", "rollback"}
        # overlay version wins for the shared key
        assert "overlay deploy" in (result["deploy"] / "SKILL.md").read_text()
