"""Dependency-light registration for canonical message observations."""

from . import lazy

_dispatch = lazy(".message_read")


def _route(children, verb, command, help_text):
    parser = children.add_parser(verb, help=help_text)
    parser.add_argument("--json", action="store_true", help="One schema-1 result object")
    parser.add_argument("message_id", metavar="MESSAGE_ID",
                        help="A message ID, or msg_ and at least 8 of its hex digits")
    parser.set_defaults(func=_dispatch, public_command=command)
    return parser


def register_message_read_subparsers(sub):
    """Create the message group; return its children for later write verbs."""
    message = sub.add_parser("message", help="Read canonical message records and delivery evidence")
    children = message.add_subparsers(dest="message_command", required=True)
    _route(children, "show", "message.show", "Show a permitted canonical message")

    listing = children.add_parser("list", help="List permitted message metadata and receipts (JSON)")
    listing.add_argument("--json", action="store_true", default=True, help="One schema-1 result object (default)")
    listing.add_argument("--since", required=True, help="ISO timestamp with offset, or lookback such as 72h")
    listing.add_argument("--until", help="Exclusive ISO end with offset (default: time of the first page)")
    listing.add_argument("--from", dest="sender", metavar="ACTOR", help="Match recorded sender")
    listing.add_argument("--to", dest="recipient", metavar="ACTOR", help="Match recorded destination")
    listing.add_argument("--kind", help="Match recorded message class")
    listing.add_argument("--limit", type=int, default=50, help="Page size from 1 to 100 (default: 50)")
    listing.add_argument("--cursor", help="Continue with data.next_cursor and the same window and filters")
    listing.set_defaults(func=_dispatch, public_command="message.list")

    observing = _route(children, "receipt", "message.receipt", "Observe final receiver integrity proof")
    observing.add_argument("--destination", metavar="BOT", help="Match the recorded recipient exactly")
    observing.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                           help="Wait 0 to 60 seconds for a final integrity verdict")

    waiting = _route(children, "wait", "message.wait", "Wait for a direct reply from the recorded peer")
    waiting.add_argument("--for", dest="wait_for", required=True, choices=("reply",))
    waiting.add_argument("--timeout", type=float, required=True, metavar="SECONDS",
                         help="Wait 1 to 60 seconds for the earliest direct reply")
    return children
