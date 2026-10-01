"""Dependency-light grammar for the read-only fleet report list."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".report_read", __package__).dispatch(args),
                   json_output=args.json)


def register_report_read_subparsers(children):
    listing = children.add_parser("list", help="List reports visible to this fleet viewer")
    listing.add_argument("--json", action="store_true", help="One schema-1 result object")
    listing.add_argument("--bot", metavar="AUTHOR", help="Filter by report author, not viewer")
    listing.add_argument("--status", metavar="STATUS", help="Filter by recorded report status")
    listing.add_argument("--since", metavar="TIME", help="RFC3339 instant with an offset")
    listing.add_argument("--unacknowledged", action="store_true",
                         help="Show reports beyond this viewer's recorded read position")
    listing.add_argument("--limit", type=int, default=100, metavar="N", help="Page size, 1 to 1000")
    listing.add_argument("--cursor", metavar="TOKEN", help="Continue the same viewer, filters and read position")
    listing.set_defaults(func=_dispatch, public_command="fleet.reports.list")
