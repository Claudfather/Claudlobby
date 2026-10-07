"""Cold bot relocation through the selected configuration and activation owners."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from uuid import uuid4

from ..command_result import CommandFailure, CommandOutput
from ..context import declared_paths


_RETAINED = (".env", "memory", "data", "projects", ".claude/session.md")


@dataclass(frozen=True)
class Move:
    root: Path
    source: object
    target: object
    source_dir: Path
    target_dir: Path
    release_id: str
    install_directory: Path
    external: tuple[str, ...] = ()
    installed: str = ""
    native_target: str = ""
    selected_plan_id: str = ""
    # (path, reviewed path_state) for the source and target fleet.yaml.
    move_inputs: tuple[tuple[str, dict], ...] = ()
    # The moved bot's Telegram access file, which the move itself rewrites.
    access_input: str = ""


_PENDING_REMEDY = ("review pending authoring with config plan and config diff, activate it "
                   "with host activate, then retry the move")


def _manifest_change(frozen, authored, bot, *, source):
    """Name the first semantic difference that is not this bot's move, else None.

    Both sides are FleetConfig values from the same load_fleet_snapshot parser.
    Only the moving bot's roster entry, its team membership and, when it was
    the source manager, an explicit replacement manager may differ.
    """
    from dataclasses import fields

    if source and bot in authored.bots or not source and (bot in frozen.bots or bot not in authored.bots):
        return "bots"
    manager = {}
    if source and frozen.manager == bot:
        if authored.manager not in frozen.bots or authored.manager == bot:
            return "manager"
        manager = {bot: authored.manager}
    for item in fields(frozen):
        name = item.name
        before, after = getattr(frozen, name), getattr(authored, name)
        if name == "bots":
            before = {key: value for key, value in before.items() if key != bot}
            after = {key: value for key, value in after.items() if key != bot}
        elif name == "manager":
            before = manager.get(before, before)
        elif name == "teams":
            before = {key: (manager.get(team.manager, team.manager),
                            [worker for worker in team.workers if worker != bot])
                      for key, team in before.items()}
            after = {key: (team.manager, [worker for worker in team.workers if worker != bot])
                     for key, team in after.items()}
        elif name == "projects" and frozen.projects_derived and authored.projects_derived:
            continue  # derived from the bots compared above
        if before != after:
            return name
    return None


def selected_inputs_fresh(plan, source, target, candidates, bot, package):
    """Refuse authoring the move's host-wide activation would ship unreviewed.

    The selected plan's recorded inputs are the fingerprints. The source and
    target fleet.yaml may change only semantically by this move; their exact
    reviewed states are returned so staging can be bound to them.
    """
    from ..active_config import context_from_plan
    from ..composer import telegram_channel_rel, telegram_handle
    from ..config import load_fleet_snapshot
    from ..config_plan import path_state

    if target.fleet.name not in plan.fleets:
        raise CommandFailure("conflict", "target fleet is not active; activate it before moving a bot into it",
                             hint=_PENDING_REMEDY)
    if {context.fleet.name for context in candidates} != set(plan.fleets):
        raise CommandFailure("conflict", "authored fleets differ from the selected configuration",
                             hint=_PENDING_REMEDY)
    manifests = plan.effects.get("fleet_manifests", {})
    if not all(isinstance(manifests.get(name), str) for name in (source.fleet.name, target.fleet.name)):
        raise CommandFailure("conflict", "selected configuration lacks the source or target manifest")
    reviewed = []
    for name, is_source in ((source.fleet.name, True), (target.fleet.name, False)):
        manifest = Path(os.path.abspath(manifests[name]))
        state = path_state(manifest, source=True)
        content = manifest.read_bytes()
        projects = manifest.parent / "projects.yaml"
        project_content = projects.read_bytes() if projects.is_file() else None
        if path_state(manifest, source=True) != state:
            raise CommandFailure("conflict", f"fleet manifest changed while reviewing the move: {manifest}")
        authored, _ = load_fleet_snapshot(manifest, content, project_content)
        frozen = context_from_plan(plan, name, package=package).fleet
        changed = _manifest_change(frozen, authored, bot, source=is_source)
        if changed is not None:
            raise CommandFailure("conflict", f"{name} fleet.yaml has pending {changed} changes beyond this move",
                                 hint=_PENDING_REMEDY)
        reviewed.append((str(manifest), state))
    handle = telegram_handle(source.fleet.bots[bot])
    access = (os.path.abspath(Path.home() / telegram_channel_rel(handle) / "access.json")
              if handle is not None else "")
    exempt = {path for path, _ in reviewed} | {access}
    for path, recorded in plan.inputs.items():
        if path in exempt:
            continue
        if path_state(Path(path), source=recorded["follow_links"]) != recorded["state"]:
            raise CommandFailure("conflict", f"authored input changed since the selected configuration: {path}",
                                 hint=_PENDING_REMEDY)
    return tuple(reviewed), access


def staged_scope(move, staged):
    """The staged plan may differ from the selected one only by this move.

    The two manifests must be exactly the reviewed bytes; the moved bot's
    access file may change; inputs may appear only under the target bot
    directory (its retained .env tier or relative account) and disappear only
    under the source bot directory. Anything else refuses before activation.
    """
    from ..config_plan import read_plan

    selected = read_plan(move.root, move.selected_plan_id)
    reviewed = dict(move.move_inputs)
    if len(reviewed) != 2 or tuple(staged.fleets) != tuple(selected.fleets):
        raise RuntimeError("staged move plan changes the active fleet set")
    for path in staged.inputs.keys() | selected.inputs.keys():
        before, after = selected.inputs.get(path), staged.inputs.get(path)
        if path in reviewed:
            if after is None or after["state"] != reviewed[path]:
                raise RuntimeError(f"fleet manifest changed after the move was reviewed: {path}")
        elif before == after:
            continue
        elif path == move.access_input and before is not None and after is not None:
            continue
        elif before is None and Path(path).is_relative_to(move.target_dir):
            continue
        elif after is None and Path(path).is_relative_to(move.source_dir):
            continue
        else:
            raise RuntimeError(f"staged move plan includes unrelated authoring: {path}")


def _git(repo, *command):
    # An inherited GIT_DIR/GIT_WORK_TREE would answer for another repository.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(["git", *command], cwd=repo, env=env,
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CommandFailure("unavailable", "source project WIP cannot be checked") from exc


def source_wip(directory):
    """Refuse unless every source checkout's authored work is also published.

    A dirty tree, detached HEAD, missing upstream, commits on any local branch
    that no remote-tracking ref holds, a stash, or unreadable Git state all
    refuse; a purge would otherwise destroy them.
    """
    projects = directory / "projects"
    if not projects.is_dir():
        return
    for repo in sorted(projects.iterdir()):
        if not (repo / ".git").exists() and not (repo / ".git").is_symlink():
            continue
        status = _git(repo, "status", "--porcelain")
        if status.returncode or status.stdout.strip():
            raise CommandFailure("conflict", f"source project has uncommitted or unknown WIP: {repo}")
        if _git(repo, "symbolic-ref", "-q", "HEAD").returncode:
            raise CommandFailure("conflict", f"source project has a detached or unreadable HEAD: {repo}")
        if _git(repo, "rev-parse", "--verify", "-q", "@{upstream}").returncode:
            raise CommandFailure("conflict", f"source project branch has no upstream: {repo}")
        unpushed = _git(repo, "rev-list", "--count", "HEAD", "--branches", "--not", "--remotes")
        if unpushed.returncode or unpushed.stdout.strip() != "0":
            raise CommandFailure("conflict", f"source project has unpushed or unknown commits: {repo}")
        stashes = _git(repo, "stash", "list")
        if stashes.returncode or stashes.stdout.strip():
            raise CommandFailure("conflict", f"source project has stashed or unknown WIP: {repo}")


def preflight(args):
    from ..active_config import context_from_plan
    from ..activation_enrollment import selected_bot_entry
    from ..activation_state import read_selection
    from ..config_plan import read_plan
    from ..context import load_context, resolve_paths
    from ..releases import read_release
    from ..supervision_inventory import Adapter, _catalog
    from ._helpers import _validation_gate

    if args.root is None or getattr(args, "seed", False):
        raise CommandFailure("invalid_argument", "bot move requires an explicit active host --root")
    if (not isinstance(args.bot, str) or not args.bot or Path(args.bot).name != args.bot
            or args.bot in {".", ".."}):
        raise CommandFailure("invalid_argument", "supply one exact bot ID")
    host = resolve_paths(root=args.root)
    root, package = host.root, host.package
    selected = read_selection(root)
    if selected is None:
        raise CommandFailure("conflict", "bot move requires an active selected release")
    plan = read_plan(root, selected["plan_id"])
    release = read_release(root, selected["release_id"])
    if (plan.release_id != release.release_id or plan.release_seal != release.seal_sha256
            or package.native != release.native_path
            or package.artifact_id != release.inputs.artifact_id):
        raise CommandFailure("release_mismatch", "selected bot move release or plan differs")

    active = []
    for name in plan.fleets:
        context = context_from_plan(plan, name, package=package)
        if args.bot in context.fleet.bots:
            active.append(context)
    sources = active
    if args.from_fleet:
        sources = [item for item in sources if item.fleet.name == args.from_fleet]
    if len(sources) != 1:
        raise CommandFailure("conflict", "select one active source fleet with --from"
                             if sources else "bot is not in the active source roster")
    source = sources[0]
    if len(active) != 1:
        raise CommandFailure("conflict", "bot is active in more than one fleet")
    if source.fleet.name == args.to:
        raise CommandFailure("invalid_argument", "source and target fleets are the same")
    authored_source = load_context(source.paths).fleet
    if args.bot in authored_source.bots or authored_source.manager == args.bot:
        raise CommandFailure("conflict", "remove bot from source fleet.yaml and declare a replacement manager first")

    external = tuple(plan.effects.get("fleet_manifests", {}).values())
    candidates = [load_context(paths) for paths in declared_paths(root, package, external=external)]
    targets = [context for context in candidates if context.fleet.name == args.to]
    if len(targets) != 1:
        raise CommandFailure("not_found" if not targets else "conflict",
                             "target fleet declaration is missing or ambiguous")
    target = targets[0]
    if args.bot not in target.fleet.bots:
        raise CommandFailure("conflict", "declare bot in target fleet.yaml before moving it")
    if any(args.bot in context.fleet.bots for context in candidates
           if context.fleet.name not in {source.fleet.name, target.fleet.name}):
        raise CommandFailure("conflict", "bot is declared in another authored fleet")
    for context in candidates:
        if not _validation_gate(context.fleet, context.paths, context="retry bot move"):
            raise CommandFailure("conflict", "authored fleet validation failed")
    move_inputs, access_input = selected_inputs_fresh(plan, source, target, candidates,
                                                      args.bot, package)

    source_dir = source.paths.bot_runtime(args.bot)
    target_dir = target.paths.bot_runtime(args.bot)
    if (not source_dir.is_dir() or source_dir.is_symlink()
            or not (source_dir / "bot.conf").is_file()
            or target_dir.is_symlink() or target_dir == source_dir):
        raise CommandFailure("conflict", "source or target bot directory is missing or redirected")
    source_wip(source_dir)
    check_copy_destinations(source_dir, target_dir)
    if getattr(args, "apply", False):
        from .releases import _executing_release
        if _executing_release(root) != release.release_id:
            raise CommandFailure("release_mismatch", "run bot move with the selected sealed release CLI")
    manager, *_ = _catalog(Adapter(package).read("svc_inventory_catalog"))
    entry = selected_bot_entry(root, source.fleet.name, args.bot, manager)
    move = Move(root, source, target, source_dir, target_dir,
                release.release_id, Path(entry["installed"]).parent, external,
                entry["installed"], entry["target"], selected["plan_id"], move_inputs,
                access_input)
    if getattr(args, "apply", False):
        update_access(move, args.bot, dry_run=True)
    return move


def no_active_assignment(move, bot):
    from ..activation_identity import read_selected_identity_bindings
    from ..plane.db import connect_ro, db_file
    from ..task_queries import list_tasks

    bindings = read_selected_identity_bindings(move.root, move.source.fleet.name,
                                               package=move.source.paths.package)
    with connect_ro(db_file(move.root)) as conn:
        page = list_tasks(conn, fleet_uid=bindings["fleet_uid"],
                          bot_uid=bindings["bots"][bot], state="open", limit=1)
    if page.issues:
        raise CommandFailure("conflict", "source assignment history is unresolved")
    if page.items:
        raise CommandFailure("conflict", "source bot has an open assignment; close or reassign it before moving")


def source_session(move, bot, *, force):
    """Observe the source against its frozen installed unit and native target.

    A stopped, de-enrolled or crashed source proves absent without --force;
    --force only accepts a ready live session. Unknown state always refuses.
    """
    from ..activation_runtime import assert_quiescent
    from ..bot_operations import _selected_adapter
    from ..supervision import build_supervision_spec
    from ..supervision_inventory import Adapter, InventoryError

    if not move.installed or not move.native_target:
        raise CommandFailure("unavailable", "source frozen native placement is unknown")
    spec = build_supervision_spec(move.source.fleet.bots[bot], move.source.fleet,
                                  move.source.paths)
    try:
        adapter = _selected_adapter(move.root, move.source.fleet.name, bot,
                                    Adapter(move.source.paths.package))
        state = adapter.read(
            "svc_bot_session_observe", spec.bot_dir, spec.label, spec.environment["TMUX_TMPDIR"],
            move.installed, move.native_target,
        ).strip()
        if state == "unknown":
            # A clean stop can leave tmux's socket file. Only the existing
            # kernel proof (inactive exact unit, empty cgroup where witnessed,
            # socket refusing connections) reads it as absent; no cleanup.
            socket = (Path(spec.environment["TMUX_TMPDIR"]) / f"tmux-{os.getuid()}" / spec.label)
            try:
                assert_quiescent(adapter, installed_file=Path(move.installed),
                                 target=move.native_target, socket_path=socket)
                state = "absent"
            except (RuntimeError, OSError):
                pass
    except (RuntimeError, InventoryError, OSError, subprocess.SubprocessError) as exc:
        raise CommandFailure("unavailable", "source private session state is unknown") from exc
    if state not in {"ready", "absent"}:
        raise CommandFailure("unavailable", "source private session state is unknown")
    if state == "ready" and not force:
        raise CommandFailure("conflict", "source bot has a live session; use --force after capturing its work")


def check_copy_destinations(source_dir, target_dir):
    for directory in (source_dir / ".claude", target_dir / ".claude"):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise CommandFailure("conflict", "bot handoff directory is not an ordinary directory")
    for name in _RETAINED:
        source, target = source_dir / name, target_dir / name
        if source.exists() or source.is_symlink():
            mode = source.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                raise CommandFailure("conflict", f"source retained path has unsupported type: {name}")
        if target.is_symlink():
            raise CommandFailure("conflict", f"target retained path is redirected: {name}")
        if target.exists():
            if not target.is_dir():
                raise CommandFailure("conflict", f"target retained path already exists: {name}")
            for parent, directories, files in os.walk(target, followlinks=False):
                if files or any((Path(parent) / child).is_symlink() for child in directories):
                    raise CommandFailure("conflict", f"target retained path is not empty: {name}")
            if source.is_symlink() or (source.exists() and not source.is_dir()):
                raise CommandFailure("conflict", f"target retained path already exists: {name}")


def copy_retained(move):
    check_copy_destinations(move.source_dir, move.target_dir)
    copied = []
    move.target_dir.mkdir(parents=True, exist_ok=True)
    for name in _RETAINED:
        source, destination = move.source_dir / name, move.target_dir / name
        if not source.exists() and not source.is_symlink():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            destination.symlink_to(os.readlink(source))
        elif source.is_dir():
            shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
            if name == ".env":
                destination.chmod(0o600)
        copied.append(str(destination))
    return copied


def update_access(move, bot, *, dry_run=False):
    from ..composer import telegram_channel_rel, telegram_handle

    source_handle = telegram_handle(move.source.fleet.bots[bot])
    target_handle = telegram_handle(move.target.fleet.bots[bot])
    if source_handle != target_handle:
        raise CommandFailure("conflict", "moving between Telegram handles requires explicit channel repair")
    if source_handle is None or not move.target.fleet.telegram_group_chat_id:
        return None
    path = Path.home() / telegram_channel_rel(source_handle) / "access.json"
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or path.parent.is_symlink()):
        raise CommandFailure("conflict", "source Telegram access file is not owned or ordinary")
    try:
        access = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandFailure("conflict", "source Telegram access file cannot be read") from exc
    if not isinstance(access, dict) or not isinstance(access.get("groups", {}), dict):
        raise CommandFailure("conflict", "source Telegram access groups are malformed")
    groups = access.get("groups", {})
    source_chat = move.source.fleet.telegram_group_chat_id
    target_chat = move.target.fleet.telegram_group_chat_id
    if set(groups) - {source_chat, target_chat}:
        raise CommandFailure("conflict", "Telegram access contains another group; resolve it explicitly")
    prior = groups.get(source_chat, groups.get(target_chat, {}))
    if not isinstance(prior, dict):
        raise CommandFailure("conflict", "source Telegram group access is malformed")
    access["groups"] = {target_chat: {
        "requireMention": prior.get("requireMention", True),
        "allowFrom": prior.get("allowFrom", []),
    }}
    if not dry_run:
        path.write_text(json.dumps(access, indent=2) + "\n")
    return str(path)


def apply_move(move, bot, *, force, cleanup):
    from ..activation import upgrade_activation
    from ..activation_state import read_selection
    from ..bot_operations import set_bot_running
    from ..config_staging import stage_configuration
    from ..releases import read_release
    from .releases import _install_direct_copies

    no_active_assignment(move, bot)
    source_session(move, bot, force=force)
    check_copy_destinations(move.source_dir, move.target_dir)
    data = {"source_fleet": move.source.fleet.name, "target_fleet": move.target.fleet.name,
            "bot": bot, "release_id": move.release_id, "source_stopped": False,
            "source_retained": True, "state": "incomplete", "plan_id": None,
            "activation_id": None, "copied": []}
    try:
        stopped = set_bot_running(root=move.root, fleet=move.source.fleet.name,
                                  bot=bot, running=False)
        data["source_stopped"] = stopped.state == "stopped"
        data["copied"] = copy_retained(move)
        access = update_access(move, bot)
        if access is not None:
            data["access_updated"] = access
        release = read_release(move.root, move.release_id)
        fleet_paths = declared_paths(move.root, move.source.paths.package, external=move.external)
        _install_direct_copies(fleet_paths)  # #1604: copies before composition, as config plan does
        plan = stage_configuration(fleet_paths, release)
        data["plan_id"] = plan.plan_id
        staged_scope(move, plan)
        activation_id = str(uuid4())
        data["activation_id"] = activation_id
        record = upgrade_activation(move.root, activation_id, plan.plan_id,
                                    move.install_directory)
        selected = read_selection(move.root)
        if (record.status != "active" or selected is None
                or selected["activation_id"] != activation_id
                or selected["plan_id"] != plan.plan_id):
            raise RuntimeError("move activation did not select its completed target plan")
        if cleanup:
            shutil.rmtree(move.source_dir)
            data["source_retained"] = False
    except Exception as exc:
        raise CommandFailure("unavailable", "bot move incomplete; inspect its plan and activation before retrying",
                             data=data, release_id=move.release_id) from exc
    data.update(state="moved", readiness="checked_during_activation", changed=True)
    return CommandOutput(data, release_id=move.release_id,
                         lines=(f"{move.source.fleet.name}/{bot} moved to {move.target.fleet.name}; "
                                f"activation {data['activation_id']} recorded active.",))


def dispatch(args):
    from .host import _operator_shell

    _operator_shell(args.root)
    try:
        move = preflight(args)
        data = {"source_fleet": move.source.fleet.name, "target_fleet": move.target.fleet.name,
                "bot": args.bot, "release_id": move.release_id,
                "source_dir": str(move.source_dir), "target_dir": str(move.target_dir),
                "copy": [name for name in _RETAINED
                         if (move.source_dir / name).exists() or (move.source_dir / name).is_symlink()],
                "native_install_directory": str(move.install_directory),
                "host_restart_scope": "all_declared_fleets",
                "cleanup_source": bool(args.cleanup_source)}
        if not args.apply:
            return CommandOutput({**data, "state": "preview", "changed": False},
                                 release_id=move.release_id,
                                 lines=(f"Preview: move {args.bot} from {move.source.fleet.name} "
                                        f"to {move.target.fleet.name}; activation restarts the host fleet roster.",))
        result = apply_move(move, args.bot, force=args.force, cleanup=args.cleanup_source)
        return CommandOutput({**data, **result.data}, result.release_id, result.lines)
    except CommandFailure:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise CommandFailure("conflict", "bot move preflight could not establish selected ownership") from exc
