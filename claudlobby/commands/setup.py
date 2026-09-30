"""Cold host and fleet setup through the release, staging, and activation owners."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
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


# Fixed product-authored assembly refusals; anything else is classified below.
_ASSEMBLY_REFUSALS = frozenset({
    "wheelhouse directory and executable interpreter are required",
    "offline wheelhouse must contain only regular local .whl files",
    "dependency lock must be UTF-8 requirements text",
    "dependency lock is empty or invalid",
    "dependency lock must not install the candidate claudlobby wheel",
    "candidate must be a local wheel",
    "candidate wheel needs one distribution metadata record",
    "candidate wheel must contain only claudlobby and its metadata",
    "candidate wheel has no canonical claudlobby entrypoint",
    "candidate wheel has unsupported artifact metadata",
    "candidate wheel is missing system.yaml",
    "sealed release differs from requested assembly or compatibility",
})


def _assembly_failure(root: Path, exc: Exception) -> CommandFailure:
    """Name the product-owned cause and any retained release directory.

    Subprocess output and nested exception text are never forwarded; only fixed
    refusals, the failing assembly step and the owned release path are shown.
    """
    from ..releases import ReleaseError

    text = str(exc) if isinstance(exc, ReleaseError) else ""
    step = re.search(r"release assembly subprocess (?:failed \(-?[0-9]{1,3}\)|timed out)", text)
    cause = (text if text in _ASSEMBLY_REFUSALS else step[0] if step else
             "a supplied wheel, lock, wheelhouse or interpreter path does not exist"
             if isinstance(exc, FileNotFoundError) else
             "inspect supplied wheel, lock, and interpreter")
    hint = "correct the supplied inputs and rerun host setup"
    retained = re.search(re.escape(str(root / "state" / "releases")) + r"/r-[0-9a-f]{64}", text)
    if retained:
        # Assembly never repairs or overwrites a retained directory; the same
        # inputs keep refusing until the operator removes it.
        cause += f"; incomplete release retained at {retained[0]}"
        hint = (f"inspect and manually remove {shlex.quote(retained[0])} (never a sealed or selected release), "
                "then rerun host setup with the same inputs")
    return CommandFailure("conflict", f"conflict: offline release assembly failed: {cause}", hint=hint)


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
    # Bot startup silently skips consent pre-acceptance and trust seeding without jq.
    missing = [binary for binary in ("tmux", "claude", "jq") if shutil.which(binary) is None]
    if missing:
        raise CommandFailure("unavailable", "unavailable: required bot runtime executable: " + ", ".join(missing))
    root = _root(args, create=True)
    try:
        release = assemble_release(root, Path(args.wheel), Path(args.dependency_lock),
                                   Path(args.wheelhouse), Path(args.interpreter))
    except (ReleaseError, ValueError, OSError) as exc:
        raise _assembly_failure(root, exc) from exc
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


def _authored_config(source: Path, fleet: str) -> bytes:
    """Read the declaration and check its fleet name before anything is written."""
    import yaml

    if not source.is_file() or source.is_symlink():
        raise CommandFailure("not_found", "fleet configuration must be a regular file")
    content = source.read_bytes()
    try:
        document = yaml.safe_load(content)
    except (yaml.YAMLError, UnicodeError) as exc:
        # Parser messages can quote authored values; name only the file.
        raise CommandFailure("conflict", "conflict: fleet configuration is not readable YAML; no declaration written",
                             hint=f"correct {source} and rerun fleet setup") from exc
    body = document.get("fleet") if isinstance(document, dict) else None
    if not isinstance(body, dict):
        raise CommandFailure("conflict", "conflict: fleet configuration has no top-level fleet mapping; "
                             "no declaration written", hint=f"correct {source} and rerun fleet setup")
    if body.get("name", "unnamed-fleet") != fleet:
        raise CommandFailure("conflict", "conflict: authored fleet name differs from --fleet; no declaration written",
                             hint=f"set fleet.name in {source} to {fleet} or pass the authored name as --fleet")
    return content


def _flat_destination(root: Path, fleet: str) -> Path:
    """Refuse a system container or an existing nested fleet of the same name."""
    from ..paths import _find_fleet_dir

    local = root / "local"
    flat = local / fleet
    if flat.is_dir() and not flat.is_symlink() and (
            (flat / ".claudron-system").exists() or (flat / ".claudron-system").is_symlink()
            or any((child / "fleet.yaml").is_file() for child in flat.iterdir() if child.is_dir())):
        raise CommandFailure("conflict", f"conflict: {flat} is a system container, not a flat fleet; "
                             "no declaration written")
    try:
        observed = _find_fleet_dir(local, fleet) if local.is_dir() and not local.is_symlink() else None
    except (ValueError, OSError) as exc:
        raise CommandFailure("conflict", "conflict: fleet name resolves at multiple paths; no declaration written") from exc
    if observed not in (None, flat):
        raise CommandFailure("conflict", f"conflict: fleet {fleet} is already declared nested at {observed}; "
                             "no flat declaration written",
                             hint=f"edit {observed / 'fleet.yaml'} in place, then run config plan and host activate")
    return flat / "fleet.yaml"


def _sync_parent(path: Path) -> None:
    parent_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _copy_config(content: bytes, destination: Path, *, replace: bool) -> tuple[bool, bytes | None, list[Path]]:
    """Install the declaration; return (changed, prior bytes, directories created)."""
    created = []
    for directory in (destination.parent.parent, destination.parent):
        if directory.is_symlink():
            raise CommandFailure("conflict", "conflict: fleet source directory is redirected")
        if not directory.exists():
            directory.mkdir()
            created.append(directory)
    if destination.is_symlink() or destination.exists() and not destination.is_file():
        raise CommandFailure("conflict", "conflict: fleet configuration target is not a regular file")
    prior = destination.read_bytes() if destination.exists() else None
    if prior is not None:
        if prior == content:
            return False, prior, created
        if not replace:
            raise CommandFailure("conflict", "conflict: fleet configuration differs; pass --replace-config to update it")
    _write_config(destination, content, replace=replace)
    return True, prior, created


def _restore_config(destination: Path, written: bytes, prior: bytes | None, created: list[Path]) -> str:
    """Undo this run's declaration write after a pre-activation refusal only.

    A concurrent edit is left alone. Returns the declaration state reported.
    """
    try:
        if destination.is_symlink() or not destination.is_file() or destination.read_bytes() != written:
            return "changed_concurrently"
        if prior is None:
            destination.unlink()
            _sync_parent(destination)
            for directory in reversed(created):
                try:
                    directory.rmdir()  # only if still empty
                except OSError:
                    break
            return "removed"
        _write_config(destination, prior, replace=True)
        return "restored"
    except OSError:
        return "restore_failed"


def _write_config(destination: Path, content: bytes, *, replace: bool) -> None:
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
        _sync_parent(destination)
    finally:
        temporary.unlink(missing_ok=True)


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
    content = _authored_config(source, args.fleet)
    try:
        selected = read_selection(root)
        external = []
        if selected is not None:
            selected_plan = read_plan(root, selected["plan_id"])
            external = [path for path in selected_plan.effects["fleet_manifests"].values()
                        if not Path(path).is_relative_to(root)]
    except (ActivationError, PlanError, KeyError, TypeError) as exc:
        raise CommandFailure("conflict", "conflict: selected fleet declarations cannot be verified") from exc
    destination = _flat_destination(root, args.fleet)
    changed, prior, created = _copy_config(content, destination, replace=args.replace_config)
    try:
        staged = _config_plan(SimpleNamespace(release=executing, fleet_path=external), root)
        if args.fleet not in staged.data["fleets"]:
            raise CommandFailure("conflict", "conflict: authored fleet name differs from --fleet; no activation performed",
                                 release_id=executing)
    except BaseException as exc:
        # Staging refused before activation: put the declaration back so the
        # stray copy cannot block every later plan. Activation is never undone.
        state = _restore_config(destination, content, prior, created) if changed else "unchanged"
        if not isinstance(exc, CommandFailure):
            raise
        raise CommandFailure(exc.error.code, f"{exc.error.message}; fleet declaration {state}",
                             data={**exc.data, "config": str(destination), "config_declaration": state},
                             release_id=exc.release_id or executing,
                             hint=(f"correct {source} and rerun fleet setup; "
                                   f"claudlobby --root {shlex.quote(str(root))} --fleet {args.fleet} config validate "
                                   "reports authored errors for an installed declaration")) from exc
    activation = _activate(SimpleNamespace(plan_id=staged.data["plan_id"],
                                            install_directory=args.install_directory,
                                            activation_id=args.activation_id, adopt_existing=False), root)
    return CommandOutput({**activation.data, "fleet": args.fleet, "config": str(destination),
                          "config_copied": changed}, activation.release_id,
                         (f"Fleet {args.fleet} configured; {activation.lines[0]}",))


def dispatch(args) -> CommandOutput:
    return _host_setup(args) if args.public_command == "host.setup" else _fleet_setup(args)
