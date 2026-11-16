"""#2924: the session handoff a reader checks, in both layouts clauDNA has saved.

clauDNA 0.35 saves each session's own R/sessions/<native session ID>/handoff.md,
R being the bot's composed CLAUDNA_STATE_DIR; earlier versions and other session
providers save the bot's .claude/session.md. A reader reads the session's own
file when it exists, and the per-directory file otherwise.
"""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess

import pytest

from claudlobby import bot_operations, handoff_file, native_session
from claudlobby.__main__ import main
from tests.conftest import constructed_env

REPO = Path(__file__).resolve().parents[1]
LIB_COMMON = REPO / "claudlobby/_runtime_scripts/lib-common.sh"


def _bot(tmp_path: Path) -> Path:
    bot = tmp_path / "bot"
    (bot / ".claude").mkdir(parents=True)
    (bot / "data").mkdir()
    return bot


def _launch(bot: Path) -> str:
    """The bot's current session, retained by the production launch owner."""
    mode, sid = native_session.launch(bot, "com.example.worker", bot / "tmp", bot.parent / "account")
    assert mode == "fresh"
    return sid


def _save(path: Path, moment: datetime | None = None) -> Path:
    moment = moment or datetime.now(timezone.utc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ncwd: /x\nlast_updated: {moment:%Y-%m-%dT%H:%M:%SZ}\nschema_version: 2\n---\ncontext\n")
    return path


def test_the_current_sessions_own_handoff_is_read_first(tmp_path):
    bot = _bot(tmp_path)
    sid = _launch(bot)
    legacy = _save(handoff_file.legacy(bot))
    assert legacy == bot / ".claude/session.md"
    assert handoff_file.resolve(bot) == legacy  # the session saved no file of its own yet
    own = _save(handoff_file.session_owned(bot, sid))
    # The composed CLAUDNA_STATE_DIR is <bot dir>/data/claudna (composer.CLAUDNA_STATE_SUBDIR).
    assert own == bot / "data/claudna/sessions" / sid / "handoff.md"
    assert handoff_file.resolve(bot) == own
    # #2927: the readers read it alone now, so a save beside it ends no wait.
    assert handoff_file.watch(bot) == [own]
    own.unlink()
    assert handoff_file.resolve(bot) == legacy
    assert handoff_file.watch(bot) == [own, legacy]  # a save can still land in either


@pytest.mark.parametrize("receipt", ["absent", "malformed"])
def test_a_bot_without_a_readable_receipt_reads_the_per_directory_file(tmp_path, capsys, receipt):
    """#2927 item 7: a receipt that is there and cannot be read reads as no receipt,
    so the per-directory file is read, and that is said on stderr, once a process.
    A bot with no receipt at all says nothing."""
    bot = _bot(tmp_path)
    legacy = handoff_file.legacy(bot)
    if receipt == "malformed":
        (bot / "data/native-session.json").write_text("{not json\n")
    rc, lines, err = _door(capsys, "watch", str(bot))
    assert (rc, lines) == (0, [str(legacy)])
    if receipt == "malformed":
        assert f"{bot}: the native session receipt cannot be read (native session receipt is invalid)" in err
        assert err.rstrip().endswith(f"{legacy} is read instead"), err
    else:
        assert err == ""
    assert handoff_file.current_session(bot) is None
    assert handoff_file.resolve(bot) == legacy
    assert handoff_file.watch(bot) == [legacy]
    assert capsys.readouterr().err == ""  # said once a process


@pytest.mark.parametrize("redirected", ["data/claudna", "data/claudna/sessions"])
def test_a_redirected_session_directory_is_refused_not_read_past(tmp_path, redirected):
    """A symlinked directory on the way could put another file under the session's
    name, so it is refused rather than read, or skipped for the per-directory file."""
    bot = _bot(tmp_path)
    sid = _launch(bot)
    _save(handoff_file.legacy(bot))
    elsewhere = tmp_path / "elsewhere"
    tail = handoff_file.session_owned(bot, sid).relative_to(bot / redirected)
    _save(elsewhere / tail)
    (bot / redirected).parent.mkdir(parents=True, exist_ok=True)
    (bot / redirected).symlink_to(elsewhere)
    with pytest.raises(handoff_file.HandoffFileError, match="not an owned directory"):
        handoff_file.resolve(bot)
    with pytest.raises(handoff_file.HandoffFileError, match="not an owned directory"):
        handoff_file.watch(bot)  # #2927: it decides by the same rule


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches a directory without its search bit")
@pytest.mark.parametrize("unsearchable", ["data/claudna", "data/claudna/sessions"])
def test_a_session_directory_that_cannot_be_searched_is_refused_by_every_reader(
        tmp_path, capsys, unsearchable):
    """#2927 item 6: a directory on the way to the session's own handoff that cannot be
    searched made the walk raise PermissionError. --check and the private door refused
    it, but the self restart's check, which bot handoff's verify also runs, let it out
    raw, where a redirected directory is refused. It refuses it the same way now."""
    bot = _bot(tmp_path)
    sid = _launch(bot)
    _save(handoff_file.legacy(bot))
    _save(handoff_file.session_owned(bot, sid))
    assert bot_operations._write_handoff_request(bot, {"requested_at": "2026-01-01T00:00:00Z"})
    (bot / unsearchable).chmod(0o600)
    try:
        with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned session handoff"):
            bot_operations._fresh_self_handoff(bot)
        assert bot_operations.handoff_state(bot)[:2] == ("unknown", "file_unverified")
        rc, lines, err = _door(capsys, "watch", str(bot))
        assert (rc, lines) == (3, []) and "the session handoff cannot be named" in err
    finally:
        (bot / unsearchable).chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches a directory without its search bit")
def test_the_sessions_own_directory_that_cannot_be_searched_is_not_refused(tmp_path, capsys):
    """#2935 item 8: the session's own directory is not refused when it cannot be searched,
    as the two above it are (the test above). Its handoff reads as absent, as if the session
    had saved none of its own, so every reader reads .claude/session.md: the self restart's
    check, --check and the private door. The hint, HandoffDirectoryRefused and the
    changelog say so."""
    bot = _bot(tmp_path)
    sid = _launch(bot)
    legacy = _save(handoff_file.legacy(bot))
    own = _save(handoff_file.session_owned(bot, sid))
    assert bot_operations._write_handoff_request(bot, {"requested_at": "2026-01-01T00:00:00Z"})
    own.parent.chmod(0o600)
    try:
        assert handoff_file.resolve(bot) == legacy
        assert handoff_file.watch(bot) == [own, legacy]  # as for a session with no file of its own
        assert bot_operations._fresh_self_handoff(bot) >= 0
        assert bot_operations.handoff_state(bot)[:2] == ("saved", "written_after_request")
        assert _door(capsys, "watch", str(bot)) == (0, [str(own), str(legacy)], "")
    finally:
        own.parent.chmod(0o700)


@pytest.mark.parametrize("data_dir", [
    pytest.param("cannot-search", marks=pytest.mark.skipif(
        os.geteuid() == 0, reason="root searches a directory without its search bit")),
    "redirected",
])
def test_a_data_directory_that_cannot_be_searched_or_is_redirected_is_not_refused(
        tmp_path, capsys, data_dir):
    """#2935 item 10: data/ itself is not refused, as data/claudna and its sessions/ are (the
    test two above). It holds the native-session receipt, which names the session. When
    data/ cannot be searched, or is a link to a directory elsewhere, that receipt cannot be
    read: the readers say so on stderr, name no session, and read .claude/session.md, as
    HandoffDirectoryRefused says. --check reads unknown (request_unreadable) for the data/ it
    cannot search, since it reads the request there first. Through the link it reads the
    request where the link points, which this test does not pin."""
    bot = _bot(tmp_path)
    sid = _launch(bot)
    legacy = _save(handoff_file.legacy(bot))
    _save(handoff_file.session_owned(bot, sid))
    assert bot_operations._write_handoff_request(bot, {"requested_at": "2026-01-01T00:00:00Z"})
    data = bot / "data"
    if data_dir == "redirected":
        elsewhere = tmp_path / "elsewhere"
        data.rename(elsewhere)
        data.symlink_to(elsewhere, target_is_directory=True)
    else:
        data.chmod(0o600)
    try:
        assert handoff_file.resolve(bot) == legacy
        assert handoff_file.watch(bot) == [legacy]
        assert bot_operations._fresh_self_handoff(bot) >= 0
        if data_dir == "cannot-search":
            assert bot_operations.handoff_state(bot)[:2] == ("unknown", "request_unreadable")
        rc, lines, err = _door(capsys, "watch", str(bot))
        assert (rc, lines) == (0, [str(legacy)]), (rc, lines, err)
        assert "the native session receipt cannot be read" in err, err
    finally:
        if data_dir == "cannot-search":
            data.chmod(0o700)


def test_a_symlinked_own_handoff_is_named_so_its_reader_refuses_it(tmp_path):
    bot = _bot(tmp_path)
    sid = _launch(bot)
    own = handoff_file.session_owned(bot, sid)
    own.parent.mkdir(parents=True)
    own.symlink_to(_save(tmp_path / "elsewhere.md"))
    _save(handoff_file.legacy(bot))  # fresh, and never read in its place
    assert handoff_file.resolve(bot) == own
    assert handoff_file.watch(bot) == [own]  # so the wait refuses it (#2927)
    with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned session handoff"):
        bot_operations._fresh_self_handoff(bot)


def test_a_session_handoff_is_named_only_by_a_native_session_id(tmp_path):
    with pytest.raises(ValueError, match="native session ID"):
        handoff_file.session_owned(_bot(tmp_path), "../../elsewhere")


def test_a_renewal_records_the_session_whose_handoff_the_fresh_one_resumes_from(tmp_path):
    bot = _bot(tmp_path)
    first = _launch(bot)
    account = bot.parent / "account"
    second = native_session.renew(bot, "com.example.worker", bot / "tmp", account)["session_id"]
    assert native_session.predecessor(bot, second) == first
    assert native_session.predecessor(bot, first) is None  # the record names one launch only
    legacy = _save(handoff_file.legacy(bot))
    assert handoff_file.resume(bot, second) == legacy  # the replaced session saved no file of its own
    own = _save(handoff_file.session_owned(bot, first))
    assert handoff_file.resume(bot, second) == own
    third = native_session.renew(bot, "com.example.worker", bot / "tmp", account)["session_id"]
    assert native_session.predecessor(bot, third) == second
    assert handoff_file.resume(bot, second) == legacy  # a later renewal replaced the record


def test_a_renewal_with_no_receipt_records_nothing(tmp_path):
    bot = _bot(tmp_path)
    sid = native_session.renew(bot, "com.example.worker", bot / "tmp", bot.parent / "account")["session_id"]
    assert not (bot / "data" / native_session.PREDECESSOR).exists()
    assert handoff_file.resume(bot, sid) == handoff_file.legacy(bot)


@pytest.mark.parametrize("record", [b"{not json\n", b'{"schema": 1}\n',
                                    b'{"schema": 1, "session_id": "x", "predecessor": "y"}\n'])
def test_a_malformed_renewal_record_is_refused(tmp_path, record):
    bot = _bot(tmp_path)
    sid = _launch(bot)
    (bot / "data" / native_session.PREDECESSOR).write_bytes(record)
    with pytest.raises(native_session.NativeSessionError, match="predecessor record is invalid"):
        handoff_file.resume(bot, sid)


def _door(capsys, *argv):
    rc = main(["_handoff-files", *argv])
    out = capsys.readouterr()
    return rc, out.out.splitlines(), out.err


def test_the_private_door_prints_the_files_one_per_line(tmp_path, capsys):
    bot = _bot(tmp_path)
    sid = _launch(bot)
    own = handoff_file.session_owned(bot, sid)
    assert _door(capsys, "watch", str(bot))[:2] == (0, [str(own), str(handoff_file.legacy(bot))])
    second = native_session.renew(bot, "com.example.worker", bot / "tmp", bot.parent / "account")["session_id"]
    _save(own)
    assert _door(capsys, "resume", str(bot), second)[:2] == (0, [str(own)])
    assert _door(capsys, "recent", str(bot))[:2] == (0, [])  # the new session saved nothing yet
    _save(handoff_file.session_owned(bot, second))
    assert _door(capsys, "watch", str(bot))[:2] == (0, [str(handoff_file.session_owned(bot, second))])
    rc, lines, _ = _door(capsys, "recent", str(bot))
    assert rc == 0 and len(lines) == 1 and 0 <= int(lines[0]) < 300
    _save(handoff_file.session_owned(bot, second), datetime.now(timezone.utc) - timedelta(hours=2))
    assert _door(capsys, "recent", str(bot))[:2] == (0, [])


@pytest.mark.parametrize("argv", [("watch", "relative/bot"), ("resume", "/abs/bot"),
                                  ("watch", "/abs/bot", "11111111-1111-4111-8111-111111111111")])
def test_the_private_door_refuses_a_malformed_call(capsys, argv):
    assert _door(capsys, *argv)[:2] == (2, [])


def test_the_private_door_refuses_without_printing_a_path(tmp_path, capsys):
    bot = _bot(tmp_path)
    sid = _launch(bot)
    (bot / "data" / native_session.PREDECESSOR).write_bytes(b"{not json\n")
    rc, lines, err = _door(capsys, "resume", str(bot), sid)
    assert (rc, lines) == (3, []) and "predecessor record is invalid" in err


def _shell(tmp_path, *args, cli=None):
    env = constructed_env(PATH="/usr/bin:/bin", HOME=str(tmp_path), TMPDIR=str(tmp_path))
    if cli is not None:
        env["CLAUDLOBBY_CLI"] = str(cli)
    return subprocess.run(["/bin/bash", "-c", '. "$1"; shift; handoff_files "$@"', "_", str(LIB_COMMON), *args],
                          env=env, capture_output=True, text=True, timeout=60)


def test_the_shell_door_asks_the_cli_and_falls_back_to_the_per_directory_file(tmp_path, test_cli):
    """A managed installation has no interpreter beside its CLI that imports the
    package, so the scripts ask the CLI's private door. With no CLI, or no answer,
    they read the one file they read before."""
    bot = _bot(tmp_path)
    sid = _launch(bot)
    legacy = str(handoff_file.legacy(bot))
    asked = _shell(tmp_path, "watch", str(bot), cli=test_cli)
    assert asked.returncode == 0, asked.stderr
    assert asked.stdout.splitlines() == [str(handoff_file.session_owned(bot, sid)), legacy]
    own = _save(handoff_file.session_owned(bot, sid))
    alone = _shell(tmp_path, "watch", str(bot), cli=test_cli)
    assert (alone.returncode, alone.stdout.splitlines()) == (0, [str(own)]), alone.stderr
    own.unlink()
    without = _shell(tmp_path, "watch", str(bot))
    assert (without.returncode, without.stdout.splitlines(), without.stderr) == (0, [legacy], "")
    unanswered = _shell(tmp_path, "watch", str(bot), cli=tmp_path / "no-such-cli")
    assert unanswered.stdout.splitlines() == [legacy]
    assert "no answer from _handoff-files watch" in unanswered.stderr


def test_the_shell_door_never_passes_on_a_line_that_is_not_a_path(tmp_path):
    bot = _bot(tmp_path)
    fake = tmp_path / "fake-cli"
    fake.write_text("#!/bin/sh\nprintf '%s\\n' /abs/one 'not a path'\n")
    fake.chmod(0o755)
    shown = _shell(tmp_path, "watch", str(bot), cli=fake)
    assert shown.stdout.splitlines() == [str(handoff_file.legacy(bot))]
