"""Where a bot's saved session handoff is, for the runtime that checks one (#2924).

clauDNA 0.35 saves each session's handoff in that session's own file,
``R/sessions/<native session ID>/handoff.md``, where R is the bot's composed
``CLAUDNA_STATE_DIR``, ``<bot dir>/data/claudna``. Earlier versions, and other
session providers, save one file in the session's start directory, the bot's
``.claude/session.md``. A reader reads the session's own file when it exists, and
the per-directory file otherwise.

The session in question is the bot's current one, from its native-session
receipt, for ``bot handoff`` and a self restart. For a fresh session's resume
it is the session that session replaced, which ``native_session.renew`` records.
R is built from the bot directory and never read from the environment, because
a manager's CLI runs with its own.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys

LEGACY = Path(".claude") / "session.md"
SESSION_HANDOFF = "handoff.md"


class HandoffFileError(ValueError):
    """A directory on the way to a session's own handoff is redirected or not this user's."""


def legacy(bot_dir: Path) -> Path:
    """The per-directory handoff: clauDNA before 0.35, and other session providers."""
    return Path(bot_dir) / LEGACY


def session_owned(bot_dir: Path, session_id: str) -> Path:
    """The handoff clauDNA 0.35 and later save for one native session."""
    from .composer import CLAUDNA_STATE_SUBDIR
    from .native_session import _uuid
    if not _uuid(session_id):
        raise ValueError("a session handoff is named by one native session ID")
    return Path(bot_dir) / CLAUDNA_STATE_SUBDIR / "sessions" / session_id / SESSION_HANDOFF


def current_session(bot_dir: Path) -> str | None:
    """The bot's current native session ID, from its receipt; None without a readable one."""
    from .native_session import NativeSessionError, read
    try:
        value = read(Path(bot_dir))
    except (NativeSessionError, OSError):
        return None
    return None if value is None else value["session_id"]


def _directories_present(bot_dir: Path, path: Path) -> bool:
    """Whether every directory from the bot directory down to path's own exists.

    A missing one means the session saved no handoff of its own. One that is
    a symlink, or is not this user's own directory, is refused rather than read
    past, as the readers refuse a redirected per-directory file.
    """
    current = Path(bot_dir)
    for part in path.parent.relative_to(current).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise HandoffFileError(f"the session handoff directory is not an owned directory: {current}")
    return True


def resolve(bot_dir: Path, session_id: str | None = None) -> Path:
    """The handoff file to read for one session, by default the bot's current one.

    It is that session's own file when one exists (a symlink counts, and its
    reader refuses it), else the per-directory file.
    """
    sid = current_session(bot_dir) if session_id is None else session_id
    if sid is not None:
        path = session_owned(bot_dir, sid)
        if _has_own(bot_dir, path):
            return path
    return legacy(bot_dir)


def _has_own(bot_dir: Path, path: Path) -> bool:
    """Whether the session's own handoff file exists, so the readers read it alone."""
    return _directories_present(bot_dir, path) and os.path.lexists(path)


def watch(bot_dir: Path) -> list[Path]:
    """The files `bot handoff` watches for the current session's save, its own first.

    While the session has its own file the readers read that file alone, so it
    is named alone: a save beside it in the per-directory file does not end the
    wait, which times out, and `--check` reads pending (#2927). Until it has
    one, a session saves either file, or both while clauDNA keeps its
    compatibility copy, so both are named. A redirected directory on the way
    to its own file is refused, as `resolve` refuses it.
    """
    sid = current_session(bot_dir)
    if sid is None:
        return [legacy(bot_dir)]
    own = session_owned(bot_dir, sid)
    return [own] if _has_own(bot_dir, own) else [own, legacy(bot_dir)]


def resume(bot_dir: Path, session_id: str) -> Path:
    """The handoff a fresh session resumes from: the one the session its renewal replaced saved."""
    from .native_session import _uuid, predecessor
    if not _uuid(session_id):
        raise ValueError("a fresh session is named by one native session ID")
    replaced = predecessor(Path(bot_dir), session_id)
    return legacy(bot_dir) if replaced is None else resolve(bot_dir, replaced)


def entry(args) -> int:
    """The private `_handoff-files` CLI door, which the runtime scripts call.

    A script cannot import this package: a managed installation loads it from its
    source through the CLI launcher. ``watch`` prints the files `watch` names and
    ``resume`` the one `resume` names, one path per line; ``recent`` prints the
    age in seconds of the current session's handoff while it is fresh by
    `bot_operations._fresh_self_handoff`, else nothing. A refusal prints nothing
    on stdout, says why on stderr and exits 3.
    """
    bot_dir = Path(args.directory)
    if not bot_dir.is_absolute() or (args.action == "resume") != (args.session is not None):
        print("usage: claudlobby _handoff-files watch|recent BOT_DIR, or resume BOT_DIR SESSION_ID",
              file=sys.stderr)
        return 2
    from .native_session import NativeSessionError
    try:
        if args.action == "recent":
            from .bot_operations import BotLifecycleError, _fresh_self_handoff
            try:
                print(max(0, int(_fresh_self_handoff(bot_dir))))
            except BotLifecycleError:
                pass  # No fresh handoff: not recent.
            return 0
        paths = watch(bot_dir) if args.action == "watch" else [resume(bot_dir, args.session)]
    except (HandoffFileError, NativeSessionError, OSError, ValueError) as exc:
        print(f"the session handoff cannot be named: {exc}", file=sys.stderr)
        return 3
    for path in paths:
        print(path)
    return 0
