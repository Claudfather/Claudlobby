"""Public selected-fleet validation over the existing validator and baseline gate."""

from __future__ import annotations

from pathlib import Path
import re

from ..command_result import CommandFailure, CommandOutput


_REMEDIES = {
    "orphan_unit": "Review the stale unit, then use host supervision reap-orphans --dry-run before --apply.",
    "orphan_grant": "Remove the unowned allow rule or equip the declaring source, then stage a plan.",
    "unsourced_grant": "Review the fleet tools.allow override and its declared source.",
    "under_grant": "Stage a plan so the equipped source's permission is composed.",
    "missing_external": "Restore the declared host dependency or remove its declaration.",
    "isolation_not_composed": "Stage and review a plan, then ask the operator to activate it.",
    "isolation_missing": "Stage and review a plan, then ask the operator to activate it.",
    "isolation_gap": "Review the bot's shared-config isolation and authored exemptions.",
    "isolation_env_read": "Remove the composed instruction that reads host-shared environment files.",
    "improper_path": "Review the emitted path guard and move the reference into the owned bot directory.",
    "denied_value": "Remove the denied value from authored configuration before staging.",
    "env_denied_value": "Remove the denied value from the environment tier before staging.",
    "env_bot_secret_leaked": "Move the bot secret out of the host-shared environment tier.",
    "unused_declaration": "Remove the unused declaration or equip its consumer.",
    "missing_tier_a": "Stage and review a plan to compose the missing setting.",
    "fleet_pulse_env_inert": "Review the fleet-pulse environment declaration and its switch carrier.",
}

_AREAS = {"improper_path": "composed_bot", "denied_value": "composed_bot",
          "env_denied_value": "environment_tier", "env_bot_secret_leaked": "host_shared_env"}


def _safe_finding(f):
    from ..freshbox import _GRANT_KINDS

    row = {"bot": f.bot_id, "kind": f.kind, "severity": f.severity}
    if f.kind in _GRANT_KINDS:
        suffix = " " + _GRANT_KINDS[f.kind][1]
        if f.detail.endswith(suffix):
            row["grant"] = f.detail[:-len(suffix)]
    elif f.kind == "orphan_unit":
        unit = f.detail.partition(" —")[0]
        if re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:plist|service)", unit):
            row["unit"] = unit
    if f.kind in _AREAS:
        row["area"] = _AREAS[f.kind]
    if f.kind in _REMEDIES:
        row["remedy"] = _REMEDIES[f.kind]
    return row


def _runtime_audit(args, context) -> CommandOutput:
    from ..freshbox import FAIL, INFO, WARN, audit_bot, audit_fleet, exits_nonzero

    bot = context.fleet.bots.get(args.bot) if args.bot else None
    if args.bot and bot is None:
        raise CommandFailure("not_found", "bot is not declared in the selected fleet")
    # The former public route included host-tier ~/.env; the library default
    # does not. Preserve that explicit scope while reusing the audit owner.
    findings = (audit_bot(bot, context.fleet, context.paths, home=Path.home()) if bot else
                audit_fleet(context.fleet, context.paths, home=Path.home()))
    counts = {severity: sum(f.severity == severity for f in findings)
              for severity in (FAIL, WARN, INFO)}
    data = {"mode": "runtime", "fleet": context.fleet.name, "bot": args.bot,
            "strict": args.strict, "findings": [_safe_finding(f) for f in findings],
            "fail_count": counts[FAIL], "warning_count": counts[WARN],
            "info_count": counts[INFO]}
    # Finding details can contain paths resolved from secret env values.
    def finding_line(row):
        subject = row.get("unit") or row.get("grant")
        return (f"runtime {row['bot']}: [{row['severity']}] {row['kind']}"
                + (f" ({subject})" if subject else "")
                + (f": {row['remedy']}" if row.get("remedy") else ""))

    lines = tuple(finding_line(row) for row in data["findings"])
    lines += (f"runtime audit: {counts[FAIL]} fail, {counts[WARN]} warn, {counts[INFO]} info",)
    if exits_nonzero(findings, strict=args.strict):
        raise CommandFailure("conflict", "runtime self-containment audit has blocking findings",
                             data=data, hint="\n".join(lines))
    return CommandOutput(data, lines=lines)


def dispatch(args) -> CommandOutput:
    from ..context import generated_selectors, load_context, resolve_paths
    from ..paths import InvalidPathSelector
    from ..validator import render_warnings, validate, warning_summary
    from ._helpers import _load_env
    from .core import _warn_baseline_gate
    import yaml

    baseline = args.warn_baseline
    if args.runtime and (baseline or args.write):
        raise CommandFailure("invalid_argument", "--warn-baseline and --write apply only to authored config validation")
    if args.write and not baseline:
        raise CommandFailure("invalid_argument", "--write needs --warn-baseline FILE")
    if args.bot and not args.runtime:
        raise CommandFailure("invalid_argument", "--bot needs --runtime")
    try:
        fleet, _ = generated_selectors(fleet=args.fleet, seed=args.seed)
        paths = resolve_paths(root=args.root, fleet=fleet, seed=args.seed)
    except FileNotFoundError as exc:
        raise CommandFailure("not_found", "selected fleet configuration was not found") from exc
    except RuntimeError as exc:
        raise CommandFailure("unavailable", "installed library package is unavailable",
                             hint="build and select a sealed release with packaged resources") from exc
    except (InvalidPathSelector, ValueError) as exc:
        raise CommandFailure("invalid_argument", "invalid root or fleet selector") from exc
    try:
        _load_env(paths)
        context = load_context(paths, fleet=fleet)
    except FileNotFoundError as exc:
        raise CommandFailure("not_found", "selected fleet configuration was not found") from exc
    except RuntimeError as exc:
        raise CommandFailure("unavailable", "installed library package is unavailable",
                             hint="build and select a sealed release with packaged resources") from exc
    except (ValueError, yaml.YAMLError) as exc:
        raise CommandFailure("conflict", "selected fleet configuration is invalid") from exc

    if args.runtime:
        return _runtime_audit(args, context)

    report = validate(context.fleet, paths)
    warnings = render_warnings(report)
    data = {"mode": "authored", "fleet": context.fleet.name, "errors": list(report.errors),
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
