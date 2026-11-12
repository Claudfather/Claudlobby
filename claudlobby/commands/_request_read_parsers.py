"""Dependency-light registration for retained request inspection."""

from . import lazy

_dispatch = lazy(".request_read")


def register_request_read_subparsers(sub):
    request = sub.add_parser("request", help="Inspect retained request outcomes")
    children = request.add_subparsers(dest="request_command", required=True)
    showing = children.add_parser("show", help="Show one retained request and independent recording proof")
    showing.add_argument("request_id", metavar="REQUEST_ID",
                         help="A request UUID, or its first 8 or more characters")
    showing.add_argument("--json", action="store_true", help="One schema-1 result object")
    showing.set_defaults(func=_dispatch, public_command="request.show")
