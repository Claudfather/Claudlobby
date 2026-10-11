"""Private owner serving through the existing host unit and activation owners."""

from pathlib import Path

from ..activation_state import ActivationRefusal


def service_command(root, cli, config):
    """Validate explicit host configuration, returning literal CLI arguments."""
    from .owner_browser import _origin
    from .owner_ingress import ServePrincipalVerifier
    from .owner_workspace_http import WorkspaceBootstrapConfig

    values = {key: config.get(key) for key in (
        "origin", "tailscale", "socket", "workspace_issuer", "workspace_origin")}
    for key, value in values.items():
        if value is not None and (not isinstance(value, str) or not value or not value.isprintable()):
            raise ValueError(f"host.jobs.plane-owner.{key} must be a nonempty printable string")
    _origin(values["origin"])
    if values["tailscale"] is None:
        raise ValueError("host.jobs.plane-owner.tailscale requires an absolute native executable")
    ServePrincipalVerifier(tailscale_binary=Path(values["tailscale"]))
    if values["socket"] is None or not Path(values["socket"]).is_absolute():
        raise ValueError("host.jobs.plane-owner.socket requires the absolute private socket path used by Serve")
    if values["workspace_issuer"] is not None or values["workspace_origin"] is not None:
        WorkspaceBootstrapConfig(values["workspace_issuer"], values["workspace_origin"])
    command = [str(cli), "--root", str(root), "host", "owner", "serve"]
    for key, value in values.items():
        if value is not None:
            command.extend(["--" + key.replace("_", "-"), value])
    return command


def unit_socket(unit):
    """Recognize only the sealed CLI's exact host-owner command."""
    prefix = (str(Path(unit.argv[0]).with_name("claudlobby")), "--root", str(unit.root),
              "host", "owner", "serve")
    if unit.command[:6] != prefix:
        return None
    arguments = unit.command[6:]
    if len(arguments) % 2 or len(set(arguments[::2])) != len(arguments[::2]):
        raise ActivationRefusal("owner service has malformed frozen arguments")
    options = dict(zip(arguments[::2], arguments[1::2]))
    return Path(options.get("--socket", str(unit.root / "state/plane/owner.sock")))


def preflight(starts, *, managed=()):
    """Refuse foreground conflicts; normal activation quiesces managed units."""
    from .owner_server import check_listener_available, OwnerServerConfigurationError

    for _, _, unit in starts.values():
        path = unit_socket(unit)
        if path is None or (unit.unit, path) in managed:
            continue
        try:
            check_listener_available(path)
        except (OwnerServerConfigurationError, OSError) as exc:
            raise ActivationRefusal(
                "private owner service cannot start: stop the foreground owner server, "
                "inspect its private socket directory, and retry activation") from exc


def managed_listeners(source_plan, inventory):
    """Use the selected plan and verified enrollment, never a label alone."""
    from ..runtime_admission import parse_unit_argv

    if source_plan is None:
        return set()
    installed = {str(unit.declaration.source) for unit in inventory.units if unit.installed}
    result = set()
    for item in source_plan.effects.get("units", ()):
        if item["source"] not in installed or not item["enroll"]:
            continue
        unit = parse_unit_argv(item["admission"]["argv"])
        path = unit_socket(unit)
        if path is not None:
            result.add((unit.unit, path))
    return result
