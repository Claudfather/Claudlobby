"""#1053 PR 6: deliveries during a compaction.

While the host's compaction slot (compaction-slot.py, the build's PR 3) holds a
lease naming a bot, a compaction is in flight there. The pane alone cannot tell:
on Claude Code 2.1.319 its footer draws "esc to interrupt" while the box is empty
or shows queued messages, but not while a message sits typed in the box. Three readers now ask the slot: bot_is_busy and the CLI's repair Enter
(which presses nothing while the lease lives) through lib-common's
compaction_lease_left, and the CLI's send (which waits for the lease to end, at
most 60 s, then sends as to any busy bot) by running the same reader itself.

The reader asks the slot's own status(), so the tests here run it against a
stand-in slot whose status() returns what each test wrote, in the real one's
shape: the slot is not on main yet.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from claudlobby import message_transport as transport
from tests.conftest import constructed_env
from tests.package_fixtures import source_package

REPO = Path(__file__).resolve().parent.parent
LIB_DIR = REPO / "claudlobby" / "_runtime_scripts"
FIXTURES = REPO / "tests" / "fixtures" / "pane-states"
IDLE_PANE = (FIXTURES / "idle-prompt.txt").read_text()
ESC_PANE = (FIXTURES / "busy-spinner.txt").read_text()
MSG = "msg_" + "a" * 32
HASH = "sha256:" + "b" * 64

# The stand-in's status() is the real one's shape: `status --json` prints it.
STANDIN = '''"""A stand-in for compaction-slot.py: status() returns answer.json beside it."""
import json
from pathlib import Path


def status():
    answer = json.loads(Path(__file__).with_name("answer.json").read_text())
    if "raise" in answer:
        raise RuntimeError(answer["raise"])
    return 0, answer
'''
FREE = {"state": "free", "lease": None, "last": None, "load_line": 40.0, "load1": 1.0}


def held(fleet="tf", bot="mgr", left=412.5):
    return {"state": "held", "last": None, "load_line": 40.0, "load1": 1.0,
            "lease": {"lease": "0123456789abcdef", "fleet": fleet, "bot": bot,
                      "taken": 1_800_000_000.0, "ends": 1_800_000_600.0,
                      "boot_id": "boot-a", "pid": 4242, "left_s": left}}


def _native(tmp_path: Path, answer=None, *, slot=True) -> Path:
    """The release's scripts, each a link to this tree's, with the stand-in slot
    in place of compaction-slot.py; slot=False leaves the slot out."""
    native = tmp_path / "native"
    native.mkdir()
    for script in LIB_DIR.iterdir():
        if script.name != "compaction-slot.py":
            (native / script.name).symlink_to(script)
    if slot:
        (native / "compaction-slot.py").write_text(STANDIN)
        (native / "answer.json").write_text(json.dumps(FREE if answer is None else answer))
    return native


def _answer(native: Path, answer) -> None:
    (native / "answer.json").write_text(json.dumps(answer))


def _python_witness(tmp_path: Path) -> tuple[Path, Path]:
    """A python3 first on PATH that logs each call and runs the real one."""
    shim, log = tmp_path / "shim", tmp_path / "python3.log"
    shim.mkdir()
    log.write_text("")
    real = shutil.which("python3")
    assert real
    (shim / "python3").write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{log}"\nexec "{real}" "$@"\n')
    (shim / "python3").chmod(0o755)
    return shim, log


def _slot_asked(log: Path) -> bool:
    return "compaction-lease (#1053)" in log.read_text()


def _lib(native: Path, call: str, **env) -> subprocess.CompletedProcess:
    """`call` in bash with the native lib-common sourced; its rc is the call's."""
    return subprocess.run(["bash", "-c", f'. "{native}/lib-common.sh"; set +e; {call}'],
                          env=constructed_env(**env), capture_output=True, text=True, timeout=60)


# --- compaction_lease_left: the one reader -------------------------------------------


def test_a_live_lease_naming_the_bot_prints_its_seconds_left(tmp_path):
    native = _native(tmp_path, held())
    done = _lib(native, "compaction_lease_left tf mgr")
    assert (done.returncode, done.stdout, done.stderr) == (0, "412.5\n", "")


@pytest.mark.parametrize("answer", [held(bot="other"), held(fleet="other"), FREE,
                                    {**FREE, "last": held()["lease"] | {"ended": "released"}}],
                         ids=["another-bot", "same-bot-another-fleet", "free", "an-ended-lease"])
def test_no_live_lease_naming_the_bot_reads_nothing(tmp_path, answer):
    native = _native(tmp_path, answer)
    done = _lib(native, "compaction_lease_left tf mgr")
    assert (done.returncode, done.stdout, done.stderr) == (1, "", "")


