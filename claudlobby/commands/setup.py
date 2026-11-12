"""Managed Git host preparation and canonical first-fleet setup."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile

from ..command_result import CommandFailure, CommandOutput
from ..setup_observations import RUNTIME_EXECUTABLES


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
    from ..bootstrap import prepare_git_installation, BundleError
    from ..resources import get_resources
    from ..supervision_inventory import Adapter, InventoryError, _catalog

    try:
        package = get_resources()
        manager, domain, directories, _, _ = _catalog(Adapter(package).read("svc_inventory_catalog"))
    except (RuntimeError, InventoryError, OSError) as exc:
        raise CommandFailure("unavailable", "unavailable: installed package or native user manager") from exc
    # Bot startup silently skips consent pre-acceptance and trust seeding without jq.
    missing = [binary for binary in RUNTIME_EXECUTABLES if shutil.which(binary) is None]
    if missing:
        raise CommandFailure("unavailable", "unavailable: required bot runtime executable: " + ", ".join(missing))
    root = _root(args, create=True)
    hint = "inspect the retained installation and retry host setup with the same explicit inputs"
    try:
        prepared = prepare_git_installation(Path(args.installation).expanduser(), root,
                                           remote=args.remote, ref=args.ref,
                                           interpreter=Path(args.interpreter).expanduser())
    except BlockingIOError as exc:
        raise CommandFailure("conflict", "conflict: Git preparation is already in progress",
                             retryable=True, hint=hint) from exc
    except OSError as exc:
        raise CommandFailure("unavailable", "unavailable: Git preparation filesystem or executable; partial work retained",
                             hint=hint) from exc
    except (ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, BundleError) and str(exc) in {
                "Git preparation requires an independent explicit host Python",
                "Git preparation requires an explicit trusted remote and ref"}:
            raise CommandFailure("invalid_argument", "invalid argument: " + str(exc)) from exc
        # Git/Python output and arbitrary retained metadata never enter the public result.
        raise CommandFailure("conflict", "conflict: Git preparation refused; existing installation retained",
                             hint=hint) from exc
    return CommandOutput(
        {**prepared, "root": str(root), "manager": manager, "domain": domain,
         "install_directories": [str(path) for path in directories]},
        None,
        (f"Git installation prepared at {prepared['installation']} (revision {prepared['source_revision']}).",),
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
    # The canonical parser decides validity from these bytes alone (no projects
    # file is read), before any declaration or update record exists. Its message
    # can quote authored values, so none of it is reported.
    from ..config import load_fleet_snapshot

    try:
        load_fleet_snapshot(source, content, None)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CommandFailure("conflict", "conflict: fleet configuration is not valid; no declaration written",
                             hint=f"correct {source} using documentation/fleet-yaml-schema.md, then rerun fleet setup") from None
    return content


def _template_config(template: str, fleet: str, repository: str | None, goal: str | None) -> bytes:
    """Materialize the one package-owned candidate; never use a source checkout."""
    import yaml

    from ..config import ScopeConfig, load_fleet_snapshot
    from ..resources import get_resources

    if template != "revenue-web-app":
        raise CommandFailure("invalid_argument", "invalid argument: unsupported team template")
    parts = repository.split("/") if isinstance(repository, str) else []
    if (len(parts) != 2
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", parts[0])
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", parts[1])
            or parts[1] in (".", "..")):
        raise CommandFailure("invalid_argument", "invalid argument: --template requires an explicit OWNER/REPO")
    if (not isinstance(goal, str) or not goal.strip() or len(goal) > 2000
            or goal != goal.strip() or any(ord(char) < 32 or ord(char) == 127 for char in goal)):
        raise CommandFailure("invalid_argument", "invalid argument: --template requires a single-paragraph --goal of 1–2000 characters")
    try:
        source = get_resources().seeds / "examples/teams/revenue-web-app/fleet.yaml"
        if source.is_symlink() or not source.is_file():
            raise ValueError("missing template")
        document = yaml.safe_load(source.read_bytes())
        body = document["fleet"]
        body["name"] = fleet
        body["service_prefix"] = f"com.claudlobby.{fleet}"
        body["mission"] = f"Approved goal: {goal}. {body['mission']}"
        scope = ScopeConfig(org=parts[0], repos=[repository])
        body["defaults"]["scope"] = {"org": scope.org, "repos": scope.repos}
        content = yaml.safe_dump(document, sort_keys=False, allow_unicode=True).encode("utf-8")
        load_fleet_snapshot(source, content, None)
        return content
    except (RuntimeError, OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        raise CommandFailure("unavailable", "unavailable: installed team template; inspect the managed Git installation") from exc


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
                             hint=f"edit {observed / 'fleet.yaml'} in place, review the authored changes, then use host update apply")
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
    from ..git_update import UpdateError, UpdateStore, _prepare_update_locked, apply_or_continue
    from ..resources import get_resources
    from .operator_context import require_operator_context

    root = _root(args)
    require_operator_context()
    if not getattr(args, "install_directory", None):
        raise CommandFailure("invalid_argument", "invalid argument: --install-directory is required")
    if args.seed or not args.fleet or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.fleet):
        raise CommandFailure("invalid_argument", "invalid argument: fleet setup requires one exact --fleet name")
    package = get_resources()
    installation = package.installation
    if installation is None or installation.data_root != root:
        raise CommandFailure("release_mismatch", "fleet setup requires this data root's managed Git CLI")
    hint = "inspect the original operation with host update status; resume it with host update continue"
    changed, prior, created = False, None, []
    destination = None
    declaration = "unchanged"
    prepared_request = None
    try:
        # Serialize declaration authoring with update preparation. An original
        # pending operation must never be retargeted by reading new setup input.
        with UpdateStore(installation) as store:
            try:
                record = store.load()
                if record is not None:
                    raise CommandFailure("conflict", "fleet setup refuses an existing update operation", hint=hint)
                if os.path.lexists(root / "state/config.json"):
                    raise CommandFailure("conflict", "fleet setup supports only the first installation")
                template = getattr(args, "template", None)
                if template is not None:
                    if args.config is not None:
                        raise CommandFailure("invalid_argument", "invalid argument: --config and --template are mutually exclusive")
                    content = _template_config(template, args.fleet, getattr(args, "repository", None), getattr(args, "goal", None))
                else:
                    if getattr(args, "repository", None) is not None or getattr(args, "goal", None) is not None:
                        raise CommandFailure("invalid_argument", "invalid argument: --repository and --goal require --template")
                    source = Path(args.config).expanduser().resolve(strict=True)
                    content = _authored_config(source, args.fleet)
                destination = _flat_destination(root, args.fleet)
                if template is not None and os.path.lexists(destination.parent / "projects.yaml"):
                    raise CommandFailure("conflict", "template target has an existing project declaration; use authored --config")
                changed, prior, created = _copy_config(content, destination, replace=args.replace_config)
                record = _prepare_update_locked(store, target=package.source_revision,
                    initialization={"directory": str(Path(args.install_directory).expanduser()),
                                    "manifests": [str(destination)]}, prepare_only=True)
                prepared_request = record["request_id"]
                result = {"update": record, "dispatch": "prepared"}
            except BaseException:
                # Rollback belongs to this same lock, before any original record exists.
                if changed and destination is not None and store.load() is None:
                    declaration = _restore_config(destination, content, prior, created)
                raise
        if not getattr(args, "plan_only", False):
            result = apply_or_continue(installation, expected_request_id=prepared_request)
    except BaseException as exc:
        if isinstance(exc, CommandFailure):
            raise
        if isinstance(exc, (UpdateError, OSError, ValueError, RuntimeError)):
            raise CommandFailure("conflict", "first fleet preparation refused; retained state requires inspection",
                                 data={"config_declaration": declaration,
                                       **({"request_id": prepared_request} if prepared_request else {})}, hint=hint) from exc
        raise
    record = result["update"]
    prepared = getattr(args, "plan_only", False)
    return CommandOutput({**result, "fleet": args.fleet, "config": str(destination),
                          "config_copied": changed, "source_revision": record["target"],
                          "config_fingerprint": record["config_fingerprint"]}, None,
                         (("First fleet prepared; resume with host update continue." if prepared else
                           "First fleet update submitted; inspect host update status."),))


def dispatch(args) -> CommandOutput:
    return _host_setup(args) if args.public_command == "host.setup" else _fleet_setup(args)
