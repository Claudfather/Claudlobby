"""Serial selected-fleet lifecycle and read-only supervision reconciliation.

Each mutation delegates to the single-bot owner, which admits the current
runtime, proves native effects, and preserves stopped or disabled intent.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .supervision_runtime import assert_quiescent
from .activation_state import ActivationError
from .bot_operations import (BotLifecycleError, BotLifecycleResult,
                             _current_bot_binding, set_bot_running)
from .operation_context import resolve_operation_scope
from .runtime_admission import GitCurrentRuntime, runtime_provenance
from .stop_record import read_stop
from .supervision_inventory import Adapter, InventoryError, collect_enrollment


class FleetLifecycleError(RuntimeError):
    """A refused or incomplete sweep retains every earlier bot's result."""

    def __init__(self, reason: str, *, completed=(), bot: str | None = None,
                 effect_attempted: bool = False, unavailable: bool = False,
                 release_id: str | None = None, source_revision: str | None = None,
                 config_fingerprint: str | None = None):
        self.completed = tuple(completed)
        self.bot = bot
        self.effect_attempted = effect_attempted
        self.unavailable = unavailable
        self.release_id = release_id
        self.source_revision, self.config_fingerprint = source_revision, config_fingerprint
        super().__init__(reason)


@dataclass(frozen=True)
class FleetLifecycleResult:
    fleet: str
    release_id: str | None
    action: str
    workers_only: bool
    completed: tuple[BotLifecycleResult, ...]
    source_revision: str | None = None
    config_fingerprint: str | None = None


@dataclass(frozen=True)
class FleetBotObservation:
    bot: str
    declared: bool
    enrolled: bool
    native_active: str
    session: str
    state: str
    target: str


@dataclass(frozen=True)
class FleetReconcileResult:
    fleet: str
    release_id: str | None
    bots: tuple[FleetBotObservation, ...]
    source_revision: str | None = None
    config_fingerprint: str | None = None


def _scope(root: Path, fleet: str | None, *, mutating: bool):
    destination, origin = resolve_operation_scope(root=root, fleet=fleet)
    if mutating and origin is not None and (origin.fleet.name != destination.fleet.name
                                            or origin.bot_id != destination.fleet.manager):
        raise FleetLifecycleError("only the selected fleet manager may operate its fleet")
    package = destination.paths.package
    if package.installation is None:
        raise FleetLifecycleError("fleet lifecycle requires a managed Git installation")
    from .active_config import read_current_config
    snapshot = read_current_config(destination.paths.root, installation=package.installation,
                                   revision=package.source_revision)
    current = GitCurrentRuntime(package.installation, snapshot["revision"], snapshot["fingerprint"], snapshot)
    return destination, origin, current


def _release_id(current):
    return None


def _targets(destination, *, workers_only: bool) -> tuple[str, ...]:
    manager = destination.fleet.manager
    workers = tuple(sorted(bot for bot in destination.fleet.bots if bot != manager))
    return workers if workers_only else (*workers, manager)


def set_fleet_running(*, root: Path, fleet: str | None, action: str,
                      workers_only: bool = False) -> FleetLifecycleResult:
    """Run one bot at a time; a later refusal cannot erase earlier outcomes."""
    if action not in {"start", "stop", "restart"} or type(workers_only) is not bool:
        raise FleetLifecycleError("select start, stop or restart and a boolean worker scope")
    destination, origin, selected = _scope(root, fleet, mutating=True)
    if origin is not None and not workers_only:
        # A manager's own stop cuts off this process; its detached self-restart
        # witnesses only that one bot, not a fleet sweep. Refuse before effects.
        raise FleetLifecycleError("manager-origin fleet lifecycle requires --workers; "
                                  "run the full sweep from an operator process",
                                  release_id=_release_id(selected), **runtime_provenance(selected))
    completed = []
    for bot in _targets(destination, workers_only=workers_only):
        try:
            result = set_bot_running(root=destination.paths.root, fleet=destination.fleet.name,
                                     bot=bot, running=action != "stop",
                                     restart=action == "restart",
                                     _expected_provenance=(None, runtime_provenance(selected)))
        except BotLifecycleError as exc:
            if (action == "restart" and exc.skip_reason in {"de_enrolled", "disabled"}
                    and not exc.effect_attempted):
                # Like the native rolling/weekly restart doors, a deliberately
                # stopped or disabled bot stays untouched; continue past it.
                completed.append(BotLifecycleResult(
                    destination.fleet.name, bot, exc.release_id or _release_id(selected),
                    exc.target or "", "skipped", False, "not_checked", reason=exc.skip_reason,
                    **runtime_provenance(selected)))
                continue
            raise FleetLifecycleError("fleet lifecycle stopped at an unverified bot",
                                      completed=completed, bot=bot,
                                      effect_attempted=exc.effect_attempted,
                                      unavailable=exc.unavailable,
                                      release_id=exc.release_id or _release_id(selected), **runtime_provenance(selected)) from exc
        except Exception as exc:
            raise FleetLifecycleError("fleet lifecycle stopped at unavailable bot evidence",
                                      completed=completed, bot=bot, unavailable=True,
                                      release_id=_release_id(selected), **runtime_provenance(selected)) from exc
        completed.append(result)
    return FleetLifecycleResult(destination.fleet.name, _release_id(selected), action,
                                workers_only, tuple(completed), **runtime_provenance(selected))


