"""Dependency-light registration for retained request inspection."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".request_read", __package__).dispatch(args),
                   json_output=args.json)


def register_request_read_subparsers(sub):
    request = sub.add_parser("request", help="Inspect retained request outcomes")
    children = request.add_subparsers(dest="request_command", required=True)
    showing = children.add_parser("show", help="Show one retained request and independent recording proof")
    showing.add_argument("request_id", metavar="REQUEST_ID")
    showing.add_argument("--json", action="store_true", help="One schema-1 result object")
    showing.set_defaults(func=_dispatch, public_command="request.show")
