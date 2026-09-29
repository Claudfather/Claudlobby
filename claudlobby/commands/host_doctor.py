"""Host/fleet diagnosis through the existing doctor and switch owners."""

from dataclasses import asdict
import os

from ..command_result import CommandFailure, CommandOutput


def dispatch(args):
    from .. import switches
    from ..activation_state import read_selection
    from ..config_plan import read_plan
    from ..context import declared_paths, generated_selectors, load_context, resolve_paths
    from ..doctor import format_report, run_doctor
    from ._helpers import _load_env

    if args.seed:
        raise CommandFailure("invalid_argument", "host doctor requires host data, not the seed")
    try:
        fleet_selector, _ = generated_selectors(fleet=args.fleet)
        paths = resolve_paths(root=args.root, fleet=fleet_selector)
    except ValueError as exc:
        raise CommandFailure("invalid_argument", "invalid host doctor fleet selector") from exc
    if args.markdown:
        if not args.switches:
            raise CommandFailure("invalid_argument", "--markdown requires --switches")
        blocks = {name: switches.format_markdown(**kw) for name, kw in switches.DOC_BLOCKS.items()}
        return CommandOutput({"documentation": blocks}, lines=tuple(
            f"--- {name}\n{body}" for name, body in blocks.items()))
    selected = read_selection(paths.root)
    external = (read_plan(paths.root, selected["plan_id"]).effects["fleet_manifests"].values()
                if selected else ())
    scopes = [paths] if fleet_selector else declared_paths(paths.root, paths.package, external=external)
    if not scopes and not args.switches:
        raise CommandFailure("not_found", "no fleet declarations; run fleet setup first")
    items, lines, failed = [], [], False
    for scope in scopes or [paths]:
        fleet = load_context(scope).fleet if scope.fleet_yaml.is_file() else None
        previous = dict(os.environ)
        try:
            _load_env(scope)
            if args.switches:
                rows = switches.resolve(scope, fleet)
                items.append({"fleet": fleet.name if fleet else None,
                              "switches": [asdict(row) for row in rows]})
                lines.append(switches.format_table(rows))
            else:
                report = run_doctor(fleet, scope, delivery=args.delivery)
                items.append({"fleet": fleet.name, **asdict(report)})
                lines.append(f"Fleet {fleet.name}\n{format_report(report)}")
                failed |= report.has_failures
        finally:
            os.environ.clear()
            os.environ.update(previous)
    data = {"root": str(paths.root), "fleets": items, "has_failures": failed}
    if failed:
        raise CommandFailure("unavailable", "host doctor found failed checks", data=data,
                             hint="\n".join(lines))
    return CommandOutput(data, selected["release_id"] if selected else None, tuple(lines))
