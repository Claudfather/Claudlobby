"""#2935 item 11: the exit status follows the answer when stderr cannot be written.

A guarded line whose write raised stays in the stream's buffer (#2934, #2936 and #2937
drop such a line). The interpreter flushes stderr once more as it ends, that flush
failed too, and the process exited 120 after a correct answer. Each case runs a real
process, since a call in this process never ends the interpreter. The command is the
private handoff door over a bot whose native-session receipt is malformed, which says
so on stderr through #2936's guarded note and answers on stdout.

A stderr on /dev/full (ENOSPC) fails the same way. That case waits on #2935: macOS has
no /dev/full, so it would skip there, and a new macOS skip needs the skip ceiling in
tests/ci_shards.py raised.
"""

import io
import os
from pathlib import Path
import subprocess
import sys

from claudlobby.__main__ import main
from tests.conftest import constructed_env

REPO = Path(__file__).resolve().parents[1]
# The native unit's entry (installation._NATIVE_CLI): this tree's package, and its main.
ENTRY = ("import sys; sys.path.insert(0, sys.argv[1]); from claudlobby.__main__ import main; "
         "raise SystemExit(main(sys.argv[2:]))")
NOTE = "the native session receipt cannot be read"


def _bot(tmp_path):
    bot = tmp_path / "bot"
    (bot / ".claude").mkdir(parents=True)
    (bot / "data").mkdir()
    (bot / "data/native-session.json").write_text("{")  # malformed: said on stderr
    return bot


def _door(tmp_path, stderr, *, fd2=None):
    bot = _bot(tmp_path)
    argv = [sys.executable, "-I", "-c", ENTRY, str(REPO), "_handoff-files", "watch", str(bot)]
    if fd2 is not None:  # fd 2 set up by the shell before the interpreter starts
        argv = ["/bin/sh", "-c", f'exec {fd2}; exec "$@"', "sh", *argv]
    run = subprocess.run(argv, stdout=subprocess.PIPE, stderr=stderr, env=constructed_env(),
                         timeout=120)
    return run, [str(bot / ".claude/session.md")]


def test_a_working_stderr_gets_the_line_and_the_answer_exits_0(tmp_path):
    run, answer = _door(tmp_path, subprocess.PIPE)
    assert NOTE in run.stderr.decode(), run.stderr  # the guarded line is written
    assert (run.returncode, run.stdout.decode().splitlines()) == (0, answer), run


def test_a_pipe_whose_reader_has_exited_loses_the_line_and_the_status_is_the_answers(tmp_path):
    read, write = os.pipe()
    os.close(read)  # every write fails: EPIPE, as a broken pipe (the interpreter ignores SIGPIPE)
    try:
        run, answer = _door(tmp_path, write)
    finally:
        os.close(write)
    assert (run.returncode, run.stdout.decode().splitlines()) == (0, answer), run  # 120 before


def test_a_stderr_closed_before_the_start_writes_nothing_and_the_answer_exits_0(tmp_path):
    """sys.stderr is None, so the guard skips the line and nothing is held: 0 before too."""
    run, answer = _door(tmp_path, subprocess.DEVNULL, fd2="2>&-")
    assert (run.returncode, run.stdout.decode().splitlines()) == (0, answer), run


def test_a_stderr_open_only_for_reading_loses_the_line_and_the_status_is_the_answers(tmp_path):
    """The installation launcher leaves fd 2 so when fd 2 was closed at its start: its runtime
    lock is opened read-only on the free fd 2, and the interpreter it starts takes that for
    stderr, so every write fails (EBADF). 120 before, as for a pipe whose reader has exited."""
    run, answer = _door(tmp_path, subprocess.DEVNULL, fd2="2</dev/null")
    assert (run.returncode, run.stdout.decode().splitlines()) == (0, answer), run


def test_a_stream_put_in_stderrs_place_is_left_open(tmp_path, monkeypatch):
    """Only the interpreter's own stderr is closed: a stream a caller put in its place (a
    test's capture here) is left as it is, though it cannot take the line."""
    class Broken(io.StringIO):
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    broken = Broken()
    monkeypatch.setattr(sys, "stderr", broken)
    assert main(["_handoff-files", "watch", str(_bot(tmp_path))]) == 0
    assert not broken.closed
