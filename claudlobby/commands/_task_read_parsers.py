"""Dependency-light registration for canonical task and assignment reads."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".task_read", __package__).dispatch(args),
                   json_output=args.json)


def _route(children, verb, command, help):
    parser = children.add_parser(verb, help=help)
    parser.add_argument("--json", action="store_true", help="One schema-1 result object")
    parser.set_defaults(func=_dispatch, public_command=command)
    return parser


def register_task_read_subparsers(sub, task_children):
    listing = _route(task_children, "list", "task.list", "List canonical tasks in the active fleet")
    listing.add_argument("--state", default="open", choices=(
        "open", "queued", "assigned", "active", "blocked", "completed", "failed", "cancelled", "all"))
    listing.add_argument("--bot", metavar="BOT", help="Only assignments for this active bot")
    listing.add_argument("--limit", type=int, default=100, metavar="N", help="Page size, 1 to 1000")
    listing.add_argument("--cursor", metavar="TOKEN", help="Continue the same scope and filters")
    showing = _route(task_children, "show", "task.show", "Show one canonical task ID")
    showing.add_argument("task_id", metavar="TASK_ID")

    assignment = sub.add_parser("assignment", help="Read canonical assignment records")
    children = assignment.add_subparsers(dest="assignment_command", required=True)
    showing = _route(children, "show", "assignment.show", "Show one canonical assignment ID")
    showing.add_argument("assignment_id", metavar="ASSIGNMENT_ID")
    return children
