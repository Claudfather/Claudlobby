"""Command implementations for the claudlobby CLI."""

from importlib import import_module

from ..command_result import execute


def lazy(module: str, *, request_id: bool = False):
    """The handler that runs `module`'s `dispatch(args)` through `execute`, importing the module
    (named relative to this package) only when its route runs. `request_id=True` puts the
    route's `--request-id` on the result; it is opt-in because `request show` keeps the request
    it reads in a positional `request_id`, which its result must not carry."""
    def dispatch(args):
        return execute(args.public_command, lambda: import_module(module, __package__).dispatch(args),
                       json_output=args.json,
                       request_id=getattr(args, "request_id", None) if request_id else None)

    return dispatch
