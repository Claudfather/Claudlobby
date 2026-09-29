"""Bounded selected-bot session and log observations; no runtime effects."""

from __future__ import annotations

from dataclasses import asdict
import os
import selectors
import signal
import subprocess
import time

from ..command_result import CommandFailure, CommandOutput


_MAX_LOG_BYTES = 512 * 1024
_NO_LOGS = "tail-fleet: no log files found"
_EMPTY_LOGS = "tail-fleet: log files contain no lines"


def _context(args):
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..operation_context import OperationContextError, resolve_operation_scope
    from ..paths import InvalidPathSelector
    from ..releases import ReleaseError

    if args.seed:
        raise CommandFailure("conflict", "runtime reads require a selected host, not seed configuration")
    try:
        context, _ = resolve_operation_scope(root=args.root, fleet=args.fleet)
    except BotNotFoundError as exc:
        raise CommandFailure("not_found", "generated bot is not declared in the selected fleet") from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid root or fleet selector") from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release or executing package differs") from exc
    except (ActivationError, PlanError, OperationContextError, ValueError) as exc:
        raise CommandFailure("conflict", "selected fleet configuration or caller origin is incomplete") from exc
    except OSError as exc:
        raise CommandFailure("unavailable", "selected fleet configuration cannot be read") from exc
    bot = getattr(args, "bot_id", None)
    if bot is not None and bot not in context.fleet.bots:
        raise CommandFailure("not_found", "bot is not declared in the selected fleet")
    return context


def _session(args, context) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..fleet_operations import FleetLifecycleError, reconcile_fleet
    from ..operation_context import OperationContextError
    from ..releases import ReleaseError
    from ..runtime_admission import ReleaseMismatch
    from ..supervision_inventory import InventoryError

    try:
        result = reconcile_fleet(root=context.paths.root, fleet=context.fleet.name,
                                 bot=args.bot_id)
    except FleetLifecycleError as exc:
        raise CommandFailure("unavailable" if exc.unavailable else "conflict",
                             "private session or native enrollment could not be established",
                             release_id=exc.release_id) from exc
    except (ReleaseMismatch, ReleaseError) as exc:
        raise CommandFailure("release_mismatch", "executing native package differs from the selected release") from exc
    except (ActivationError, PlanError, OperationContextError) as exc:
        raise CommandFailure("conflict", "selected bot configuration or activation is incomplete") from exc
    except (InventoryError, OSError) as exc:
        raise CommandFailure("unavailable", "private session or native enrollment is unavailable") from exc
    bot = next((row for row in result.bots if row.bot == args.bot_id), None)
    if bot is None:
        raise CommandFailure("unavailable", "declared bot is missing from selected native observation",
                             release_id=result.release_id)
    data = {"fleet": result.fleet, "bot": asdict(bot),
            "meaning": "native enrollment and private session observed; no transcript or caller PID read"}
    line = (f"{result.fleet}/{bot.bot}: {bot.state}; native={bot.native_active}; "
            f"session={bot.session}; enrolled={str(bot.enrolled).lower()}")
    return CommandOutput(data, release_id=result.release_id, lines=(line,))


def _log_release(context) -> str:
    from ..activation_state import ActivationError, read_selection
    from ..context import native_environment

    try:
        selected = read_selection(context.paths.root)
    except ActivationError as exc:
        raise CommandFailure("conflict", "selected release state cannot be read") from exc
    if not selected:
        raise CommandFailure("conflict", "log reads require an active selected release")
    try:
        executing = native_environment(context.paths).get("CLAUDLOBBY_RELEASE_ID")
    except (OSError, RuntimeError, ValueError) as exc:
        raise CommandFailure("release_mismatch", "executing native package cannot be bound to the selected release") from exc
    if executing != selected["release_id"]:
        raise CommandFailure("release_mismatch", "executing native package differs from the selected release")
    return executing


def _tail(context, bot: str, lines: int, timeout: float) -> dict:
    script = context.paths.lib / "tail-fleet.sh"
    if not script.is_file() or not os.access(script, os.X_OK):
        return {"bot": bot, "status": "unavailable", "text": None}
    command = [str(script), "--fleet", context.fleet.name,
               "--fleet-dir", str(context.paths.source_dir),
               "--bot", bot, "--lines", str(lines)]
    env = {**os.environ, "CLAUDLOBBY_ROOT": str(context.paths.root)}
    try:
        with subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, start_new_session=True) as process:
            output = bytearray()
            deadline = time.monotonic() + timeout
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        ready = selector.select(max(0, deadline - time.monotonic()))
                        if not ready:
                            raise TimeoutError
                        chunk = os.read(process.stdout.fileno(), min(65536, _MAX_LOG_BYTES + 1 - len(output)))
                        if not chunk:
                            break
                        output.extend(chunk)
                        if len(output) > _MAX_LOG_BYTES:
                            raise ValueError("selected log output exceeds bound")
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except (OSError, TimeoutError, ValueError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                return {"bot": bot, "status": "unavailable", "text": None}
            returncode = process.returncode
    except (OSError, UnicodeError):
        return {"bot": bot, "status": "unavailable", "text": None}
    if returncode:
        return {"bot": bot, "status": "unavailable", "text": None}
    try:
        text = output.decode("utf-8").rstrip("\n")
    except UnicodeError:
        return {"bot": bot, "status": "unavailable", "text": None}
    status = ("missing" if text == _NO_LOGS else
              "empty" if text == _EMPTY_LOGS else "read")
    return {"bot": bot, "status": status, "text": text if status == "read" else None}


def _logs(args, context) -> CommandOutput:
    lines = args.lines
    if lines < 1 or lines > 200:
        raise CommandFailure("invalid_argument", "--lines must be between 1 and 200")
    release_id = _log_release(context)
    bots = [args.bot_id] if args.public_command == "bot.logs" else sorted(context.fleet.bots)
    if len(bots) > 64:
        raise CommandFailure("unavailable", "fleet has too many bots for one bounded log read",
                             release_id=release_id,
                             hint="read one declared bot with bot logs BOT")
    deadline = time.monotonic() + 15
    items = []
    for bot in bots:
        remaining = deadline - time.monotonic()
        items.append(_tail(context, bot, lines, remaining) if remaining > 0 else
                     {"bot": bot, "status": "unavailable", "text": None})
    data = {"fleet": context.fleet.name, "items": items,
            "lines_per_file": lines, "source": "selected native tail-fleet.sh", "next_cursor": None}
    if sum(len((item["text"] or "").encode("utf-8")) for item in items) > _MAX_LOG_BYTES:
        raise CommandFailure("unavailable", "selected log output exceeds the bounded result",
                             data={"fleet": context.fleet.name, "lines_per_file": lines},
                             release_id=release_id,
                             hint="reduce --lines or read one bot")
    if any(item["status"] == "unavailable" for item in items):
        raise CommandFailure("unavailable", "one or more selected bot log sources could not be read",
                             data=data, release_id=release_id)
    if args.public_command == "bot.logs" and items[0]["status"] == "missing":
        raise CommandFailure("not_found", "selected bot has no log files yet",
                             data=data, release_id=release_id)
    human = tuple(item["text"] if item["status"] == "read" else
                  f"{context.fleet.name}/{item['bot']}: {item['status']} logs"
                  for item in items)
    return CommandOutput(data, release_id=release_id, lines=human)


def dispatch(args) -> CommandOutput:
    context = _context(args)
    return _session(args, context) if args.public_command == "bot.session" else _logs(args, context)
