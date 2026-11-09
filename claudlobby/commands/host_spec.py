"""`claudlobby host spec`: what this host is and what its OS can enforce; a read that changes nothing."""

from __future__ import annotations

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..context import resolve_paths
    from ..infra.spec import UnsupportedHost, read_spec

    paths, error = None, None
    try:
        paths = resolve_paths(root=args.root)
    except (ValueError, OSError) as exc:
        error = str(exc)
    try:
        spec = read_spec(paths=paths, paths_error=error)
    except UnsupportedHost as exc:
        raise CommandFailure("unavailable", str(exc)) from exc
    return CommandOutput(spec.as_dict(), lines=spec.lines())
