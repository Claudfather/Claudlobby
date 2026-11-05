"""Exact Claude continuation metadata; no model requests or transcript discovery.

The native SessionStart hook supplies identity. Lifecycle owners separately prove
an idle private pane, stop it, and recheck the retained bounded transcript prefix.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import tempfile
from uuid import UUID, uuid4

TAIL_BYTES = 65536
NAME = "native-session.json"


class NativeSessionError(ValueError):
    pass


class NativeSessionPending(NativeSessionError):
    """The retained launch is valid but its SessionStart hook has not finished."""


class NativeSessionBusy(NativeSessionError):
    """The bound process is present but has not reached an empty idle prompt."""


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _plain(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise NativeSessionError("native session path is redirected")
    info = path.lstat()
    if info.st_uid != os.getuid() or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise NativeSessionError("native session path is not owned")
    return path


def _inside(value, bot_dir):
    """The bot directory or a directory below it, named without redirection."""
    if not isinstance(value, str):
        return False
    path = Path(value)
    try:
        return path.is_relative_to(bot_dir) and path.resolve() == path
    except (OSError, RuntimeError, ValueError):
        return False


def _path(bot_dir):
    return _plain(_plain(bot_dir, directory=True) / "data", directory=True) / NAME


def read(bot_dir):
    path = _path(bot_dir)
    try:
        raw = _plain(path).read_bytes()
    except FileNotFoundError:
        return None
    try:
        value = json.loads(raw)
        fields = {"schema", "session_id", "transcript", "account", "cwd", "label", "tmpdir", "process", "pending", "checkpoint"}
        if (set(value) not in (fields, fields | {"provider_start"})
                or "provider_start" in value and (not isinstance(value["provider_start"], str)
                    or not value["provider_start"].startswith(("Darwin:", "Linux:"))
                    or len(value["provider_start"]) > 128
                    or any(c in value["provider_start"] for c in "\r\n\t"))
                or value["schema"] != 1 or not _uuid(value["session_id"])
                or value["cwd"] != str(bot_dir) or type(value["pending"]) is not bool
                or not all(isinstance(value[k], str) for k in ("transcript", "account", "label", "tmpdir"))
                or value["process"] is not None and not isinstance(value["process"], str)):
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise NativeSessionError("native session receipt is invalid") from exc
    return value


def _write(bot_dir, value, *, expected=None, allow_checkpoint_change=False):
    # Native observations happen outside this short lock. Every writer must
    # compare the receipt it observed before replacing it, including /clear.
    path = _path(bot_dir)
    lock = path.with_name(".native-session.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "rb") as guard:
        info = os.fstat(guard.fileno())
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise NativeSessionError("native session lock is not owned")
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        actual = read(bot_dir)
        if actual != expected:
            # A SessionStart (notably /clear) supersedes a checkpoint of the
            # same identity. Never let checkpoint-only publication discard it.
            same_identity = (allow_checkpoint_change and actual is not None and expected is not None
                             and {k: v for k, v in actual.items() if k != "checkpoint"}
                             == {k: v for k, v in expected.items() if k != "checkpoint"})
            if not same_identity:
                raise NativeSessionError("native session identity changed before receipt publication")
        if os.path.lexists(path):
            _plain(path)
        fd, name = tempfile.mkstemp(prefix=".native-session-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(json.dumps(value, sort_keys=True).encode())
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)


def _binding(value, bot_dir, label, tmpdir, account=None):
    if (value is None or value["cwd"] != str(bot_dir) or value["label"] != label
            or value["tmpdir"] != str(tmpdir) or account is not None and value["account"] != str(account)):
        raise NativeSessionError("exact native session is unavailable; existing bots require explicit session adoption before restart")
    from .isolation import transcript_slug
    expected = Path(value["account"]) / "projects" / transcript_slug(bot_dir) / (value["session_id"] + ".jsonl")
    if Path(value["transcript"]) != expected:
        raise NativeSessionError("native transcript differs from its exact account/cwd/session")
    return value


def _image(value, *, prefix=None):
    path = _plain(value["transcript"])
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        size = before.st_size if prefix is None else prefix
        if size <= 0 or before.st_size < size:
            raise NativeSessionError("native transcript is missing or truncated")
        offset = max(0, size - TAIL_BYTES)
        stream.seek(offset)
        tail = stream.read(size - offset)
        after = os.fstat(stream.fileno())
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or not tail.endswith(b"\n")):
        raise NativeSessionBusy("native transcript is still being written")
    return {"device": before.st_dev, "inode": before.st_ino, "size": size,
            "tail_sha256": hashlib.sha256(tail).hexdigest()}


def observe(adapter, bot_dir, label, tmpdir, *, caller=None, process_start=False):
    args = ((str(caller) if caller is not None else "", "start") if process_start
            else (str(caller),) if caller is not None else ())
    result = adapter.call("svc_bot_native_session", bot_dir, label, tmpdir, *args)
    try:
        fields = result.stdout.strip().split("\t")
        if len(fields) != (4 if process_start else 3):
            raise ValueError()
        pid, generation, idle = fields[:3]
        if result.returncode or not pid.isdigit() or int(pid) <= 1 or not generation.isdigit() or idle not in {"idle", "unready"}:
            raise ValueError()
    except (ValueError, AttributeError) as exc:
        raise NativeSessionError("native process/session observation is unavailable") from exc
    value = {"process": generation + ":" + pid, "idle": idle == "idle"}
    if process_start:
        value["process_start"] = fields[3]
    return value


def _provider_session(bot_dir, account, current):
    """Read only the exact native PID record; never enumerate provider state."""
    pid = int(current["process"].split(":")[1])
    path = _plain(Path(account) / "sessions" / (str(pid) + ".json"))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_uid != os.getuid() or not stat.S_ISREG(before.st_mode):
            raise NativeSessionError("native provider PID record is not owned")
        content = stream.read(TAIL_BYTES + 1)
        after = os.fstat(stream.fileno())
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (len(content) > TAIL_BYTES or any(getattr(before, k) != getattr(after, k) for k in fields)
            or any(getattr(after, k) != getattr(path.lstat(), k) for k in fields)):
        raise NativeSessionError("native provider PID record changed during adoption")
    try:
        raw = json.loads(content)
        keys = ("pid", "sessionId", "cwd", "procStart", "kind", "entrypoint", "pidDomain")
        identity = {key: raw[key] for key in keys}
        if (type(identity["pid"]) is not int or identity["pid"] != pid
                or not _uuid(identity["sessionId"]) or identity["cwd"] != str(bot_dir)
                or identity["kind"] != "interactive" or identity["entrypoint"] != "cli"
                or not isinstance(identity["procStart"], str)
                or not isinstance(identity["pidDomain"], str)):
            raise ValueError()
    except (ValueError, TypeError, KeyError) as exc:
        raise NativeSessionError("native provider PID record does not bind this interactive bot") from exc
    if current["process_start"].startswith("Darwin:") and identity["pidDomain"] == "darwin":
        expected = "Darwin:" + identity["procStart"]
    elif (current["process_start"].startswith("Linux:") and identity["procStart"].isdigit()
            and re.fullmatch(r"linux:[0-9a-f]{32}:pid:\[[0-9]+\]", identity["pidDomain"])):
        expected = "Linux:" + identity["procStart"] + ":" + identity["pidDomain"]
    else:
        raise NativeSessionError("native provider PID domain or process-start shape is unavailable")
    if current["process_start"] != expected:
        raise NativeSessionError("native provider PID record belongs to another process start or domain")
    return identity


def _adopted_value(bot_dir, label, tmpdir, account, current, identity):
    from .isolation import transcript_slug
    sid = identity["sessionId"]
    return {"schema": 1, "session_id": sid,
            "transcript": str(Path(account) / "projects" / transcript_slug(bot_dir) / (sid + ".jsonl")),
            "account": str(account), "cwd": str(bot_dir), "label": label, "tmpdir": str(tmpdir),
            "process": current["process"], "pending": False, "checkpoint": None,
            "provider_start": current["process_start"]}


def checkpoint_account(bot_dir, label, adapter, *, context=None):
    """Validate current composition/provider once before a bounded idle wait."""
    if context is None:
        context, account = _context(adapter.package, adapter.package.installation.data_root, bot_dir, label)
    else:
        from .composer import account_dir
        from .isolation import expand_home
        bot = context.fleet.bots[context.bot_id]
        if (context.paths.bot_runtime(context.bot_id) != Path(bot_dir) or bot.agent_cli != "claude"
                or label != f"{context.fleet.service_prefix}.{context.bot_id}"):
            raise NativeSessionError("frozen native continuation context names another bot")
        account = expand_home(account_dir(bot, context.fleet), Path.home())
        if account is None:
            raise NativeSessionError("frozen native account path is unavailable")
    require_provider(context)
    return account


def checkpoint(bot_dir, label, tmpdir, adapter, *, operation_id, account=None):
    if account is None:
        account = checkpoint_account(bot_dir, label, adapter)
    original = read(bot_dir)
    adopting = original is None or "provider_start" in original
    value = None if original is None else _binding(original, bot_dir, label, tmpdir, account)
    current = observe(adapter, bot_dir, label, tmpdir, process_start=adopting)
    if adopting and not current["process_start"].startswith(("Darwin:", "Linux:")):
        raise NativeSessionError("native provider process domain is unsupported")
    if original is not None and (value["pending"] or value["process"] != current["process"]
            or adopting and value["provider_start"] != current["process_start"]):
        raise NativeSessionError("native session is not the recorded process")
    if not current["idle"]:
        raise NativeSessionBusy("native session is busy or has queued input")
    identity = _provider_session(bot_dir, account, current) if adopting else None
    if adopting:
        value = _adopted_value(bot_dir, label, tmpdir, account, current, identity)
    image = _image(value)
    if read(bot_dir) != original:
        raise NativeSessionError("native session identity changed during checkpoint observation")
    after = observe(adapter, bot_dir, label, tmpdir, process_start=adopting)
    if after["process"] != current["process"] or adopting and after["process_start"] != current["process_start"]:
        raise NativeSessionError("native process changed during checkpoint observation")
    if not after["idle"]:
        raise NativeSessionBusy("native session became busy during checkpoint observation")
    if adopting and _provider_session(bot_dir, account, after) != identity:
        raise NativeSessionError("native provider session changed during adoption")
    value = {**value, "checkpoint": image}
    _write(bot_dir, value, expected=original)
    return {"kind": "native-session-v1", "operation_id": operation_id, "session": value, "transcript": image}


def verify(bot_dir, proof):
    if not isinstance(proof, dict) or set(proof) != {"kind", "operation_id", "session", "transcript"} or proof["kind"] != "native-session-v1":
        raise NativeSessionError("native checkpoint proof is invalid")
    value = proof["session"]
    _binding(value, bot_dir, value["label"], value["tmpdir"])
    if read(bot_dir) != value or _image(value, prefix=proof["transcript"]["size"]) != proof["transcript"]:
        raise NativeSessionError("native checkpoint session/transcript differs from the original proof")


def verify_live(bot_dir, proof, adapter):
    """Recheck an adopted legacy process immediately before its native stop."""
    value = proof["session"]
    if "provider_start" not in value:
        return
    verify(bot_dir, proof)
    current = observe(adapter, bot_dir, value["label"], value["tmpdir"], process_start=True)
    if (current["process"] != value["process"] or current["process_start"] != value["provider_start"]
            or not current["idle"]):
        raise NativeSessionError("adopted native process changed before stop")
    identity = _provider_session(bot_dir, Path(value["account"]), current)
    if identity["sessionId"] != value["session_id"] or read(bot_dir) != value:
        raise NativeSessionError("adopted provider session changed before stop; continue the same operation")


def launch(bot_dir, label, tmpdir, account):
    value = read(bot_dir)
    original = value
    if value is None:
        if (Path(bot_dir) / "data/.spawn").exists():
            raise NativeSessionError("older bot needs explicit native session adoption; no latest-session guess or handoff fallback")
        from .isolation import transcript_slug
        sid = str(uuid4())
        value = {"schema": 1, "session_id": sid, "transcript": str(account / "projects" / transcript_slug(bot_dir) / (sid + ".jsonl")),
                 "account": str(account), "cwd": str(bot_dir), "label": label, "tmpdir": str(tmpdir), "process": None, "pending": True, "checkpoint": None}
        mode = "fresh"
    else:
        _binding(value, bot_dir, label, tmpdir, account)
        if value["process"] is None:
            mode = "fresh"  # Retry a launch that never acquired a SessionStart receipt.
        else:
            if value["checkpoint"] is not None and _image(value, prefix=value["checkpoint"]["size"]) != value["checkpoint"]:
                raise NativeSessionError("native transcript changed before continuation launch")
            _image(value)
            mode = "resume"
        value = {**value, "pending": True}
    _write(bot_dir, value, expected=original)
    return mode, value["session_id"]


def capture(bot_dir, label, tmpdir, account, adapter, payload, *, caller):
    # The payload names the session's current directory, which follows its shell
    # into a checkout. The receipt keeps the bot directory a resume starts in.
    if (not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionStart"
            or payload.get("source") not in {"startup", "resume", "clear", "compact"}
            or not _uuid(payload.get("session_id")) or not _inside(payload.get("cwd"), bot_dir)):
        raise NativeSessionError("unsupported native SessionStart identity")
    current = observe(adapter, bot_dir, label, tmpdir, caller=caller)
    prior = _binding(read(bot_dir), bot_dir, label, tmpdir, account)
    if prior["pending"]:
        if payload["session_id"] != prior["session_id"]:
            raise NativeSessionError("SessionStart does not match the retained launch identity")
    elif prior["process"] != current["process"] or (payload["source"] != "clear" and payload["session_id"] != prior["session_id"]):
        raise NativeSessionError("SessionStart does not belong to the recorded native process")
    from .isolation import transcript_slug
    transcript = account / "projects" / transcript_slug(bot_dir) / (payload["session_id"] + ".jsonl")
    if payload.get("transcript_path") != str(transcript):
        raise NativeSessionError("SessionStart transcript differs from the configured account")
    hooked = {key: value for key, value in prior.items() if key != "provider_start"}
    _write(bot_dir, {**hooked, "session_id": payload["session_id"], "transcript": str(transcript),
                     "process": current["process"], "pending": False, "checkpoint": None}, expected=prior, allow_checkpoint_change=True)


def confirm(bot_dir, *, expected=None):
    value = read(bot_dir)
    if value is None:
        raise NativeSessionError("native SessionStart has not confirmed the retained launch")
    if expected is not None and any(value[key] != expected[key] for key in ("session_id", "account", "cwd", "label", "tmpdir", "transcript")):
        raise NativeSessionError("resumed native session differs from the original checkpoint")
    if value["pending"]:
        raise NativeSessionPending("native SessionStart has not confirmed the retained launch")
    if value["process"] is None:
        raise NativeSessionError("native SessionStart has not confirmed the retained launch")
    return value


def _context(package, root, bot_dir, label):
    from .active_config import read_current_config, load_active_context
    from .composer import account_dir
    from .isolation import expand_home
    snapshot = read_current_config(root, installation=package.installation, revision=package.source_revision)
    rows = [row for row in snapshot["units"] if row["scope"] == "bot" and row["working_directory"] == str(bot_dir)
            and row["admission"].get("unit") == label]
    if not rows:
        raise NativeSessionError("native session target is not in committed configuration")
    row = rows[0]
    context = load_active_context(root, row["fleet"], bot=row["bot"], package=package)
    bot = context.fleet.bots[row["bot"]]
    if bot.agent_cli != "claude":
        raise NativeSessionError("native continuation currently requires Claude Code")
    account = expand_home(account_dir(bot, context.fleet), Path.home())
    if account is None or not account.is_absolute():
        raise NativeSessionError("native account path is unavailable")
    return context, account


def require_provider(context):
    from .claude_version import measure
    from .composer import _scheduler_tool_path
    bot = context.fleet.bots[context.bot_id]
    version = measure(context.paths, binary=bot.env.get("CLAUDE_BIN") or None,
                      launch_path=_scheduler_tool_path(context.paths))
    if version.version is None:
        raise NativeSessionError("native continuation cannot measure the configured Claude provider: " + (version.why or "unavailable"))
    if tuple(map(int, version.version.split("."))) < (2, 1, 257):
        raise NativeSessionError("native continuation requires Claude Code >= 2.1.257 (--system-prompt-snapshot off); configured provider is " + version.version)


def entry(args):
    """Private short-lived CLI entry; no update lock or model/tool invocation."""
    import sys
    from .resources import get_resources
    from .supervision_inventory import Adapter
    try:
        package = get_resources()
        root, bot_dir = Path(args.root), Path(args.directory)
        context, account = _context(package, root, bot_dir, args.label)
        if args.action == "capture":
            capture(bot_dir, args.label, Path(args.tmpdir), account, Adapter(package), json.load(sys.stdin), caller=os.getpid())
        elif args.action == "confirm":
            expected = _binding(read(bot_dir), bot_dir, args.label, Path(args.tmpdir), account)
            value = confirm(bot_dir, expected=expected)
            current = observe(Adapter(package), bot_dir, args.label, Path(args.tmpdir))
            if value["process"] != current["process"]:
                raise NativeSessionError("native SessionStart process changed before readiness")
        else:
            require_provider(context)
            mode, sid = launch(bot_dir, args.label, Path(args.tmpdir), account)
            print(mode + "\t" + sid)
        return 0
    except NativeSessionPending as exc:
        print("native continuation pending: " + str(exc), file=sys.stderr)
        return 75
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print("native continuation unavailable: " + str(exc), file=sys.stderr)
        return 6
