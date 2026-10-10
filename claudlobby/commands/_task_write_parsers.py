"""Dependency-light registration for the first canonical task mutations."""

from importlib import import_module

from ..command_result import execute

_DEADLINE_HELP = ("RFC3339 deadline, or 'none' for open-ended; omitted freezes now plus the "
                  "fleet manager's observability.dispatch_deadline (default 86400s, 0 open-ended)")


def _dispatch(args):
    module = (".assignment_delivery" if args.public_command == "assignment.deliver" else
              ".task_recheck" if args.public_command == "task.recheck" else ".task_write")
    return execute(args.public_command,
                   lambda: import_module(module, __package__).dispatch(args),
                   json_output=args.json, request_id=args.request_id)


def _route(children, verb, command, help):
    parser = children.add_parser(verb, help=help)
    parser.add_argument("--json", action="store_true", help="One schema-1 result object")
    parser.add_argument("--request-id", required=True, metavar="UUID",
                        help="Caller-retained canonical UUID for this operation")
    parser.set_defaults(func=_dispatch, public_command=command)
    return parser


def add_report_flags(parser):
    parser.add_argument("--percent", type=int, metavar="0..100")
    parser.add_argument("--pr", metavar="URL")
    parser.add_argument("--pr-role", choices=("authored", "reviewed"))
    parser.add_argument("--artifact", action="append", default=[], metavar="URL")
    parser.add_argument("--issue", action="append", default=[], metavar="URL")
    parser.add_argument("--skill", metavar="NAME")


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
    assigning.add_argument("--expected-by", metavar="RFC3339|none", help=_DEADLINE_HELP)
    assigning.add_argument("--checkin", metavar="ID")
    assigning.add_argument("--by", metavar="ACTOR", help="Provenance, never caller authority")

    withdrawing = _route(task_children, "withdraw", "task.withdraw", "Cancel open fleet work")
    withdrawing.add_argument("task_id", metavar="TASK_ID")
    withdrawing.add_argument("--reason", required=True, metavar="TEXT")
    withdrawing.add_argument("--by", metavar="ACTOR", help="Provenance, never caller authority")

    escalating = _route(task_children, "escalate", "task.escalate",
                        "Ask for human guidance on open fleet work")
    escalating.add_argument("task_id", metavar="TASK_ID")
    escalating.add_argument("--question", required=True, metavar="TEXT")
    escalating.add_argument("--by", metavar="ACTOR", help="Provenance, never caller authority")

    nudging = _route(task_children, "nudge", "task.nudge",
                     "Record a nudge and ask the current fleet manager to act")
    nudging.add_argument("task_id", metavar="TASK_ID")
    nudging.add_argument("--reason", required=True, metavar="TEXT")
    nudging.add_argument("--by", metavar="ACTOR", help="Provenance, never caller authority")

    feedback = _route(task_children, "feedback", "task.feedback",
                      "Comment on resolved task work and notify its configured lead")
    feedback.add_argument("task_id", metavar="TASK_ID")
    feedback.add_argument("--actor", required=True, metavar="human:ACTOR",
                          help="Explicit existing local human actor")
    feedback.add_argument("--expected-assignment", required=True, metavar="ASG_ID|none",
                          help="Selected current assignment, or explicit none for unassigned/terminal work")
    feedback.add_argument("--text", required=True, metavar="TEXT")

    rechecking = _route(task_children, "recheck", "task.recheck",
                        "Ask the current fleet manager about overdue open work")
    from ..task_defaults import DEFAULT_MAX_AGE_H, DEFAULT_REPEAT_H
    rechecking.add_argument("--max-age-h", type=float, default=DEFAULT_MAX_AGE_H)
    rechecking.add_argument("--repeat-h", type=float, default=DEFAULT_REPEAT_H)
    rechecking.add_argument("--dry-run", action="store_true")
    rechecking.add_argument("--by", metavar="ACTOR", help="Provenance, never caller authority")

    reassigning = _route(task_children, "reassign", "task.reassign",
                         "Close the current assignment and route its successor")
    reassigning.add_argument("task_id", metavar="TASK_ID")
    reassigning.add_argument("--bot", required=True, metavar="BOT")
    reassigning.add_argument("--reason", required=True, metavar="TEXT")
    reassigning.add_argument("--expected-by", metavar="RFC3339|none", help=_DEADLINE_HELP)
    reassigning.add_argument("--by", metavar="ACTOR", help="Provenance, never caller authority")

    accepting = _route(assignment_children, "accept", "assignment.accept",
                       "Acknowledge the caller's exact current assignment")
    accepting.add_argument("assignment_id", metavar="ASSIGNMENT_ID")

    delivering = _route(assignment_children, "deliver", "assignment.deliver",
                        "Deliver a recorded assignment to its current worker")
    delivering.add_argument("assignment_id", metavar="ASSIGNMENT_ID")
    delivering.add_argument("--file", required=True, metavar="FILE")
    delivering.add_argument("--retry-uncertain", action="store_true")

    for verb in ("progress", "block", "return", "complete", "fail"):
        reporting = _route(assignment_children, verb, "assignment." + verb,
                           "Record a linked report, then notify the fleet manager")
        reporting.add_argument("assignment_id", metavar="ASSIGNMENT_ID")
        reporting.add_argument("--summary" if verb in ("progress", "complete") else "--reason",
                               required=True, metavar="TEXT")
        add_report_flags(reporting)
