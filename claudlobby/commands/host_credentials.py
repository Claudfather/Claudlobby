"""Public credential diagnosis through the existing reconciler."""

from __future__ import annotations

from dataclasses import asdict

from ..command_result import CommandFailure, CommandOutput


def _reconcile(args) -> CommandOutput:
    from ..context import generated_selectors, resolve_context
    from ..credentials import exits_nonzero, format_report, reconcile
    from ..env_tiers import ResolverUnavailable
    from ..paths import InvalidPathSelector

    try:
        fleet, _ = generated_selectors(fleet=args.fleet, seed=args.seed)
        context = resolve_context(root=args.root, fleet=fleet, seed=args.seed)
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


def _check(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..context import resolve_paths
    from ..credential_check import CredentialCheckError, check_credentials
    from ..operation_context import OperationContextError
    from ..paths import InvalidPathSelector
    from ..releases import ReleaseError
    from ..runtime_admission import ReleaseMismatch

    if args.seed:
        raise CommandFailure("invalid_argument", "selected credential check cannot use the seed fleet")
    try:
        root = resolve_paths(root=args.root).root
        result = check_credentials(root=root, fleet=args.fleet)
    except CredentialCheckError as exc:
        raise CommandFailure("unavailable" if exc.effect_attempted else "conflict", str(exc),
                             data={"fleet": args.fleet, "native_outcome":
                                   "unknown" if exc.effect_attempted else "unattempted"}) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid fleet or root selector") from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "credential check requires the selected executable") from exc
    except (ActivationError, PlanError, ReleaseError, OperationContextError) as exc:
        raise CommandFailure("conflict", "selected credential scope cannot be verified") from exc
    return CommandOutput({"fleet": result.fleet, "native_outcome": result.checks,
                          "credential_health": result.health,
                          "state_path": str(result.state_path)}, result.release_id,
                         (f"{result.fleet}: credential probe tick completed; inspect "
                          f"{result.state_path} for provider status.",))


def dispatch(args) -> CommandOutput:
    if args.public_command == "host.credentials.reconcile":
        return _reconcile(args)
    if args.public_command == "host.credentials.check":
        return _check(args)
    raise CommandFailure("invalid_argument", "unsupported credential command")
