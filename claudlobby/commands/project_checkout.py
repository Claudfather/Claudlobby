"""Public selected-release repository preparation, shared with guided setup."""
import os

from ..command_result import CommandFailure, CommandOutput


def dispatch(args):
    from ..active_config import resolve_active_context
    from ..context import resolve_paths
    from ..project_checkout import prepare_checkout
    from ..runtime_admission import mutation_admission

    if args.seed:
        raise CommandFailure("invalid_argument", "repository preparation requires an active fleet")
    root = resolve_paths(root=args.root).root
    try:
        with mutation_admission(root, expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
            context = resolve_active_context(root=root, fleet=args.fleet, bot=args.bot_id)
            result = prepare_checkout(context, args.bot_id, args.repository)
            return CommandOutput(result, release.release_id,
                                 (f"{args.bot_id}: {result['state']} checkout at {result['path']}. Application setup has not been run.",))
    except (OSError, ValueError, RuntimeError) as exc:
        raise CommandFailure("unavailable", "repository preparation unavailable; " + str(exc)) from exc
