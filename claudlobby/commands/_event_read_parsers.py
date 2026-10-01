"""Dependency-light registration for canonical fleet event reads."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".events", __package__).dispatch(args),
                   json_output=args.json)


def register_event_read_subparsers(sub):
    event = sub.add_parser("event", help="Read fleet events recorded on the Plane")
    children = event.add_subparsers(dest="event_action", required=True)
    listing = children.add_parser("list", help="List recent events in the selected fleet")
    listing.add_argument("--bot", help="Filter by bot name")
    listing.add_argument("--type", help="Filter by event type")
    listing.add_argument("--source", help="Filter by emitting script")
    listing.add_argument("--critical", action="store_true", help="Only registry-critical events")
    listing.add_argument("--since", help="Window or instant: 24h, 7d, 30m, or ISO instant")
    listing.add_argument("--limit", type=int, default=50, help="Page size, 1 to 1000")
    listing.add_argument("--cursor", help="Continue the same fleet and filters")
    listing.add_argument("--json", action="store_true", help="One schema-1 result object")
    listing.set_defaults(func=_dispatch, public_command="event.list")
    showing = children.add_parser("show", help="Show one event by its stable Plane event ID")
    showing.add_argument("event_id", metavar="EVENT_ID")
    showing.add_argument("--json", action="store_true", help="One schema-1 result object")
    showing.set_defaults(func=_dispatch, public_command="event.show")