def test_a_release_without_the_slot_never_starts_python(tmp_path):
    native = _native(tmp_path, slot=False)
    shim, log = _python_witness(tmp_path)
    done = _lib(native, "compaction_lease_left tf mgr", PATH=f"{shim}:{os.environ['PATH']}")
    assert (done.returncode, done.stdout, done.stderr) == (1, "", "")
    assert not _slot_asked(log)


@pytest.mark.parametrize("call", ['compaction_lease_left "" mgr', 'compaction_lease_left tf ""',
                                  "compaction_lease_left"])
def test_an_unnamed_bot_is_never_asked_about(tmp_path, call):
    native = _native(tmp_path, held())
    shim, log = _python_witness(tmp_path)
    done = _lib(native, call, PATH=f"{shim}:{os.environ['PATH']}")
    assert (done.returncode, done.stdout) == (1, "")
    assert not _slot_asked(log)


def test_a_slot_that_cannot_answer_reads_no_lease_and_says_why(tmp_path):
    native = _native(tmp_path, {"raise": "no host slot"})
    done = _lib(native, "compaction_lease_left tf mgr")
    assert (done.returncode, done.stdout) == (3, "")
    assert "compaction_lease_left: the compaction slot could not be read for tf/mgr" in done.stderr
    assert "RuntimeError: no host slot" in done.stderr


def test_a_python3_that_cannot_run_reads_no_lease_and_says_so(tmp_path):
    native = _native(tmp_path, held())
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "python3").write_text("#!/bin/sh\nexit 127\n")
    (broken / "python3").chmod(0o755)
    done = _lib(native, "compaction_lease_left tf mgr", PATH=f"{broken}:{os.environ['PATH']}")
    assert (done.returncode, done.stdout) == (3, "")
    assert "no answer from python3" in done.stderr


# --- bot_is_busy: an idle-looking pane under a live lease is busy --------------------


def _tmux_stub(tmp_path: Path, pane: str) -> tuple[Path, Path]:
    (tmp_path / "pane.txt").write_text(pane)
    witness = tmp_path / "tmux-calls.log"
    witness.write_text("")
    stub = tmp_path / "bin" / "tmux"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text("#!/bin/bash\n"
                    f'echo "$*" >> "{witness}"\n'
                    'for a in "$@"; do [ "$a" = "capture-pane" ] && '
                    f'{{ cat "{tmp_path / "pane.txt"}"; exit 0; }}; done\n'
                    "exit 0\n")
    stub.chmod(0o755)
    return stub, witness


def _bot(tmp_path: Path, fleet="tf", bot="mgr", conf_fleet="tf") -> Path:
    bot_dir = tmp_path / "local" / fleet / "runtime" / "bots" / bot
    (bot_dir / "data").mkdir(parents=True)
    (bot_dir / "bot.conf").write_text(f"export BOT_ID='{bot}'\nexport FLEET_NAME='{conf_fleet}'\n")
    return bot_dir


def _busy(tmp_path, native, pane, *, bot_dir=None, caller_fleet="tf", python_path=None):
    stub, witness = _tmux_stub(tmp_path, pane)
    path = f"{python_path}:{os.environ['PATH']}" if python_path else os.environ["PATH"]
    where = f' "{bot_dir}"' if bot_dir else ""
    done = _lib(native, f'bot_is_busy "com.example.tf.mgr" "mgr"{where}', TMUX_BIN=stub,
                CLAUDLOBBY_ROOT=tmp_path, FLEET_NAME=caller_fleet, PATH=path)
    return done, witness


def test_an_idle_pane_reads_busy_while_a_lease_names_the_bot(tmp_path):
    native = _native(tmp_path, held())
    done, witness = _busy(tmp_path, native, IDLE_PANE, bot_dir=_bot(tmp_path))
    assert done.returncode == 0, done.stderr
    assert "capture-pane" in witness.read_text()


def test_an_idle_pane_reads_idle_when_the_lease_names_another_bot(tmp_path):
    native = _native(tmp_path, held(bot="other"))
    done, _ = _busy(tmp_path, native, IDLE_PANE, bot_dir=_bot(tmp_path))
    assert (done.returncode, done.stderr) == (1, "")


def test_an_idle_pane_reads_idle_with_no_lease(tmp_path):
    native = _native(tmp_path, FREE)
    done, _ = _busy(tmp_path, native, IDLE_PANE, bot_dir=_bot(tmp_path))
    assert (done.returncode, done.stderr) == (1, "")


