"""Tests for claudlobby/newskill.py — skill scaffolding and CLI integration."""

from __future__ import annotations

from claudlobby.newskill import render_skill, interactive_collect


class TestRenderSkill:
    def test_minimal(self):
        content = render_skill("my-skill", "Does something useful", None)
        assert "name: my-skill" in content
        assert 'description: "Does something useful"' in content
        assert "# My Skill" in content
        assert "argument-hint" not in content

    def test_with_argument_hint(self):
        content = render_skill("deploy", "Deploy a service", "<env> [--force]")
        assert 'argument-hint: "<env> [--force]"' in content
        assert "# Deploy" in content
        assert "/deploy" in content

    def test_frontmatter_structure(self):
        content = render_skill("test", "Test skill", None)
        lines = content.split("\n")
        assert lines[0] == "---"
        # Find closing ---
        closing = lines.index("---", 1)
        assert closing > 0


class TestInteractiveCollect:
    def test_prompts_for_fields(self, monkeypatch):
        answers = iter(["my-skill", "Does a thing", ""])
        monkeypatch.setattr("builtins.input", lambda _: next(answers))
        name, desc, hint = interactive_collect()
        assert name == "my-skill"
        assert desc == "Does a thing"
        assert hint is None

    def test_with_argument_hint(self, monkeypatch):
        answers = iter(["deploy", "Deploy stuff", "<env>"])
        monkeypatch.setattr("builtins.input", lambda _: next(answers))
        name, desc, hint = interactive_collect()
        assert name == "deploy"
        assert hint == "<env>"


class TestCmdNewSkill:
    def test_creates_skill_dir(self, tmp_path):
        from claudlobby.__main__ import main

        (tmp_path / "library" / "skills").mkdir(parents=True)
        (tmp_path / "lib").mkdir()
        rc = main(
            [
                "--root",
                str(tmp_path),
                "new-skill",
                "--name",
                "my-tool",
                "--description",
                "A useful tool",
            ]
        )
        assert rc == 0
        skill_md = tmp_path / "library" / "skills" / "my-tool" / "SKILL.md"
        assert skill_md.is_file()
        content = skill_md.read_text()
        assert "name: my-tool" in content
        assert "A useful tool" in content

    def test_rejects_duplicate(self, tmp_path):
        from claudlobby.__main__ import main

        (tmp_path / "library" / "skills" / "existing").mkdir(parents=True)
        (tmp_path / "lib").mkdir()
        rc = main(
            [
                "--root",
                str(tmp_path),
                "new-skill",
                "--name",
                "existing",
                "--description",
                "Already there",
            ]
        )
        assert rc == 1

    def test_requires_description(self, tmp_path):
        from claudlobby.__main__ import main

        (tmp_path / "library" / "skills").mkdir(parents=True)
        (tmp_path / "lib").mkdir()
        rc = main(["--root", str(tmp_path), "new-skill", "--name", "no-desc"])
        assert rc == 1
