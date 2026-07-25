"""Tests for loader.py — frontmatter parsing, heading demotion, library loading, expertise, and voice."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent


from claudlobby.loader import (
    _demote_headings,
    _derive_title,
    _parse_expertise_permissions,
    _strip_leading_title_heading,
    integration_tool_grants,
    iter_integration_grants,
    load_library_item,
    load_library_items_overlay,
    load_voice,
    parse_expertise_file,
    parse_frontmatter,
    parse_guardrail_permissions,
)
from claudlobby.paths import Paths


# ── parse_frontmatter ────────────────────────────────────────────────


class TestParseFrontmatter:
    def test_valid_frontmatter(self):
        text = dedent("""\
            ---
            title: My Title
            description: A short description
            ---

            Body text here.
        """)
        fm, body = parse_frontmatter(text)
        assert fm["title"] == "My Title"
        assert fm["description"] == "A short description"
        assert body.strip() == "Body text here."

    def test_no_frontmatter(self):
        text = "Just a plain markdown file.\n\nWith paragraphs."
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_empty_frontmatter(self):
        text = "---\n---\n\nBody."
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert body.strip() == "Body."

    def test_missing_closing_fence(self):
        text = "---\ntitle: Oops\nNo closing fence here."
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_malformed_yaml(self):
        text = "---\n: [invalid yaml\n---\n\nBody."
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_frontmatter_not_dict(self):
        text = "---\n- a list\n- not a dict\n---\n\nBody."
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_frontmatter_with_crlf(self):
        text = "---\r\ntitle: CRLF\r\n---\r\n\r\nBody."
        fm, body = parse_frontmatter(text)
        assert fm["title"] == "CRLF"
        assert "Body." in body

    def test_body_stripped_of_leading_newlines(self):
        text = "---\ntitle: X\n---\n\n\n\nBody."
        fm, body = parse_frontmatter(text)
        assert body == "Body."

    def test_frontmatter_fence_not_at_start(self):
        text = "  ---\ntitle: Indented\n---\n\nBody."
        fm, body = parse_frontmatter(text)
        assert fm == {}  # leading spaces disqualify


# ── _derive_title ────────────────────────────────────────────────────


class TestDeriveTitle:
    def test_kebab_case(self):
        assert _derive_title("multi-angle-orchestration") == "Multi angle orchestration"

    def test_snake_case(self):
        assert _derive_title("report_back") == "Report back"

    def test_single_word(self):
        assert _derive_title("readme") == "Readme"

    def test_empty_stem(self):
        assert _derive_title("") == ""

    def test_all_dashes_passthrough(self):
        # "---" → "   " after replacement, but .strip() makes it empty → returns stem
        assert _derive_title("---") == "---"

    def test_mixed_separators(self):
        assert _derive_title("my-great_protocol") == "My great protocol"


# ── _demote_headings ─────────────────────────────────────────────────


class TestDemoteHeadings:
    def test_h1_becomes_h2(self):
        assert _demote_headings("# Title") == "## Title"

    def test_h2_becomes_h3(self):
        assert _demote_headings("## Section") == "### Section"

    def test_h5_becomes_h6(self):
        assert _demote_headings("##### Deep") == "###### Deep"

    def test_h6_left_alone(self):
        assert _demote_headings("###### Max") == "###### Max"

    def test_no_headings(self):
        text = "Just a paragraph.\n\nAnother one."
        assert _demote_headings(text) == text

    def test_headings_inside_code_fence_untouched(self):
        text = "```\n# Not a heading\n## Also not\n```"
        assert _demote_headings(text) == text

    def test_headings_inside_tilde_fence_untouched(self):
        text = "~~~\n# Not a heading\n~~~"
        assert _demote_headings(text) == text

    def test_mixed_fenced_and_unfenced(self):
        text = "# Real heading\n```\n# Code\n```\n## Another real"
        result = _demote_headings(text)
        lines = result.splitlines()
        assert lines[0] == "## Real heading"
        assert lines[2] == "# Code"  # inside fence — untouched
        assert lines[4] == "### Another real"

    def test_multiple_headings(self):
        text = "# One\n## Two\n### Three"
        result = _demote_headings(text)
        lines = result.splitlines()
        assert lines[0] == "## One"
        assert lines[1] == "### Two"
        assert lines[2] == "#### Three"

    def test_heading_without_space_not_demoted(self):
        # "#word" without space after # is not a heading
        assert _demote_headings("#nospace") == "#nospace"


# ── _strip_leading_title_heading ─────────────────────────────────────


class TestStripLeadingTitleHeading:
    def test_matching_h1_stripped(self):
        body = "# My Title\n\nContent here."
        result = _strip_leading_title_heading(body, "My Title")
        assert result == "Content here."

    def test_case_insensitive_match(self):
        body = "# my title\n\nContent."
        result = _strip_leading_title_heading(body, "My Title")
        assert result == "Content."

    def test_non_matching_heading_kept(self):
        body = "# Different Title\n\nContent."
        result = _strip_leading_title_heading(body, "My Title")
        assert result == body

    def test_no_heading(self):
        body = "Just content, no heading."
        result = _strip_leading_title_heading(body, "Title")
        assert result == body

    def test_blank_lines_after_heading_stripped(self):
        body = "# Title\n\n\n\nContent."
        result = _strip_leading_title_heading(body, "Title")
        assert result == "Content."

    def test_empty_body(self):
        assert _strip_leading_title_heading("", "Title") == ""

    def test_whitespace_only(self):
        assert _strip_leading_title_heading("  \n  \n", "Title") == "  \n  \n"

    def test_h2_also_stripped_when_matching(self):
        body = "## Title\n\nContent."
        result = _strip_leading_title_heading(body, "Title")
        assert result == "Content."

    def test_leading_blank_lines_skipped(self):
        body = "\n\n# Title\n\nContent."
        result = _strip_leading_title_heading(body, "Title")
        # The function finds and strips the heading + trailing blanks,
        # but preceding blank lines remain as empty entries in the list
        assert "Content." in result


# ── load_library_item ────────────────────────────────────────────────


class TestLoadLibraryItem:
    def test_missing_file_returns_none(self, tmp_path):
        assert load_library_item(tmp_path / "nope.md") is None

    def test_simple_file_with_frontmatter(self, tmp_path):
        p = tmp_path / "my-protocol.md"
        p.write_text(
            dedent("""\
            ---
            title: My Protocol
            description: Does things
            ---

            # My Protocol

            Step 1. Do the thing.
        """)
        )
        item = load_library_item(p)
        assert item is not None
        assert item.title == "My Protocol"
        assert item.description == "Does things"
        # Leading title heading should be stripped, then body demoted
        assert "# My Protocol" not in item.body
        assert "Step 1" in item.body
        assert item.source_path == p

    def test_no_frontmatter_derives_title(self, tmp_path):
        p = tmp_path / "report-back.md"
        p.write_text("Report back when done.")
        item = load_library_item(p)
        assert item is not None
        assert item.title == "Report back"  # derived from filename
        assert item.description is None

    def test_heading_demotion_applied(self, tmp_path):
        p = tmp_path / "test.md"
        p.write_text("---\ntitle: Test\n---\n\n## Section\n\nText.")
        item = load_library_item(p)
        assert "### Section" in item.body

    def test_title_heading_stripped_before_demotion(self, tmp_path):
        """Leading H1 matching title is stripped, then remaining headings demoted."""
        p = tmp_path / "test.md"
        p.write_text("---\ntitle: Test\n---\n\n# Test\n\n## Sub\n\nText.")
        item = load_library_item(p)
        assert "# Test" not in item.body
        assert "### Sub" in item.body  # ## → ### after demotion


# ── load_library_items_overlay ───────────────────────────────────────


class TestLoadLibraryItemsOverlay:
    def _make_paths(self, root: Path):
        from claudlobby.paths import Paths

        return Paths(root=root, fleet_dir=root)

    def test_loads_single_file(self, tmp_path):
        root = tmp_path / "claudlobby"
        (root / "library" / "protocols").mkdir(parents=True)
        (root / "library" / "protocols" / "report-back.md").write_text(
            "---\ntitle: Report-Back\n---\n\nReport back."
        )
        paths = self._make_paths(root)
        items = load_library_items_overlay(["report-back"], paths, "protocols")
        assert len(items) == 1
        assert items[0].title == "Report-Back"

    def test_skips_missing(self, tmp_path):
        root = tmp_path / "claudlobby"
        (root / "library" / "protocols").mkdir(parents=True)
        paths = self._make_paths(root)
        items = load_library_items_overlay(["nonexistent"], paths, "protocols")
        assert items == []

    def test_deduplicates(self, tmp_path):
        root = tmp_path / "claudlobby"
        (root / "library" / "protocols").mkdir(parents=True)
        (root / "library" / "protocols" / "rp.md").write_text(
            "---\ntitle: RP\n---\n\nBody."
        )
        paths = self._make_paths(root)
        items = load_library_items_overlay(["rp", "rp"], paths, "protocols")
        assert len(items) == 1

    def test_folder_expansion(self, tmp_path):
        root = tmp_path / "claudlobby"
        sub = root / "library" / "lessons" / "topic"
        sub.mkdir(parents=True)
        (sub / "one.md").write_text("---\ntitle: One\n---\n\nA.")
        (sub / "two.md").write_text("---\ntitle: Two\n---\n\nB.")
        paths = self._make_paths(root)
        items = load_library_items_overlay(["topic/"], paths, "lessons")
        assert len(items) == 2
        titles = {i.title for i in items}
        assert titles == {"One", "Two"}

    def test_nested_file_path(self, tmp_path):
        root = tmp_path / "claudlobby"
        sub = root / "library" / "lessons" / "deep"
        sub.mkdir(parents=True)
        (sub / "nested.md").write_text("---\ntitle: Nested\n---\n\nContent.")
        paths = self._make_paths(root)
        items = load_library_items_overlay(["deep/nested"], paths, "lessons")
        assert len(items) == 1
        assert items[0].title == "Nested"


# ── _parse_expertise_permissions ─────────────────────────────────────


class TestParseExpertisePermissions:
    def test_no_permissions_returns_none(self):
        assert _parse_expertise_permissions({}) is None

    def test_empty_permissions_returns_none(self):
        assert _parse_expertise_permissions({"permissions": None}) is None

    def test_non_dict_permissions_returns_none(self):
        assert _parse_expertise_permissions({"permissions": "not a dict"}) is None

    def test_full_permissions(self):
        fm = {
            "permissions": {
                "allow": ["Read", "Write"],
                "deny": ["Bash"],
                "allow_all": True,
                "bash_allow": ["git *", "npm *"],
            }
        }
        perms = _parse_expertise_permissions(fm)
        assert perms is not None
        assert perms.allow == ["Read", "Write"]
        assert perms.deny == ["Bash"]
        assert perms.allow_all is True
        assert perms.bash_allow == ["git *", "npm *"]

    def test_partial_permissions(self):
        fm = {"permissions": {"allow": ["Read"]}}
        perms = _parse_expertise_permissions(fm)
        assert perms.allow == ["Read"]
        assert perms.deny == []
        assert perms.allow_all is False
        assert perms.bash_allow == []


# ── parse_expertise_file ─────────────────────────────────────────────


class TestParseExpertiseFile:
    def test_missing_file_returns_none(self, tmp_path):
        assert parse_expertise_file(tmp_path / "nope.md") is None

    def test_h1_with_em_dash_separator(self, tmp_path):
        p = tmp_path / "eng.md"
        p.write_text("# {{BOT_NAME}} — Engineer\n\nBuild things.")
        item = parse_expertise_file(p)
        assert item is not None
        assert item.title_label == "Engineer"
        assert "Build things." in item.body
        assert "# " not in item.body

    def test_h1_with_hyphen_separator(self, tmp_path):
        p = tmp_path / "eng.md"
        p.write_text("# BotName - Worker\n\nDo work.")
        item = parse_expertise_file(p)
        assert item.title_label == "Worker"

    def test_h1_with_en_dash_separator(self, tmp_path):
        p = tmp_path / "eng.md"
        p.write_text("# Bot – Manager\n\nManage.")
        item = parse_expertise_file(p)
        assert item.title_label == "Manager"

    def test_h1_without_separator(self, tmp_path):
        p = tmp_path / "eng.md"
        p.write_text("# Orchestrator\n\nBody text.")
        item = parse_expertise_file(p)
        assert item.title_label == "Orchestrator"
        assert "Body text." in item.body

    def test_no_h1(self, tmp_path):
        p = tmp_path / "eng.md"
        p.write_text("Just body text, no heading.")
        item = parse_expertise_file(p)
        assert item.title_label is None
        assert "Just body text" in item.body

    def test_empty_file(self, tmp_path):
        p = tmp_path / "eng.md"
        p.write_text("")
        item = parse_expertise_file(p)
        assert item.title_label is None
        assert item.body == ""

    def test_frontmatter_title_label_wins(self, tmp_path):
        p = tmp_path / "eng.md"
        p.write_text(
            dedent("""\
            ---
            title_label: From Frontmatter
            ---

            # {{BOT_NAME}} — From H1

            Body.
        """)
        )
        item = parse_expertise_file(p)
        assert item.title_label == "From Frontmatter"

    def test_permissions_extracted(self, tmp_path):
        p = tmp_path / "eng.md"
        p.write_text(
            dedent("""\
            ---
            permissions:
              allow: [Read, Grep]
              deny: [Bash]
            ---

            # Engineer

            Build.
        """)
        )
        item = parse_expertise_file(p)
        assert item.permissions is not None
        assert item.permissions.allow == ["Read", "Grep"]
        assert item.permissions.deny == ["Bash"]

    def test_no_permissions(self, tmp_path):
        p = tmp_path / "eng.md"
        p.write_text("# Eng\n\nBody.")
        item = parse_expertise_file(p)
        assert item.permissions is None


# ── load_voice ───────────────────────────────────────────────────────


class TestLoadVoice:
    def test_missing_file_returns_none(self, tmp_path):
        assert load_voice(tmp_path / "nope.md") is None

    def test_modern_frontmatter_name(self, tmp_path):
        p = tmp_path / "astrid.md"
        p.write_text(
            dedent("""\
            ---
            name: Astrid
            description: Senior engineer voice
            ---

            Direct and principled.
        """)
        )
        item = load_voice(p)
        assert item is not None
        assert item.title == "Astrid"
        assert item.description == "Senior engineer voice"
        assert "Direct and principled." in item.body

    def test_legacy_voice_h2(self, tmp_path):
        p = tmp_path / "old-style.md"
        p.write_text("## Voice: Old Pal\n\nCharming and folksy.")
        item = load_voice(p)
        assert item.title == "Old Pal"
        assert "Charming and folksy." in item.body
        assert "## Voice:" not in item.body

    def test_legacy_voice_h2_with_leading_blanks(self, tmp_path):
        p = tmp_path / "old.md"
        p.write_text("\n\n## Voice: Legacy\n\nBody.")
        item = load_voice(p)
        assert item.title == "Legacy"

    def test_no_frontmatter_no_voice_h2(self, tmp_path):
        p = tmp_path / "plain-voice.md"
        p.write_text("Just a voice description.\n\nPersonality traits.")
        item = load_voice(p)
        assert item.title == "Plain voice"  # derived from filename
        assert "Just a voice description." in item.body

    def test_frontmatter_name_takes_precedence(self, tmp_path):
        p = tmp_path / "dual.md"
        p.write_text(
            dedent("""\
            ---
            name: Frontmatter Name
            ---

            ## Voice: Legacy Name

            Content.
        """)
        )
        item = load_voice(p)
        # Frontmatter wins
        assert item.title == "Frontmatter Name"


# ── integration tool_grants (F2: additive list, folder-aware) ────────


class TestIntegrationToolGrants:
    def _paths(self, root):
        return Paths(root=root, fleet_dir=None)

    def _write_integration(self, root, rel, body_fm=""):
        p = root / "library" / "integrations" / f"{rel}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntitle: {rel}\n{body_fm}---\n\n# {rel}\n")
        return p

    def test_reads_tool_grants_from_integration_md(self, tmp_path):
        self._write_integration(
            tmp_path, "github", 'tool_grants:\n  - "mcp__github__*"\n'
        )
        grants = integration_tool_grants(self._paths(tmp_path), "github")
        assert grants == ["mcp__github__*"]

    def test_missing_tool_grants_returns_empty(self, tmp_path):
        self._write_integration(tmp_path, "plain")
        assert integration_tool_grants(self._paths(tmp_path), "plain") == []

    def test_unknown_integration_returns_empty(self, tmp_path):
        assert integration_tool_grants(self._paths(tmp_path), "ghost") == []

    def test_non_list_tool_grants_returns_empty(self, tmp_path):
        self._write_integration(tmp_path, "bad", 'tool_grants: "not-a-list"\n')
        assert integration_tool_grants(self._paths(tmp_path), "bad") == []

    def test_iter_resolves_single_and_dir_name(self, tmp_path):
        self._write_integration(
            tmp_path, "slack", 'tool_grants:\n  - "mcp__slack__*"\n'
        )
        self._write_integration(
            tmp_path, "conn/gmail", 'tool_grants:\n  - "mcp__claude_ai_Gmail__*"\n'
        )
        pairs = iter_integration_grants(self._paths(tmp_path), ["slack", "conn/gmail"])
        assert pairs == [
            ("slack", ["mcp__slack__*"]),
            ("conn/gmail", ["mcp__claude_ai_Gmail__*"]),
        ]

    def test_iter_expands_dir_folder(self, tmp_path):
        # A dir/ folder-expansion entry resolves every member's grants — the
        # bypass rajan flagged: a malformed grant nested in an expanded folder
        # must not be silently skipped.
        self._write_integration(
            tmp_path, "conn/native", 'tool_grants:\n  - "mcp__claude_ai_Gmail__*"\n'
        )
        self._write_integration(tmp_path, "conn/bad", 'tool_grants:\n  - "rm -rf /"\n')
        pairs = dict(iter_integration_grants(self._paths(tmp_path), ["conn/"]))
        assert pairs == {
            "conn/native": ["mcp__claude_ai_Gmail__*"],
            "conn/bad": ["rm -rf /"],
        }

    def test_iter_unknown_entry_yields_empty_grants(self, tmp_path):
        assert iter_integration_grants(self._paths(tmp_path), ["ghost"]) == [
            ("ghost", [])
        ]


# ── guardrail permissions (F2: deny-capable block, shared schema) ────


class TestGuardrailPermissions:
    def test_reads_permissions_block(self, tmp_path):
        p = tmp_path / "guard.md"
        p.write_text(
            "---\ntitle: Guard\npermissions:\n  deny: [Write, Edit]\n"
            "  allow: [Read]\n---\n\n# Guard\n"
        )
        perms = parse_guardrail_permissions(p)
        assert perms is not None
        assert perms.deny == ["Write", "Edit"]
        assert perms.allow == ["Read"]

    def test_prose_only_guardrail_returns_none(self, tmp_path):
        # Snowflake SELECT-only stays prose (grammar can't express it) — no permissions block.
        p = tmp_path / "snowflake-read-only.md"
        p.write_text(
            "---\ntitle: Snowflake — read-only\n---\n\n"
            "# Snowflake — read-only\n\nWithout confirmation, only SELECT.\n"
        )
        assert parse_guardrail_permissions(p) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert parse_guardrail_permissions(tmp_path / "nope.md") is None