def test_the_bots_own_fleet_decides_not_the_callers(tmp_path):
    # A caller in another fleet asks about this bot: the lease names its fleet.
    native = _native(tmp_path, held(fleet="tf"))
    done, _ = _busy(tmp_path, native, IDLE_PANE, bot_dir=_bot(tmp_path), caller_fleet="ops")
    assert done.returncode == 0, done.stderr
    # And a lease taken under the caller's fleet is not this bot's.
    _answer(native, held(fleet="ops"))
    done, _ = _busy(tmp_path, native, IDLE_PANE, bot_dir=_bot(tmp_path / "b"), caller_fleet="ops")
    assert (done.returncode, done.stderr) == (1, "")


def test_a_bot_found_by_its_session_name_is_asked_about_under_its_fleet(tmp_path):
    native = _native(tmp_path, held())
    _bot(tmp_path)
    done, _ = _busy(tmp_path, native, IDLE_PANE)  # no bot_dir: resolved from "mgr"
    assert done.returncode == 0, done.stderr


def test_a_busy_pane_is_busy_without_asking_the_slot(tmp_path):
    native = _native(tmp_path, FREE)
    shim, log = _python_witness(tmp_path)
    done, _ = _busy(tmp_path, native, ESC_PANE, bot_dir=_bot(tmp_path), python_path=shim)
    assert done.returncode == 0
    assert not _slot_asked(log)


def test_a_fresh_tool_call_is_busy_without_asking_tmux_or_the_slot(tmp_path):
    native = _native(tmp_path, FREE)
    bot_dir = _bot(tmp_path)
    (bot_dir / "data" / ".last-tool-call").touch()
    shim, log = _python_witness(tmp_path)
    done, witness = _busy(tmp_path, native, IDLE_PANE, bot_dir=bot_dir, python_path=shim)
    assert done.returncode == 0
    assert witness.read_text() == ""
    assert not _slot_asked(log)


def test_a_slot_that_cannot_answer_leaves_the_pane_to_decide(tmp_path):
    native = _native(tmp_path, {"raise": "no host slot"})
    done, _ = _busy(tmp_path, native, IDLE_PANE, bot_dir=_bot(tmp_path))
    assert done.returncode == 1
    assert "the compaction slot could not be read for tf/mgr" in done.stderr


# --- the CLI's send: it waits for the lease to end, at most 60 s ---------------------


@pytest.fixture
def destination(tmp_path):
    root, sockets = tmp_path / "data", tmp_path / "s"
    root.mkdir()
    sockets.mkdir()
    return transport.TransportDestination(root, "tf", "com.test.mgr", "mgr", sockets)


class _Time:
    """A clock the waits move: each sleep advances it."""

    def __init__(self, events):
        self.now, self.sleeps, self.events = 1000.0, [], events

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.events.append(("sleep", seconds))
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    events = []
    fake = _Time(events)
    monkeypatch.setattr(transport, "_clock", fake.clock)
    monkeypatch.setattr(transport, "_sleep", fake.sleep)
    return fake


def _reads(events, answers):
    answers = iter(answers)

    def read(package, destination):
        events.append(("lease", destination.fleet, destination.session))
        return next(answers)
    return read


def _native_call(events):
    def runner(command, **kwargs):
        events.append(("send", command[-1]))
        return subprocess.CompletedProcess(
            command, 0, f"transport-v1\tinvoked\ntransport-v1\tresult\t0\t{HASH}\t42\n".encode(), b"")
    return runner


def test_a_send_waits_for_the_lease_to_end_then_sends_once(destination, clock, capsys):
    events = clock.events
    result = transport.send(source_package(), destination, message_id=MSG, body="hello",
                            runner=_native_call(events), lease=_reads(events, [300.0, 298.0, None]))
    assert result == transport.TransportOutcome("submitted", HASH, 42, 0)
    assert events == [("lease", "tf", "mgr"), ("sleep", 2), ("lease", "tf", "mgr"), ("sleep", 2),
                      ("lease", "tf", "mgr"), ("send", "mgr")]
    assert "waited 4 s for tf/mgr; its compaction has ended" in capsys.readouterr().err


def test_a_lease_that_outlives_the_wait_sends_after_60_s(destination, clock, capsys):
    # 200 reads of a live lease, then none: a wait with no limit stops there, 400 s in.
    events = clock.events
    result = transport.send(source_package(), destination, message_id=MSG, body="hello",
                            runner=_native_call(events), lease=_reads(events, [300.0] * 200 + [None]))
    assert result.status == "submitted"
    assert sum(clock.sleeps) == 60 and len(clock.sleeps) == 30
    assert [e[0] for e in events].count("lease") == 31
    assert events[-1] == ("send", "mgr") and [e[0] for e in events].count("send") == 1
    assert "waited 60 s for tf/mgr; its compaction is still in flight" in capsys.readouterr().err


