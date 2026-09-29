"""Cold host and fleet setup through the release, staging, and activation owners."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import tempfile
from types import SimpleNamespace

from ..command_result import CommandFailure, CommandOutput


def _root(args, *, create: bool = False) -> Path:
    if args.root is None:
        raise CommandFailure("invalid_argument", "invalid argument: an explicit --root is required")
    path = Path(args.root).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise CommandFailure("invalid_argument", "invalid argument: --root must be an absolute, real directory")
    path = path.resolve()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or (path / "state").resolve() != path / "state":
        raise CommandFailure("conflict", "conflict: host data root is absent or state is redirected")
    return path


def _host_setup(args) -> CommandOutput:
    if args.fleet is not None or args.seed:
        raise CommandFailure("invalid_argument", "invalid argument: host setup rejects --fleet and --seed")
    from ..release_install import assemble_release
    from ..releases import ReleaseError
    from ..resources import get_resources
    from ..supervision_inventory import Adapter, InventoryError, _catalog

    try:
        package = get_resources()
        manager, domain, directories, _, _ = _catalog(Adapter(package).read("svc_inventory_catalog"))
    except (RuntimeError, InventoryError, OSError) as exc:
        raise CommandFailure("unavailable", "unavailable: installed package or native user manager") from exc
    missing = [binary for binary in ("tmux", "claude") if shutil.which(binary) is None]
    if missing:
        raise CommandFailure("unavailable", "unavailable: required bot runtime executable: " + ", ".join(missing))
    root = _root(args, create=True)
    try:
        release = assemble_release(root, Path(args.wheel), Path(args.dependency_lock),
                                   Path(args.wheelhouse), Path(args.interpreter))
    except (ReleaseError, ValueError, OSError) as exc:
        raise CommandFailure("conflict", "conflict: offline release assembly failed; inspect supplied wheel, lock, and interpreter") from exc
    if release.inputs.artifact_id != package.artifact_id:
        raise CommandFailure("release_mismatch", "release mismatch: wheel differs from this installed setup command",
                             release_id=release.release_id)
    return CommandOutput(
        {"root": str(root), "release_id": release.release_id, "cli": str(release.cli_path),
         "manager": manager, "domain": domain, "install_directories": [str(p) for p in directories],
         "selection": "unchanged"},
        release.release_id,
        (f"Release {release.release_id} assembled. Run {release.cli_path} --root {root} --fleet F fleet setup --config FILE --install-directory PATH.",),
    )


def _copy_config(source: Path, destination: Path, *, replace: bool) -> bool:
    if not source.is_file() or source.is_symlink():
        raise CommandFailure("not_found", "fleet configuration must be a regular file")
    content = source.read_bytes()
    for directory in (destination.parent.parent, destination.parent):
        if directory.is_symlink():
            raise CommandFailure("conflict", "conflict: fleet source directory is redirected")
        directory.mkdir(exist_ok=True)
    if destination.is_symlink() or destination.exists() and not destination.is_file():
        raise CommandFailure("conflict", "conflict: fleet configuration target is not a regular file")
    if destination.exists():
        if destination.read_bytes() == content:
            return False
        if not replace:
            raise CommandFailure("conflict", "conflict: fleet configuration differs; pass --replace-config to update it")
    fd, name = tempfile.mkstemp(prefix=".fleet-yaml-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        if replace:
            os.replace(temporary, destination)
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise CommandFailure("conflict", "conflict: fleet configuration changed while setup was preparing") from exc
        parent_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _fleet_setup(args) -> CommandOutput:
    from ..activation_state import ActivationError, read_selection
    from ..config_plan import PlanError, read_plan
    from .host import _activate, _operator_shell
    from .releases import _config_plan, _executing_release, _release

    root = _root(args)
    _operator_shell()  # activation itself checks native ancestry
    if args.seed or not args.fleet or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.fleet):
        raise CommandFailure("invalid_argument", "invalid argument: fleet setup requires one exact --fleet name")
    executing = _executing_release(root)
    if executing is None:
        raise CommandFailure("release_mismatch", "release mismatch: fleet setup requires a sealed release CLI")
    _release(root, executing)
    source = Path(args.config).expanduser().resolve(strict=True)
    destination = root / "local" / args.fleet / "fleet.yaml"
    changed = _copy_config(source, destination, replace=args.replace_config)
    try:
        selected = read_selection(root)
        external = []
        if selected is not None:
            prior = read_plan(root, selected["plan_id"])
            external = [path for path in prior.effects["fleet_manifests"].values()
                        if not Path(path).is_relative_to(root)]
    except (ActivationError, PlanError, KeyError, TypeError) as exc:
        raise CommandFailure("conflict", "conflict: selected fleet declarations cannot be verified") from exc
    staged = _config_plan(SimpleNamespace(release=executing, fleet_path=external), root)
    if args.fleet not in staged.data["fleets"]:
        raise CommandFailure("conflict", "conflict: authored fleet name differs from --fleet; no activation performed",
                             release_id=executing)
    activation = _activate(SimpleNamespace(plan_id=staged.data["plan_id"],
                                            install_directory=args.install_directory,
                                            activation_id=args.activation_id, adopt_existing=False), root)
    return CommandOutput({**activation.data, "fleet": args.fleet, "config": str(destination),
                          "config_copied": changed}, activation.release_id,
                         (f"Fleet {args.fleet} configured; {activation.lines[0]}",))


def dispatch(args) -> CommandOutput:
    return _host_setup(args) if args.public_command == "host.setup" else _fleet_setup(args)
