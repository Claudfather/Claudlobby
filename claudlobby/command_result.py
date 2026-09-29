"""Stdlib result contract for new public commands; legacy routes are unchanged."""

from __future__ import annotations

import argparse
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import json
import shlex
import sys
from typing import Callable
from uuid import uuid4

from .reference_hints import ReferenceHint


_EXITS = {"internal_error": 1, "selection_defect": 1, "invalid_argument": 2, "not_found": 3,
          "conflict": 4, "wrong_reference": 4, "ambiguous_reference": 4,
          "delivery_unknown": 5, "delivery_failed": 5, "notification_failed": 5,
          "unavailable": 6, "release_mismatch": 7, "timeout": 8,
          "receipt_unobservable": 9, "receipt_mismatch": 10, "recording_degraded": 11}
_PUBLIC = {("brief",): "brief", ("host", "releases"): "host.releases", ("host", "status"): "host.status",
           ("library", "list"): "library.list", ("library", "create"): "library.create",
           ("host", "env", "tiers"): "host.env.tiers", ("host", "cache", "warm"): "host.cache.warm",
           ("host", "job", "list"): "host.job.list", ("host", "job", "show"): "host.job.show",
           ("host", "job", "run"): "host.job.run",
           ("host", "supervision", "reap-orphans"): "host.supervision.reap-orphans",
           ("host", "credentials", "reconcile"): "host.credentials.reconcile",
           ("host", "credentials", "check"): "host.credentials.check",
           ("host", "github-app", "setup"): "host.github-app.setup",
           ("host", "github-app", "token"): "host.github-app.token",
           ("host", "update", "runtime"): "host.update.runtime",
           ("host", "update", "siblings"): "host.update.siblings",
           ("host", "setup"): "host.setup", ("host", "doctor"): "host.doctor", ("fleet", "setup"): "fleet.setup",
           ("host", "activate"): "host.activate", ("config", "validate"): "config.validate",
           ("config", "plan"): "config.plan", ("config", "explain"): "config.explain",
           ("config", "diff"): "config.diff", ("migration", "plan"): "migration.plan",
           ("migration", "status"): "migration.status",
           ("fleet", "reports", "submit"): "fleet.reports.submit",
           ("fleet", "reports", "list"): "fleet.reports.list",
           ("fleet", "reports", "ack"): "fleet.reports.ack",
           ("checkin", "list"): "checkin.list", ("checkin", "show"): "checkin.show",
           ("checkin", "record"): "checkin.record",
           ("checkin", "selection", "verify"): "checkin.selection.verify",
           ("checkin", "selection", "focus-refs"): "checkin.selection.focus-refs"}
_PUBLIC.update({(domain, verb): f"{domain}.{verb}" for domain, verbs in (
    ("context", ("show",)), ("bot", ("list", "show", "capabilities", "status", "session", "logs", "usage", "start", "stop", "restart", "move", "create")),
    ("fleet", ("show", "status", "logs", "uptime", "inbox", "usage", "start", "stop", "restart", "reconcile", "reload", "pulse")), ("project", ("list", "show")),
    ("task", ("list", "show", "reviews", "admit", "assign", "withdraw", "reassign", "escalate", "nudge")),
    ("assignment", ("show", "deliver", "accept", "progress", "block", "return", "complete", "fail")),
    ("message", ("show", "receipt", "wait", "send", "reply")),
    ("request", ("show",)),
    ("workstream", ("list", "show", "open", "progress", "renew", "block", "unblock", "close", "prune")),
    ("event", ("list", "show"))) for verb in verbs})
_PUBLIC.update({("bot", "automation", action): f"bot.automation.{action}"
                for action in ("status", "pause", "resume", "record")})
_invocation = ContextVar("public_cli_invocation", default=(None, False))


@dataclass(frozen=True)
class CommandError:
    code: str
    message: str
    retryable: bool = False
    hint: str | ReferenceHint | None = None


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
    def __init__(self, code: str, message: str, *, hint: str | ReferenceHint | None = None,
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
            hint = result.error.hint
            if isinstance(hint, ReferenceHint):
                if hint.next_command:
                    print(f"inspect claudlobby {shlex.join(hint.next_command)}", file=sys.stderr)
                elif hint.candidates:
                    for candidate in hint.candidates:
                        print(f"candidate {candidate.task_id}"
                              f"{f' / {candidate.assignment_id}' if candidate.assignment_id else ''}: "
                              f"claudlobby {shlex.join(candidate.command)}", file=sys.stderr)
                    if hint.total_matches > len(hint.candidates):
                        print(f"{hint.total_matches - len(hint.candidates)} more candidates; "
                              "select a canonical ID", file=sys.stderr)
            else:
                print(hint, file=sys.stderr)
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
            return _PUBLIC.get(tuple(tokens[index:index + 3]),
                               _PUBLIC.get(tuple(tokens[index:index + 2]), token))
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