def test_a_lease_about_to_end_is_waited_out_not_polled_past(destination, clock):
    events = clock.events
    transport.send(source_package(), destination, message_id=MSG, body="hello",
                   runner=_native_call(events), lease=_reads(events, [0.7, None]))
    assert clock.sleeps == [0.7]


def test_no_lease_sends_at_once(destination, clock, capsys):
    events = clock.events
    result = transport.send(source_package(), destination, message_id=MSG, body="hello",
                            runner=_native_call(events), lease=_reads(events, [None]))
    assert result.status == "submitted"
    assert events == [("lease", "tf", "mgr"), ("send", "mgr")]
    assert capsys.readouterr().err == ""


def test_every_send_asks_the_slot(destination, clock, monkeypatch):
    # The callers name no lease reader: send() must use compaction_lease itself.
    events = clock.events
    monkeypatch.setattr(transport, "compaction_lease", _reads(events, [5.0, None]))
    transport.send(source_package(), destination, message_id=MSG, body="hello",
                   runner=_native_call(events))
    assert [e[0] for e in events] == ["lease", "sleep", "lease", "send"]


def test_a_bad_call_is_refused_before_any_wait(destination, clock):
    events = clock.events
    with pytest.raises(ValueError):
        transport.send(source_package(), destination, message_id="msg_short", body="hello",
                       runner=_native_call(events), lease=_reads(events, [300.0]))
    assert events == []


# --- compaction_lease and the repair, through the real lib-common --------------------


def _package(native: Path):
    return replace(source_package(), native=native)


def test_compaction_lease_reads_the_slot_for_the_destinations_bot(tmp_path, destination, capsys):
    native = _native(tmp_path, held())
    assert transport.compaction_lease(_package(native), destination) == 412.5
    _answer(native, held(bot="other"))
    assert transport.compaction_lease(_package(native), destination) is None
    assert capsys.readouterr().err == ""
    _answer(native, {"raise": "no host slot"})
    assert transport.compaction_lease(_package(native), destination) is None
    err = capsys.readouterr().err
    assert "the compaction slot could not be read for tf/mgr (unreadable RuntimeError: no host slot)" in err


def test_the_cli_runs_the_reader_lib_common_runs(tmp_path):
    # One reader, two callers: the CLI runs the very text compaction_lease_left runs.
    done = _lib(LIB_DIR, 'printf "%s" "$_COMPACTION_LEASE_PY"')
    assert done.returncode == 0 and done.stdout == transport._COMPACTION_LEASE_PY


def test_a_release_without_the_slot_starts_no_process_for_a_send(tmp_path, destination):
    native = _native(tmp_path, slot=False)
    assert transport.compaction_lease(_package(native), destination,
                                      runner=lambda *a, **k: pytest.fail("a process started")) is None


def test_compaction_lease_reads_nothing_from_a_runner_that_fails(tmp_path, destination):
    native = _native(tmp_path, held())

    def times_out(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    assert transport.compaction_lease(_package(native), destination, runner=times_out) is None

    def answers(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, b"held soon\n", b"")
    assert transport.compaction_lease(_package(native), destination, runner=answers) is None

    def asks(command, **kwargs):
        # No shell and no native helper: the reader text, run with the data root.
        assert command[1:5] == ["-I", "-S", "-c", transport._COMPACTION_LEASE_PY]
        assert command[5:] == [str(native / "compaction-slot.py"), "tf", "mgr"]
        assert kwargs["env"]["CLAUDLOBBY_ROOT"] == str(destination.root)
        return subprocess.CompletedProcess(command, 0, b"held 30.0\n", b"")
    assert transport.compaction_lease(_package(native), destination, runner=asks) == 30.0


def test_another_bots_lease_does_not_delay_a_send(tmp_path, destination, monkeypatch):
    native = _native(tmp_path, held(bot="other"))
    monkeypatch.setattr(transport, "_sleep", lambda seconds: pytest.fail("a send waited"))
    events = []
    result = transport.send(_package(native), destination, message_id=MSG, body="hello",
                            runner=_native_call(events))
    assert result.status == "submitted" and events == [("send", "mgr")]


def test_the_repair_presses_nothing_while_a_lease_names_the_bot(tmp_path, destination):
    native = _native(tmp_path, held())
    outcome = transport.press_held_enter(_package(native), destination, message_id=MSG)
    assert outcome == transport.EnterRepairOutcome("compacting", False, None, None)


def test_the_repair_still_looks_when_the_lease_names_another_bot(tmp_path, destination):
    native = _native(tmp_path, held(bot="other"))
    outcome = transport.press_held_enter(_package(native), destination, message_id=MSG)
    # No tmux server answers for this destination, so the look reads nothing.
    assert (outcome.verdict, outcome.pressed) == ("unknown", False)
