"""Public selected-fleet validation over the existing validator and baseline gate."""

from __future__ import annotations

from pathlib import Path

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..context import load_context, resolve_paths
    from ..paths import InvalidPathSelector
    from ..validator import render_warnings, validate, warning_summary
    from ._helpers import _load_env
    from .core import _warn_baseline_gate
    import yaml

    baseline = args.warn_baseline
    if args.write and not baseline:
        raise CommandFailure("invalid_argument", "--write needs --warn-baseline FILE")
    try:
        paths = resolve_paths(root=args.root, fleet=args.fleet, seed=args.seed)
    except (InvalidPathSelector, ValueError) as exc:
        raise CommandFailure("invalid_argument", "invalid root or fleet selector") from exc
    try:
        _load_env(paths)
        context = load_context(paths, fleet=args.fleet)
    except FileNotFoundError as exc:
        raise CommandFailure("not_found", "selected fleet configuration was not found") from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise CommandFailure("conflict", "selected fleet configuration is invalid") from exc

    report = validate(context.fleet, paths)
    warnings = render_warnings(report)
    data = {"fleet": context.fleet.name, "errors": list(report.errors),
            "warnings": warnings, "warning_categories": report.warning_categories,
            "error_count": len(report.errors), "warning_count": len(report.warnings),
            "strict": args.strict, "warning_baseline": baseline,
            "baseline_written": False}
    lines = (*report.errors, *warnings)
    if report.warnings:
        lines += (warning_summary(report),)
    findings = "\n".join(lines)
    gate = _warn_baseline_gate(report, Path(baseline), write=args.write) if baseline else 0
    data["baseline_written"] = bool(baseline and args.write and gate == 0)
    if args.strict and report.has_issues:
        raise CommandFailure("conflict", "strict validation found errors or warnings",
                             data=data, hint=findings)
    if report.has_errors:
        raise CommandFailure("conflict", "fleet configuration has validation errors",
                             data=data, hint=findings)
    if gate == 2:
        raise CommandFailure("unavailable", "warning baseline could not be read or written", data=data)
    if gate == 1:
        raise CommandFailure("conflict", "warning baseline has new or grown categories",
                             data=data, hint=findings)
    if not report.has_issues:
        lines += (f"fleet.yaml OK ({len(context.fleet.bots)} bots, {len(context.fleet.teams)} teams)",)
    return CommandOutput(data, lines=lines)