def reconcile_fleet(*, root: Path, fleet: str | None, bot: str | None = None,
                    adapter: Adapter | None = None) -> FleetReconcileResult:
    """Report declared, enrolled, native-active and private-session evidence separately."""
    destination, _, selected = _scope(root, fleet, mutating=False)
    if bot is not None and bot not in destination.fleet.bots:
        raise FleetLifecycleError("bot is not declared in the selected fleet")
    bindings = {}
    # The single-bot owner already binds committed placement, native domain,
    # generated bytes and bot.conf. Reconciliation uses those same checks.
    for name in ((bot,) if bot is not None else _targets(destination, workers_only=False)):
        try:
            binding = _current_bot_binding(destination.paths.root, selected, destination, name, adapter)
            binding.verify_configuration()
        except (BotLifecycleError, ValueError, OSError) as exc:
            raise FleetLifecycleError("fleet reconciliation has unavailable committed bot evidence",
                                      bot=name, unavailable=True, **runtime_provenance(selected)) from exc
        bindings[name] = binding
        adapter = binding.adapter
    declarations = next(iter(bindings.values())).declarations
    platform = next(iter(bindings.values())).manager
    bot_names = frozenset(declaration.source.name for declaration in declarations
                          if declaration.scope == "bot" and declaration.fleet == destination.fleet.name
                          and (bot is None or declaration.bot == bot))
    inventory = collect_enrollment(destination.paths.root, declarations, adapter=adapter,
                                   only_names=bot_names).require_complete()
    completed = []
    for bot in ((bot,) if bot is not None else _targets(destination, workers_only=False)):
        try:
            entry = bindings[bot].entry
            units = [unit for unit in inventory.units if unit.declaration.scope == "bot"
                     and unit.declaration.fleet == destination.fleet.name
                     and unit.declaration.bot == bot]
            if (len(units) != 1 or units[0].target != entry["target"]
                    or len(units[0].installed) > 1
                    or units[0].installed and units[0].installed[0].path != entry["installed"]):
                raise InventoryError("selected bot placement differs from native inventory")
            unit = units[0]
            spec = bindings[bot].spec
            if spec.bot_dir != unit.declaration.working_directory:
                raise InventoryError("selected bot directory differs from native inventory")
            observation = adapter.call("svc_bot_session_observe", spec.bot_dir, spec.label,
                                       spec.environment["TMUX_TMPDIR"], entry["installed"],
                                       entry["target"])
            if observation.returncode or observation.stdout.strip() not in {"ready", "absent", "unknown"}:
                raise InventoryError("private bot session observation is unavailable")
            session = observation.stdout.strip()
            if session == "unknown":
                # A clean stop leaves tmux's socket file (bot stop keeps it), and the
                # observer reads that as unknown. As bot move does, only the kernel
                # proof (inactive exact unit, empty cgroup where witnessed, socket
                # refusing connections) reads it as absent; nothing is removed (#2227).
                socket = (Path(spec.environment["TMUX_TMPDIR"]) / f"tmux-{os.getuid()}"
                          / spec.label)
                try:
                    assert_quiescent(adapter, installed_file=Path(entry["installed"]),
                                     target=entry["target"], socket_path=socket)
                    session = "absent"
                except (RuntimeError, OSError):
                    pass
            enrolled = bool(unit.installed)
            active = dict(unit.properties).get("ActiveState", "unknown")
            if active not in {"active", "inactive", "unknown"}:
                raise InventoryError("native active state is unfamiliar")
            # #2243 F8: the stop door's record beside no enrolled unit and no ready
            # session is a deliberate stop, whatever the probe could tell (absent,
            # or unknown where the quiet proof fails); the session field says which.
            stopped = not enrolled and session != "ready" and read_stop(spec.bot_dir) is not None
            state = ("stopped" if stopped else
                     "indeterminate" if session == "unknown" or active == "unknown" else
                     "healthy" if enrolled and session == "ready" else
                     "orphan" if session == "ready" else
                     "missing" if enrolled else "unsupervised_down")
            completed.append(FleetBotObservation(bot, True, enrolled, active, session,
                                                 state, entry["target"]))
        except (ActivationError, InventoryError, OSError) as exc:
            raise FleetLifecycleError("fleet reconciliation stopped at unavailable bot evidence",
                                      completed=completed, bot=bot,
                                      unavailable=True,
                                      release_id=_release_id(selected), **runtime_provenance(selected)) from exc
    from .active_config import read_current_config
    if read_current_config(destination.paths.root, installation=selected.installation,
                           revision=selected.revision) != selected.snapshot:
        raise FleetLifecycleError("committed fleet configuration changed during reconciliation",
                                  completed=completed, unavailable=True, **runtime_provenance(selected))
    return FleetReconcileResult(destination.fleet.name, _release_id(selected), tuple(completed),
                                **runtime_provenance(selected))
