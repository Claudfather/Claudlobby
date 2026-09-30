"""Rejected .env input stays out of diagnostics, without changing acceptance."""

import subprocess
from pathlib import Path

import pytest


LIB = Path(__file__).resolve().parents[1] / "lib" / "lib-common.sh"


def _parse(tmp_path, content, *, show_value=False, after=""):
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    env_file = tmp_path / "synthetic input.env"
    env_file.write_text(content)
    # Construct from nothing: no host tiers, credentials, channels or sockets.
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_STATE_HOME": str(home / "state"),
        "CLAUDLOBBY_ROOT": str(root),
        "PLANE_EMIT_DISABLED": "1",
        "PLANE_SOCKET": str(root / "absent.sock"),
        "PLANE_EMIT_CLI": "/usr/bin/false",
        "TMUX_BIN": "/usr/bin/false",
        "TELEGRAM_STATE_DIR": str(home / "telegram"),
    }
    command = '. "$1"; parse_env_file "$2"'
    command += '; printf "%s" "${VALUE-<unset>}"' if show_value else '; test "${VALUE+x}" != x'
    command += after
    result = subprocess.run(
        ["/bin/bash", "-c", command, "_", str(LIB), str(env_file)],
        env=env, cwd=root, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not (root / "state").exists()
    assert list(home.iterdir()) == []
    return result, env_file


@pytest.mark.parametrize("line", [
    "SENTINEL_INVALID_NO_EQUALS",
    "bad-key=SENTINEL_INVALID_KEY",
    "  export 9KEY=SENTINEL_INVALID_EXPORT",
])
def test_invalid_line_never_logs_value(tmp_path, line):
    result, path = _parse(tmp_path, line)
    assert "SENTINEL" not in result.stdout + result.stderr
    assert result.stdout == ""
    assert result.stderr == f"parse_env_file: {path}:1: invalid assignment\n"


@pytest.mark.parametrize("syntax", ["semicolon", "pipe", "backtick", "substitution"])
def test_rejected_metacharacters_never_log_value(tmp_path, syntax):
    marker = tmp_path / "executed"
    payload = {
        "semicolon": f"; touch '{marker}'",
        "pipe": f"| touch '{marker}'",
        "backtick": f"`touch '{marker}'`",
        "substitution": f"$(touch '{marker}')",
    }[syntax]
    sentinel = f"SENTINEL_{syntax.upper()}"
    # Blank/comment/accepted lines count, and the rejected last line has no LF.
    result, path = _parse(tmp_path, f"# comment\n\nGOOD=ok\n  export VALUE={sentinel}{payload}")
    assert sentinel not in result.stdout + result.stderr
    assert result.stdout == ""
    assert result.stderr == f"parse_env_file: {path}:4: disallowed shell syntax\n"
    assert not marker.exists()


@pytest.mark.parametrize("line,expected", [
    ("VALUE=plain\n", "plain"),
    ("  export VALUE='quoted value'", "quoted value"),
    ('export VALUE="double quoted"\n', "double quoted"),
    ("VALUE=", ""),
])
def test_valid_value_exports_without_diagnostic(tmp_path, line, expected):
    result, _ = _parse(tmp_path, line, show_value=True)
    assert result.stdout == expected
    assert result.stderr == ""


def test_input_keys_cannot_change_diagnostic_location(tmp_path):
    result, path = _parse(tmp_path, "line_number=99\nfile=SENTINEL_PATH\nBROKEN\n",
                          after='; printf "%s" "${line_number-<unset>}"')
    assert result.stdout == "99"
    assert result.stderr == f"parse_env_file: {path}:3: invalid assignment\n"
    assert "SENTINEL" not in result.stderr
