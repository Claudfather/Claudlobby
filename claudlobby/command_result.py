"""Stdlib result contract for new public commands; legacy routes are unchanged."""

from __future__ import annotations

import argparse
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import json
import sys
from typing import Callable
from uuid import uuid4


_EXITS = {"internal_error": 1, "invalid_argument": 2, "not_found": 3,
          "conflict": 4, "wrong_reference": 4, "ambiguous_reference": 4,
          "delivery_unknown": 5, "delivery_failed": 5, "notification_failed": 5,
          "unavailable": 6, "release_mismatch": 7, "timeout": 8,
          "receipt_unobservable": 9, "receipt_mismatch": 10, "recording_degraded": 11}
_PUBLIC = {("host", "releases"): "host.releases", ("config", "plan"): "config.plan",
           ("config", "diff"): "config.diff", ("migration", "plan"): "migration.plan",
           ("migration", "status"): "migration.status"}
_PUBLIC.update({(domain, verb): f"{domain}.{verb}" for domain, verbs in (
    ("context", ("show",)), ("bot", ("list", "show", "capabilities")),
    ("fleet", ("show",)), ("project", ("list", "show"))) for verb in verbs})
_invocation = ContextVar("public_cli_invocation", default=(None, False))


@dataclass(frozen=True)
class CommandError:
    code: str
    message: str
    retryable: bool = False
    hint: str | None = None


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    command: str
    request_id: str | None
    release_id: str | None
    data: dict
    error: CommandError | None
    schema_version: int = 1

    @property
    def exit_code(self) -> int:
        return _EXITS[self.error.code] if self.error else 0


@dataclass(frozen=True)
class CommandOutput:
    data: dict
    release_id: str | None = None
    lines: tuple[str, ...] = ()


class CommandFailure(Exception):
    def __init__(self, code: str, message: str, *, hint: str | None = None,
                 retryable: bool = False, data: dict | None = None,
                 release_id: str | None = None):
        if code not in _EXITS:
            raise ValueError("unknown public error code")
        self.error = CommandError(code, message, retryable, hint)
        self.data = data if data is not None else {}
        self.release_id = release_id
        super().__init__(message)


def emit(result: CommandResult, *, json_output: bool, lines: tuple[str, ...] = ()) -> int:
    if json_output:
        print(json.dumps(asdict(result), sort_keys=True, separators=(",", ":")))
    elif result.error:
        print(result.error.message, file=sys.stderr)
        if result.error.hint:
            print(result.error.hint, file=sys.stderr)
    else:
        print("\n".join(lines) if lines else f"{result.command}: complete")
    return result.exit_code


def execute(command: str, operation: Callable[[], CommandOutput], *, json_output: bool,
            request_id: str | None = None) -> int:
    """Preserve handled partial results; unexpected exception text stays private."""
    lines = ()
    try:
        output = operation()
        lines = output.lines
        result = CommandResult(True, command, request_id, output.release_id, output.data, None)
    except CommandFailure as exc:
        result = CommandResult(False, command, request_id, exc.release_id, exc.data, exc.error)
    except (ImportError, OSError) as exc:
        component = "application dependencies" if isinstance(exc, ImportError) else "host data access"
        result = CommandResult(False, command, request_id, None, {},
                               CommandError("unavailable", f"unavailable: {component}"))
    except Exception as exc:
        diagnostic = str(uuid4())
        # Exception messages/arguments can contain config values. The diagnostic
        # identifies this failure without printing those values or a traceback.
        print(f"diagnostic {diagnostic}: {type(exc).__name__}", file=sys.stderr)
        result = CommandResult(False, command, request_id, None, {},
                               CommandError("internal_error", f"internal error; see {diagnostic}"))
    return emit(result, json_output=json_output, lines=lines)


def _command_from_argv(tokens: list[str]) -> str | None:
    """Recognize only literal grammar, skipping values of existing root options."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("--root", "--fleet"):
            index += 2
            continue
        if token.startswith(("--root=", "--fleet=")) or token in (
                "--seed", "-v", "--verbose", "--json"):
            index += 1
            continue
        if token in {part[0] for part in _PUBLIC}:
            return _PUBLIC.get(tuple(tokens[index:index + 2]), token)
        return None
    return None


class ResultArgumentParser(argparse.ArgumentParser):
    """Structured syntax errors for migrated routes, argparse behavior elsewhere."""
    def parse_args(self, args=None, namespace=None):
        tokens = list(sys.argv[1:] if args is None else args)
        reset = _invocation.set((_command_from_argv(tokens), "--json" in tokens))
        try:
            return super().parse_args(tokens, namespace)
        finally:
            _invocation.reset(reset)

    def error(self, message):
        command, json_output = _invocation.get()
        if command is None:
            return super().error(message)
        # argparse's message can quote arbitrary argument values. Fixed wording
        # and an exact help route preserve the syntax contract without leaking.
        result = CommandResult(False, command, None, None, {}, CommandError(
            "invalid_argument", "invalid argument: command syntax",
            hint=f"inspect claudlobby {command.replace('.', ' ')} --help"))
        emit(result, json_output=json_output)
        self.exit(2)
