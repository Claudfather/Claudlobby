"""Operator cleanup of a bot already removed by an activated configuration."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

from ..command_result import CommandFailure, CommandOutput


def _last_declaration(root, selected, fleet, bot, package):
    """Follow committed selection ancestry; the nearest declaration owns residue."""
    from ..activation_state import read_activation
    from ..active_config import context_from_plan
    from ..config_plan import read_plan
    from ..releases import read_release

    seen = set()
    cursor = selected
    while cursor:
        activation_id = cursor["activation_id"]
        if activation_id in seen:
            raise CommandFailure("conflict", "activation selection history is cyclic")
        seen.add(activation_id)
        record = read_activation(root, activation_id)
        if record.status != "active":
            raise CommandFailure("conflict", "activation selection history is incomplete")
        intent = record.body["intent"]
        if (intent["plan_id"] != cursor["plan_id"]
                or intent["release_id"] != cursor["release_id"]):
            raise CommandFailure("conflict", "activation history selection differs from its record")
        plan = read_plan(root, intent["plan_id"])
        old_release = read_release(root, intent["release_id"], verify_files=False)
        if (plan.release_id != intent["release_id"]
                or plan.release_seal != old_release.seal_sha256):
            raise CommandFailure("conflict", "activation history plan differs from selection")
        if fleet in plan.fleets:
            context = context_from_plan(plan, fleet, package=package)
            if bot in context.fleet.bots:
                return plan, context
        previous = record.body["previous_selection"]
        cursor = previous
    raise CommandFailure("not_found", "no retained activated declaration owns this bot")


def _preflight(root, fleet, bot, package, selected, release):
    from ..active_config import context_from_plan
    from ..config_plan import path_state, read_plan
    from ..config_units import planned_units
    from ..context import load_context
    from ..supervision_inventory import Adapter, _catalog

    current = read_plan(root, selected["plan_id"])
    if current.release_id != release.release_id or current.release_seal != release.seal_sha256:
        raise CommandFailure("release_mismatch", "selected configuration differs from release")
    if fleet not in current.fleets:
        raise CommandFailure("not_found", "fleet is not in the active configuration")
    active = context_from_plan(current, fleet, package=package)
    if bot in active.fleet.bots or active.fleet.manager == bot:
        raise CommandFailure("conflict", "activate a plan without this bot before removal")
    authored = load_context(active.paths)
    if bot in authored.fleet.bots or authored.fleet.manager == bot:
        raise CommandFailure("conflict", "remove the bot from authored fleet.yaml before removal")

    prior, old = _last_declaration(root, selected, fleet, bot, package)
    bot_dir = old.paths.bot_runtime(bot)
    if bot_dir != active.paths.bot_runtime(bot) or bot_dir.parent.is_symlink():
        raise CommandFailure("conflict", "retained bot directory is redirected")
    adapter = Adapter(package)
    manager, domain, directories, names, _ = _catalog(adapter.read("svc_inventory_catalog"))
    declarations = [(declaration, item) for declaration, item in planned_units(prior, manager)
                    if declaration.scope == "bot" and declaration.fleet == fleet
                    and declaration.bot == bot and item["enroll"]]
    if len(declarations) != 1:
        raise CommandFailure("conflict", "retained bot has no unique frozen native unit")
    old_unit, _ = declarations[0]
    label = old_unit.source.stem if manager == "Darwin" else old_unit.source.name.removesuffix(".service")
    name = old_unit.source.name
    if manager == "Darwin" and domain == f"user/{os.getuid()}":
        _, gui_domain, gui_directories, gui_names, _ = _catalog(
            adapter.in_selected_gui(f"gui/{os.getuid()}/{label}").read("svc_inventory_catalog"))
        if gui_domain != f"gui/{os.getuid()}":
            raise CommandFailure("conflict", "selected GUI native inventory is unavailable")
        directories.extend(gui_directories)
        names.update(gui_names)
    if any(declaration.source.name == name for declaration, _ in planned_units(current, manager)):
        raise CommandFailure("conflict", "current configuration reuses the retired native label")
    if name in names or any((directory / name).exists() or (directory / name).is_symlink()
                            for directory in directories):
        raise CommandFailure("conflict", "retired native label is installed or loaded")
    if not bot_dir.exists() and not bot_dir.is_symlink():
        return bot_dir, label, False
    info = bot_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or bot_dir.resolve() != bot_dir:
        raise CommandFailure("conflict", "retained bot directory is not an owned directory")
    conf = bot_dir / "bot.conf"
    changes = [change for change in prior.changes if change.target == str(conf)]
    if (len(changes) != 1 or changes[0].after.get("kind") != "file"
            or path_state(conf)["node"] != changes[0].after):
        raise CommandFailure("conflict", "retained bot.conf differs from frozen declaration")
    return bot_dir, label, True


def dispatch(args) -> CommandOutput:
    from ..activation_state import read_selection
    from ..context import resolve_paths
    from ..runtime_admission import mutation_admission
    from ..bot_operations import _operation_lock
    from .move_bot import source_wip

    if args.seed or args.root is None or not args.fleet:
        raise CommandFailure("invalid_argument", "bot remove requires explicit --root and --fleet")
    if any(name in os.environ for name in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE")):
        raise CommandFailure("conflict", "bot remove requires an operator shell")
    if not args.bot_id or Path(args.bot_id).name != args.bot_id or args.bot_id in {".", ".."}:
        raise CommandFailure("invalid_argument", "supply one exact bot ID")
    try:
        paths = resolve_paths(root=args.root, fleet=args.fleet)
        root = paths.root
        with mutation_admission(root, expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
            with _operation_lock(root):
                selected = read_selection(root)
                if selected is None or selected["release_id"] != release.release_id:
                    raise CommandFailure("conflict", "active selection changed before bot removal")
                bot_dir, label, retained = _preflight(root, args.fleet, args.bot_id,
                                                      paths.package, selected, release)
                if retained:
                    if args.purge:
                        source_wip(bot_dir)
                    native = release.native_path / "spin-down-bot.sh"
                    env = os.environ.copy()
                    env.update({"CLAUDLOBBY_CLI": str(release.cli_path),
                                "CLAUDLOBBY_RELEASE_ID": release.release_id,
                                "CLAUDLOBBY_NATIVE_DIR": str(release.native_path),
                                "CLAUDLOBBY_ARTIFACT_ID": release.inputs.artifact_id,
                                "CLAUDLOBBY_ROOT": str(root), "FLEET_NAME": args.fleet})
                    command = [str(native), "--retired-service", label,
                               "--expected-return", "none", "--reason", "operator bot remove"]
                    if args.purge:
                        command.append("--purge")
                    command.append(str(bot_dir))
                    result = subprocess.run(command, env=env, capture_output=True,
                                            text=True, timeout=60)
                    if result.returncode:
                        raise CommandFailure("unavailable", "bot teardown refused or outcome is unverified",
                                             data={"fleet": args.fleet, "bot": args.bot_id,
                                                   "native_outcome": "unknown"})
                data = {"fleet": args.fleet, "bot": args.bot_id, "purged": args.purge,
                        "changed": retained, "native_outcome": "observed" if retained else "unattempted"}
                return CommandOutput(data, release_id=release.release_id,
                                     lines=(f"{args.fleet}/{args.bot_id}: removed after activated de-enrollment"
                                            + ("; directory purged." if args.purge else "; directory retained."),))
    except CommandFailure:
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise CommandFailure("conflict", "bot removal could not prove selected ownership") from exc
