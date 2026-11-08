"""Command implementations for the claudlobby CLI."""

from importlib import import_module

from ..command_result import execute


def lazy(module: str, *, request_id: bool = False):
    """The handler a parser route sets as `func` when its command is `dispatch(args)` in `module`,
    a name relative to this package. The module is imported only when the route runs, so building
    the parser imports no command code. With `request_id`, the result carries the route's
    `--request-id` (null on a route without one); without it, the result carries none, even where
    `args.request_id` exists: `request show` takes the request it reads as a positional
    `request_id`."""
    def dispatch(args):
        return execute(args.public_command, lambda: import_module(module, __package__).dispatch(args),
                       json_output=args.json,
                       request_id=getattr(args, "request_id", None) if request_id else None)

    return dispatch
