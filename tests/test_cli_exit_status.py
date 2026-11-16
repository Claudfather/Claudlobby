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

Items 15 and 17: the cases after them leave main by its other two ways, a SystemExit (a
usage refusal) and an exception, while stderr cannot be written. Item 21: the last case is
a usage refusal by a subcommand's own parser.
"""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys

import pytest

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


def _cli(args, stderr, *, fd2=None):
    argv = [sys.executable, "-I", "-c", ENTRY, str(REPO), *args]
    if fd2 is not None:  # fd 2 set up by the shell before the interpreter starts
        argv = ["/bin/sh", "-c", f'exec {fd2}; exec "$@"', "sh", *argv]
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=stderr, env=constructed_env(),
                          timeout=120)


def _door(tmp_path, stderr, *, fd2=None):
    bot = _bot(tmp_path)
    return _cli(["_handoff-files", "watch", str(bot)], stderr, fd2=fd2), [str(bot / ".claude/session.md")]


@contextlib.contextmanager
def _unwritable(kind):
    """stderr and fd2 for _cli: a pipe whose reader has exited, a stream open only for
    reading (the launcher's read-only lock, below), or fd 2 closed before the start."""
    if kind == "pipe":
        read, write = os.pipe()
        os.close(read)
        try:
            yield write, None
        finally:
            os.close(write)
    else:
        yield subprocess.DEVNULL, {"read-only": "2</dev/null", "closed": "2>&-"}[kind]


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


@pytest.mark.parametrize("kind", ["pipe", "read-only", "closed"])
def test_a_usage_refusal_exits_2_when_stderr_cannot_be_written(kind):
    """Item 17: argparse's usage write raised on such a stderr, and the refusal exited 1 (the
    exception's status), where a stderr on /dev/null gives 2. The CLI's parser drops the
    write now, so it exits 2. Item 15, the SystemExit leg: for a pipe or a read-only stream
    the dropped lines stay in the buffer, and main closes the stream on this way out too;
    left open, its last flush fails and the status is 120."""
    with _unwritable(kind) as (stderr, fd2):
        run = _cli(["_no-such-door"], stderr, fd2=fd2)
    assert run.returncode == 2, run  # 1 before


def test_a_usage_refusal_on_a_working_stderr_is_unchanged():
    run = _cli(["_no-such-door"], subprocess.PIPE)
    lines = run.stderr.decode().splitlines()
    assert (run.returncode, run.stdout, lines[0].split()[:2]) == (2, b"", ["usage:", "claudlobby"]), run
    assert lines[-1].startswith("claudlobby: error: argument cmd: invalid choice: '_no-such-door'"), run


def test_an_exception_that_leaves_main_with_stderr_unwritable_exits_1_not_120():
    """Item 15, the exception leg, with no test-only hook: a command's own stderr line that is
    not guarded raises when stderr cannot take it. Here the handoff door's usage line, for a
    directory that is not absolute (a refusal that exits 2 on a stderr that works). The
    exception ends the process with status 1, its traceback lost. main closes the stream on
    this way out too; left open, its last flush fails and the status is 120."""
    with _unwritable("pipe") as (stderr, fd2):
        run = _cli(["_handoff-files", "watch", "bot"], stderr, fd2=fd2)
    assert (run.returncode, run.stdout) == (1, b""), run


def test_a_sub_parsers_own_refusal_exits_2_when_stderr_cannot_be_written():
    """Item 21: the usage refusals above are the CLI parser's own (an unknown command). A
    subcommand's parser takes the same guard only because add_subparsers builds it from the
    CLI parser's own class, argparse's default, since no call passes parser_class. Here
    `_handoff-files watch` with no directory, which that subcommand's parser refuses for a
    required argument. `_handoff-files` is a private door, not a migrated route: command_result
    takes a command line for a migrated route only by the first word of a public operation
    (`_command_from_argv`), and `_handoff-files` is none, so ResultArgumentParser.error leaves
    the refusal to argparse, which writes it through the guarded `_print_message`. With the
    sub-parsers built from a class without the guard (ResultArgumentParser itself), that write
    raised on such a stderr and the refusal exited 1."""
    run = _cli(["_handoff-files", "watch"], subprocess.PIPE)
    assert (run.returncode, run.stderr.decode().splitlines()[-1]) == (
        2, "claudlobby _handoff-files: error: the following arguments are required: directory"), run
    with _unwritable("pipe") as (stderr, fd2):
        run = _cli(["_handoff-files", "watch"], stderr, fd2=fd2)
    assert (run.returncode, run.stdout) == (2, b""), run  # 1 without the guard
