"""Public credential diagnosis through the existing reconciler."""

from __future__ import annotations

from dataclasses import asdict

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..context import resolve_context
    from ..credentials import exits_nonzero, format_report, reconcile
    from ..env_tiers import ResolverUnavailable
    from ..paths import InvalidPathSelector

    try:
        context = resolve_context(root=args.root, fleet=args.fleet, seed=args.seed)
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid fleet selector") from exc
    except FileNotFoundError as exc:
        raise CommandFailure("not_found", "fleet declaration is missing") from exc
    except ValueError as exc:
        raise CommandFailure("conflict", "fleet declaration cannot be loaded") from exc
    try:
        findings, scope = reconcile(context.paths, context.fleet)
    except ResolverUnavailable as exc:
        # No narrower fallback: an unreadable tier is not an empty tier.
        raise CommandFailure("unavailable", "credential tier resolution is unavailable") from exc
    report = format_report(findings, scope)
    failures = sum(row.is_failure for row in findings)
    unknown = sum(row.verdict == "UNKNOWN" for row in findings)
    data = {"fleet": context.fleet.name, "scope": scope,
            "findings": [asdict(row) for row in findings],
            "failed": failures, "unknown": unknown}
    if exits_nonzero(findings):
        raise CommandFailure("conflict", f"credential reconciliation found {failures} failure(s)",
                             data=data, hint=report)
    return CommandOutput(data, lines=(report,))
