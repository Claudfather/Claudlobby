"""log-rotate.sh leaves a log at or under the cap exactly as it was, and cuts a
longer one to its last N lines with the log's own mode kept (#3165)."""

import os
import stat
import subprocess

import pytest

from tests.conftest import constructed_env

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROTATE = os.path.join(REPO_ROOT, "claudlobby", "_runtime_scripts", "log-rotate.sh")
# A fixed past mtime: a rewrite cannot land on it by chance.
OLD = 1_600_000_000


def _run(tmp_path, *paths, keep=500):
    return subprocess.run(
        ["bash", ROTATE, "--keep", str(keep), *map(str, paths)],
        env=constructed_env(CLAUDLOBBY_ROOT=tmp_path / "root"),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _log(path, lines, mode, *, last=""):
    path.write_text("".join(f"line {i}\n" for i in range(1, lines + 1)) + last)
    os.chmod(path, mode)
    os.utime(path, (OLD, OLD))
    return path


@pytest.mark.parametrize("lines", [100, 500])
def test_a_log_at_or_under_the_cap_is_left_alone(tmp_path, lines):
    log = _log(tmp_path / "app.log", lines, 0o644)
    body = log.read_bytes()
    before = os.stat(log)
    r = _run(tmp_path, log)
    assert r.returncode == 0, r.stderr + r.stdout
    after = os.stat(log)
    assert after.st_ino == before.st_ino
    assert stat.S_IMODE(after.st_mode) == 0o644
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    # mtime and ctime both: a rewrite or a chmod would move the ctime.
    assert (after.st_mtime_ns, after.st_ctime_ns) == (before.st_mtime_ns, before.st_ctime_ns)
    assert log.read_bytes() == body


@pytest.mark.parametrize("mode", [0o644, 0o640], ids=lambda m: f"{m:04o}")
def test_a_longer_log_is_cut_to_the_cap_and_keeps_its_mode(tmp_path, mode):
    log = _log(tmp_path / "app.log", 600, mode)
    r = _run(tmp_path, log)
    assert r.returncode == 0, r.stderr + r.stdout
    assert log.read_text().splitlines() == [f"line {i}" for i in range(101, 601)]
    assert stat.S_IMODE(os.stat(log).st_mode) == mode


def test_a_last_line_without_a_newline_counts_toward_the_cap(tmp_path):
    # 500 whole lines and one still being written are 501 lines to tail, so
    # the log is cut, its oldest line going.
    log = _log(tmp_path / "app.log", 500, 0o644, last="partial")
    r = _run(tmp_path, log)
    assert r.returncode == 0, r.stderr + r.stdout
    assert log.read_text() == "".join(f"line {i}\n" for i in range(2, 501)) + "partial"


def test_a_keep_that_is_not_a_whole_number_is_refused(tmp_path):
    log = _log(tmp_path / "app.log", 600, 0o644)
    r = _run(tmp_path, log, keep="5x")
    assert r.returncode == 2 and "--keep" in r.stderr, r.stderr
    assert log.read_text().count("\n") == 600
