"""Dependency-light grammar for the first generated-bot message mutation."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute("message.send", lambda: import_module(".message_write", __package__).dispatch(args),
                   json_output=args.json, request_id=args.request_id)


def register_message_write_subparsers(children):
    sending = children.add_parser("send", help="Send one ordinary message to a declared bot")
    sending.add_argument("--json", action="store_true", help="One schema-1 result object")
    sending.add_argument("--to", required=True, metavar="BOT_OR_FLEET/BOT")
    content = sending.add_mutually_exclusive_group(required=True)
    content.add_argument("--text", metavar="TEXT")
    content.add_argument("--file", metavar="FILE")
    sending.add_argument("--kind", choices=("question", "notice", "chat"), default="chat")
    sending.add_argument("--request-id", required=True, metavar="UUID")
    sending.add_argument("--retry-uncertain", action="store_true")
    sending.set_defaults(func=_dispatch, public_command="message.send")
