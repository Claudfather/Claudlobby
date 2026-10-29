"""Private owner serving through the existing host unit and activation owners."""

from contextvars import ContextVar
from functools import wraps
import os
from pathlib import Path
import subprocess

from ..activation_state import ActivationRefusal


def service_command(root, cli, config):
    """Validate explicit host configuration, returning literal CLI arguments."""
    from .owner_browser import _origin
    from .owner_ingress import ServePrincipalVerifier
    from .owner_workspace_http import WorkspaceBootstrapConfig

    values = {key: config.get(key) for key in (
        "origin", "tailscale", "tailscale_socket", "socket", "workspace_issuer", "workspace_origin")}
    for key, value in values.items():
        if value is not None and (not isinstance(value, str) or not value or not value.isprintable()):
            raise ValueError(f"host.jobs.plane-owner.{key} must be a nonempty printable string")
    _origin(values["origin"])
    if values["tailscale"] is None:
        raise ValueError("host.jobs.plane-owner.tailscale requires an absolute native executable")
    ServePrincipalVerifier(tailscale_binary=Path(values["tailscale"]),
                           tailscale_socket=Path(values["tailscale_socket"]) if values["tailscale_socket"] else None)
    if values["socket"] is not None and not Path(values["socket"]).is_absolute():
        raise ValueError("host.jobs.plane-owner.socket requires an absolute private socket path")
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
        if item["source"] not in installed or not item["enroll"] or item["admission"] is None:
            continue  # a unit template (#2522) has no admission and listens on nothing
        unit = parse_unit_argv(item["admission"]["argv"])
        path = unit_socket(unit)
        if path is not None:
            result.add((unit.unit, path))
    return result


# Process proof lasts for one synchronous adapter invocation, never a server
# instance/session. Context-local state keeps concurrent requests independent.
_ACTION_PROCESS = ContextVar('owner_action_process', default=None)
_ACTION_SELECTORS = ('BOT_ID', 'BOT_NAME', 'BOT_DIR', 'BOT_SERVICE', 'FLEET_ROOT',
                     'FLEET_NAME', 'CLAUDLOBBY_FLEET', 'CLAUDLOBBY_TIMER_CONTEXT')
_ACTION_IDENTITY = ('CLAUDLOBBY_ROOT', 'CLAUDLOBBY_RELEASE_ID', 'CLAUDLOBBY_CLI',
                    'CLAUDLOBBY_NATIVE_DIR', 'CLAUDLOBBY_ARTIFACT_ID')


def _action_context(root, package):
    from .owner_access import AccessDenied

    if any(key in os.environ for key in _ACTION_SELECTORS):
        raise AccessDenied('generated_context_refused')
    return (root, package.native, package.artifact_id,
            tuple((key, os.environ.get(key)) for key in _ACTION_IDENTITY))


def check_action_process(root, package):
    """Recheck selectors at each authorization; never inherit another call's proof."""
    from .owner_access import AccessDenied

    context = _action_context(root, package)
    proof = _ACTION_PROCESS.get()
    if proof is not None and proof != context:
        raise AccessDenied('generated_context_refused')
    if any(key in os.environ for key in _ACTION_IDENTITY[:2]) and proof != context:
        raise AccessDenied('generated_context_refused')


def admitted_action(method):
    """Admit direct operators as before; positively prove generated owner service."""
    @wraps(method)
    def invoke(self, *args, **kwargs):
        from ..activation_state import ActivationError, read_selection
        from ..config_plan import PlanError, read_plan
        from ..config_units import planned_units
        from ..runtime_admission import RuntimeIdentity, mutation_admission, parse_unit_argv
        from ..supervision_inventory import Adapter, FileSnapshot, InventoryError, _catalog, collect_enrollment
        from .owner_access import AccessDenied

        context = _action_context(self.root, self.package)
        # No environment exception: even matching root/release needs native
        # enrollment and positive ancestry. Foreground calls keep their contract.
        if not any(key in os.environ for key in _ACTION_IDENTITY[:2]):
            return method(self, *args, **kwargs)
        try:
            identity = RuntimeIdentity(RuntimeIdentity.current().cli, self.package.native,
                                       self.package.artifact_id)
            expected = (str(self.root), os.environ.get('CLAUDLOBBY_RELEASE_ID'),
                        str(identity.cli), str(identity.native), identity.artifact_id)
            if (not expected[1] or tuple(value for _, value in context[3]) != expected):
                raise AccessDenied('generated_context_refused')
            with mutation_admission(self.root, identity=identity, expected_release=expected[1]):
                selected = read_selection(self.root)
                plan = read_plan(self.root, selected['plan_id'])
                adapter = Adapter(self.package)
                manager = _catalog(adapter.read('svc_inventory_catalog'))[0]
                owners = []
                for declaration, item in planned_units(plan, manager):
                    if item['admission'] is None:
                        continue  # a unit template (#2522) has no admission and is never the owner
                    unit = parse_unit_argv(item['admission']['argv'])
                    if (item['enroll'] and declaration.scope == 'host'
                            and declaration.fleet is None and declaration.bot is None
                            and declaration.service is None and unit.phase == 'producers'
                            and unit.mode == 'exec' and unit.root == self.root
                            and unit.release_id == expected[1]
                            and tuple(dict(declaration.environment).get(key)
                                      for key in _ACTION_IDENTITY) == expected
                            and unit.command[:6] == (str(identity.cli), '--root',
                                str(self.root), 'host', 'owner', 'serve')
                            and unit_socket(unit) is not None):
                        owners.append((declaration, item))
                if len(owners) != 1:
                    raise AccessDenied('generated_context_refused')
                declaration, item = owners[0]
                frozen = FileSnapshot.read(declaration.source)
                if (frozen.link is not None or frozen.sha256 != item['sha256']
                        or frozen.mode != item['mode']):
                    raise AccessDenied('generated_context_refused')
                inventory = collect_enrollment(self.root, (declaration,), adapter=adapter,
                    only_names=frozenset({declaration.source.name}))
                inventory.require_complete()
                if len(inventory.units) != 1 or len(inventory.units[0].installed) != 1:
                    raise AccessDenied('generated_context_refused')
                enrolled = inventory.units[0]
                if enrolled.generated != frozen:
                    raise AccessDenied('generated_context_refused')
                proof = adapter.call('svc_activation_assert_external', enrolled.installed[0].path,
                                     enrolled.target, str(os.getpid()))
                if proof.returncode != 1:
                    raise AccessDenied('generated_context_refused')
                token = _ACTION_PROCESS.set(context)
                try:
                    return method(self, *args, **kwargs)
                finally:
                    _ACTION_PROCESS.reset(token)
        except (ActivationError, PlanError, InventoryError, OSError,
                subprocess.SubprocessError) as exc:
            raise AccessDenied('generated_context_refused') from exc
    return invoke
