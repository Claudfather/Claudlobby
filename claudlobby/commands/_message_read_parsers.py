"""Dependency-light registration for canonical message observations."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".message_read", __package__).dispatch(args),
                   json_output=args.json)


def _route(children, verb, command, help_text):
    parser = children.add_parser(verb, help=help_text)
    parser.add_argument("--json", action="store_true", help="One schema-1 result object")
    parser.add_argument("message_id", metavar="MESSAGE_ID")
    parser.set_defaults(func=_dispatch, public_command=command)
    return parser


def register_message_read_subparsers(sub):
    """Create the message group; return its children for later write verbs."""
    message = sub.add_parser("message", help="Read canonical message records and delivery evidence")
    children = message.add_subparsers(dest="message_command", required=True)
    _route(children, "show", "message.show", "Show a permitted canonical message")

    observing = _route(children, "receipt", "message.receipt", "Observe final receiver integrity proof")
    observing.add_argument("--destination", metavar="BOT", help="Match the recorded recipient exactly")
    observing.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                           help="Wait 0 to 60 seconds for a final integrity verdict")

    waiting = _route(children, "wait", "message.wait", "Wait for a direct reply from the recorded peer")
    waiting.add_argument("--for", dest="wait_for", required=True, choices=("reply",))
    waiting.add_argument("--timeout", type=float, required=True, metavar="SECONDS",
                         help="Wait 1 to 60 seconds for the earliest direct reply")
    return children
