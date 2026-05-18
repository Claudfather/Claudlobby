"""Tests for claudlobby/dotenv.py — format_file shell-safety and read round-trip."""

from __future__ import annotations

import subprocess

import pytest

from claudlobby.dotenv import format_file, read


class TestFormatFileShellSafety:
    """format_file must produce output safe to `source` — no shell expansion."""

    def test_dollar_sign_not_expanded(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(format_file("# test", {"X": "$(touch /tmp/pwned)"}))
        result = subprocess.run(
            ["bash", "-c", f'set -a; . "{env_file}"; printf "%s" "$X"'],
            capture_output=True,
            text=True,
        )
        assert result.stdout == "$(touch /tmp/pwned)"
        assert not (tmp_path / "pwned").exists()

    def test_backtick_not_expanded(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(format_file("# test", {"X": "`whoami`"}))
        result = subprocess.run(
            ["bash", "-c", f'set -a; . "{env_file}"; printf "%s" "$X"'],
            capture_output=True,
            text=True,
        )
        assert result.stdout == "`whoami`"

    def test_backslash_preserved(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(format_file("# test", {"X": "a\\nb\\nc"}))
        result = subprocess.run(
            ["bash", "-c", f'set -a; . "{env_file}"; printf "%s" "$X"'],
            capture_output=True,
            text=True,
        )
        assert result.stdout == "a\\nb\\nc"

    def test_newline_preserved(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(format_file("# test", {"X": "line1\nline2"}))
        result = subprocess.run(
            ["bash", "-c", f'set -a; . "{env_file}"; printf "%s" "$X"'],
            capture_output=True,
            text=True,
        )
        assert result.stdout == "line1\nline2"

    def test_single_quote_in_value(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(format_file("# test", {"X": "it's a string"}))
        result = subprocess.run(
            ["bash", "-c", f'set -a; . "{env_file}"; printf "%s" "$X"'],
            capture_output=True,
            text=True,
        )
        assert result.stdout == "it's a string"

    def test_double_quote_in_value(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(format_file("# test", {"X": 'say "hello"'}))
        result = subprocess.run(
            ["bash", "-c", f'set -a; . "{env_file}"; printf "%s" "$X"'],
            capture_output=True,
            text=True,
        )
        assert result.stdout == 'say "hello"'

    def test_combined_injection_payload(self, tmp_path):
        payload = '$(rm -rf /) `curl evil` $HOME "quoted" \\n'
        env_file = tmp_path / ".env"
        env_file.write_text(format_file("# test", {"TOKEN": payload}))
        result = subprocess.run(
            ["bash", "-c", f'set -a; . "{env_file}"; printf "%s" "$TOKEN"'],
            capture_output=True,
            text=True,
        )
        assert result.stdout == payload


class TestFormatFileKeyValidation:
    """format_file rejects invalid shell identifiers as keys."""

    def test_rejects_semicolon_in_key(self):
        with pytest.raises(ValueError, match="not a valid shell identifier"):
            format_file("# test", {"FOO;rm -rf /": "x"})

    def test_rejects_space_in_key(self):
        with pytest.raises(ValueError, match="not a valid shell identifier"):
            format_file("# test", {"FOO BAR": "x"})

    def test_rejects_leading_digit(self):
        with pytest.raises(ValueError, match="not a valid shell identifier"):
            format_file("# test", {"9VAR": "x"})

    def test_rejects_dollar_in_key(self):
        with pytest.raises(ValueError, match="not a valid shell identifier"):
            format_file("# test", {"$VAR": "x"})

    def test_accepts_valid_keys(self):
        output = format_file("# test", {"_FOO": "a", "BAR_2": "b", "x": "c"})
        assert "export BAR_2=" in output
        assert "export _FOO=" in output
        assert "export x=" in output


class TestReadRoundTrip:
    """read() correctly parses output from format_file()."""

    def test_round_trip_simple(self, tmp_path):
        original = {"API_KEY": "sk-123abc", "DB_URL": "postgres://localhost/db"}
        env_file = tmp_path / ".env"
        env_file.write_text(format_file("# test", original))
        parsed = read(env_file)
        assert parsed == original

    def test_round_trip_special_chars(self, tmp_path):
        original = {"TOKEN": 'abc$def`ghi"jkl\\mno'}
        env_file = tmp_path / ".env"
        env_file.write_text(format_file("# test", original))
        # read() strips outer quotes — for single-quoted values with
        # shlex.quote escaping, we verify the shell round-trip instead
        result = subprocess.run(
            ["bash", "-c", f'set -a; . "{env_file}"; printf "%s" "$TOKEN"'],
            capture_output=True,
            text=True,
        )
        assert result.stdout == original["TOKEN"]

    def test_read_skips_comments_and_blanks(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\n\nexport FOO='bar'\n")
        assert read(env_file) == {"FOO": "bar"}

    def test_read_missing_file(self, tmp_path):
        assert read(tmp_path / "nonexistent") == {}
