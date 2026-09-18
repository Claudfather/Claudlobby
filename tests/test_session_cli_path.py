"""#1567 -- a bot session resolves the bare `claudlobby` CLI on a venv install.

`lib/start-bot.sh` sets a bot session PATH to system dirs, `~/.local/bin`, the
bun and npm global bins, and Homebrew -- never the compositor's own venv. On a
host whose install keeps the CLI only at
`$CLAUDLOBBY_ROOT/.venv/bin/claudlobby` (the PEP 668 venv shape
getting-started.md documents), a shipped skill grant that names the CLI bare
(`Bash(claudlobby checkins *)`) cannot resolve inside the session, and the
path-form fallback a session finds on its own does not match that grant --
outside auto permission mode an unattended beat stalls on a prompt.

`session_cli_path` (lib/lib-common.sh) is the fix: called once, right after
start-bot.sh sets PATH, it symlinks the one `claudlobby` name into a
host-local shim dir and appends that dir to PATH, leaving everything a
session already resolves unchanged. These tests exercise the REAL function
the same way tests/test_roster_doors.py exercises `declared_bots_strict` -- a
subprocess sources the real lib/lib-common.sh under a fake CLAUDLOBBY_ROOT and
a controlled PATH, calls it, and prints what it needs to assert on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib" / "lib-common.sh"
START_BOT = REPO_ROOT / "lib" / "start-bot.sh"

MARKER = "VENV_CLI_MARKER"
SAFE_PATH = "/usr/bin:/bin"


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _bare_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    return root


def _venv_root(tmp_path: Path, extra_bins: tuple[str, ...] = ()) -> Path:
    root = tmp_path / "root"
    _stub(root / ".venv" / "bin" / "claudlobby", f"echo {MARKER}")
    for name in extra_bins:
        _stub(root / ".venv" / "bin" / name, f"echo {name}")
    return root


def _run(root: Path, path: str, snippet: str) -> subprocess.CompletedProcess:
    """Source the real lib-common.sh (relaxed mode) then run snippet."""
    return subprocess.run(
        ["bash", "-c", f'set +e; source "{LIB}" >/dev/null 2>&1; set +e; {snippet}'],
        capture_output=True,
        text=True,
        timeout=60,
        env={"CLAUDLOBBY_ROOT": str(root), "HOME": str(root), "PATH": path},
    )


def _path_line(stdout: str) -> str:
    return next(ln for ln in stdout.splitlines() if ln.startswith("PATH="))[len("PATH="):]


def test_a_venv_only_install_resolves_the_bare_cli(tmp_path: Path):
    root = _venv_root(tmp_path)
    result = _run(
        root,
        SAFE_PATH,
        'session_cli_path; rc=$?; '
        'printf "RC=%s\\n" "$rc"; '
        'printf "RESOLVED=%s\\n" "$(command -v claudlobby)"; '
        'printf "OUTPUT=%s\\n" "$(claudlobby)"',
    )
    assert "RC=0" in result.stdout, (result.stdout, result.stderr)
    assert f"RESOLVED={root}/state/bin/claudlobby" in result.stdout, result.stdout
    assert f"OUTPUT={MARKER}" in result.stdout, result.stdout
    shim = root / "state" / "bin" / "claudlobby"
    assert shim.is_symlink(), "state/bin/claudlobby must be a symlink"
    assert shim.resolve() == (root / ".venv" / "bin" / "claudlobby").resolve()


def test_only_the_one_name_is_exposed(tmp_path: Path):
    root = _venv_root(tmp_path, extra_bins=("pip", "python"))
    result = _run(root, SAFE_PATH, 'session_cli_path; printf "RC=%s\\n" "$?"')
    assert "RC=0" in result.stdout, (result.stdout, result.stderr)
    shim_dir = root / "state" / "bin"
    assert sorted(p.name for p in shim_dir.iterdir()) == ["claudlobby"]


def test_the_shim_dir_is_appended_never_prepended(tmp_path: Path):
    root = _venv_root(tmp_path)
    custom = tmp_path / "custom" / "bin"
    custom.mkdir(parents=True)
    original = ["/usr/bin", "/bin", str(custom)]
    result = _run(root, ":".join(original), 'session_cli_path; printf "PATH=%s\\n" "$PATH"')
    entries = _path_line(result.stdout).split(":")
    assert entries[:-1] == original, entries
    assert entries[-1] == f"{root}/state/bin", entries


def test_a_host_with_the_cli_already_on_path_is_untouched(tmp_path: Path):
    root = _bare_root(tmp_path)
    early_bin = tmp_path / "earlybin"
    _stub(early_bin / "claudlobby", "echo EARLY_MARKER")
    original = f"{early_bin}:{SAFE_PATH}"
    result = _run(
        root, original,
        'session_cli_path; rc=$?; printf "RC=%s\\n" "$rc"; printf "PATH=%s\\n" "$PATH"',
    )
    assert "RC=0" in result.stdout, (result.stdout, result.stderr)
    assert _path_line(result.stdout) == original, result.stdout
    assert not (root / "state").exists()


def test_no_venv_cli_means_no_change_and_no_error(tmp_path: Path):
    root = _bare_root(tmp_path)
    result = _run(
        root, SAFE_PATH,
        'session_cli_path; rc=$?; printf "RC=%s\\n" "$rc"; printf "PATH=%s\\n" "$PATH"',
    )
    assert "RC=0" in result.stdout, (result.stdout, result.stderr)
    assert _path_line(result.stdout) == SAFE_PATH, result.stdout
    assert not (root / "state").exists()


def test_it_is_idempotent_across_boots(tmp_path: Path):
    root = _venv_root(tmp_path)
    snippet = 'session_cli_path; rc=$?; printf "RC=%s\\n" "$rc"; printf "PATH=%s\\n" "$PATH"'
    first = _run(root, SAFE_PATH, snippet)
    second = _run(root, SAFE_PATH, snippet)
    for result in (first, second):
        assert "RC=0" in result.stdout, (result.stdout, result.stderr)
        entries = _path_line(result.stdout).split(":")
        assert entries.count(f"{root}/state/bin") == 1, entries
    shim_dir = root / "state" / "bin"
    assert [p.name for p in shim_dir.iterdir()] == ["claudlobby"]
    assert (shim_dir / "claudlobby").is_symlink()


def test_a_read_only_state_dir_never_fails_the_boot(tmp_path: Path):
    root = _venv_root(tmp_path)
    state_dir = root / "state"
    state_dir.mkdir()
    state_dir.chmod(0o555)
    marker = tmp_path / "trapped"
    # -E (errtrace) is armed explicitly, the same way install_error_trap arms
    # it, so the ERR trap actually reaches inside the session_cli_path
    # function rather than only top-level commands (#844) -- without it this
    # test could not tell a guarded failure from a trap that never runs at
    # all.
    trap_body = f'printf TRAPPED > "{marker}"'
    script = (
        f'source "{LIB}" >/dev/null 2>&1; '
        'set -Eeuo pipefail; '
        f"trap '{trap_body}' ERR; "
        'session_cli_path; rc=$?; '
        'printf "RC=%s\\n" "$rc"; '
        'printf "PATH=%s\\n" "$PATH"'
    )
    try:
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            env={"CLAUDLOBBY_ROOT": str(root), "HOME": str(root), "PATH": SAFE_PATH},
        )
        assert "RC=0" in result.stdout, (result.stdout, result.stderr)
        assert _path_line(result.stdout) == SAFE_PATH, result.stdout
        assert not marker.exists(), "ERR trap fired: session_cli_path let a failure propagate"
    finally:
        state_dir.chmod(0o755)


def test_the_launcher_calls_it_right_after_it_sets_path():
    lines = START_BOT.read_text().splitlines()
    path_idx = next(i for i, ln in enumerate(lines) if ln.startswith("export PATH="))
    tiered_idx = next(i for i, ln in enumerate(lines) if "source_env_tiered" in ln)
    call_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "session_cli_path"), None)
    assert call_idx is not None, "session_cli_path is not called in start-bot.sh"
    assert path_idx < call_idx < tiered_idx, (path_idx, call_idx, tiered_idx)
