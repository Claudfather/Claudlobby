"""Dependency-light registration for the first canonical task mutations."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".task_write", __package__).dispatch(args),
                   json_output=args.json, request_id=args.request_id)


def _route(children, verb, command, help):
    parser = children.add_parser(verb, help=help)
    parser.add_argument("--json", action="store_true", help="One schema-1 result object")
    parser.add_argument("--request-id", required=True, metavar="UUID",
                        help="Caller-retained canonical UUID for this operation")
    parser.set_defaults(func=_dispatch, public_command=command)
    return parser


def register_task_write_subparsers(task_children, assignment_children):
    admitting = _route(task_children, "admit", "task.admit", "Admit unassigned fleet work")
    admitting.add_argument("--title", required=True, metavar="TEXT")
    admitting.add_argument("--body-file", metavar="FILE")
    admitting.add_argument("--repo", metavar="OWNER/REPO")
    admitting.add_argument("--project", metavar="KEY")
    admitting.add_argument("--workstream", metavar="ID")
    admitting.add_argument("--by", metavar="ACTOR", help="Provenance, never caller authority")

    assigning = _route(task_children, "assign", "task.assign", "Assign queued work without delivery")
    assigning.add_argument("task_id", metavar="TASK_ID")
    assigning.add_argument("--bot", required=True, metavar="BOT")
    assigning.add_argument("--expected-by", metavar="RFC3339")
    assigning.add_argument("--checkin", metavar="ID")
    assigning.add_argument("--by", metavar="ACTOR", help="Provenance, never caller authority")

    accepting = _route(assignment_children, "accept", "assignment.accept",
                       "Acknowledge the caller's exact current assignment")
    accepting.add_argument("assignment_id", metavar="ASSIGNMENT_ID")
