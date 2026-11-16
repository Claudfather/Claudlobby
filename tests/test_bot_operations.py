"""Selected bot placement remains readable after deliberate de-enrollment."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import socket
import stat
import subprocess
import sys
from types import SimpleNamespace
import time

import pytest

from claudlobby import bot_operations, context, active_config, handoff_file, native_session
from claudlobby.__main__ import main
from claudlobby.supervision_runtime import RuntimeEvidenceError
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.config_units import current_git_declarations
from tests.conftest import constructed_env, fake_tmux_input_box
from tests.package_fixtures import source_package
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401
from tests.test_self_restart_handoff import _checkpointed, _stamp
from tests.test_git_composition import candidate  # noqa: F401 — lightweight Git composition fixture


def test_private_control_uses_one_exact_native_send(tmp_path):
    """Exercise the shell adapter with fake tmux; no live session is touched."""
    native = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/supervisor.sh"
    private = tmp_path / "native"
    private.mkdir()
    (private / "lib-common.sh").write_text("""
tmux_session_name() { printf 'worker'; }
bot_tmux() { printf 'tmux %s\\n' "$*" >> "$CONTROL_LOG"; }
pane_send_key() { printf 'key %s\\n' "$*" >> "$CONTROL_LOG"; }
pane_send_verified() {
    printf 'pane %s %s %s ticks=%s\\n' "$1" "$2" "$3" "$PANE_SEND_VERIFY_TICKS" >> "$CONTROL_LOG"
}
""")
    log = tmp_path / "calls"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "_SUPERVISOR_LIB_DIR": str(private), "CONTROL_LOG": str(log),
           "PLANE_EMIT_DISABLED": "1"}
    script = '. "$1"; svc_bot_session_observe() { printf "ready\\n"; }; svc_bot_control_exact "$2" "$3" "$4" "$5"'
    def run(control):
        return subprocess.run(["/bin/bash", "-c", script, "control", str(native),
                               str(tmp_path / "bot"), "worker.socket", str(tmp_path), control],
                              env=env, capture_output=True, text=True, timeout=5)

    interrupt = run("interrupt")
    assert interrupt.returncode == 0 and interrupt.stdout == "control-submitted\n"
    # #2036: the Escape is a keystroke into the pane, so it takes the pane's send
    # lock through pane_send_key, never a bare send-keys.
    assert log.read_text() == "key worker.socket worker Escape interrupt\n"
    log.unlink()
    compact = run("compact")
    assert compact.returncode == 0 and compact.stdout == "control-submitted\n"
    assert log.read_text() == "pane worker.socket worker /compact ticks=0\n"
    log.unlink()
    refused = run("message")
    assert refused.returncode == 3 and refused.stdout == "" and not log.exists()


@pytest.mark.parametrize("message, mode, expected_rc", [
    ("no server running on", "retired", 0),
    ("missing endpoint", "retired", 0),
    ("missing endpoint", "refuse", 3),
    ("no server running on", "refuse", 3),
    ("no server running on", "retired-purge", 3),
    ("permission denied on", "retired", 3),
    ("wrong socket", "retired", 3),
])
@pytest.mark.parametrize("endpoint", ["absent", "stale", "file", "listening"])
def test_retired_private_server_handles_stale_socket_without_hiding_errors(tmp_path, message, mode, expected_rc, endpoint):
    native = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/supervisor.sh"
    private = tmp_path / "native"
    private.mkdir()
    (private / "lib-common.sh").write_text('''
tmux_session_name() { printf 'worker'; }
tmux_socket_for_bot() { printf 'worker.socket'; }
bot_tmux() {
    [ "$2" = list-sessions ] || exit 99
    if [ "$FAILURE" = 'missing endpoint' ]; then
        printf 'error connecting to %s/tmux-%s/worker.socket (No such file or directory)\\n' "$SOCKET_ROOT" "$(id -u)" >&2
        return 1
    fi
    printf '%s %s/tmux-%s/worker.socket\\n' "$FAILURE" "$SOCKET_ROOT" "$(id -u)" >&2
    return 1
}
''')
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "_SUPERVISOR_LIB_DIR": str(private), "FAILURE": message, "PLANE_EMIT_DISABLED": "1",
           "SOCKET_ROOT": str(tmp_path), "CLAUDLOBBY_NATIVE_PYTHON": sys.executable}
    endpoint_path = tmp_path / f"tmux-{os.getuid()}" / "worker.socket"
    endpoint_path.parent.mkdir()
    listener = None
    if endpoint in {"stale", "listening"}:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(endpoint_path))
        if endpoint == "stale":
            listener.close()
            listener = None
        else:
            listener.listen(1)
    elif endpoint == "file":
        endpoint_path.write_text("foreign non-socket node")
    if endpoint in {"file", "listening"}:
        expected_rc = 3
    if message == "wrong socket":
        env.update(FAILURE="no server running on", SOCKET_ROOT=str(tmp_path / "other"))
    try:
        result = subprocess.run(["/bin/bash", "-c", '. "$1"; svc_activation_stop_private_server "$2" worker.socket "$3" "$4"',
                                 "stop", str(native), str(tmp_path / "bot"), str(tmp_path), mode],
                                env=env, capture_output=True, text=True, timeout=5)
    finally:
        if listener is not None:
            listener.close()
    assert result.returncode == expected_rc
    if mode == "retired-purge":
        assert "liveness is unverified" in result.stderr


@pytest.mark.parametrize("stdout, attempted", [("", False), ("effect-attempted\n", True)])
def test_disenroll_native_phase_is_reported_honestly(stdout, attempted, tmp_path, monkeypatch):
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import bot_runtime

    class Native:
        def call(self, function, *args, timeout=30):
            assert function == "svc_bot_disenroll_exact"
            return subprocess.CompletedProcess([function], 3, stdout,
                                               "activation supervision unknown: caller ancestry")

    with pytest.raises(bot_operations.BotLifecycleError) as error:
        bot_operations._native(Native(), "svc_bot_disenroll_exact", "source", "installed")
    assert error.value.effect_attempted is attempted
    assert ("after beginning" if attempted else "before a native effect") in str(error.value)

    monkeypatch.setattr(context, "resolve_paths", lambda **kwargs: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(bot_operations, "set_bot_running", lambda **kwargs: (_ for _ in ()).throw(
        bot_operations.BotLifecycleError(str(error.value), effect_attempted=attempted,
                                         unavailable=True, release_id="selected", target="gui/501/worker")))
    args = SimpleNamespace(seed=False, bot_id="worker", public_command="bot.stop",
                           ceiling=None, fleet="example", root=tmp_path)
    with pytest.raises(CommandFailure) as public:
        bot_runtime.dispatch(args)
    assert public.value.data["native_outcome"] == ("unknown" if attempted else "unattempted")
    if not attempted:
        assert "before a native effect" in public.value.error.message


def test_lifecycle_lock_contention_is_a_retryable_conflict(tmp_path, monkeypatch):
    from claudlobby.command_result import CommandFailure
    from claudlobby.commands import bot_runtime

    (tmp_path / "state").mkdir(exist_ok=True)
    with bot_operations._operation_lock(tmp_path):
        with pytest.raises(bot_operations.BotLifecycleError) as busy:
            with bot_operations._operation_lock(tmp_path):
                pytest.fail("second lifecycle lock was granted")
    assert busy.value.busy and not busy.value.effect_attempted
    with bot_operations._operation_lock(tmp_path):
        pass

    monkeypatch.setattr(context, "resolve_paths", lambda **kwargs: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(bot_operations, "set_bot_running", lambda **kwargs: (_ for _ in ()).throw(busy.value))
    args = SimpleNamespace(seed=False, bot_id="worker", public_command="bot.restart",
                           ceiling=None, fleet="example", root=tmp_path)
    with pytest.raises(CommandFailure) as public:
        bot_runtime.dispatch(args)
    assert public.value.error.code == "conflict" and public.value.error.retryable
    assert "another bot lifecycle operation" in public.value.error.message
    assert public.value.data["native_outcome"] == "unattempted"


def test_current_bot_placement_survives_removal_of_installed_unit(git_bot_binding):
    case=git_bot_binding
    binding=bot_operations._current_bot_binding(case.root,case.current,case.destination,"lead",case.adapter)
    installed=Path(binding.entry["installed"])
    installed.write_bytes(binding.declaration.source.read_bytes())
    installed.unlink()
    assert bot_operations._current_bot_binding(case.root,case.current,case.destination,"lead",case.adapter).entry==binding.entry


def test_self_restart_requires_fresh_owned_frontmatter(tmp_path):
    bot_dir = tmp_path / "bot"
    handoff_dir = bot_dir / ".claude"
    handoff_dir.mkdir(parents=True)
    handoff = handoff_dir / "session.md"
    now = datetime.now(timezone.utc)
    handoff.write_text(f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
    bot_operations._fresh_self_handoff(bot_dir)
    offset = now.astimezone(timezone(timedelta(hours=-4))).isoformat(timespec="seconds")
    handoff.write_text(f'---\nlast_updated: "{offset}"\n---\ncontext\n')
    bot_operations._fresh_self_handoff(bot_dir)
    handoff.write_text(f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%S}\n---\ncontext\n")
    with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned"):
        bot_operations._fresh_self_handoff(bot_dir)
    handoff.write_text("---\nlast_updated: 2020-01-01T00:00:00Z\n---\ncontext\n")
    with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned"):
        bot_operations._fresh_self_handoff(bot_dir)
    with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned"):
        bot_operations._fresh_self_handoff(bot_dir, observed_capture=True)
    future = now + timedelta(minutes=7)
    handoff.write_text(f"---\nlast_updated: {future:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
    bot_operations._fresh_self_handoff(bot_dir)
    handoff.unlink()
    foreign = tmp_path / "foreign.md"
    foreign.write_text(f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
    handoff.symlink_to(foreign)
    with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned"):
        bot_operations._fresh_self_handoff(bot_dir)


def test_a_late_handoff_is_read_against_the_request_the_door_recorded(tmp_path):
    """#963/#990: the door waits 30 s and a handoff takes minutes. Saved means
    written after the recorded request, by a capture that says so."""
    bot_dir = tmp_path / "bot"
    (bot_dir / ".claude").mkdir(parents=True)
    (bot_dir / "data").mkdir()
    handoff = bot_dir / ".claude/session.md"

    def stamp(epoch):
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def write(text, at):
        handoff.write_text(text)
        os.utime(handoff, (at, at))

    now = int(time.time()) + 0.5  # mid-second, so each stamp reads the same second
    asked = now - 600  # the door asked ten minutes ago
    older = f"---\nlast_updated: {stamp(asked - 3600)}\n---\nolder context\n"
    write(older, asked - 3600)
    assert bot_operations.handoff_state(bot_dir)[:2] == ("unknown", "no_request_recorded")
    # Old second-resolution receipts remain readable after the shared writer changes.
    (bot_dir / "data/.handoff-request").write_text(json.dumps({"requested_at": stamp(asked)}))
    # Not written since: also what a turn that ends without writing it leaves (#963).
    assert bot_operations.handoff_state(bot_dir)[:2] == ("pending", "not_written_since_request")
    handoff.unlink()
    assert bot_operations.handoff_state(bot_dir)[:2] == ("pending", "not_written_since_request")

    write(f"---\nlast_updated: {stamp(now)}\n---\nnew context\n", now)
    saved = bot_operations.handoff_state(bot_dir)
    assert saved[:2] == ("saved", "written_after_request")
    assert (saved.requested_at, saved.written_at) == (stamp(asked), stamp(now))

    # Activation's reference refresh rewrites the file over the old capture.
    handoff.write_bytes(bot_operations._refresh_envelope(bot_dir, stamp(now)) + older.encode())
    assert bot_operations.handoff_state(bot_dir)[:2] == ("unknown", "capture_predates_request")
    foreign = tmp_path / "foreign.md"
    foreign.write_text(f"---\nlast_updated: {stamp(now)}\n---\nnew context\n")
    handoff.unlink()
    handoff.symlink_to(foreign)
    assert bot_operations.handoff_state(bot_dir)[:2] == ("unknown", "file_unverified")
    (bot_dir / "data/.handoff-request").write_text("not a record\n")
    assert bot_operations.handoff_state(bot_dir)[:2] == ("unknown", "request_unreadable")


def _worker_bot_dir(tmp_path):
    bot_dir = tmp_path / "runtime/bots/worker"
    (bot_dir / ".claude").mkdir(parents=True)
    (bot_dir / "bot.conf").write_text("BOT_ID=worker\nBOT_NAME=worker\nFLEET_NAME=example\n"
                                       "BOT_SERVICE=com.example.worker\nTMUX_SOCKET=com.example.worker\n")
    return bot_dir


def _pre_stop_handoff(tmp_path, bot_dir, *mode, cli=None, **extra_env):
    script = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/pre-stop-handoff.sh"
    env = constructed_env(PATH="/usr/bin:/bin", HOME=tmp_path, TMPDIR=tmp_path, CLAUDLOBBY_ROOT=tmp_path)
    if cli:
        env["CLAUDLOBBY_CLI"] = str(cli)
    env.update(extra_env)
    return subprocess.run(["/bin/bash", str(script), str(bot_dir), *mode],
                          env=env, capture_output=True, text=True, timeout=60)


def test_explicit_handoff_keeps_running_session_env_while_stop_cleans_it(tmp_path, test_cli):
    bot_dir = _worker_bot_dir(tmp_path)
    _checkpointed(bot_dir, refreshed=datetime.now(timezone.utc), captured=datetime.now(timezone.utc), field=None)
    secret_env = bot_dir / ".tmux-env"
    secret_env.write_text("private launch values\n")
    explicit = _pre_stop_handoff(tmp_path, bot_dir, "--explicit", cli=test_cli)
    assert explicit.returncode == 0, explicit.stderr
    assert explicit.stdout.strip() == "handoff-skipped:recent"
    assert secret_env.read_text() == "private launch values\n"
    stopping = _pre_stop_handoff(tmp_path, bot_dir, cli=test_cli)
    assert stopping.returncode == 0, stopping.stderr
    assert not secret_env.exists()
    # A handoff older than 300 s and no session to send one to: that explicit
    # exit keeps the file as well. The stop above, on the fresh handoff, took
    # its early exit and removed the file, so it is written again.
    secret_env.write_text("private launch values\n")
    written = datetime.now(timezone.utc).timestamp() - 400
    os.utime(bot_dir / ".claude/session.md", (written, written))
    no_session = _pre_stop_handoff(tmp_path, bot_dir, "--explicit")
    assert no_session.returncode == 0, no_session.stderr
    assert no_session.stdout.strip() == "handoff-skipped:no-session"
    assert secret_env.exists(), "the explicit no-session exit deleted the launch-time secret file"
    assert secret_env.read_text() == "private launch values\n"


def _handoff_below_envelope(tmp_path, envelope, capture_age, written_age):
    """A worker whose handoff, written `written_age` seconds ago, holds
    activation's reference refresh envelope (#2094; `envelope` names its time
    field, None writes none) above a capture `capture_age` seconds old, or above
    text with no frontmatter when `capture_age` is None."""
    bot_dir = _worker_bot_dir(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if capture_age is None:
        handoff = bot_dir / ".claude/session.md"
        handoff.write_bytes(bot_operations._refresh_envelope(bot_dir, _stamp(now), field=envelope)
                            + b"older context, no frontmatter\n")
    else:
        handoff, _ = _checkpointed(bot_dir, refreshed=now, captured=now - timedelta(seconds=capture_age),
                                   field=envelope)
    written = now.timestamp() - written_age
    os.utime(handoff, (written, written))
    return bot_dir


@pytest.mark.parametrize("envelope, capture_age, written_age, with_cli, recent", [
    pytest.param("references_refreshed", 10, 0, True, True, id="envelope-capture-10s"),
    pytest.param("references_refreshed", 400, 0, True, False, id="envelope-capture-400s"),
    pytest.param("references_refreshed", 10, 400, True, False, id="envelope-capture-10s-written-400s-ago"),
    pytest.param("references_refreshed", None, 0, True, False, id="envelope-only"),
    pytest.param("last_updated", None, 0, True, False, id="pre-2094-envelope-only"),
    pytest.param("references_refreshed", 10, 0, False, False, id="no-reader"),
    pytest.param(None, 400, 0, True, False, id="unmarked-written-now"),
    pytest.param(None, 306, 243, True, False, id="ordinary-mtime243s-capture306s"),
    pytest.param(None, 10, 0, True, True, id="ordinary-fresh-capture"),
    pytest.param(None, 10, 0, False, False, id="ordinary-no-reader"),
    pytest.param(None, 10, 400, True, False, id="unmarked-written-400s-ago"),
])
def test_explicit_handoff_is_recent_by_the_capture_below_the_refresh_envelope(
        tmp_path, test_cli, envelope, capture_age, written_age, with_cli, recent):
    """#2119 item 9(a): below activation's reference refresh, the capture's own
    last_updated decides, read by the self-restart check (#2123), so a handoff
    is recent when a self restart would accept it. An unmarked handoff uses the same capture verifier.
    A capture that does not parse, or no reader, is
    not recent."""
    bot_dir = _handoff_below_envelope(tmp_path, envelope, capture_age, written_age)
    explicit = _pre_stop_handoff(tmp_path, bot_dir, "--explicit", cli=test_cli if with_cli else None)
    assert explicit.returncode == 0, explicit.stderr
    # No session runs here: a handoff not read as recent would be sent to one.
    assert explicit.stdout.strip() == ("handoff-skipped:recent" if recent
                                       else "handoff-skipped:no-session"), explicit.stderr


@pytest.mark.parametrize("own_age, per_directory, recent", [
    pytest.param(10, True, True, id="own-fresh-per-directory-stale"),
    pytest.param(400, True, False, id="own-stale-per-directory-fresh"),
    pytest.param(10, False, True, id="own-fresh-no-per-directory-file")])
def test_explicit_handoff_is_recent_by_the_current_sessions_own_handoff(
        tmp_path, test_cli, own_age, per_directory, recent):
    """#2924: the recent check reads the file the current session saved, its own
    (clauDNA 0.35) when it has one, through the CLI's private door. At main it read
    .claude/session.md alone, so each case read the other way. #2927: in the first
    two cases the per-directory file, written now, passes the script's own pre-check
    (a file written in the last 300 s) before the door is asked; with no such file
    only the session's own file can pass it."""
    bot_dir = _worker_bot_dir(tmp_path)
    (bot_dir / "data").mkdir()
    _, sid = native_session.launch(bot_dir, "com.example.worker", tmp_path / "tmp", tmp_path / "account")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if per_directory:
        _checkpointed(bot_dir, refreshed=now, captured=now - timedelta(seconds=7200 if recent else 10),
                      field=None)
    assert handoff_file.legacy(bot_dir).exists() == per_directory
    own = handoff_file.session_owned(bot_dir, sid)
    own.parent.mkdir(parents=True)
    own.write_text(f"---\nlast_updated: {_stamp(now - timedelta(seconds=own_age))}\n---\ncontext\n")
    explicit = _pre_stop_handoff(tmp_path, bot_dir, "--explicit", cli=test_cli)
    assert explicit.returncode == 0, explicit.stderr
    # No session runs here: a handoff not read as recent would be sent to one.
    assert explicit.stdout.strip() == ("handoff-skipped:recent" if recent
                                       else "handoff-skipped:no-session"), explicit.stderr


def _pane_that_saves(tmp_path, save):
    """The bot's pane is the shared idle input box (conftest.fake_tmux_input_box);
    the first Enter, which submits the handoff command, first runs the shell
    line `save`: the session's save, before the box empties."""
    box = fake_tmux_input_box(tmp_path / "box-tmux", tmp_path / "box")
    submitted = shlex.quote(str(tmp_path / "submitted"))
    tmux = tmp_path / "tmux"
    tmux.write_text("#!/bin/bash\n"
                    f'if [ "${{@: -1}}" = Enter ] && [ ! -e {submitted} ]; then : > {submitted}; {save}; fi\n'
                    f'exec {shlex.quote(str(box))} "$@"\n')
    tmux.chmod(0o755)
    return tmux


@pytest.mark.parametrize("own_at_request, saved_to, said", [
    pytest.param(True, "own", "handoff-saved", id="own-file-saved"),
    pytest.param(True, "per-directory", "handoff-timeout", id="saved-beside-the-own-file"),
    pytest.param(True, "own-symlink", "handoff-unavailable", id="own-file-redirected"),
    pytest.param(False, "own", "handoff-saved", id="first-own-file"),
    pytest.param(False, "per-directory", "handoff-saved", id="per-directory-file-saved"),
])
def test_the_wait_ends_on_a_save_in_the_file_the_readers_read(
        tmp_path, test_cli, own_at_request, saved_to, said):
    """#2927 item 3(c): the explicit handoff's wait loop over the watched files,
    driven through a fake pane. Once the session has its own file the readers read
    it alone, so the wait watches it alone: a save beside it in .claude/session.md
    runs the wait out (30 s), the way --check reads pending, where at main it ended
    the wait as a save. Until the session has a file of its own a save in either
    ends it, and a watched file replaced by a symlink is refused."""
    bot_dir = _worker_bot_dir(tmp_path)
    (bot_dir / "data").mkdir()
    _, sid = native_session.launch(bot_dir, "com.example.worker", tmp_path / "tmp", tmp_path / "account")
    legacy, own = handoff_file.legacy(bot_dir), handoff_file.session_owned(bot_dir, sid)
    hour_ago = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    for path in ([own] if own_at_request else []) + [legacy]:  # saved an hour ago: not recent
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nlast_updated: {_stamp(hour_ago)}\n---\nolder context\n")
        os.utime(path, (hour_ago.timestamp(), hour_ago.timestamp()))
    fresh = f"---\nlast_updated: {_stamp(datetime.now(timezone.utc))}\n---\nnew context\n"
    if saved_to == "own-symlink":
        elsewhere = tmp_path / "elsewhere.md"
        elsewhere.write_text(fresh)
        save = f"ln -sfn {shlex.quote(str(elsewhere))} {shlex.quote(str(own))}"
    else:
        target = own if saved_to == "own" else legacy
        save = (f"mkdir -p {shlex.quote(str(target.parent))} && "
                f"printf %s {shlex.quote(fresh)} > {shlex.quote(str(target))}")
    explicit = _pre_stop_handoff(
        tmp_path, bot_dir, "--explicit", cli=test_cli, TMUX_BIN=str(_pane_that_saves(tmp_path, save)),
        SESSION_HANDOFF_COMMAND="/handoff-save --auto",  # a bare command: unverifiable, so sent
        PANE_SEND_SETTLE_S="0", PANE_SEND_SHOWN_TICKS="2", PANE_SEND_VERIFY_TICKS="1")
    assert (tmp_path / "submitted").exists(), explicit.stderr
    assert explicit.stdout.strip() == said, explicit.stderr
    assert explicit.returncode == (0 if said == "handoff-saved" else 3), explicit.stderr


def test_the_recent_check_needs_no_interpreter_beside_the_cli(tmp_path, test_cli):
    """A managed installation's CLI is a launcher in INSTALL/bin with no interpreter
    beside it that imports the package (#2886), so the check asks the CLI's private
    door. At main it looked for that interpreter, found none, and never read a
    handoff as recent there (#2924)."""
    launcher = tmp_path / "install/bin/claudlobby"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(f'#!/bin/sh\nexec "{test_cli}" "$@"\n')
    launcher.chmod(0o755)
    assert not (launcher.parent / "python").exists(), "precondition: no interpreter beside it"
    bot_dir = _worker_bot_dir(tmp_path)
    now = datetime.now(timezone.utc)
    _checkpointed(bot_dir, refreshed=now, captured=now, field=None)
    explicit = _pre_stop_handoff(tmp_path, bot_dir, "--explicit", cli=launcher)
    assert explicit.returncode == 0, explicit.stderr
    assert explicit.stdout.strip() == "handoff-skipped:recent", explicit.stderr


def test_stop_skips_by_the_age_of_the_capture_below_the_refresh_envelope(tmp_path, test_cli):
    """The stop path names the capture's age, not the file's: written now, 30 s old."""
    bot_dir = _handoff_below_envelope(tmp_path, "references_refreshed", 30, 0)
    stopping = _pre_stop_handoff(tmp_path, bot_dir, cli=test_cli)
    assert stopping.returncode == 0, stopping.stderr
    said = stopping.stdout.strip()
    assert said.startswith("Recent handoff exists (") and said.endswith(" seconds old), skipping"), said
    assert 30 <= int(said.split("(", 1)[1].split()[0]) < 300, said


@pytest.mark.parametrize("stalled, late_witness", [
    pytest.param(False, False, id="False"),
    pytest.param(True, False, id="True"),
    pytest.param(False, True, id="False-late-witness"),
    pytest.param(True, True, id="True-late-witness"),
])
def test_self_restart_witness_survives_requesting_process_exit(tmp_path, stalled, late_witness):
    """The one-shot witness, not the dying pane, owns the final outcome.

    A late witness is descheduled for a second between its admission and its wait
    (#2137). Looking late at a caller that has exited, it still logs complete: the
    caller finished, so the restart is safe."""
    bot_dir = tmp_path / "bot"
    (bot_dir / ".claude").mkdir(parents=True)
    (bot_dir / "logs").mkdir()
    now = datetime.now(timezone.utc)
    (bot_dir / ".claude/session.md").write_text(
        f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
    code = """
import os
import sys
import time
from pathlib import Path
from claudlobby import bot_operations as b
root, bot_dir = map(Path, sys.argv[1:3])
stalled = sys.argv[3] == '1'
b._SELF_RESPONSE_WAIT_S = 0.05 if stalled else 30
if sys.argv[4] == '1':
    parent, real_select = os.getpid(), b.select.select
    def late_select(*args):
        if os.getpid() != parent:
            time.sleep(1)
        return real_select(*args)
    b.select.select = late_select
from contextlib import contextmanager
from types import SimpleNamespace
@contextmanager
def admitted(*args, **kwargs):
    yield SimpleNamespace(revision='a'*40, config_fingerprint='b'*64)
b.mutation_admission = admitted
b.RuntimeIdentity.current = staticmethod(lambda: object())
def restart(**kwargs):
    kwargs['_on_lock']()
    (bot_dir / 'native-effect').write_text('attempted')
    return b.BotLifecycleResult('fleet', 'bot', None, 'unit', 'running', True,
                                'session_ready', 'captured')
b.set_bot_running = restart
b._schedule_self_restart(root=root, fleet='fleet', bot='bot', ceiling=None,
                         bot_dir=bot_dir)
if stalled:
    # Stalled means still alive when the witness's wait ends, so stay until it
    # logs that verdict: a fixed sleep raced the witness's own scheduling (#2137).
    log, deadline = bot_dir / 'logs/startup.log', time.monotonic() + 8
    while time.monotonic() < deadline and not (
            log.exists() and '"status":"incomplete"' in log.read_text()):
        time.sleep(0.01)
os._exit(0)  # even an abrupt caller death releases the child to finish
"""
    process = subprocess.run([os.sys.executable, "-c", code, str(tmp_path), str(bot_dir),
                              "1" if stalled else "0", "1" if late_witness else "0"],
                             capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    log = bot_dir / "logs/startup.log"
    for _ in range(100):
        if log.exists() and ('"status":"incomplete"' if stalled else '"status":"complete"') in log.read_text():
            break
        time.sleep(0.02)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["status"] == ("incomplete" if stalled else "complete")
    if stalled:
        assert "requesting CLI did not finish" in rows[0]["reason"]
        assert not (bot_dir / "native-effect").exists()
    else:
        assert rows[0]["readiness"] == "session_ready"
        assert (bot_dir / "native-effect").read_text() == "attempted"


def _worker_cli(cold, monkeypatch, capsys, *, exec_start_pre=None):
    """The selected worker's bot CLI over a fake native adapter.

    ``exec_start_pre`` seals the worker's systemd unit with that
    ExecStartPre line, the way the compositor renders a boot stagger.
    """
    root, installation, plan, host = cold
    package = host.package
    # This helper starts as the local operator. Generated-origin cases below
    # supply their own complete carriers explicitly.
    for name in ("FLEET_NAME", "CLAUDLOBBY_FLEET", "BOT_ID", "BOT_NAME", "BOT_DIR",
                 "BOT_SERVICE", "FLEET_ROOT", "CLAUDLOBBY_ROOT"):
        monkeypatch.delenv(name, raising=False)
    # The shared fixture has already published generated Git units/config; this
    # helper adds only each test's native observations and optional boot stagger.
    if exec_start_pre is not None:
        snapshot=deepcopy(host.snapshot)
        unit=next(item for item in snapshot["units"] if item["source"].endswith("/com.example.worker.service"))
        source=Path(unit["source"])
        content=source.read_bytes().replace(b"[Service]\n",f"[Service]\nExecStartPre={exec_start_pre}\n".encode(),1)
        source.write_bytes(content)
        unit["sha256"]=hashlib.sha256(content).hexdigest()
        for output in snapshot["owned_outputs"]:
            if output["target"]==str(source):output["state"]["sha256"]=unit["sha256"]
        snapshot["fingerprint"]=active_config._fingerprint(snapshot)
        active_config.publish_current_config(snapshot,installation=installation,
                                             expected_previous=host.snapshot["fingerprint"])
        host.snapshot=snapshot
    monkeypatch.setattr(context, "get_resources", lambda: package)
    for declaration in current_git_declarations(host.snapshot,"Linux"):
        if declaration.scope=="bot":
            (host.directory/declaration.source.name).write_bytes(declaration.source.read_bytes())
            host.states[declaration.source.name]="enabled loaded active"

    class Native:
        def __init__(self):
            self.package = package
            self.session = "ready"
            self.actions = []
            self.calls = []
            self.handoff_rc = 0
            self.control_rc = 0
            self.explicit_handoff = "handoff-skipped:recent"
            # Where a saved handoff lands (#2924): "session" is the current session's own file.
            self.handoff_layout = "legacy"
            # What the explicit handoff script says on stderr, which the adapter captures (#2927).
            self.handoff_stderr = ""
            # A callable that runs the real pre-stop-handoff.sh for the explicit handoff (#2927).
            self.run_script = None
            self.readiness = "bridge-ready"
            self.fence_args = []
            self.target = "com.example.worker.service"
            self.manager = "Linux"
            self.enroll_needs_s = 0
            self.native_pid = 4242
            self.native_idle = True

        def call(self, function, *args, timeout=30):
            self.calls.append(function)
            target = self.target
            if function == "svc_activation_handoff" and self.run_script is not None:
                return self.run_script(*args)
            if function == "svc_inventory_catalog":
                value=f"manager\tLinux\ndirectory\t{host.directory}\n"
                value+="".join(f"installed\t{path.name}\n" for path in sorted(host.directory.iterdir()))
                value+="".join(f"loaded\t{name}\n" for name in sorted(host.states))
                return subprocess.CompletedProcess([function],0,value,"")
            if function == "svc_inventory_state":
                value = host.states.get(args[1], "not-found not-found inactive")
            elif function == "svc_bot_session_observe":
                value = self.session
            elif function == "svc_bot_native_session":
                value = f"{self.native_pid}\t1700000000\t{'idle' if self.native_idle else 'unready'}"
            elif function == "svc_activation_quiet":
                value = "inactive\tno-cgroup-witness"
            elif function == "svc_activation_bot_fence":
                self.fence_args.append(args)
                value = "0\tprivate-fence"
            elif function == "svc_activation_bot_ready":
                value = self.readiness
            elif function == "svc_activation_handoff":
                value = self.explicit_handoff if len(args) == 4 else ""
                if value == "handoff-saved":
                    handoff = Path(args[0]) / ".claude/session.md"
                    if self.handoff_layout == "session":
                        handoff = handoff_file.session_owned(
                            Path(args[0]), native_session.read(Path(args[0]))["session_id"])
                    handoff.parent.mkdir(parents=True, exist_ok=True)
                    # The real provider saved promptly but invented a future
                    # timestamp. Explicit native capture witnessed this write.
                    now = datetime.now(timezone.utc) + timedelta(minutes=7)
                    handoff.write_text(f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
            elif function == "svc_bot_control_exact":
                assert args[:2] == (root / "runtime/bots/worker", "com.example.worker")
                assert Path(args[2]).is_absolute()
                assert args[3] in {"interrupt", "compact"}
                value = "control-submitted" if self.control_rc == 0 else ""
            elif function == "svc_bot_disenroll_exact":
                self.actions.append("stop")
                Path(args[1]).unlink()
                host.states[target] = "not-found not-found inactive"
                value = ""
            elif function == "svc_bot_enroll_exact":
                self.actions.append("start")
                Path(args[1]).write_bytes(Path(args[0]).read_bytes())
                host.states[target] = ("unchanged loaded inactive" if self.manager == "Darwin"
                                       else "enabled loaded active")
                if timeout < self.enroll_needs_s:
                    # systemctl blocks through the unit's ExecStartPre: the
                    # adapter kills the client at its budget, the job runs on.
                    raise subprocess.TimeoutExpired([function], timeout)
                # The native launch callback confirms the exact retained UUID;
                # no Claude process or model is launched by this adapter.
                self.native_pid += 1
                from tests.test_native_session import retain_native_session
                retain_native_session(bot_directory, "com.example.worker", private_tmpdir, account, self)
                value = ""
            else:
                raise AssertionError(f"unexpected native call: {function}")
            return subprocess.CompletedProcess([function],
                                               self.handoff_rc if function == "svc_activation_handoff" else
                                               self.control_rc if function == "svc_bot_control_exact" else 0,
                                               value + "\n",
                                               self.handoff_stderr if function == "svc_activation_handoff" else "")

        def read(self, function, *args):
            return self.call(function, *args).stdout

    native = Native()
    monkeypatch.setattr(bot_operations, "Adapter", lambda _: native)
    bot_directory = root / "runtime/bots/worker"
    declaration = next(row for row in current_git_declarations(host.snapshot, "Linux")
                       if row.scope == "bot" and row.bot == "worker")
    selected, account = native_session._context(package, root, bot_directory, "com.example.worker")
    spec = bot_operations.build_supervision_spec(selected.fleet.bots["worker"], selected.fleet, selected.paths)
    private_tmpdir = Path(spec.environment["TMUX_TMPDIR"])
    monkeypatch.setattr(native_session, "require_provider", lambda _: None)
    from tests.test_native_session import retain_native_session
    retain_native_session(bot_directory, "com.example.worker", private_tmpdir, account, native)
    native.calls.clear()  # Fixture establishment is not an operator lifecycle action.

    def observed(_root, _declarations, _adapter, target, installed_path):
        assert target == native.target
        installed_rows = (SimpleNamespace(path=str(installed_path)),) if installed_path.exists() else ()
        active = host.states[target].split()[-1]
        return SimpleNamespace(installed=installed_rows, properties=(("ActiveState", active),))

    monkeypatch.setattr(bot_operations, "_observed", observed)
    monkeypatch.setattr(bot_operations, "assert_quiescent", lambda *a, **kw: None)
    # A real recent-skip is backed by an actual owned capture. Tests which
    # exercise missing/stale saves replace or remove it explicitly.
    checkpoint = root / "runtime/bots/worker/.claude/session.md"
    checkpoint.parent.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    checkpoint.write_text(f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")

    def call(*argv, expected=0, use_root=True):
        root_flag = ["--root", str(root)] if use_root else []
        actual = main([*root_flag, "--json", *argv])
        result = json.loads(capsys.readouterr().out)
        assert actual == expected, json.dumps(result, indent=2)
        assert result["ok"] is (expected == 0)
        return result
    return SimpleNamespace(root=root, installation=installation, plan=plan, host=host,
                           native=native, call=call, private_tmpdir=private_tmpdir, account=account)


@pytest.mark.parametrize("manager, active", [
    ("Linux", "active"), ("Linux", "inactive"), ("Darwin", "inactive"),
])
def test_restart_preserves_native_disabled_intent_before_any_effect(cold, monkeypatch, capsys, manager, active):
    worker = _worker_cli(cold, monkeypatch, capsys)
    native = worker.native
    native.manager = manager
    if manager == "Darwin":
        from claudlobby.operation_context import resolve_operation_scope
        destination,_=resolve_operation_scope(root=worker.root,fleet="example")
        original=bot_operations._current_bot_binding(worker.root,
            bot_operations.GitCurrentRuntime(worker.installation,worker.host.snapshot["revision"],
                worker.host.snapshot["fingerprint"],worker.host.snapshot),
            destination,"worker",native)
        native.target=f"user/{os.getuid()}/com.example.worker"
        monkeypatch.setattr(bot_operations,"_current_bot_binding",lambda *_a,**_k:replace(
            original,manager=manager,entry={**original.entry,"target":native.target}))
    worker.host.states[native.target] = f"disabled loaded {active}"
    def observed(_root, _declarations, _adapter, target, installed_path):
        assert target == native.target and installed_path.exists()
        key = "UnitFileState" if manager == "Linux" else "EnabledState"
        return SimpleNamespace(installed=(SimpleNamespace(path=str(installed_path)),),
                               properties=(("ActiveState", active), (key, "disabled")))
    monkeypatch.setattr(bot_operations, "_observed", observed)
    refused = worker.call("bot", "restart", "worker", expected=4)
    assert "disabled; use bot start" in refused["error"]["message"]
    assert native.actions == []
    assert set(native.calls) <= {"svc_inventory_catalog"}

    # Explicit start remains the deliberate enable/enroll door. An inactive
    # bot has no live context to checkpoint, and start may enroll it normally.
    if manager == "Linux" and active == "inactive":
        native.session = "absent"
        result = worker.call("bot", "start", "worker")["data"]
        assert result["changed"] is True and native.actions == ["start"]
        assert worker.host.states[native.target] == "enabled loaded active"


def test_public_bot_start_stop_decisions_use_selected_placement(cold, monkeypatch, capsys):  # noqa: F811
    worker = _worker_cli(cold, monkeypatch, capsys)
    root, installation, plan, host, native, call = (worker.root, worker.installation, worker.plan,
                                               worker.host, worker.native, worker.call)

    healthy = call("bot", "start", "worker")["data"]
    assert healthy["state"] == "running" and healthy["changed"] is False
    assert healthy["readiness"] == "current_session_ready" and native.actions == []

    native.calls.clear()
    skipped = call("bot", "handoff", "worker")["data"]
    assert skipped["handoff"] == "skipped" and skipped["reason"] == "recent"
    assert "svc_activation_handoff" in native.calls and native.actions == []
    for control in ("interrupt", "compact"):
        native.calls.clear()
        submitted = call("bot", control, "worker")["data"]
        assert submitted["outcome"] == "submitted" and submitted["control"] == control
        assert native.calls.index("svc_bot_session_observe") < native.calls.index("svc_bot_control_exact")
        assert native.actions == []
    native.control_rc = 3
    unknown_control = call("bot", "compact", "worker", expected=6)
    assert unknown_control["data"]["outcome"] == "unknown"
    assert "inspect" in unknown_control["error"]["hint"]
    native.control_rc = 0
    before_control = native.calls.count("svc_bot_control_exact")
    assert call("bot", "interrupt", "other", expected=4)["error"]["code"] == "conflict"
    assert native.calls.count("svc_bot_control_exact") == before_control
    # The same selected state must resolve identically for a generated manager
    # caller with an implicit fleet and one that spells out --fleet.
    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", "manager")
    monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots/manager"))
    monkeypatch.setenv("FLEET_ROOT", str(root))
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    monkeypatch.setenv("CLAUDLOBBY_INSTALLATION", str(installation.directory))
    implicit = call("bot", "handoff", "worker", use_root=False)["data"]
    explicit = call("--fleet", "example", "bot", "handoff", "worker", use_root=False)["data"]
    assert implicit == explicit == skipped
    before_refusal = native.calls.count("svc_activation_handoff")
    assert call("bot", "handoff", "manager", expected=4)["error"]["code"] == "conflict"
    monkeypatch.setenv("BOT_ID", "worker")
    monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots/worker"))
    assert call("bot", "handoff", "worker", expected=4)["error"]["code"] == "conflict"
    assert native.calls.count("svc_activation_handoff") == before_refusal
    assert call("bot", "compact", "worker", expected=4)["error"]["code"] == "conflict"
    assert native.calls.count("svc_bot_control_exact") == before_control
    for key in ("FLEET_NAME", "BOT_ID", "BOT_DIR", "FLEET_ROOT",
                "CLAUDLOBBY_ROOT"):
        monkeypatch.delenv(key)
    native.explicit_handoff = ""  # the old native rc0 is not proof of a handoff
    unknown = call("bot", "handoff", "worker", expected=6)
    assert unknown["error"]["code"] == "unavailable" and native.actions == []
    assert unknown["data"]["handoff"] == "unknown"
    assert unknown["data"]["reason"] == "unverified"
    before_retry = native.calls.count("svc_activation_handoff")
    pending = call("bot", "handoff", "worker", expected=6)
    assert pending["data"]["reason"] == "pending"
    assert native.calls.count("svc_activation_handoff") == before_retry
    # The first request genuinely completes before a distinct ordinary save.
    now = datetime.now(timezone.utc)
    _checkpointed(root / "runtime/bots/worker", refreshed=now, captured=now, field=None)
    native.explicit_handoff = "handoff-timeout"
    native.handoff_rc = 3
    timed_out = call("bot", "handoff", "worker", expected=6)
    assert timed_out["error"]["code"] == "unavailable"
    assert timed_out["data"]["handoff"] == "unknown"
    assert timed_out["data"]["reason"] == "timeout"
    assert "do not automatically resend" in timed_out["error"]["hint"]
    assert native.actions == []
    now = datetime.now(timezone.utc)
    _checkpointed(root / "runtime/bots/worker", refreshed=now, captured=now, field=None)
    native.handoff_rc = 0
    native.explicit_handoff = "handoff-saved"
    saved = call("bot", "handoff", "worker")["data"]
    assert saved["handoff"] == "saved" and saved["reason"] == "fresh_file_verified"
    assert native.actions == []  # a handoff never stops or re-enrolls the bot
    before_wrong = len(native.calls)
    assert call("bot", "handoff", "other", expected=4)["error"]["code"] == "conflict"
    assert len(native.calls) == before_wrong

    native.session = "unknown"
    assert call("bot", "start", "worker", expected=6)["error"]["code"] == "unavailable"
    assert native.actions == []

    native.session = "absent"  # exact active/exited unit with missing private socket
    recovered = call("bot", "start", "worker")["data"]
    assert recovered["changed"] is True and recovered["readiness"] == "bridge_ready"
    assert native.actions == ["start"]

    stopped = call("bot", "stop", "worker")["data"]
    assert stopped["state"] == "stopped" and stopped["changed"] is True
    assert native.actions == ["start", "stop"]
    restarted = call("bot", "start", "worker")["data"]
    assert restarted["state"] == "running" and restarted["changed"] is True
    assert native.actions == ["start", "stop", "start"]

    for observed_state in ("activating",):
        host.states[native.target] = f"enabled loaded {observed_state}"
        before = len(native.actions)
        for operation in ("start", "restart"):
            refused = call("bot", operation, "worker", expected=4)
            assert observed_state in refused["error"]["message"]
            assert "bot stop" in refused["error"]["message"]
        assert len(native.actions) == before
    host.states[native.target] = "enabled loaded active"

    for caller, target in (("worker", "manager"), ("manager", "manager")):
        monkeypatch.setenv("FLEET_NAME", "example")
        monkeypatch.setenv("BOT_ID", caller)
        monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
        monkeypatch.setenv("FLEET_ROOT", str(root))
        monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots" / caller))
        monkeypatch.setenv("CLAUDLOBBY_INSTALLATION", str(installation.directory))
        refused = call("bot", "stop", target, expected=4)
        assert refused["error"]["code"] == "conflict"
        assert native.actions == ["start", "stop", "start"]

    # launchd's selected launcher has exited, but its exact private tmux
    # session is still ready. Native inactive alone must not trigger a restart.
    for key in ("BOT_ID", "FLEET_NAME", "BOT_DIR", "FLEET_ROOT",
                "CLAUDLOBBY_ROOT"):
        monkeypatch.delenv(key)
    original_binding = bot_operations._current_bot_binding
    from claudlobby.operation_context import resolve_operation_scope
    destination,_=resolve_operation_scope(root=root,fleet="example")
    current_runtime=bot_operations.GitCurrentRuntime(installation,host.snapshot["revision"],
        host.snapshot["fingerprint"],host.snapshot)
    binding=original_binding(root,current_runtime,destination,"worker",native)
    native.manager="Darwin"
    native.target=f"gui/{os.getuid()}/com.example.worker"
    host.states[native.target]="unchanged loaded inactive"
    # Native tuple behavior is isolated here; exact Darwin domain/placement
    # admission uses the real current binding in the dedicated tests below.
    monkeypatch.setattr(bot_operations,"_current_bot_binding",lambda *_a,**_k:replace(
        binding,manager="Darwin",entry={**binding.entry,"target":native.target}))
    native.session = "ready"
    current = call("bot", "start", "worker")["data"]
    assert current["state"] == "running" and current["changed"] is False
    assert current["readiness"] == "current_session_ready"
    assert native.actions == ["start", "stop", "start"]

    host.states[native.target] = "unchanged unloaded inactive"
    assert call("bot", "start", "worker", expected=4)["error"]["code"] == "conflict"
    assert native.actions == ["start", "stop", "start"]
    host.states[native.target] = "unchanged loaded inactive"

    native.session = "unknown"
    assert call("bot", "start", "worker", expected=6)["error"]["code"] == "unavailable"
    assert native.actions == ["start", "stop", "start"]

    native.session = "ready"
    assert call("bot", "stop", "worker")["data"]["state"] == "stopped"
    started = call("bot", "start", "worker")["data"]
    assert started["changed"] is True and started["readiness"] == "bridge_ready"
    assert host.states[native.target] == "unchanged loaded inactive"
    assert native.actions == ["start", "stop", "start", "stop", "start"]

    # Restart is an intentional bounce even when launchd's old private
    # session is ready. It must not re-enroll a deliberately stopped bot.
    native.calls.clear()
    bounced = call("bot", "restart", "worker")["data"]
    assert bounced["state"] == "running" and bounced["changed"] is True
    assert bounced["readiness"] == "bridge_ready"
    assert bounced["handoff"] == "native_session"
    assert "svc_activation_handoff" not in native.calls
    assert native.calls.index("svc_bot_session_observe") < native.calls.index("svc_bot_native_session")
    assert native.calls.index("svc_bot_native_session") < native.calls.index("svc_activation_bot_fence")
    assert native.calls.index("svc_activation_bot_fence") < native.calls.index("svc_bot_enroll_exact")
    assert native.calls.index("svc_bot_enroll_exact") < native.calls.index("svc_activation_bot_ready")
    assert native.actions == ["start", "stop", "start", "stop", "start", "start"]
    assert call("bot", "stop", "worker")["data"]["state"] == "stopped"
    refused = call("bot", "restart", "worker", expected=4)
    assert refused["error"]["code"] == "conflict"
    assert native.actions == ["start", "stop", "start", "stop", "start", "start", "stop"]

    call("bot", "start", "worker")
    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", "manager")
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    monkeypatch.setenv("FLEET_ROOT", str(root))
    monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots/manager"))
    monkeypatch.setenv("CLAUDLOBBY_INSTALLATION", str(installation.directory))
    manager_bounce = call("bot", "restart", "worker")["data"]
    assert manager_bounce["changed"] is True
    assert native.actions[-2:] == ["start", "start"]
    # The self restart now schedules its existing witness without a model
    # handoff precondition. Its child owns native identity/quiet validation.
    with monkeypatch.context() as patch:
        patch.setattr(bot_operations, "_schedule_self_restart", lambda **kwargs:
                      bot_operations.BotLifecycleResult("example", "manager", None,
                                                       "", "requested", False, "not_checked",
                                                       "native_session", "request-1", "/private/startup.log"))
        before_self = len(native.actions)
        requested = call("bot", "restart", "manager")["data"]
        assert requested["state"] == "requested"
        assert requested["native_outcome"] == "unattempted"
        assert requested["runtime_state"] == "unknown"
        assert requested["request_id"] == "request-1"
        assert len(native.actions) == before_self

    native.native_pid += 1  # A different pane cannot consume the retained identity.
    before_incomplete = len(native.actions)
    call("bot", "restart", "worker", expected=6)
    assert len(native.actions) == before_incomplete
    native.native_pid -= 1
    # Restoring the original native observation permits the same exact session;
    # no model save was requested and no conflicting identity was adopted.
    assert call("bot", "restart", "worker", "--ceiling", "0", expected=2)["error"]["code"] == "invalid_argument"
    before = len(native.actions)
    assert call("bot", "restart", "worker", "--ceiling", "37")["data"]["readiness"] == "bridge_ready"
    assert native.fence_args[-1][-1] == "37"
    assert len(native.actions) == before + 1

    # The generated weekly timer carries fleet/root scope, not BOT_ID. Run the
    # actual public CLI through selected admission and this recorded native
    # adapter; a shell CLI stub would miss the origin-binding regression.
    for key in ("FLEET_NAME", "BOT_ID", "BOT_DIR"):
        monkeypatch.delenv(key)
    monkeypatch.setenv("CLAUDLOBBY_FLEET", "example")
    monkeypatch.setenv("FLEET_ROOT", str(root))
    before = len(native.actions)
    timer_bounce = call("bot", "restart", "worker", "--ceiling", "37")["data"]
    assert timer_bounce["readiness"] == "bridge_ready"
    assert len(native.actions) == before + 1 and native.fence_args[-1][-1] == "37"
    native.readiness = "session-ready"
    assert call("bot", "restart", "worker")["data"]["readiness"] == "session_ready"
    native.readiness = "unexpected"
    assert call("bot", "restart", "worker", expected=6)["error"]["code"] == "unavailable"
    before_bad_scope = len(native.actions)
    monkeypatch.setenv("FLEET_ROOT", str(root / "other-fleet"))
    assert call("bot", "restart", "worker", expected=4)["error"]["code"] == "conflict"
    assert len(native.actions) == before_bad_scope

    # The admitted one-shot child uses the same selected lifecycle path and
    # native receipt. It must never send a model handoff to the pane it stops.
    worker_dir = root / "runtime/bots/worker"
    (worker_dir / ".claude").mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    (worker_dir / ".claude/session.md").write_text(
        f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
    monkeypatch.setenv("FLEET_ROOT", str(root))
    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", "worker")
    monkeypatch.setenv("BOT_DIR", str(worker_dir))
    native.readiness = "bridge-ready"
    native.calls.clear()
    self_bounce = bot_operations.set_bot_running(root=root, fleet="example", bot="worker",
                                                 running=True, restart=True, _self_child=True)
    assert self_bounce.handoff == "native_session" and self_bounce.readiness == "bridge_ready"
    assert "svc_activation_handoff" not in native.calls
    for key in ("FLEET_ROOT", "FLEET_NAME", "BOT_ID", "BOT_DIR"):
        monkeypatch.delenv(key)
    monkeypatch.setattr(bot_operations,"_current_bot_binding",original_binding)
    native.manager = "Linux"
    native.target = "com.example.worker.service"
    native.readiness = "bridge-ready"
    host.states[native.target] = "enabled loaded failed"
    before_failed = len(native.actions)
    recovered_failed = call("bot", "start", "worker")["data"]
    assert recovered_failed["state"] == "running" and recovered_failed["changed"] is True
    assert len(native.actions) == before_failed + 1
    host.states[native.target] = "enabled loaded failed"
    refused_restart = call("bot", "restart", "worker", expected=4)
    assert "failed" in refused_restart["error"]["message"]
    assert "bot stop" in refused_restart["error"]["message"]
    assert len(native.actions) == before_failed + 1


def test_a_handoff_the_session_saves_in_its_own_file_is_verified(cold, monkeypatch, capsys):  # noqa: F811
    """#2924: clauDNA 0.35 saves the current session's own file and leaves
    .claude/session.md as an earlier version wrote it, so the door verifies the
    session's own file. At main it verified the other one and refused."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    legacy = worker.root / "runtime/bots/worker/.claude/session.md"
    legacy.write_text("---\nlast_updated: 2020-01-01T00:00:00Z\n---\nolder context\n")
    older = time.time() - 3600
    os.utime(legacy, (older, older))
    worker.native.explicit_handoff, worker.native.handoff_layout = "handoff-saved", "session"
    saved = worker.call("bot", "handoff", "worker")["data"]
    assert (saved["handoff"], saved["reason"]) == ("saved", "fresh_file_verified")
    assert legacy.read_text().endswith("older context\n")
    checked = worker.call("bot", "handoff", "worker", "--check")["data"]
    assert (checked["handoff"], checked["reason"]) == ("saved", "written_after_request")


@pytest.mark.parametrize("layout", ["session", "legacy"], ids=["session-owned", "per-directory"])
def test_a_handoff_that_lands_after_the_door_timed_out_reads_saved_on_check(
        cold, monkeypatch, capsys, layout):  # noqa: F811
    """#963/#990: a handoff takes minutes and the door waits 30 s. The door
    records its request; `bot handoff BOT --check` reads the handoff file
    against it later, sends nothing and takes no lifecycle lock. #2924: in either
    layout. A session that saves its own file leaves .claude/session.md older than
    the request, which at main read pending for good and refused every later request."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    root, installation, native, call = worker.root, worker.installation, worker.native, worker.call
    handoff = root / "runtime/bots/worker/.claude/session.md"
    handoff.parent.mkdir(exist_ok=True)
    older = time.time() - 3600
    stamp = datetime.fromtimestamp(older, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    handoff.write_text(f"---\nlast_updated: {stamp}\n---\nolder context\n")
    os.utime(handoff, (older, older))
    assert call("bot", "handoff", "worker", "--check")["data"]["reason"] == "no_request_recorded"

    native.explicit_handoff = "handoff-timeout"
    native.handoff_rc = 3
    timed_out = call("bot", "handoff", "worker", expected=6)
    assert timed_out["data"]["reason"] == "timeout"
    assert "bot handoff worker --check" in timed_out["error"]["hint"]
    native.calls.clear()
    with bot_operations._operation_lock(root):  # another lifecycle operation is running
        pending = call("bot", "handoff", "worker", "--check")["data"]
    assert (pending["handoff"], pending["reason"]) == ("pending", "not_written_since_request")
    # Another request is refused while it reads pending, and the refusal says how it resolves.
    again = call("bot", "handoff", "worker", expected=6)
    assert again["data"]["reason"] == "pending"
    assert "resolves once worker's session saves a handoff" in again["error"]["hint"]
    assert "svc_activation_handoff" not in native.calls

    now = datetime.now(timezone.utc)
    if layout == "session":
        bot_dir = handoff.parents[1]
        handoff = handoff_file.session_owned(bot_dir, native_session.read(bot_dir)["session_id"])
        handoff.parent.mkdir(parents=True)
    handoff.write_text(f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\nnew context\n")
    saved = call("bot", "handoff", "worker", "--check")["data"]
    assert (saved["handoff"], saved["reason"]) == ("saved", "written_after_request")
    assert saved["requested_at"] <= saved["written_at"]
    assert "svc_activation_handoff" not in native.calls and native.actions == []

    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", "worker")
    monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots/worker"))
    monkeypatch.setenv("FLEET_ROOT", str(root))
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    monkeypatch.setenv("CLAUDLOBBY_INSTALLATION", str(installation.directory))
    refused = call("bot", "handoff", "manager", "--check", expected=4)
    assert refused["error"]["code"] == "conflict"


@pytest.mark.parametrize("marker", ["handoff-saved", "handoff-timeout"], ids=["wait-ended", "wait-ran-out"])
def test_a_save_beside_the_sessions_own_handoff_does_not_resolve_its_request(
        cold, monkeypatch, capsys, marker):  # noqa: F811
    """#2927 item 3(b): the session's own handoff is older than the request and
    .claude/session.md is written after it. The readers read the session's own
    file alone, so the door does not read saved and --check reads pending, whether
    the wait ended on that write (a wait that watched .claude/session.md: before
    #2927, or after its watch read fell back to that file) or ran out (#2927)."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    own = handoff_file.session_owned(bot_dir, native_session.read(bot_dir)["session_id"])
    own.parent.mkdir(parents=True)
    hour_ago = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    own.write_text(f"---\nlast_updated: {_stamp(hour_ago)}\n---\nolder context\n")
    os.utime(own, (hour_ago.timestamp(), hour_ago.timestamp()))
    assert handoff_file.resolve(bot_dir) == own
    worker.native.explicit_handoff = marker  # "handoff-saved" writes .claude/session.md
    worker.native.handoff_rc = 3 if marker == "handoff-timeout" else 0
    answered = worker.call("bot", "handoff", "worker", expected=6)["data"]
    assert (answered["handoff"], answered["reason"]) == (
        "unknown", "unverified" if marker == "handoff-saved" else "timeout")
    legacy = handoff_file.legacy(bot_dir)
    if marker == "handoff-timeout":  # the save lands after the wait ran out
        legacy.write_text(f"---\nlast_updated: {_stamp(datetime.now(timezone.utc))}\n---\nnew context\n")
    checked = worker.call("bot", "handoff", "worker", "--check")["data"]
    requested = datetime.strptime(checked["requested_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert legacy.stat().st_mtime >= requested.timestamp(), "precondition: written after the request"
    assert (checked["handoff"], checked["reason"]) == ("pending", "not_written_since_request")


def _handoff_with_stderr(worker, capsys, *argv, expected=0):
    """worker.call, keeping what the CLI said on stderr."""
    actual = main(["--root", str(worker.root), "--json", "bot", "handoff", "worker", *argv])
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert actual == expected, json.dumps(result, indent=2) + "\nstderr:\n" + captured.err
    return result, captured.err


def test_bot_handoff_passes_on_what_its_script_said_on_stderr(cold, monkeypatch, capsys):  # noqa: F811
    """#2927 item 7: the adapter captures the explicit handoff script's stderr, and bot
    handoff printed none of it. It passes it on, at most 20 lines, and says how many
    more there were."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    worker.native.handoff_stderr = "".join(f"line {n}\n" for n in range(1, 26))
    result, err = _handoff_with_stderr(worker, capsys)
    assert (result["data"]["handoff"], result["data"]["reason"]) == ("skipped", "recent")
    said = [line for line in err.splitlines() if line.startswith("bot handoff worker: ")]
    assert said == [f"bot handoff worker: line {n}" for n in range(1, 21)] + [
        "bot handoff worker: 5 more lines of the handoff script's stderr not shown"]


class _BrokenStream:
    """A stderr whose reader has gone: every write and flush raises EPIPE."""

    def write(self, text):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


def _closed_stream():
    stream = io.StringIO()
    stream.close()
    return stream


@pytest.mark.parametrize("stream", ["broken", "closed", "none"])
def test_a_stderr_bot_handoff_cannot_write_to_stops_nothing(cold, monkeypatch, capsys, stream):  # noqa: F811
    """Note 5 of the #2929 review (#2927): bot handoff passes on its script's stderr just
    before it records the script's result, and a stderr it could not write to raised
    there, so the result went unrecorded. Broken (its reader gone), closed, or never
    open (None, where print writes to stdout instead), it loses those lines and nothing
    else: the result is recorded, and the answer is the one a working stderr gets."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    worker.native.explicit_handoff = "handoff-saved"  # the fake saves .claude/session.md
    worker.native.handoff_stderr = "line 1\nline 2\n"
    with monkeypatch.context() as patched:
        patched.setattr(sys, "stderr", {"broken": _BrokenStream(), "closed": _closed_stream(),
                                        "none": None}[stream])
        answered = worker.call("bot", "handoff", "worker")["data"]
    assert (answered["handoff"], answered["reason"]) == ("saved", "fresh_file_verified"), answered
    request = json.loads((bot_dir / "data/.handoff-request").read_text())
    assert request["native_result"] == {"marker": "handoff-saved", "returncode": 0}, request


@pytest.mark.parametrize("stream", ["broken", "closed", "none"])
def test_an_unreadable_receipt_bot_handoff_cannot_say_stops_nothing(cold, monkeypatch, capsys, stream):  # noqa: F811
    """Item 2 of #2935: the check of the save reads the native session receipt, and says on
    stderr when it cannot be read. That note had no guard: with a stderr that could not take
    it, a broken one made the check refuse a handoff directory that is fine, and a closed one
    raised a bare ValueError past every handler; --check did the same. Broken, closed or
    never open (None, where print writes to stdout instead), the note is lost and nothing
    else: the readers read .claude/session.md, and bot handoff and --check answer what they
    answer with a working stderr."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    native_session._path(bot_dir).write_text("{\n")  # a receipt that cannot be read
    worker.native.explicit_handoff = "handoff-saved"  # the fake saves .claude/session.md
    said = {"handoff": set(), "check": set()}  # the note is said once a process: each run says it
    monkeypatch.setattr(handoff_file, "_SAID", said["handoff"])
    with monkeypatch.context() as patched:
        patched.setattr(sys, "stderr", {"broken": _BrokenStream(), "closed": _closed_stream(),
                                        "none": None}[stream])
        answered = worker.call("bot", "handoff", "worker")["data"]
        patched.setattr(handoff_file, "_SAID", said["check"])
        checked = worker.call("bot", "handoff", "worker", "--check")["data"]
    assert all(any("native session receipt cannot be read" in note for note in notes)
               for notes in said.values()), said
    assert (answered["handoff"], answered["reason"]) == ("saved", "fresh_file_verified"), answered
    assert (checked["handoff"], checked["reason"]) == ("saved", "written_after_request"), checked
    request = json.loads((bot_dir / "data/.handoff-request").read_text())
    assert request["native_result"] == {"marker": "handoff-saved", "returncode": 0}, request


@pytest.mark.parametrize("refused_by", [
    pytest.param("cannot-search", marks=pytest.mark.skipif(
        os.geteuid() == 0, reason="root searches a directory without its search bit")),
    "redirected",
])
def test_a_session_directory_bot_handoff_refuses_gets_its_own_refusal(
        cold, monkeypatch, capsys, tmp_path, refused_by):  # noqa: F811
    """#2927 item 6: bot handoff sent the save, then its verify met a directory on the way
    to the session's own handoff that it cannot search. The PermissionError reached the
    CLI as a native-state refusal that read the effect as unattempted; it is the door's
    unverified answer now, and --check reads it unverified. A redirected directory there
    is refused the same way. Note 3 of the #2929 review (#2927): --check reads unknown
    (file_unverified) until the directory is repaired, so the hint to read it "until it
    reads saved" named a wait with no end; the hint says to repair the directory first."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    own = handoff_file.session_owned(bot_dir, native_session.read(bot_dir)["session_id"])
    sessions = own.parent.parent
    if refused_by == "redirected":  # the session's own directory is a link to one elsewhere
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        sessions.mkdir(parents=True)
        own.parent.symlink_to(elsewhere, target_is_directory=True)
    else:
        own.parent.mkdir(parents=True)
    own.write_text(f"---\nlast_updated: {_stamp(datetime.now(timezone.utc))}\n---\ncontext\n")
    worker.native.explicit_handoff = "handoff-saved"  # the fake saves .claude/session.md
    if refused_by == "cannot-search":
        sessions.chmod(0o600)
    try:
        refused = worker.call("bot", "handoff", "worker", expected=6)
        checked = worker.call("bot", "handoff", "worker", "--check")["data"]
    finally:
        sessions.chmod(0o700)
    assert (refused["data"].get("handoff"), refused["data"].get("reason")) == ("unknown", "unverified"), refused
    assert refused["data"]["native_outcome"] == "unknown", refused
    hint = refused["error"].get("hint") or ""
    assert "repair it first" in hint and "bot handoff worker --check" in hint, refused
    assert "until it reads saved" not in hint, refused
    # #2935 item 8: the session's own directory is not refused for want of a search.
    assert "or one above the session's own directory cannot be searched or read" in hint, refused
    assert (checked["handoff"], checked["reason"]) == ("unknown", "file_unverified")


def test_a_claude_directory_redirected_after_the_request_gets_the_directory_refusal(
        cold, monkeypatch, capsys, tmp_path):  # noqa: F811
    """Item 3 of #2935: while the session has no handoff of its own, the check of the save
    reads .claude/session.md, and it refused a .claude that is redirected or not this user's
    with the answer whose hint says to read --check "until it reads saved". It gets the hint a
    refused session directory gets now: repair it first. Here the save lands, and then .claude
    is redirected, before the check. --check is refused until the repair, by the read of the
    effective configuration; once repaired, it reads the save."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    claude, elsewhere = worker.root / "runtime/bots/worker/.claude", tmp_path / "elsewhere"
    worker.native.explicit_handoff = "handoff-saved"  # the fake saves .claude/session.md
    save = worker.native.call

    def save_then_redirect(function, *args, timeout=30):
        outcome = save(function, *args, timeout=timeout)
        if function == "svc_activation_handoff":  # .claude moves elsewhere, and a link replaces it
            claude.rename(elsewhere)
            claude.symlink_to(elsewhere, target_is_directory=True)
        return outcome

    monkeypatch.setattr(worker.native, "call", save_then_redirect)
    refused = worker.call("bot", "handoff", "worker", expected=6)
    assert (refused["data"].get("handoff"), refused["data"].get("reason")) == ("unknown", "unverified"), refused
    assert refused["data"]["native_outcome"] == "unknown", refused
    hint = refused["error"].get("hint") or ""
    assert "repair it first" in hint and "bot handoff worker --check" in hint, refused
    assert "until it reads saved" not in hint, refused
    unrepaired = worker.call("bot", "handoff", "worker", "--check", expected=4)
    assert unrepaired["error"]["message"] == "selected bot configuration or activation is incomplete", unrepaired
    claude.unlink()
    elsewhere.rename(claude)  # repaired
    repaired = worker.call("bot", "handoff", "worker", "--check")["data"]
    assert (repaired["handoff"], repaired["reason"]) == ("saved", "written_after_request"), repaired


@pytest.mark.parametrize("directory, cause", [
    pytest.param("data", "cannot be searched", marks=pytest.mark.skipif(
        os.geteuid() == 0, reason="root searches a directory without its search bit")),
    ("data", "is redirected (a link)"),
    ("data", "is not a directory"),
    (".claude", "is not this user's"),
    (".claude", "is not a directory"),
    pytest.param(".claude", "cannot be searched", marks=pytest.mark.skipif(
        os.geteuid() == 0, reason="root searches a directory without its search bit")),
    ("bot", "is not this user's"),
])
def test_a_directory_bot_handoff_cannot_record_its_request_through_is_named(
        cold, monkeypatch, capsys, tmp_path, directory, cause):  # noqa: F811
    """#2935 item 12: before it records a request, bot handoff reads the earlier one in data/,
    and a data/ it cannot search, or a redirected one, refused it with the generic "bot native
    or session readiness cannot be established" and a hint that named neither the directory
    nor the repair. Item 3, the half before the request: a .claude that is not this user's was
    refused the same way, at the file clock that stamps the request. Each answer names the
    directory, the cause and the repair now, and says nothing was sent: the handoff is asked
    again once the directory is repaired, not read with --check. Another user's .claude is
    stood in for by its lstat, which names another owner."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    path = bot_dir if directory == "bot" else bot_dir / directory
    elsewhere = tmp_path / "elsewhere"
    another_owner = [False]
    lstat = Path.lstat

    def owned_by_another(self, *args, **kwargs):
        info = lstat(self, *args, **kwargs)
        if not (another_owner[0] and self == path):
            return info
        fields = list(info)
        fields[4] = os.getuid() + 1  # st_uid
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", owned_by_another)
    if cause == "cannot be searched":
        path.chmod(0o600)
    elif cause == "is redirected (a link)":
        path.rename(elsewhere)
        path.symlink_to(elsewhere, target_is_directory=True)
    elif cause == "is not a directory":
        path.rename(elsewhere)
        path.write_text("")
    else:
        another_owner[0] = True
    worker.native.calls.clear()
    try:
        refused = worker.call("bot", "handoff", "worker", expected=6)
    finally:  # repaired
        if cause == "cannot be searched":
            path.chmod(0o700)
        elif cause in ("is redirected (a link)", "is not a directory"):
            path.unlink()
            elsewhere.rename(path)
        another_owner[0] = False
    assert refused["error"]["message"] == (f"worker's {directory} directory {cause}, so bot handoff "
                                           "recorded no request and sent nothing"), refused
    assert refused["data"]["native_outcome"] == "unattempted", refused
    hint = refused["error"].get("hint") or ""
    repair = {"cannot be searched": "give this user search permission on it",
              "is redirected (a link)": "put the directory itself back in place of the link",
              "is not a directory": "put the directory itself back in its place",
              "is not this user's": "give it back to this user"}[cause]
    assert hint.startswith(f"{repair} (worker's {directory} directory), then ask for the handoff "
                           "again: claudlobby --json bot handoff worker."), refused
    assert "--check has no request to read" in hint, refused
    assert "svc_activation_handoff" not in worker.native.calls, worker.native.calls
    assert not (bot_dir / "data/.handoff-request").exists()
    worker.native.explicit_handoff = "handoff-saved"  # the fake saves .claude/session.md
    asked_again = worker.call("bot", "handoff", "worker")["data"]
    assert (asked_again["handoff"], asked_again["reason"]) == ("saved", "fresh_file_verified"), asked_again


def test_a_refusal_no_directory_explains_keeps_the_generic_answer(cold, monkeypatch, capsys):  # noqa: F811
    """#2935 item 14: a refusal that no directory explains keeps the generic answer. A
    malformed .handoff-request under a healthy data/ refuses the request read, and the data/
    it lies in is usable, so nothing is named: the generic message and hint, as before #2938's
    named refusal, with nothing sent and the file left for inspection."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    request = worker.root / "runtime/bots/worker/data/.handoff-request"
    request.write_text("{")
    worker.native.calls.clear()
    refused = worker.call("bot", "handoff", "worker", expected=6)
    assert refused["error"]["message"] == "bot native or session readiness cannot be established", refused
    assert refused["error"]["hint"] == ("inspect the exact private session; use bot restart for an "
                                        "intentional bounce"), refused
    assert refused["data"]["native_outcome"] == "unattempted", refused
    assert "svc_activation_handoff" not in worker.native.calls, worker.native.calls
    assert request.read_text() == "{"


@pytest.mark.parametrize("case, handoff, reason", [
    pytest.param("own-file-saved", "saved", "fresh_file_verified", id="own-file-saved"),
    pytest.param("capture-from-before-the-request", "unknown", "unverified",
                 id="capture-from-before-the-request"),
    pytest.param("symlinked-per-directory-file", "saved", "fresh_file_verified",
                 id="symlinked-per-directory-file"),
    pytest.param("no-answer-from-the-door", "saved", "fresh_file_verified", id="no-answer-from-the-door"),
    pytest.param("malformed-receipt", "saved", "fresh_file_verified", id="malformed-receipt"),
])
def test_the_door_verifies_the_save_its_script_waited_for(
        cold, monkeypatch, capsys, tmp_path, test_cli, case, handoff, reason):  # noqa: F811
    """#2927 items 9 and 10: bot handoff runs the real pre-stop-handoff.sh --explicit,
    whose session saves through a fake pane (_pane_that_saves), and then verifies the
    file its readers read, in one run. The session had its own file, an hour old, at
    the request. A save the script saw reads saved only when the file holds a capture
    written for the request: one stamped two minutes before it is fresh by age, and
    the door refuses it. Item 10: a symlinked .claude/session.md beside the
    session's own file refuses nothing, since only the session's own file is watched,
    and the own file is the one verified (the readers refuse a symlinked one). Item 7:
    with no answer from the CLI's door, the script watches .claude/session.md alone and
    says so on stderr, which bot handoff passes on; the session's compatibility copy
    there ends the wait, and the session's own file is still the one verified. Note 4 of
    the #2929 review (#2927): a native-session receipt that cannot be read stops nothing
    before the request, since nothing reads it there. The script's door says so on
    stderr, which bot handoff passes on, and every reader then reads .claude/session.md,
    where the session's compatibility copy is the save verified."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    own = handoff_file.session_owned(bot_dir, native_session.read(bot_dir)["session_id"])
    legacy = handoff_file.legacy(bot_dir)
    hour_ago = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    own.parent.mkdir(parents=True)
    for path in (own, legacy):  # saved an hour ago: not recent
        path.write_text(f"---\nlast_updated: {_stamp(hour_ago)}\n---\nolder context\n")
        os.utime(path, (hour_ago.timestamp(), hour_ago.timestamp()))
    now = datetime.now(timezone.utc)
    if case == "symlinked-per-directory-file":
        elsewhere = tmp_path / "elsewhere.md"
        elsewhere.write_text(f"---\nlast_updated: {_stamp(now)}\n---\nnew context\n")
        legacy.unlink()
        legacy.symlink_to(elsewhere)
    if case == "malformed-receipt":  # nothing reads it before the request
        native_session._path(bot_dir).write_text("{\n")
    # The session stamps its capture as it saves, after the request. A stamp taken
    # here can fall in the second before the request's, and the door refuses that.
    stamp = (shlex.quote(_stamp(now - timedelta(seconds=120))) if case == "capture-from-before-the-request"
             else '"$(date -u +%Y-%m-%dT%H:%M:%SZ)"')
    capture = shlex.quote("---\nlast_updated: %s\n---\nnew context\n")
    save = f"printf -- {capture} {stamp} > {shlex.quote(str(own))}"
    if case in ("no-answer-from-the-door", "malformed-receipt"):  # and clauDNA's compatibility copy
        save += f" && printf -- {capture} {stamp} > {shlex.quote(str(legacy))}"
    if case != "no-answer-from-the-door":
        # The fixture installation's launcher has no runtime environment, so the CLI
        # its composed bot.conf names gives the script no answer. A shim runs this
        # checkout's CLI in its place, as an installation's launcher runs its own.
        worker.installation.cli.write_text(f'#!/bin/sh\nexec {shlex.quote(str(test_cli))} "$@"\n')
        worker.installation.cli.chmod(0o755)
    pane = tmp_path / "pane"
    pane.mkdir()
    tmux = _pane_that_saves(pane, save)
    worker.native.run_script = lambda directory, *_: _pre_stop_handoff(
        pane, Path(directory), "--explicit", TMUX_BIN=str(tmux),
        SESSION_HANDOFF_COMMAND="/handoff-save --auto",  # a bare command: unverifiable, so sent
        PANE_SEND_SETTLE_S="0", PANE_SEND_SHOWN_TICKS="2", PANE_SEND_VERIFY_TICKS="1")
    result, err = _handoff_with_stderr(worker, capsys, expected=0 if handoff == "saved" else 6)
    assert (pane / "submitted").exists(), err
    assert (result["data"]["handoff"], result["data"]["reason"]) == (handoff, reason), err
    fell_back = f"bot handoff worker: handoff_files: no answer from _handoff-files watch; reading {legacy} alone"
    assert (fell_back in err.splitlines()) is (case == "no-answer-from-the-door"), err
    unreadable = (f"bot handoff worker: {bot_dir}: the native session receipt cannot be read "
                  f"(native session receipt is invalid), so the session's own handoff cannot be "
                  f"named and {legacy} is read instead")
    assert err.splitlines().count(unreadable) == (1 if case == "malformed-receipt" else 0), err
    assert legacy.is_symlink() is (case == "symlinked-per-directory-file")


def test_live_restart_refuses_unconfirmed_native_identity(cold, monkeypatch, capsys):
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    receipt = native_session.read(bot_dir)
    receipt["pending"] = True
    native_session._write(bot_dir, receipt, expected=native_session.read(bot_dir))
    failed = worker.call("bot", "restart", "worker", expected=6)
    assert failed["error"]["code"] == "unavailable"
    assert worker.native.actions == []
    assert "svc_activation_bot_fence" not in worker.native.calls
    assert "svc_activation_handoff" not in worker.native.calls


def test_planned_restart_waits_for_native_idle_and_resumes_exact_session(cold, monkeypatch, capsys):
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    original = native_session.read(bot_dir)
    worker.native.native_idle = False
    waits = []
    def become_idle(seconds):
        waits.append(seconds)
        worker.native.native_idle = True
    monkeypatch.setattr(bot_operations.time, "sleep", become_idle)
    worker.native.calls.clear()
    resumed = bot_operations.set_bot_running(root=worker.root, fleet="example", bot="worker",
                                             running=True, restart=True)
    assert waits and resumed.handoff == "native_session" and resumed.state == "running"
    current = native_session.read(bot_dir)
    assert current["session_id"] == original["session_id"]
    assert current["process"] != original["process"] and current["pending"] is False
    assert worker.native.actions == ["start"]
    assert "svc_activation_handoff" not in worker.native.calls
    assert not (bot_dir / "data/.handoff-request").exists()


@pytest.mark.parametrize("stream", ["working", "broken", "closed", "none"])
def test_a_stderr_the_idle_wait_cannot_write_to_stops_no_restart(cold, monkeypatch, capsys, stream):  # noqa: F811
    """Item 9 of #2935: bot restart's planned checkpoint waits for the native session to go
    idle, and says so on stderr every 30 s. That line had no guard, and it is printed in the
    wait's handler for a busy session, where the handler beside it does not take what it
    raises: a broken stderr ended the wait with a BrokenPipeError, which bot restart answered
    as native state that cannot be established, and a closed one with a bare ValueError.
    Broken, closed or never open (None, where print writes to stdout instead), the line is
    lost, and the restart waits, then resumes the same session, as it does with a working
    stderr, which gets the line. A host update's checkpoint waits in the same function."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    original = native_session.read(bot_dir)
    worker.native.native_idle = False
    waits = []
    def become_idle(seconds):
        waits.append(seconds)
        worker.native.native_idle = True
    monkeypatch.setattr(bot_operations.time, "sleep", become_idle)
    worker.native.calls.clear()
    stderr = {"working": io.StringIO(), "broken": _BrokenStream(), "closed": _closed_stream(),
              "none": None}[stream]
    with monkeypatch.context() as patched:
        patched.setattr(sys, "stderr", stderr)
        restarted = worker.call("bot", "restart", "worker")["data"]
    assert waits and worker.native.native_idle, "the restart never waited for native idle"
    if stream == "working":
        assert "waiting for native idle from com.example.worker" in stderr.getvalue()
    assert (restarted["state"], restarted["handoff"]) == ("running", "native_session"), restarted
    current = native_session.read(bot_dir)
    assert current["session_id"] == original["session_id"]
    assert current["process"] != original["process"] and current["pending"] is False
    assert worker.native.actions == ["start"]
    assert "svc_activation_handoff" not in worker.native.calls


@pytest.mark.parametrize("receipt_state", ["pending", "unknown", "malformed", pytest.param(
    "unreadable", marks=pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files"))])
def test_native_restart_preserves_independent_optional_handoff(cold, monkeypatch, capsys, receipt_state):
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    native_before = native_session.read(bot_dir)
    request = bot_operations._new_handoff_request(bot_dir)
    request["native_result"] = ({"marker": "handoff-timeout", "returncode": 3}
                                if receipt_state == "unknown" else None)
    assert bot_operations._write_handoff_request(bot_dir, request)
    path = bot_dir / "data/.handoff-request"
    if receipt_state == "malformed":
        path.write_bytes(b"{unreadable as JSON\n")
    original = path.read_bytes()
    if receipt_state in {"pending", "unknown"}:
        refused = worker.call("bot", "handoff", "worker", expected=6)
        assert refused["data"]["reason"] == "pending"
        assert path.read_bytes() == original
    worker.native.calls.clear()
    before = path.stat()
    # Keep a read-only descriptor so unchanged bytes can be checked while the
    # optional receipt remains inaccessible by path throughout continuation.
    with path.open("rb") as retained:
        if receipt_state == "unreadable":
            path.chmod(0)
        try:
            proof = bot_operations.save_planned_checkpoint(
                bot_dir, "com.example.worker", str(worker.private_tmpdir),
                worker.native, operation_id="update-one")
            assert proof["session"]["session_id"] == native_before["session_id"]
            restarted = worker.call("bot", "restart", "worker")["data"]
            assert restarted["state"] == "running" and restarted["handoff"] == "native_session"
            current = native_session.confirm(bot_dir, expected=proof["session"])
            assert current["process"] != native_before["process"]
            bot_operations.retire_planned_checkpoint(bot_dir, proof)
            retained.seek(0)
            assert retained.read() == original
            after = path.stat()
            assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
            assert stat.S_IMODE(after.st_mode) == (0 if receipt_state == "unreadable"
                                                  else stat.S_IMODE(before.st_mode))
        finally:
            if receipt_state == "unreadable":
                path.chmod(stat.S_IMODE(before.st_mode))
    assert path.read_bytes() == original
    assert "svc_activation_handoff" not in worker.native.calls
    assert worker.native.actions == ["start"]


def test_stopped_native_checkpoint_retains_prefix_and_exact_identity(cold, monkeypatch, capsys):
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    proof = bot_operations.save_planned_checkpoint(bot_dir, "com.example.worker", str(worker.private_tmpdir),
                                                  worker.native, operation_id="update-one")
    assert json.loads(json.dumps(proof)) == proof and proof["operation_id"] == "update-one"
    transcript = Path(proof["session"]["transcript"])
    transcript.write_bytes(transcript.read_bytes() + b"SessionEnd appended after stop\n")
    bot_operations.verify_checkpoint_after_stop(bot_dir, proof)
    native_session.launch(bot_dir, "com.example.worker", worker.private_tmpdir, worker.account)
    with pytest.raises(native_session.NativeSessionError, match="not confirmed"):
        bot_operations.retire_planned_checkpoint(bot_dir, proof)
    from tests.test_native_session import retain_native_session
    worker.native.native_pid += 1
    retain_native_session(bot_dir, "com.example.worker", worker.private_tmpdir, worker.account, worker.native)
    bot_operations.retire_planned_checkpoint(bot_dir, proof)
    changed = native_session.read(bot_dir)
    changed["session_id"] = "00000000-0000-4000-8000-000000000099"
    native_session._write(bot_dir, changed, expected=native_session.read(bot_dir))
    with pytest.raises(native_session.NativeSessionError, match="differs"):
        bot_operations.retire_planned_checkpoint(bot_dir, proof)


@pytest.mark.parametrize("session, expected", [("absent", 0), ("unknown", 6)])
def test_restart_distinguishes_dead_session_recovery_from_unknown_live_state(
        cold, monkeypatch, capsys, session, expected):  # noqa: F811
    worker = _worker_cli(cold, monkeypatch, capsys)
    worker.native.session = session
    result = worker.call("bot", "restart", "worker", expected=expected)
    assert "svc_activation_handoff" not in worker.native.calls
    assert worker.native.actions == (["start"] if session == "absent" else [])
    if session == "absent":
        assert result["data"]["handoff"] == "not_applicable"


def test_a_staggered_unit_restarts_and_starts_within_its_boot_delay(cold, monkeypatch, capsys):  # noqa: F811
    """systemctl restart and enable --now block through ExecStartPre. A worker
    staggered 30 s returned at 32.9 s on the host, past the enroll call's fixed
    30 s, so a restart that worked read as an unverified effect (#2087)."""
    worker = _worker_cli(cold, monkeypatch, capsys, exec_start_pre="/bin/sleep 30")
    worker.native.enroll_needs_s = 33
    restarted = worker.call("bot", "restart", "worker")["data"]
    assert restarted["state"] == "running" and restarted["readiness"] == "bridge_ready"
    worker.host.states[worker.native.target] = "enabled loaded inactive"
    started = worker.call("bot", "start", "worker")["data"]
    assert started["state"] == "running" and started["changed"] is True
    assert worker.native.actions == ["start", "start"]


def test_a_hang_past_the_units_own_stagger_stays_unverified(cold, monkeypatch, capsys):  # noqa: F811
    """The budget grows by the unit's stagger and no more: a hang is still unknown."""
    worker = _worker_cli(cold, monkeypatch, capsys, exec_start_pre="/bin/sleep 30")
    worker.native.enroll_needs_s = 61
    hung = worker.call("bot", "restart", "worker", expected=6)
    assert hung["error"]["message"] == "bot lifecycle effect is unverified; inspect native state"
    assert hung["data"]["native_outcome"] == "unknown"


def test_an_unstaggered_unit_keeps_the_fixed_enroll_budget(cold, monkeypatch, capsys):  # noqa: F811
    worker = _worker_cli(cold, monkeypatch, capsys)
    worker.native.enroll_needs_s = 31
    slow = worker.call("bot", "restart", "worker", expected=6)
    assert slow["data"]["native_outcome"] == "unknown"


def test_a_changed_unit_is_refused_before_any_effect(cold, monkeypatch, capsys):  # noqa: F811
    """The budget reads only sealed bytes: activation admits a plain /bin/sleep
    stagger alone, and a unit changed since is refused before any native effect,
    so no enroll ever runs on a guessed delay."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    source = worker.root / "runtime/bots/worker/com.example.worker.service"
    source.write_bytes(source.read_bytes().replace(
        b"[Service]\n", b"[Service]\nExecStartPre=/bin/sh -c 'sleep 33'\n", 1))
    refused = worker.call("bot", "restart", "worker", expected=4)
    assert refused["error"]["code"] == "conflict"
    assert "svc_bot_enroll_exact" not in worker.native.calls and worker.native.actions == []


def test_a_stop_waits_for_the_session_it_stopped_to_exit(cold, monkeypatch, capsys):  # noqa: F811
    """#2227: KillMode=process marks a bot unit stopped while its session is still
    exiting, so the stop's quiet check gets a bounded wait instead of one read.
    A repeated stop of the de-enrolled bot waits the same way."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    waits = []
    monkeypatch.setattr(bot_operations, "assert_quiescent",
                        lambda *args, **kwargs: waits.append(kwargs.get("settle_s", 0)))
    stopped = worker.call("bot", "stop", "worker")["data"]
    assert stopped["state"] == "stopped" and stopped["changed"] is True
    assert stopped["native_outcome"] == "observed"
    again = worker.call("bot", "stop", "worker")["data"]
    assert again["state"] == "stopped" and again["changed"] is False
    assert waits == [bot_operations._STOP_SETTLE_S] * 2 and 0 < waits[0] <= 60


def test_a_stop_whose_session_never_exits_still_reads_unverified(cold, monkeypatch, capsys):  # noqa: F811
    """The wait is bounded: a session still in the cgroup at the bound refuses,
    and the stop reads unverified, never stopped."""
    worker = _worker_cli(cold, monkeypatch, capsys)

    def still_exiting(*args, **kwargs):
        raise RuntimeEvidenceError("svc_activation_quiet", kwargs["target"], "native refusal (3)")
    monkeypatch.setattr(bot_operations, "assert_quiescent", still_exiting)
    stuck = worker.call("bot", "stop", "worker", expected=6)
    assert stuck["error"]["message"] == "bot lifecycle effect is unverified; inspect native state"
    assert stuck["data"]["native_outcome"] == "unknown"
    assert worker.native.actions == ["stop"]


def test_a_stop_that_did_not_happen_reads_unverified_without_waiting(cold, monkeypatch, capsys):  # noqa: F811
    """A unit still enrolled and active after the stop call is refused on its
    state read, before any quiet wait: the wait covers only a session exiting."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    worker.host.states[worker.native.target] = "enabled loaded active"
    waits = []
    monkeypatch.setattr(bot_operations, "assert_quiescent",
                        lambda *args, **kwargs: waits.append(kwargs))
    real_call = worker.native.call

    def nothing_stops(function, *args, timeout=30):
        if function == "svc_bot_disenroll_exact":  # the call returns; the unit runs on
            worker.native.actions.append("stop")
            return subprocess.CompletedProcess([function], 0, "effect-attempted\n", "")
        return real_call(function, *args, timeout=timeout)
    monkeypatch.setattr(worker.native, "call", nothing_stops)
    failed = worker.call("bot", "stop", "worker", expected=6)
    assert failed["error"]["message"] == "bot lifecycle effect is unverified; inspect native state"
    assert failed["data"]["native_outcome"] == "unknown"
    assert worker.native.actions == ["stop"] and waits == []


def _plane_rows(monkeypatch, *, refuse=False):
    """The rows the lifecycle door hands the plane, or a plane that refuses them."""
    rows = []

    def emit(_root, events, require_commit=False):
        if refuse:
            raise OSError("the plane is unavailable")
        rows.extend(events)
        return [SimpleNamespace(status="committed") for _ in events]
    monkeypatch.setattr(bot_operations, "emit_batch", emit, raising=False)
    return rows


def test_bot_stop_records_the_stop_locally_and_on_the_plane_and_start_clears_it(cold, monkeypatch, capsys):  # noqa: F811
    """#2243: the stop door always leaves its local record, the fact fleet-pulse reads to keep a
    stopped bot silent, and a bot_stopped row naming the caller; bot start removes the record and
    writes bot_started. Stopping a bot that is already stopped records it too (the rollout path
    for a bot stopped before the record existed)."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    rows = _plane_rows(monkeypatch)
    record = worker.root / "runtime/bots/worker/data/.stopped"

    stopped = worker.call("bot", "stop", "worker", "--reason", "parked until the review lands")["data"]
    assert stopped["state"] == "stopped" and stopped["changed"] is True
    assert stopped["recording"] == "committed" and worker.native.actions == ["stop"]
    saved = json.loads(record.read_text())
    assert saved["by"] == "operator" and saved["reason"] == "parked until the review lands"
    assert abs(saved["stopped_epoch"] - time.time()) < 300 and saved["stopped_at"].endswith("Z")
    (row,) = rows
    assert row["payload"]["event"] == "bot_stopped"
    assert row["payload"]["subject"] == f"bot:{stopped['fleet']}/worker"
    data = row["payload"]["data"]["data"]
    assert (data["by"], data["reason"], data["changed"]) == ("operator", "parked until the review lands", True)
    assert data["request_id"] == saved["request_id"]

    rows.clear()
    again = worker.call("bot", "stop", "worker")["data"]
    assert again["state"] == "stopped" and again["changed"] is False
    assert json.loads(record.read_text())["reason"] is None
    (row,) = rows
    assert row["payload"]["event"] == "bot_stopped" and row["payload"]["data"]["data"]["changed"] is False

    rows.clear()
    started = worker.call("bot", "start", "worker")["data"]
    assert started["state"] == "running" and started["recording"] == "committed"
    assert not record.exists()
    (row,) = rows
    assert row["payload"]["event"] == "bot_started" and row["payload"]["data"]["data"]["by"] == "operator"


def test_a_plane_that_refuses_the_row_neither_blocks_the_stop_nor_its_record(cold, monkeypatch, capsys):  # noqa: F811
    """#2243 F3: a stop must not depend on the plane. The record the sweep reads is local and
    written first; the refused row is reported as recording degraded."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    _plane_rows(monkeypatch, refuse=True)
    stopped = worker.call("bot", "stop", "worker")["data"]
    assert stopped["state"] == "stopped" and stopped["recording"] == "degraded"
    assert worker.native.actions == ["stop"]
    saved = json.loads((worker.root / "runtime/bots/worker/data/.stopped").read_text())
    assert saved["by"] == "operator"


def test_a_stop_reason_is_one_printable_line(cold, monkeypatch, capsys):  # noqa: F811
    worker = _worker_cli(cold, monkeypatch, capsys)
    _plane_rows(monkeypatch)
    refused = worker.call("bot", "stop", "worker", "--reason", "two\nlines", expected=2)
    assert refused["error"]["code"] == "invalid_argument"
    assert worker.native.actions == []
    assert not (worker.root / "runtime/bots/worker/data/.stopped").exists()


def test_a_stop_refused_before_any_effect_leaves_no_record(cold, monkeypatch, capsys):  # noqa: F811
    """#2243: the stop door writes its record first. A stop refused before any native effect
    stopped nothing, so its record goes again: the bot was never stopped on purpose."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    _plane_rows(monkeypatch)
    record = worker.root / "runtime/bots/worker/data/.stopped"
    real_call = worker.native.call
    seen = []

    def refuses(function, *args, timeout=30):
        if function == "svc_bot_disenroll_exact":  # refused, and no effect-attempted marker
            seen.append(record.exists())
            return subprocess.CompletedProcess([function], 3, "", "")
        return real_call(function, *args, timeout=timeout)
    monkeypatch.setattr(worker.native, "call", refuses)
    refused = worker.call("bot", "stop", "worker", expected=6)
    assert refused["error"]["code"] == "unavailable"
    assert refused["data"]["native_outcome"] == "unattempted"
    assert seen == [True]  # the record was written before the native call
    assert not record.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes a file whatever its directory's mode")
def test_a_start_that_cannot_remove_the_stop_record_says_so(cold, monkeypatch, capsys):  # noqa: F811
    """#2243: a stop record left behind keeps a later loss of the bot's unit silent, so a start
    that cannot remove it says so, as `stop_record_kept` in the JSON and a line in the text. The
    start itself stands, and its plane row is recorded."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    rows = _plane_rows(monkeypatch)
    data = worker.root / "runtime/bots/worker/data"
    worker.call("bot", "stop", "worker")
    rows.clear()
    original_unlink = Path.unlink
    stop_record = data / ".stopped"
    def deny_only_stop_record(path, *args, **kwargs):
        if path == stop_record:
            raise PermissionError("stop record removal refused")
        return original_unlink(path, *args, **kwargs)
    # Native continuation must still write its mandatory receipt; isolate the
    # existing stop-record cleanup failure instead of denying all bot data writes.
    with monkeypatch.context() as denied:
        denied.setattr(Path, "unlink", deny_only_stop_record)
        started = worker.call("bot", "start", "worker")["data"]
        assert started["state"] == "running" and started["changed"] is True
        assert started["stop_record_kept"] is True and started["recording"] == "committed"
        (row,) = rows
        assert row["payload"]["event"] == "bot_started"
        assert (data / ".stopped").exists()
        # a start that finds the bot running retries the removal, and its text says so too
        assert main(["--root", str(worker.root), "bot", "start", "worker"]) == 0
        out = capsys.readouterr().out
        assert "could not remove this bot's stop record" in out, out
    again = worker.call("bot", "start", "worker")["data"]
    assert "stop_record_kept" not in again and not (data / ".stopped").exists()


def test_fresh_self_handoff_keeps_300_s_unless_the_activation_passes_a_wider_window(tmp_path):
    """#963: bot handoff and self restart call the verifier at its 300 s default; only the activation
    path passes max_age_s."""
    bot_dir = _handoff_below_envelope(tmp_path, "references_refreshed", 400, 400)
    with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned session handoff"):
        bot_operations._fresh_self_handoff(bot_dir)
    assert 399 <= bot_operations._fresh_self_handoff(bot_dir, max_age_s=900) <= 402


@pytest.fixture
def git_bot_binding(candidate, monkeypatch):
    """Real committed unit hashes and bot.conf; no native manager is invoked."""
    from claudlobby import active_config
    from claudlobby.composer import compose_launchd_plist, compose_systemd_unit
    from claudlobby.config_plan import path_state
    from claudlobby.config_units import unit_family
    from claudlobby.runtime_admission import GitCurrentRuntime

    installation, paths, fleet, _ = candidate
    from claudlobby.paths import Paths
    paths = Paths(installation.data_root, package=replace(paths.package, native=installation.native))
    bot = fleet.bots["lead"]
    bot_dir = paths.bot_runtime("lead")
    bot_dir.mkdir(parents=True)
    configuration = bot_dir / "bot.conf"
    configuration.write_text("# committed bot context\n")
    configuration.chmod(0o600)
    files = {"com.example.lead.service": (compose_systemd_unit(bot, fleet, paths).encode(), 0o644),
             "com.example.lead.plist": (compose_launchd_plist(bot, fleet, paths).encode(), 0o644)}
    for name, (content, mode) in files.items():
        path = bot_dir / name
        path.write_bytes(content)
        path.chmod(mode)
    (installation.data_root / "state").mkdir()
    directory = installation.data_root / "native-user"
    directory.mkdir()
    snapshot = {"installation": str(installation.directory), "data_root": str(installation.data_root),
                "revision": paths.package.source_revision, "fingerprint": "b" * 64,
                "supervision": {"manager": "Linux", "domain": "", "directory": str(directory)},
                "units": unit_family(files, destination=bot_dir, scope="bot", phase="bots",
                                     fleet="example", bot="lead"),
                "owned_outputs": [{"target": str(configuration), "state": path_state(configuration)["node"]}]}
    current = GitCurrentRuntime(installation, snapshot["revision"], snapshot["fingerprint"], deepcopy(snapshot))
    monkeypatch.setattr(active_config, "read_current_config", lambda *_args, **_kwargs: snapshot)
    destination = SimpleNamespace(paths=paths, fleet=fleet)
    adapter = SimpleNamespace(package=paths.package, read=lambda function:
        f"manager\tLinux\ndirectory\t{directory}\n")
    return SimpleNamespace(root=installation.data_root, current=current, snapshot=snapshot,
                           destination=destination, adapter=adapter, directory=directory,
                           configuration=configuration)


def test_git_current_bot_binding_retains_deenrolled_placement_and_honest_provenance(git_bot_binding):
    case = git_bot_binding
    binding = bot_operations._current_bot_binding(case.root, case.current, case.destination,
                                                  "lead", case.adapter)
    binding.verify_configuration()
    assert binding.entry == {"source": str(binding.declaration.source),
                             "target": "com.example.lead.service",
                             "installed": str(case.directory / "com.example.lead.service")}
    assert not Path(binding.entry["installed"]).exists()  # A deliberate stop retains placement authority.
    result = bot_operations._bot_result(bot_operations.BotLifecycleResult, case.current,
        "example", "lead", bot_operations._release_id(case.current), binding.entry["target"],
        "stopped", False, "not_running")
    assert result.release_id is None
    assert result.source_revision == case.current.revision
    assert result.config_fingerprint == case.current.config_fingerprint


@pytest.mark.parametrize("drift", ["snapshot", "unit", "configuration", "redirected", "manager", "duplicate"])
def test_git_current_bot_binding_refuses_changed_or_ambiguous_context(git_bot_binding, drift):
    case = git_bot_binding
    current = case.current
    if drift == "snapshot":
        case.snapshot["fingerprint"] = "c" * 64
    elif drift == "unit":
        Path(next(unit["source"] for unit in case.snapshot["units"] if unit["platform"] == "Linux")).write_text("changed")
    elif drift == "configuration":
        case.configuration.write_text("uncommitted context")
    elif drift == "redirected":
        (case.directory / "com.example.lead.service").symlink_to(case.configuration)
    elif drift == "manager":
        case.snapshot["supervision"]["manager"] = "Darwin"
        current = replace(current, snapshot=deepcopy(case.snapshot))
    else:
        case.snapshot["units"].append(deepcopy(case.snapshot["units"][0]))
        current = replace(current, snapshot=deepcopy(case.snapshot))
    with pytest.raises((bot_operations.BotLifecycleError, ValueError)):
        binding = bot_operations._current_bot_binding(case.root, current, case.destination,
                                                      "lead", case.adapter)
        binding.verify_configuration()


def test_git_current_bot_without_committed_placement_cannot_guess_start_destination(git_bot_binding, monkeypatch):
    case = git_bot_binding
    case.snapshot.pop("supervision")
    current = replace(case.current, snapshot=deepcopy(case.snapshot))
    inventory = SimpleNamespace(units=[SimpleNamespace(
        declaration=SimpleNamespace(source=case.destination.paths.bot_runtime("lead") / "com.example.lead.service"),
        installed=())])
    inventory.require_complete = lambda: inventory
    monkeypatch.setattr(bot_operations, "collect_enrollment", lambda *_args, **_kwargs: inventory)
    with pytest.raises(bot_operations.BotLifecycleError, match="placement is unavailable"):
        bot_operations._current_bot_binding(case.root, current, case.destination, "lead", case.adapter)


def test_git_handoff_check_reports_committed_provenance_without_sealed_selection(git_bot_binding, monkeypatch):
    case = git_bot_binding
    monkeypatch.setattr(bot_operations, "resolve_operation_scope", lambda **_: (case.destination, None))
    result = bot_operations.check_handoff(root=case.root, fleet="example", bot="lead")
    assert result.release_id is None
    assert result.source_revision == case.current.revision
    assert result.config_fingerprint == case.current.config_fingerprint
    assert result.reason == "no_request_recorded"


def test_self_restart_refuses_changed_committed_provenance_before_admitted_callback(git_bot_binding, monkeypatch):
    from contextlib import contextmanager
    case = git_bot_binding
    @contextmanager
    def admitted(*_args, **_kwargs):
        yield case.current
    monkeypatch.setattr(bot_operations, "mutation_admission", admitted)
    monkeypatch.setattr(bot_operations, "resolve_operation_scope", lambda **_: pytest.fail("provenance drift escaped"))
    with pytest.raises(bot_operations.BotLifecycleError, match="configuration changed"):
        bot_operations.set_bot_running(root=case.root, fleet="example", bot="lead", running=True,
            restart=True, _self_child=True, identity=object(),
            _expected_provenance=(None, {"source_revision": "0" * 40, "config_fingerprint": "0" * 64}),
            _on_lock=lambda: pytest.fail("caller admitted before provenance check"))


@pytest.mark.parametrize("committed, caller, switch", [
    ("gui", "gui", False), ("gui", "user", True), ("user", "user", False),
])
def test_git_bot_binding_uses_exact_committed_darwin_domain(git_bot_binding, committed, caller, switch):
    case = git_bot_binding
    uid = os.getuid()
    case.snapshot["supervision"].update(manager="Darwin", domain=f"{committed}/{uid}")
    current = replace(case.current, snapshot=deepcopy(case.snapshot))
    switches = []
    class Native:
        package = case.adapter.package
        def __init__(self, domain):
            self.domain = domain
        def read(self, function):
            assert function == "svc_inventory_catalog"
            return (f"manager\tDarwin\ndomain\t{self.domain}\ndirectory\t{case.directory}\n"
                    "PID\tStatus\tLabel\n")
        def in_selected_gui(self, target):
            switches.append(target)
            return Native(f"gui/{uid}")
    binding = bot_operations._current_bot_binding(case.root, current, case.destination,
                                                  "lead", Native(f"{caller}/{uid}"))
    assert binding.entry["target"] == f"{committed}/{uid}/com.example.lead"
    assert binding.entry["installed"] == str(case.directory / "com.example.lead.plist")
    assert switches == ([f"gui/{uid}/com.example.lead"] if switch else [])


def test_git_bot_binding_refuses_gui_caller_for_committed_user_domain(git_bot_binding):
    case = git_bot_binding
    uid = os.getuid()
    case.snapshot["supervision"].update(manager="Darwin", domain=f"user/{uid}")
    current = replace(case.current, snapshot=deepcopy(case.snapshot))
    adapter = SimpleNamespace(package=case.adapter.package, read=lambda _:
        f"manager\tDarwin\ndomain\tgui/{uid}\ndirectory\t{case.directory}\nPID\tStatus\tLabel\n",
        in_selected_gui=lambda _: pytest.fail("committed user target silently changed to GUI"))
    with pytest.raises(bot_operations.BotLifecycleError, match="domain differs"):
        bot_operations._current_bot_binding(case.root, current, case.destination, "lead", adapter)


@pytest.mark.parametrize("tail",[("move","worker","--to","other"),("remove","worker","--purge")])
def test_unsupported_git_roster_changes_refuse_before_all_effects(tmp_path,capsys,tail):
    before=tuple(tmp_path.iterdir())
    assert main(["--root",str(tmp_path),"--fleet","example","--json","bot",*tail])==4
    result=json.loads(capsys.readouterr().out)
    assert result["data"]=={"native_outcome":"unattempted","changed":False}
    assert "Git roster changes are not yet supported" in result["error"]["message"]
    assert tuple(tmp_path.iterdir())==before



def test_fresh_restart_starts_a_new_session_and_a_plain_restart_continues(cold, monkeypatch, capsys):  # noqa: F811
    """#2696: --fresh replaces the native session; without it a restart continues it."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    original = native_session.read(bot_dir)
    fresh = worker.call("bot", "restart", "worker", "--fresh")["data"]
    assert fresh["state"] == "running" and fresh["handoff"] == "fresh_session"
    renewed = native_session.read(bot_dir)
    assert renewed["session_id"] != original["session_id"]
    assert renewed["pending"] is False and renewed["process"] != original["process"]
    assert Path(original["transcript"]).exists()
    assert worker.native.actions == ["start"]
    assert "svc_activation_handoff" not in worker.native.calls
    continued = worker.call("bot", "restart", "worker")["data"]
    assert continued["handoff"] == "native_session"
    assert native_session.read(bot_dir)["session_id"] == renewed["session_id"]


def test_a_fresh_restart_refused_before_its_effect_keeps_the_retained_session(cold, monkeypatch, capsys):  # noqa: F811
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    receipt = native_session.read(bot_dir)
    receipt["pending"] = True
    native_session._write(bot_dir, receipt, expected=native_session.read(bot_dir))
    failed = worker.call("bot", "restart", "worker", "--fresh", expected=6)
    assert failed["error"]["code"] == "unavailable" and worker.native.actions == []
    assert native_session.read(bot_dir) == receipt


def test_a_fresh_restart_is_unverified_unless_the_renewed_session_started(cold, monkeypatch, capsys):  # noqa: F811
    """The restart proves its fresh session after readiness, as a host update proves a continued one."""
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    original = native_session.read(bot_dir)
    native_call = worker.native.call

    def restored(function, *args, **kwargs):
        if function == "svc_bot_enroll_exact":
            # Another writer put the retired identity back before the launch.
            native_session._write(bot_dir, original, expected=native_session.read(bot_dir))
        return native_call(function, *args, **kwargs)
    monkeypatch.setattr(worker.native, "call", restored)
    failed = worker.call("bot", "restart", "worker", "--fresh", expected=6)
    assert failed["error"]["code"] == "unavailable" and failed["data"]["native_outcome"] == "unknown"
    assert worker.native.actions == ["start"]
    assert native_session.read(bot_dir)["session_id"] == original["session_id"]


def test_a_fresh_restart_whose_unit_restart_failed_starts_fresh_after_stop_and_start(
        cold, monkeypatch, capsys):  # noqa: F811
    worker = _worker_cli(cold, monkeypatch, capsys)
    _plane_rows(monkeypatch)
    bot_dir = worker.root / "runtime/bots/worker"
    native_call = worker.native.call

    def refused(function, *args, **kwargs):
        if function == "svc_bot_enroll_exact":
            raise OSError("unit restart refused")
        return native_call(function, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(worker.native, "call", refused)
        worker.call("bot", "restart", "worker", "--fresh", expected=6)
    renewed = native_session.read(bot_dir)
    assert renewed["pending"] is True and worker.native.actions == []
    # The retired session still runs, so a restart that would continue it refuses.
    worker.call("bot", "restart", "worker", expected=6)
    worker.call("bot", "stop", "worker")
    worker.call("bot", "start", "worker")
    started = native_session.read(bot_dir)
    assert started["session_id"] == renewed["session_id"] and started["pending"] is False


@pytest.mark.parametrize("kwargs", [{"running": True}, {"running": False}])
def test_only_a_restart_chooses_a_fresh_session(tmp_path, kwargs):  # noqa: F811
    with pytest.raises(bot_operations.BotLifecycleError, match="only a restart"):
        bot_operations.set_bot_running(root=tmp_path, fleet="example", bot="worker", fresh=True, **kwargs)



def test_a_fresh_self_restart_needs_a_recent_handoff_before_and_after_the_idle_wait(
        cold, monkeypatch, capsys):  # noqa: F811
    worker = _worker_cli(cold, monkeypatch, capsys)
    bot_dir = worker.root / "runtime/bots/worker"
    original = native_session.read(bot_dir)
    handoff = bot_dir / ".claude/session.md"
    handoff.write_text("---\nlast_updated: 2020-01-01T00:00:00Z\n---\nold context\n")

    def no_admission(*_args, **_kwargs):
        raise AssertionError("a stale handoff must refuse before admission")
    with monkeypatch.context() as scoped:
        scoped.setattr(bot_operations, "mutation_admission", no_admission)
        with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned"):
            bot_operations._schedule_self_restart(root=worker.root, fleet="example", bot="worker",
                                                  ceiling=None, bot_dir=bot_dir, fresh=True)
    assert not (bot_dir / "logs/startup.log").exists() or "self_restart" not in (
        bot_dir / "logs/startup.log").read_text()
    for key, value in (("CLAUDLOBBY_ROOT", str(worker.root)), ("FLEET_ROOT", str(worker.root)),
                       ("CLAUDLOBBY_INSTALLATION", str(worker.installation.directory)),
                       ("FLEET_NAME", "example"), ("BOT_ID", "worker"), ("BOT_DIR", str(bot_dir))):
        monkeypatch.setenv(key, value)
    # The admitted child re-checks after its idle wait, before any effect.
    with pytest.raises(bot_operations.BotLifecycleError, match="fresh owned"):
        bot_operations.set_bot_running(root=worker.root, fleet="example", bot="worker", running=True,
                                       restart=True, fresh=True, _self_child=True)
    assert worker.native.actions == [] and native_session.read(bot_dir)["session_id"] == original["session_id"]
    now = datetime.now(timezone.utc)
    handoff.write_text(f"---\nlast_updated: {now:%Y-%m-%dT%H:%M:%SZ}\n---\ncontext\n")
    renewed = bot_operations.set_bot_running(root=worker.root, fleet="example", bot="worker",
                                             running=True, restart=True, fresh=True, _self_child=True)
    assert renewed.handoff == "fresh_session" and worker.native.actions == ["start"]
    assert native_session.read(bot_dir)["session_id"] != original["session_id"]
