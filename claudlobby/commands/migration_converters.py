"""Public result boundary for the existing converter operation owners."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from importlib import import_module
from io import StringIO
import logging

from ..command_result import CommandFailure, CommandOutput

_OWNERS = {
    "env": ("env_migrate", "cmd_env_migrate"),
    "data": ("data_migrate", "cmd_data_migrate"),
    "cron": ("cron_migrate", "cmd_cron_migrate"),
    "memory": ("memory_migrate", "cmd_memory_migrate"),
    "lessons": ("lessons_migrate", "cmd_lessons_migrate"),
    "workstreams": ("plane", "cmd_plane_import_workstreams"),
}


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))


def dispatch(args) -> CommandOutput:
    """Keep each converter's I/O owner, translating its exit into the public contract.

    Legacy helpers may call ``sys.exit`` and include source bytes in error
    messages. Neither those messages nor unexpected exception text cross this
    boundary on failure.
    """
    name = args.migration_command
    if name == "workstreams" and args.archive and not args.apply:
        raise CommandFailure("invalid_argument", "invalid argument: --archive requires --apply")
    module, function = _OWNERS[name]
    logger = logging.getLogger("claudlobby")
    capture = _Capture()
    output = StringIO()
    errors = StringIO()
    old_propagate = logger.propagate
    logger.addHandler(capture)
    logger.propagate = False
    try:
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                rc = getattr(import_module(f".{module}", __package__), function)(args)
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
    finally:
        logger.removeHandler(capture)
        logger.propagate = old_propagate

    if rc:
        code = {2: "invalid_argument", 3: "unavailable", 6: "recording_degraded"}.get(rc, "conflict")
        raise CommandFailure(code, f"{code}: migration {name} could not complete")
    lines = tuple(capture.lines + output.getvalue().splitlines() + errors.getvalue().splitlines())
    return CommandOutput({"mode": "apply" if args.apply else "preview", "messages": list(lines)}, lines=lines)
