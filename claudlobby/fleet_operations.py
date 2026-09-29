"""Serial selected-fleet lifecycle and read-only supervision reconciliation.

Each mutation delegates to the single-bot owner, which admits the selected
release, proves native effects, and makes a stopped bot stay de-enrolled.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .activation_enrollment import selected_bot_entry
from .activation_state import ActivationError, read_selection
from .bot_operations import BotLifecycleError, BotLifecycleResult, _selected_adapter, set_bot_running
from .config_plan import read_plan
from .config_units import current_declarations
from .operation_context import resolve_operation_scope
from .supervision import build_supervision_spec
from .supervision_inventory import Adapter, InventoryError, collect_enrollment


class FleetLifecycleError(RuntimeError):
    """A refused or incomplete sweep retains every earlier bot's result."""

    def __init__(self, reason: str, *, completed=(), bot: str | None = None,
                 effect_attempted: bool = False, unavailable: bool = False,
                 release_id: str | None = None):
        self.completed = tuple(completed)
        self.bot = bot
        self.effect_attempted = effect_attempted
        self.unavailable = unavailable
        self.release_id = release_id
        super().__init__(reason)


@dataclass(frozen=True)
class FleetLifecycleResult:
    fleet: str
    release_id: str
    action: str
    workers_only: bool
    completed: tuple[BotLifecycleResult, ...]


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
    release_id: str
    bots: tuple[FleetBotObservation, ...]


def _scope(root: Path, fleet: str | None, *, mutating: bool):
    destination, origin = resolve_operation_scope(root=root, fleet=fleet)
    if mutating and origin is not None and (origin.fleet.name != destination.fleet.name
                                            or origin.bot_id != destination.fleet.manager):
        raise FleetLifecycleError("only the selected fleet manager may operate its fleet")
    selected = read_selection(destination.paths.root)
    if selected is None:
        raise ActivationError("fleet lifecycle requires an active selected release")
    return destination, origin, selected


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
                                  release_id=selected["release_id"])
    completed = []
    for bot in _targets(destination, workers_only=workers_only):
        try:
            result = set_bot_running(root=destination.paths.root, fleet=destination.fleet.name,
                                     bot=bot, running=action != "stop",
                                     restart=action == "restart")
        except BotLifecycleError as exc:
            raise FleetLifecycleError("fleet lifecycle stopped at an unverified bot",
                                      completed=completed, bot=bot,
                                      effect_attempted=exc.effect_attempted,
                                      unavailable=exc.unavailable,
                                      release_id=exc.release_id or selected["release_id"]) from exc
        except Exception as exc:
            raise FleetLifecycleError("fleet lifecycle stopped at unavailable bot evidence",
                                      completed=completed, bot=bot, unavailable=True,
                                      release_id=selected["release_id"]) from exc
        completed.append(result)
    return FleetLifecycleResult(destination.fleet.name, selected["release_id"], action,
                                workers_only, tuple(completed))


def reconcile_fleet(*, root: Path, fleet: str | None,
                    adapter: Adapter | None = None) -> FleetReconcileResult:
    """Report declared, enrolled, native-active and private-session evidence separately."""
    destination, _, selected = _scope(root, fleet, mutating=False)
    plan = read_plan(destination.paths.root, selected["plan_id"])
    if plan.release_id != selected["release_id"]:
        raise FleetLifecycleError("selected fleet plan differs from selected release")
    adapter = adapter or Adapter(destination.paths.package)
    if adapter.package.native != destination.paths.package.native:
        raise FleetLifecycleError("fleet native adapter differs from selected release")
    adapter = _selected_adapter(destination.paths.root, destination.fleet.name,
                                destination.fleet.manager, adapter)
    catalog = adapter.read("svc_inventory_catalog")
    from .supervision_inventory import _catalog
    platform, _, _, _, _ = _catalog(catalog)
    declarations = current_declarations(plan, platform)
    bot_names = frozenset(declaration.source.name for declaration in declarations
                          if declaration.scope == "bot" and declaration.fleet == destination.fleet.name)
    inventory = collect_enrollment(destination.paths.root, declarations, adapter=adapter,
                                   only_names=bot_names).require_complete()
    completed = []
    for bot in _targets(destination, workers_only=False):
        try:
            entry = selected_bot_entry(destination.paths.root, destination.fleet.name, bot, platform)
            units = [unit for unit in inventory.units if unit.declaration.scope == "bot"
                     and unit.declaration.fleet == destination.fleet.name
                     and unit.declaration.bot == bot]
            if (len(units) != 1 or units[0].target != entry["target"]
                    or len(units[0].installed) > 1
                    or units[0].installed and units[0].installed[0].path != entry["installed"]):
                raise InventoryError("selected bot placement differs from native inventory")
            unit = units[0]
            spec = build_supervision_spec(destination.fleet.bots[bot], destination.fleet,
                                          destination.paths)
            if spec.bot_dir != unit.declaration.working_directory:
                raise InventoryError("selected bot directory differs from native inventory")
            observation = adapter.call("svc_bot_session_observe", spec.bot_dir, spec.label,
                                       spec.environment["TMUX_TMPDIR"], entry["installed"],
                                       entry["target"])
            if observation.returncode or observation.stdout.strip() not in {"ready", "absent", "unknown"}:
                raise InventoryError("private bot session observation is unavailable")
            session = observation.stdout.strip()
            enrolled = bool(unit.installed)
            active = dict(unit.properties).get("ActiveState", "unknown")
            if active not in {"active", "inactive", "unknown"}:
                raise InventoryError("native active state is unfamiliar")
            state = ("indeterminate" if session == "unknown" or active == "unknown" else
                     "healthy" if enrolled and session == "ready" else
                     "orphan" if session == "ready" else
                     "missing" if enrolled else "unsupervised_down")
            completed.append(FleetBotObservation(bot, True, enrolled, active, session,
                                                 state, entry["target"]))
        except (ActivationError, InventoryError, OSError) as exc:
            raise FleetLifecycleError("fleet reconciliation stopped at unavailable bot evidence",
                                      completed=completed, bot=bot,
                                      unavailable=True,
                                      release_id=selected["release_id"]) from exc
    return FleetReconcileResult(destination.fleet.name, selected["release_id"], tuple(completed))
