"""Tests for CLI helper functions extracted in issue #26."""

from __future__ import annotations

import pytest

from claudlobby.commands._helpers import _parse_rename_map


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
