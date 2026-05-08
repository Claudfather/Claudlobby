"""Tests for CLI helper functions extracted in issue #26."""
from __future__ import annotations

import pytest

from claudlobby.__main__ import _parse_rename_map, _parse_env_line


# ── _parse_rename_map ─────────────────────────────────────────────────

class TestParseRenameMap:
    def test_empty_list(self):
        assert _parse_rename_map([]) == {}

    def test_single_entry(self):
        assert _parse_rename_map(["clog=assistant"]) == {"clog": "assistant"}

    def test_multiple_entries(self):
        result = _parse_rename_map(["clog=assistant", "eng=worker"])
        assert result == {"clog": "assistant", "eng": "worker"}

    def test_strips_whitespace(self):
        assert _parse_rename_map(["  clog = assistant  "]) == {"clog": "assistant"}

    def test_value_with_equals(self):
        result = _parse_rename_map(["bot=dir=with=equals"])
        assert result == {"bot": "dir=with=equals"}

    def test_missing_equals_raises(self):
        with pytest.raises(ValueError, match="--map expects"):
            _parse_rename_map(["no-equals-here"])

    def test_mixed_valid_invalid(self):
        with pytest.raises(ValueError, match="--map expects"):
            _parse_rename_map(["good=entry", "bad"])


# ── _parse_env_line ───────────────────────────────────────────────────

class TestParseEnvLine:
    def test_simple_var(self):
        assert _parse_env_line("FOO=bar") == ("FOO", "bar")

    def test_export_prefix(self):
        assert _parse_env_line("export FOO=bar") == ("FOO", "bar")

    def test_strips_quotes(self):
        assert _parse_env_line('FOO="bar"') == ("FOO", "bar")
        assert _parse_env_line("FOO='bar'") == ("FOO", "bar")

    def test_blank_line(self):
        assert _parse_env_line("") is None
        assert _parse_env_line("   ") is None

    def test_comment(self):
        assert _parse_env_line("# this is a comment") is None

    def test_no_equals(self):
        assert _parse_env_line("just-a-word") is None

    def test_value_with_equals(self):
        assert _parse_env_line("URL=https://example.com?a=1") == (
            "URL", "https://example.com?a=1"
        )

    def test_whitespace_handling(self):
        assert _parse_env_line("  export  KEY  = value  ") == ("KEY", "value")
