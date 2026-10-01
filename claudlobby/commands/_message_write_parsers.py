"""Dependency-light grammar for the first generated-bot message mutation."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command, lambda: import_module(".message_write", __package__).dispatch(args),
                   json_output=args.json, request_id=args.request_id)


def _ack_dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".report_write", __package__).dispatch(args),
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

    replying = children.add_parser("reply", help="Reply to a recorded message from a declared bot")
    replying.add_argument("--json", action="store_true", help="One schema-1 result object")
    replying.add_argument("message_id", metavar="MESSAGE_ID")
    content = replying.add_mutually_exclusive_group(required=True)
    content.add_argument("--text", metavar="TEXT")
    content.add_argument("--file", metavar="FILE")
    replying.add_argument("--request-id", required=True, metavar="UUID")
    replying.add_argument("--retry-uncertain", action="store_true")
    replying.set_defaults(func=_dispatch, public_command="message.reply")


def register_report_write_subparsers(fleet_children):
    from ._report_read_parsers import register_report_read_subparsers
    from ._task_write_parsers import add_report_flags

    reports = fleet_children.add_parser("reports", help="Fleet report operations")
    children = reports.add_subparsers(dest="report_command", required=True)
    register_report_read_subparsers(children)
    acknowledging = children.add_parser("ack", help="Acknowledge the exact report prefix you read")
    acknowledging.add_argument("--json", action="store_true", help="One schema-1 result object")
    acknowledging.add_argument("--through", required=True, metavar="ACK_CURSOR")
    acknowledging.add_argument("--request-id", required=True, metavar="UUID")
    acknowledging.set_defaults(func=_ack_dispatch, public_command="fleet.reports.ack")
    submitting = children.add_parser("submit", help="Send an explicitly unlinked report to your manager")
    submitting.add_argument("--json", action="store_true", help="One schema-1 result object")
    submitting.add_argument("--status", required=True,
                            choices=("progress", "blocked", "completed", "failed"))
    submitting.add_argument("--summary", required=True, metavar="TEXT")
    submitting.add_argument("--request-id", required=True, metavar="UUID")
    submitting.add_argument("--retry-uncertain", action="store_true")
    add_report_flags(submitting)
    submitting.set_defaults(func=_dispatch, public_command="fleet.reports.submit")
