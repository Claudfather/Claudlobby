"""`claudlobby host spec`: what this host is and what its OS can enforce; a read that changes nothing."""

from __future__ import annotations

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..infra.spec import UnsupportedHost, read_spec

    try:
        spec = read_spec(root=args.root)
    except UnsupportedHost as exc:
        raise CommandFailure("unavailable", str(exc)) from exc
    return CommandOutput(spec.as_dict(), lines=spec.lines())
