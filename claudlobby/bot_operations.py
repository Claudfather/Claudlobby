"""Selected-release bot enrollment and de-enrollment.

The active activation journal owns native placement; the sealed configuration
owns bot identity. This module observes one exact target before and after the
existing native supervisor adapter acts. It never purges a bot or changes the
active roster, release selection, or retained actor bindings.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat
import subprocess

from .activation_enrollment import selected_bot_entry, _target
from .activation_runtime import assert_quiescent
from .activation_state import ActivationError, read_selection
from .config_plan import path_state, read_plan
from .config_units import current_declarations, planned_units
from .operation_context import resolve_operation_scope
from .runtime_admission import RuntimeIdentity, mutation_admission, validate_unit_admission
from .supervision import build_supervision_spec
from .supervision_inventory import Adapter, InventoryError, _catalog, collect_enrollment


class BotLifecycleError(RuntimeError):
    def __init__(self, reason: str, *, effect_attempted: bool = False,
                 unavailable: bool = False, release_id: str | None = None,
                 target: str | None = None):
        self.effect_attempted = effect_attempted
        self.unavailable = unavailable
        self.release_id = release_id
        self.target = target
        super().__init__(reason)


@dataclass(frozen=True)
class BotLifecycleResult:
    fleet: str
    bot: str
    release_id: str
    target: str
    state: str
    changed: bool
    readiness: str
    handoff: str = "not_applicable"


@contextmanager
def _operation_lock(root: Path):
    """Serialize selected bot operations after host mutation admission."""
    path = root / "state/bot-lifecycle.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        linked = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino)):
            raise BotLifecycleError("bot lifecycle lock is not private or changed")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _selected_unit(root, plan, release, package, adapter, fleet, bot):
    manager, domain, directories, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
    declarations = current_declarations(plan, manager)
    entry = selected_bot_entry(root, fleet, bot, manager)
    selected = [(declaration, item) for declaration, item in planned_units(plan, manager)
                if declaration.scope == "bot" and declaration.fleet == fleet
                and declaration.bot == bot and item["enroll"]]
    if len(selected) != 1:
        raise BotLifecycleError("selected bot has no unique declared native unit")
    declaration, item = selected[0]
    target = _target(manager, domain, declaration.source)
    installed = Path(entry["installed"])
    if (entry["target"] != target or entry["source"] != str(declaration.source)
            or installed.parent not in directories or installed.parent.resolve() != installed.parent
            or installed == declaration.source or installed.is_symlink()):
        raise BotLifecycleError("selected bot native placement is redirected or ambiguous")
    validate_unit_admission(release, declaration, item, plan.blob(item["sha256"]))
    return manager, declaration, entry, declarations


def _observed(root, declarations, adapter, target, installed):
    inventory = collect_enrollment(root, declarations, adapter=adapter).require_complete()
    matches = [unit for unit in inventory.units if unit.target == target]
    if len(matches) != 1:
        raise BotLifecycleError("bot has no unique native inventory observation")
    unit = matches[0]
    if len(unit.installed) > 1 or unit.installed and unit.installed[0].path != str(installed):
        raise BotLifecycleError("bot has another installed native definition")
    return unit


def _native(adapter, function, *args, timeout=30):
    effect = function in {"svc_bot_enroll_exact", "svc_bot_disenroll_exact"}
    try:
        result = adapter.call(function, *args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BotLifecycleError(f"{function} native observation is unavailable",
                                effect_attempted=effect, unavailable=True) from exc
    if result.returncode:
        raise BotLifecycleError(f"{function} refused or native state is unknown ({result.returncode})",
                                effect_attempted=effect, unavailable=True)
    return result.stdout.strip()


def _confirm_stopped(adapter, installed, target, socket_path, *, effect_attempted=False):
    state = _native(adapter, "svc_inventory_state", installed, target)
    if state not in {"not-found not-found inactive", "unchanged unloaded inactive"}:
        raise BotLifecycleError("bot de-enrollment is not established",
                                effect_attempted=effect_attempted)
    try:
        assert_quiescent(adapter, installed_file=installed, target=target, socket_path=socket_path)
    except (ActivationError, OSError) as exc:
        raise BotLifecycleError("bot native/session quiescence is unavailable",
                                effect_attempted=effect_attempted, unavailable=True) from exc


def _confirm_running(adapter, installed, target, manager, *, session_ready=False,
                     effect_attempted=False):
    state = _native(adapter, "svc_inventory_state", installed, target)
    if (state != ("unchanged loaded active" if manager == "Darwin" else "enabled loaded active")
            and not (manager == "Darwin" and state == "unchanged loaded inactive"
                     and session_ready)):
        raise BotLifecycleError("bot native enrollment is not active",
                                effect_attempted=effect_attempted)


def set_bot_running(*, root: Path, fleet: str | None, bot: str, running: bool,
                    restart: bool = False, ceiling: int | None = None,
                    identity: RuntimeIdentity | None = None,
                    adapter: Adapter | None = None) -> BotLifecycleResult:
    """Start, stop or restart one exact selected bot; prove native effects."""
    if restart and not running:
        raise BotLifecycleError("restart requires a running target")
    if ceiling is not None and (not restart or isinstance(ceiling, bool)
                                or not isinstance(ceiling, int) or ceiling <= 0):
        raise BotLifecycleError("restart ceiling must be a positive integer")
    if not isinstance(bot, str) or not bot or Path(bot).name != bot or bot in {".", ".."}:
        raise BotLifecycleError("supply one exact bot ID")
    identity = identity or RuntimeIdentity.current()
    with mutation_admission(root, identity=identity,
                            expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
        with _operation_lock(root):
            destination, origin = resolve_operation_scope(root=root, fleet=fleet)
            if origin is not None and (origin.fleet.name != destination.fleet.name
                                       or origin.bot_id != destination.fleet.manager
                                       or bot == origin.bot_id):
                raise BotLifecycleError("only the selected fleet manager may operate another bot")
            if bot not in destination.fleet.bots:
                raise BotLifecycleError("bot is not declared in the selected active fleet")
            selected = read_selection(root)
            if selected is None or selected["release_id"] != release.release_id:
                raise BotLifecycleError("active selection changed before bot operation")
            plan = read_plan(root, selected["plan_id"])
            if plan.release_id != release.release_id or plan.release_seal != release.seal_sha256:
                raise BotLifecycleError("active bot plan differs from selected release")
            adapter = adapter or Adapter(destination.paths.package)
            if adapter.package.native != release.native_path:
                raise BotLifecycleError("bot native adapter differs from selected release")
            manager, declaration, entry, declarations = _selected_unit(
                root, plan, release, destination.paths.package, adapter, destination.fleet.name, bot)
            spec = build_supervision_spec(destination.fleet.bots[bot], destination.fleet,
                                          destination.paths)
            native_label = (entry["target"].removesuffix(".service") if manager == "Linux"
                            else entry["target"].split("/")[-1])
            if (spec.bot_dir != declaration.working_directory
                    or spec.label != declaration.source.stem or spec.label != native_label):
                raise BotLifecycleError("bot supervision spec differs from frozen native unit")
            installed = Path(entry["installed"])
            socket = Path(spec.environment["TMUX_TMPDIR"]) / f"tmux-{os.getuid()}" / spec.label
            unit = _observed(root, declarations, adapter, entry["target"], installed)
            if not running:
                if unit.installed:
                    try:
                        _native(adapter, "svc_bot_disenroll_exact", declaration.source, installed,
                                entry["target"], spec.bot_dir, spec.label,
                                spec.environment["TMUX_TMPDIR"])
                        _confirm_stopped(adapter, installed, entry["target"], socket,
                                         effect_attempted=True)
                        after = _observed(root, declarations, adapter, entry["target"], installed)
                        if after.installed:
                            raise BotLifecycleError("bot unit remains installed")
                    except (BotLifecycleError, ActivationError, InventoryError, OSError) as exc:
                        raise BotLifecycleError("bot stop effect or proof is incomplete",
                                                effect_attempted=True,
                                                release_id=release.release_id,
                                                target=entry["target"]) from exc
                else:
                    _confirm_stopped(adapter, installed, entry["target"], socket)
                return BotLifecycleResult(destination.fleet.name, bot, release.release_id,
                                          entry["target"], "stopped", bool(unit.installed), "not_running")
            bot_conf = spec.bot_dir / "bot.conf"
            changes = [change for change in plan.changes if change.target == str(bot_conf)]
            if len(changes) != 1 or changes[0].after.get("kind") != "file" or path_state(bot_conf)["node"] != changes[0].after:
                raise BotLifecycleError("generated bot.conf differs from selected frozen configuration")
            if restart:
                # A deliberate stop removed supervision. Restart may bounce only
                # an already enrolled exact unit; bot start owns re-enrollment.
                if not unit.installed:
                    raise BotLifecycleError("bot is de-enrolled; use bot start")
                if dict(unit.properties).get("ActiveState") not in {"active", "inactive"}:
                    raise BotLifecycleError("bot native active state is indeterminate",
                                            unavailable=True)
            handoff = "not_applicable"
            if restart:
                session = _native(adapter, "svc_bot_session_observe", spec.bot_dir,
                                  spec.label, spec.environment["TMUX_TMPDIR"])
                if session not in {"ready", "unknown", "absent"}:
                    raise BotLifecycleError("current bot session observation is indeterminate",
                                            unavailable=True)
                if session != "absent":
                    # The existing pre-stop door is best effort: even its rc 0
                    # means attempted, not that a new handoff file was written.
                    # Run it while the old tmux session still exists, before
                    # the native restart invokes start-bot's kill-session.
                    try:
                        attempted = adapter.call("svc_activation_handoff", spec.bot_dir,
                                                 spec.label, spec.environment["TMUX_TMPDIR"],
                                                 timeout=45)
                        handoff = "attempted" if attempted.returncode == 0 else "unavailable"
                    except (InventoryError, OSError, subprocess.SubprocessError):
                        handoff = "unavailable"
            if unit.installed and not restart:
                props = dict(unit.properties)
                if (props.get("ActiveState") == "active"
                        or manager == "Darwin" and props.get("ActiveState") == "inactive"):
                    session = _native(adapter, "svc_bot_session_observe", spec.bot_dir,
                                      spec.label, spec.environment["TMUX_TMPDIR"])
                    if session == "ready":
                        _confirm_running(adapter, installed, entry["target"], manager,
                                         session_ready=True)
                        return BotLifecycleResult(destination.fleet.name, bot, release.release_id,
                                                  entry["target"], "running", False,
                                                  "current_session_ready")
                    if session != "absent":
                        raise BotLifecycleError("current bot session readiness is indeterminate; "
                                                "inspect the session or explicitly restart",
                                                unavailable=True)
                elif props.get("ActiveState") == "inactive":
                    try:
                        assert_quiescent(adapter, installed_file=installed,
                                         target=entry["target"], socket_path=socket)
                    except (ActivationError, OSError) as exc:
                        raise BotLifecycleError("inactive bot session cannot be proved quiet",
                                                unavailable=True) from exc
                else:
                    raise BotLifecycleError("bot native active state is indeterminate", unavailable=True)
            elif not unit.installed:
                _confirm_stopped(adapter, installed, entry["target"], socket)
            fence_args = (root, spec.bot_dir) if ceiling is None else (root, spec.bot_dir, str(ceiling))
            fence = _native(adapter, "svc_activation_bot_fence", *fence_args).split("\t")
            if len(fence) != 2 or not fence[0].isdigit() or not fence[1]:
                raise BotLifecycleError("bot readiness fence is incomplete")
            try:
                _native(adapter, "svc_bot_enroll_exact", declaration.source, installed, entry["target"])
                _observed(root, declarations, adapter, entry["target"], installed)
                _native(adapter, "svc_activation_bot_ready", root, spec.bot_dir,
                        fence[0], fence[1], timeout=int(fence[0]) + 30)
                session_ready = False
                if manager == "Darwin":
                    session_ready = _native(adapter, "svc_bot_session_observe", spec.bot_dir,
                                            spec.label, spec.environment["TMUX_TMPDIR"]) == "ready"
                    if not session_ready:
                        raise BotLifecycleError("current bot session readiness is indeterminate",
                                                effect_attempted=True, unavailable=True)
                _confirm_running(adapter, installed, entry["target"], manager,
                                 session_ready=session_ready, effect_attempted=True)
            except (BotLifecycleError, ActivationError, InventoryError, OSError) as exc:
                raise BotLifecycleError("bot restart effect or readiness proof is incomplete" if restart
                                        else "bot start effect or readiness proof is incomplete",
                                        effect_attempted=True,
                                        release_id=release.release_id,
                                        target=entry["target"]) from exc
            return BotLifecycleResult(destination.fleet.name, bot, release.release_id,
                                      entry["target"], "running", True, "bridge_ready", handoff)
