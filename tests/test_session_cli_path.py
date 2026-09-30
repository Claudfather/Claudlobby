"""A bot session selects its composed CLI without exposing a release's tools.

Exercise the real session_cli_path function under private data and release
roots. The per-bot link must win over an ambient CLI, while python/pip retain
their original PATH resolution. No application imports or live boots occur.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "claudlobby/_runtime_scripts" / "lib-common.sh"
START_BOT = REPO_ROOT / "claudlobby/_runtime_scripts" / "start-bot.sh"

MARKER = "SELECTED_CLI_MARKER"
SAFE_PATH = "/usr/bin:/bin"


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _installation(tmp_path: Path, bot_name: str = "solo", extra_bins=()):
    root = tmp_path / "data"
    bot_dir = root / "runtime" / "bots" / bot_name
    bot_dir.mkdir(parents=True, exist_ok=True)
    cli = tmp_path / "releases" / bot_name / "bin" / "claudlobby"
    _stub(cli, f"echo {MARKER}_{bot_name}")
    for name in extra_bins:
        _stub(cli.parent / name, f"echo RELEASE_{name}")
    return root, bot_dir, cli


def _env(root: Path, bot_dir: Path | None, cli: Path | None, path: str) -> dict:
    env = {
        "PLANE_EMIT_DISABLED": "1", "CLAUDLOBBY_ROOT": str(root),
        "HOME": str(root), "PATH": path,
    }
    if bot_dir is not None:
        env["BOT_DIR"] = str(bot_dir)
    if cli is not None:
        env["CLAUDLOBBY_CLI"] = str(cli)
    return env


def _script(snippet: str) -> str:
    return f"source {shlex.quote(str(LIB))} >/dev/null 2>&1; set +e; {snippet}"


def _run(root, bot_dir, cli, path, snippet) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", "-c", _script(snippet)], capture_output=True, text=True,
        timeout=15, env=_env(root, bot_dir, cli, path),
    )


def _path_line(stdout: str) -> str:
    return next(ln for ln in stdout.splitlines() if ln.startswith("PATH="))[len("PATH="):]


STATUS = 'session_cli_path; rc=$?; printf "RC=%s\\nPATH=%s\\n" "$rc" "$PATH"'


def test_selected_install_resolves_the_bare_cli(tmp_path: Path):
    root, bot_dir, cli = _installation(tmp_path)
    result = _run(
        root, bot_dir, cli, SAFE_PATH,
        STATUS + '; printf "RESOLVED=%s\\nOUTPUT=%s\\n" "$(command -v claudlobby)" "$(claudlobby)"',
    )
    link = bot_dir / ".cli" / "bin" / "claudlobby"
    assert "RC=0" in result.stdout, (result.stdout, result.stderr)
    assert f"RESOLVED={link}" in result.stdout
    assert f"OUTPUT={MARKER}_solo" in result.stdout
    assert link.is_symlink() and link.readlink() == cli
    assert not (root / "state" / "bin").exists()


def test_only_the_one_name_is_exposed(tmp_path: Path):
    root, bot_dir, cli = _installation(tmp_path, extra_bins=("pip", "python"))
    ambient = tmp_path / "ambient"
    for name in ("pip", "python"):
        _stub(ambient / name, f"echo AMBIENT_{name}")
    result = _run(
        root, bot_dir, cli, f"{ambient}:{SAFE_PATH}",
        STATUS + '; python; pip',
    )
    assert "RC=0" in result.stdout, (result.stdout, result.stderr)
    assert "AMBIENT_python\nAMBIENT_pip" in result.stdout
    assert sorted(p.name for p in (bot_dir / ".cli" / "bin").iterdir()) == ["claudlobby"]
    assert str(cli.parent) not in _path_line(result.stdout).split(":")


def test_only_the_bot_cli_directory_is_prepended(tmp_path: Path):
    root, bot_dir, cli = _installation(tmp_path)
    original = ["/usr/bin", "/bin", str(tmp_path / "custom")]
    result = _run(root, bot_dir, cli, ":".join(original), STATUS)
    assert _path_line(result.stdout).split(":") == [str(bot_dir / ".cli" / "bin"), *original]


def test_selected_cli_replaces_stale_path_precedence(tmp_path: Path):
    root, bot_dir, cli = _installation(tmp_path)
    ambient = tmp_path / "ambient"
    _stub(ambient / "claudlobby", "echo STALE_CLI")
    result = _run(
        root, bot_dir, cli, f"{ambient}:{SAFE_PATH}",
        'claudlobby; ' + STATUS + '; claudlobby',
    )
    assert result.stdout.splitlines()[0] == "STALE_CLI"
    assert "RC=0" in result.stdout, (result.stdout, result.stderr)
    assert result.stdout.splitlines()[-1] == f"{MARKER}_solo"


@pytest.mark.parametrize("failure", ("unset", "missing", "not-executable", "relative"))
def test_unusable_selected_cli_refuses_before_mutation(tmp_path: Path, failure: str):
    root, bot_dir, cli = _installation(tmp_path)
    # Neither an ambient CLI nor a guessed data-root venv licenses success.
    _stub(root / ".venv" / "bin" / "claudlobby", "echo GUESSED_CLI")
    selected = cli
    if failure == "unset":
        selected = None
    elif failure == "missing":
        selected = tmp_path / "absent" / "claudlobby"
    elif failure == "not-executable":
        cli.chmod(0o644)
    else:
        selected = Path("relative/claudlobby")
    original = f"{root}/.venv/bin:{SAFE_PATH}"
    result = _run(root, bot_dir, selected, original, STATUS)
    assert "RC=1" in result.stdout, (result.stdout, result.stderr)
    assert "CLAUDLOBBY_CLI" in result.stderr
    assert _path_line(result.stdout) == original
    assert not (bot_dir / ".cli").exists()


def test_unresolved_bot_dir_refuses_before_mutation(tmp_path: Path):
    root, bot_dir, cli = _installation(tmp_path)
    result = _run(root, None, cli, SAFE_PATH, STATUS)
    assert "RC=1" in result.stdout, (result.stdout, result.stderr)
    assert "BOT_DIR" in result.stderr
    assert _path_line(result.stdout) == SAFE_PATH
    assert not (bot_dir / ".cli").exists()
    assert not (root / ".cli").exists()


def test_it_is_idempotent_across_boots_and_repeated_calls(tmp_path: Path):
    root, bot_dir, cli = _installation(tmp_path)
    for _ in range(2):
        result = _run(root, bot_dir, cli, SAFE_PATH, 'session_cli_path; ' + STATUS)
        assert "RC=0" in result.stdout, (result.stdout, result.stderr)
        assert _path_line(result.stdout).split(":").count(str(bot_dir / ".cli" / "bin")) == 1
    assert [p.name for p in (bot_dir / ".cli" / "bin").iterdir()] == ["claudlobby"]


def test_an_operator_file_is_not_overwritten(tmp_path: Path):
    root, bot_dir, cli = _installation(tmp_path)
    link = bot_dir / ".cli" / "bin" / "claudlobby"
    _stub(link, "echo OPERATOR_FILE")
    before = link.read_bytes()
    result = _run(root, bot_dir, cli, SAFE_PATH, STATUS)
    assert "RC=1" in result.stdout, (result.stdout, result.stderr)
    assert "non-symlink" in result.stderr
    assert _path_line(result.stdout) == SAFE_PATH
    assert not link.is_symlink() and link.read_bytes() == before


def test_a_redirected_cli_directory_is_not_written(tmp_path: Path):
    root, bot_dir, cli = _installation(tmp_path)
    external = tmp_path / "operator"
    external.mkdir()
    (bot_dir / ".cli").symlink_to(external, target_is_directory=True)
    result = _run(root, bot_dir, cli, SAFE_PATH, STATUS)
    assert "RC=1" in result.stdout, (result.stdout, result.stderr)
    assert "redirected" in result.stderr
    assert not list(external.iterdir())


def test_extra_cli_directory_tools_are_not_exposed(tmp_path: Path):
    root, bot_dir, cli = _installation(tmp_path)
    extra = bot_dir / ".cli" / "bin" / "python"
    _stub(extra, "echo OPERATOR_PYTHON")
    result = _run(root, bot_dir, cli, SAFE_PATH, STATUS)
    assert "RC=1" in result.stdout, (result.stdout, result.stderr)
    assert "unexpected" in result.stderr
    assert _path_line(result.stdout) == SAFE_PATH
    assert not (extra.parent / "claudlobby").exists()


def test_the_launcher_calls_it_right_after_it_sets_path():
    lines = START_BOT.read_text().splitlines()
    path_idx = next(i for i, ln in enumerate(lines) if ln.startswith("export PATH"))
    tiered_idx = next(i for i, ln in enumerate(lines) if "source_env_tiered" in ln)
    call_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "session_cli_path"), None)
    assert call_idx is not None, "session_cli_path is not called in start-bot.sh"
    assert path_idx < call_idx < tiered_idx, (path_idx, call_idx, tiered_idx)


def test_concurrent_bots_keep_their_own_selected_release(tmp_path: Path):
    installations = [_installation(tmp_path, name) for name in ("first", "second")]
    # Two starts per bot also cover the cold-link creation race.
    starts = installations * 2
    jobs = [
        subprocess.Popen(
            ["/bin/bash", "-c", _script('session_cli_path || exit 1; claudlobby')],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_env(root, bot_dir, cli, SAFE_PATH),
        )
        for root, bot_dir, cli in starts
    ]
    results = [job.communicate(timeout=15) for job in jobs]
    for job, (out, err), (_, bot_dir, cli) in zip(jobs, results, starts):
        assert job.returncode == 0, (out, err)
        assert out.strip() == f"{MARKER}_{bot_dir.name}"
        assert (bot_dir / ".cli" / "bin" / "claudlobby").readlink() == cli
    assert not (installations[0][0] / "state" / "bin").exists()


def test_a_stale_link_is_repointed(tmp_path: Path):
    root, bot_dir, cli = _installation(tmp_path)
    link = bot_dir / ".cli" / "bin" / "claudlobby"
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "old-release" / "claudlobby")
    result = _run(root, bot_dir, cli, SAFE_PATH, STATUS + '; claudlobby')
    assert "RC=0" in result.stdout, (result.stdout, result.stderr)
    assert result.stdout.splitlines()[-1] == f"{MARKER}_solo"
    assert link.readlink() == cli
    assert [p.name for p in link.parent.iterdir()] == ["claudlobby"]
