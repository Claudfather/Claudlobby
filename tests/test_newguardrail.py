"""Tests for claudlobby/newguardrail.py — guardrail scaffolding."""

from __future__ import annotations

from claudlobby.newguardrail import render_guardrail, interactive_collect


class TestRenderGuardrail:
    def test_basic(self):
        content = render_guardrail(
            "no-yolo", "No YOLO deploys", "Never deploy untested code"
        )
        assert "title: No YOLO deploys" in content
        assert "description: Never deploy untested code" in content
        assert "# No YOLO deploys" in content

    def test_frontmatter_structure(self):
        content = render_guardrail("test", "Test Rule", "A test")
        lines = content.split("\n")
        assert lines[0] == "---"
        closing = lines.index("---", 1)
        assert closing > 0


class TestInteractiveCollect:
    def test_prompts_for_fields(self, monkeypatch):
        answers = iter(
            ["no-push-prod", "Never push to prod", "Direct prod pushes are forbidden"]
        )
        monkeypatch.setattr("builtins.input", lambda _: next(answers))
        name, title, desc = interactive_collect()
        assert name == "no-push-prod"
        assert title == "Never push to prod"
        assert desc == "Direct prod pushes are forbidden"


class TestCmdNewGuardrail:
    def test_creates_guardrail_file(self, tmp_path):
        from claudlobby.__main__ import main

        (tmp_path / "library" / "guardrails").mkdir(parents=True)
        (tmp_path / "lib").mkdir()
        rc = main(
            [
                "--root",
                str(tmp_path),
                "new-guardrail",
                "--name",
                "no-yolo",
                "--title",
                "No YOLO deploys",
                "--description",
                "Never deploy untested",
            ]
        )
        assert rc == 0
        guardrail = tmp_path / "library" / "guardrails" / "no-yolo.md"
        assert guardrail.is_file()
        content = guardrail.read_text()
        assert "title: No YOLO deploys" in content
        assert "Never deploy untested" in content

    def test_rejects_duplicate(self, tmp_path):
        from claudlobby.__main__ import main

        (tmp_path / "library" / "guardrails").mkdir(parents=True)
        (tmp_path / "library" / "guardrails" / "existing.md").write_text("x")
        (tmp_path / "lib").mkdir()
        rc = main(
            [
                "--root",
                str(tmp_path),
                "new-guardrail",
                "--name",
                "existing",
                "--description",
                "Already there",
            ]
        )
        assert rc == 1

    def test_auto_generates_title(self, tmp_path):
        from claudlobby.__main__ import main

        (tmp_path / "library" / "guardrails").mkdir(parents=True)
        (tmp_path / "lib").mkdir()
        rc = main(
            [
                "--root",
                str(tmp_path),
                "new-guardrail",
                "--name",
                "no-force-push",
                "--description",
                "Prevents force pushes",
            ]
        )
        assert rc == 0
        content = (tmp_path / "library" / "guardrails" / "no-force-push.md").read_text()
        assert "title: No Force Push" in content
