"""Host/fleet diagnosis through the existing doctor and switch owners."""

from dataclasses import asdict
import os
import sys

from ..command_result import CommandFailure, CommandOutput


def dispatch(args):
    from .. import switches
    from ..active_config import runtime_read_stamp, read_current_config, load_active_context
    from ..config_plan import read_plan, PlanError
    from ..activation_state import ActivationError
    from ..context import declared_paths, generated_selectors, load_context, resolve_paths
    from ..doctor import format_report, run_doctor
    from ._helpers import _load_env

    if args.seed:
        raise CommandFailure("invalid_argument", "host doctor requires host data, not the seed")
    try:
        fleet_selector, _ = generated_selectors(fleet=args.fleet)
        paths = resolve_paths(root=args.root)
        if paths.package.installation is None:
            paths = resolve_paths(root=args.root, fleet=fleet_selector)
    except ValueError as exc:
        raise CommandFailure("invalid_argument", "invalid host doctor fleet selector") from exc
    if args.markdown:
        if not args.switches:
            raise CommandFailure("invalid_argument", "--markdown requires --switches")
        blocks = {name: switches.format_markdown(**kw) for name, kw in switches.DOC_BLOCKS.items()}
        return CommandOutput({"documentation": blocks}, lines=tuple(
            f"--- {name}\n{body}" for name, body in blocks.items()))
    try:
        stamp = runtime_read_stamp(paths.root, package=paths.package)
        if paths.package.installation is not None:
            snapshot = read_current_config(paths.root, installation=paths.package.installation,
                                           revision=paths.package.source_revision)
            names = [fleet_selector] if fleet_selector else list(snapshot["fleets"])
            contexts = [load_active_context(paths.root, name, package=paths.package) for name in names]
            scopes = [(context.paths, context.fleet) for context in contexts]
        else:
            selected = stamp.selection if stamp else None
            external = (read_plan(paths.root, selected["plan_id"]).effects["fleet_manifests"].values()
                        if selected else ())
            declared = [paths] if fleet_selector else declared_paths(paths.root, paths.package, external=external)
            scopes = [(scope, load_context(scope).fleet if scope.fleet_yaml.is_file() else None) for scope in declared]
        if not scopes and not args.switches:
            raise CommandFailure("not_found", "no fleet declarations; run fleet setup first")
    except (ActivationError, PlanError, OSError) as exc:
        raise CommandFailure("conflict", "current doctor configuration cannot be read") from exc
    items, lines, failed = [], [], False
    for scope, fleet in scopes or [(paths, None)]:
        previous = dict(os.environ)
        try:
            _load_env(scope)
            if args.switches:
                rows = switches.resolve(scope, fleet)
                items.append({"fleet": fleet.name if fleet else None,
                              "switches": [row.as_dict() for row in rows]})
                lines.append(switches.format_table(rows))
            else:
                report = run_doctor(fleet, scope, delivery=args.delivery)
                items.append({"fleet": fleet.name, **asdict(report),
                              "switch_rows": [row.as_dict() for row in report.switch_rows]})
                lines.append(f"Fleet {fleet.name}\n{format_report(report)}")
                failed |= report.has_failures
        finally:
            os.environ.clear()
            os.environ.update(previous)
    platform_limitations = []
    if sys.platform == "darwin":
        platform_limitations.append({
            "job": "orphan-browser-reaper", "state": "native_reaping_disabled",
            "reason": ("macOS launchd adopts ordinary desktop browsers; parent PID alone"
                       " cannot prove a browser belongs to a dead bot, so native reaping is disabled"),
        })
        lines.append("orphan-browser-reaper: OFF on macOS — launchd parentage cannot prove"
                     " bot ownership; inspect browser usage separately")
    data = {"root": str(paths.root), "fleets": items, "has_failures": failed,
            "platform_limitations": platform_limitations}
    if stamp is not None:
        stamp.check(paths.root, package=paths.package, message="current configuration changed during diagnosis")
        data.update(stamp.provenance)
        if stamp.kind == "git":
            data["running_source_revision"] = None
    if failed:
        raise CommandFailure("unavailable", "host doctor found failed checks", data=data,
                             hint="\n".join(lines))
    return CommandOutput(data, stamp.release_id if stamp else None, tuple(lines))
