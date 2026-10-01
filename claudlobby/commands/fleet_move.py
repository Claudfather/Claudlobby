"""Cold, authoring-only relocation of one flat fleet directory."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from ..command_result import CommandFailure, CommandOutput


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_MARKER = ("# claudlobby nested-system container\n"
           "# Fleet declarations live one level below this directory.\n")


def _real_directory(path: Path, description: str) -> None:
    try:
        node = path.lstat()
    except FileNotFoundError as exc:
        raise CommandFailure("not_found", f"{description} is absent") from exc
    if not stat.S_ISDIR(node.st_mode) or path.resolve() != path:
        raise CommandFailure("conflict", f"{description} is redirected or not a directory")


def _fleet_at(path: Path, root: Path, package, name: str) -> None:
    from ..context import load_context
    from ..paths import Paths

    _real_directory(path, "fleet source")
    manifest = path / "fleet.yaml"
    if not manifest.is_file() or manifest.is_symlink():
        raise CommandFailure("conflict", "fleet source has no real fleet.yaml")
    try:
        context = load_context(Paths(root, package=package, fleet_dir=path), fleet=name)
    except (OSError, ValueError, TypeError) as exc:
        raise CommandFailure("conflict", "fleet source declaration is unreadable or names another fleet") from exc
    if context.paths.fleet_dir != path:
        raise CommandFailure("conflict", "fleet source declaration resolves elsewhere")
    if (path / "runtime").exists() or (path / "runtime").is_symlink():
        raise CommandFailure("conflict", "fleet runtime directory exists; only cold authoring source can move")


def _cold_host(root: Path, package) -> None:
    from ..activation_state import ActivationError, read_selection
    from ..supervision_inventory import Adapter, InventoryError, collect_enrollment

    try:
        if read_selection(root) is not None:
            raise CommandFailure("conflict", "fleet move requires no selected release")
        history = root / "state" / "activations"
        if history.is_symlink() or history.exists() and (not history.is_dir() or any(history.iterdir())):
            raise CommandFailure("conflict", "fleet move requires no activation history")
        collect_enrollment(root, (), bootstrap_empty=True,
                           adapter=Adapter(package)).require_complete()
    except (ActivationError, InventoryError, OSError) as exc:
        raise CommandFailure("conflict", "cold selection or complete native inventory cannot be proved") from exc


def _ensure_container(directory: Path) -> None:
    if directory.exists() or directory.is_symlink():
        _real_directory(directory, "system container")
    else:
        directory.mkdir()
        _real_directory(directory, "system container")
    if (directory / "fleet.yaml").exists() or (directory / "fleet.yaml").is_symlink():
        raise CommandFailure("conflict", "system destination is itself a fleet")
    marker = directory / ".claudron-system"
    if marker.is_symlink() or marker.exists() and not marker.is_file():
        raise CommandFailure("conflict", "system container marker is redirected")
    if not marker.exists():
        try:
            with marker.open("x") as stream:
                stream.write(_MARKER)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if not marker.is_file() or marker.is_symlink():
                raise CommandFailure("conflict", "system container marker changed")


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError, locked_activation
    from ..paths import _find_fleet_dir, _root_manifest_names_fleet
    from ..resources import get_resources
    from .host import _operator_shell
    from .setup import _root

    _operator_shell(args.root)
    if (args.seed or not args.fleet or not _NAME.fullmatch(args.fleet)
            or not _NAME.fullmatch(args.system) or args.fleet == args.system):
        raise CommandFailure("invalid_argument", "fleet move requires distinct exact --fleet and --system names")
    raw_root = Path(args.root).expanduser() if args.root else None
    if raw_root is None or not raw_root.is_absolute() or raw_root.resolve() != raw_root:
        raise CommandFailure("invalid_argument", "fleet move requires a real absolute --root")
    root = _root(args)
    local = root / "local"
    _real_directory(local, "local fleet source directory")
    source = local / args.fleet
    container = local / args.system
    target = container / args.fleet
    package = get_resources()

    renamed = False
    try:
        with locked_activation(root):
            if _root_manifest_names_fleet(root, args.fleet):
                raise CommandFailure("conflict", "root and overlay declare the same fleet")
            try:
                observed = _find_fleet_dir(local, args.fleet)
            except ValueError as exc:
                raise CommandFailure("conflict", "fleet source resolves at multiple paths") from exc
            if observed not in (source, target):
                raise CommandFailure("conflict", "fleet source is absent or ambiguous")
            if target.exists() or target.is_symlink():
                if source.exists() or source.is_symlink() or observed != target:
                    raise CommandFailure("conflict", "flat and nested fleet sources collide")
                _real_directory(container, "system container")
                _fleet_at(target, root, package, args.fleet)
                _cold_host(root, package)
                return CommandOutput({"fleet": args.fleet, "system": args.system,
                                      "source": str(source), "destination": str(target),
                                      "status": "already_nested"},
                                     lines=(f"Fleet {args.fleet} is already at {target}; no move performed.",))

            _fleet_at(source, root, package, args.fleet)
            _cold_host(root, package)
            if container.exists() or container.is_symlink():
                _real_directory(container, "system container")
                if (container / "fleet.yaml").exists() or (container / "fleet.yaml").is_symlink():
                    raise CommandFailure("conflict", "system destination is itself a fleet")
                destination_parent = container
            else:
                destination_parent = local
            if source.stat().st_dev != destination_parent.stat().st_dev:
                raise CommandFailure("conflict", "fleet move requires a same-filesystem atomic rename")
            _ensure_container(container)
            if target.exists() or target.is_symlink():
                raise CommandFailure("conflict", "fleet destination appeared before rename")
            try:
                os.rename(source, target)
                renamed = True
            except OSError as exc:
                raise CommandFailure("unavailable", "fleet directory rename failed; inspect both paths before retrying") from exc
            for parent in (local, container):
                fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
    except ActivationError as exc:
        raise CommandFailure("conflict", "fleet move cannot acquire or verify the host activation lock") from exc
    except OSError as exc:
        code = "commit_unknown" if renamed else "unavailable"
        raise CommandFailure(code, "fleet move result is uncertain; inspect both paths before retrying"
                             if renamed else "fleet move preparation failed before rename") from exc
    return CommandOutput({"fleet": args.fleet, "system": args.system,
                          "source": str(source), "destination": str(target),
                          "status": "moved"},
                         lines=(f"Moved cold fleet {args.fleet} to {target}. Review config plan and activate it separately.",))
