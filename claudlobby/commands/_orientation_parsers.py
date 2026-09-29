"""Dependency-light registration for read-only context and equipment views."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".orientation", __package__).dispatch(args),
                   json_output=args.json)


def _inbox_dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".inbox_read", __package__).dispatch(args),
                   json_output=args.json)


def _usage_dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".usage_read", __package__).dispatch(args),
                   json_output=args.json)


def _status_dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".status_read", __package__).dispatch(args),
                   json_output=args.json)


def _automation_dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".automation", __package__).dispatch(args),
                   json_output=args.json, request_id=getattr(args, "request_id", None))


def _bot_runtime_dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".bot_runtime", __package__).dispatch(args),
                   json_output=args.json)


def _fleet_runtime_dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".fleet_runtime", __package__).dispatch(args),
                   json_output=args.json)


def _move_dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".move_bot", __package__).dispatch(args),
                   json_output=args.json)


def register_orientation_subparsers(sub):
    for domain, verbs in (("context", ("show",)),
                          ("bot", ("list", "show", "capabilities", "start", "stop", "restart")),
                          ("fleet", ("show",)),
                          ("project", ("list", "show"))):
        group = sub.add_parser(domain, help=f"Read {domain} declarations and available evidence")
        children = group.add_subparsers(dest=f"{domain}_command", required=True)
        if domain == "fleet":
            status = children.add_parser("status", help="Read fleet session, native and recorded status")
            status.add_argument("--json", action="store_true", help="One schema-1 result object")
            status.set_defaults(func=_status_dispatch, public_command="fleet.status")
            uptime = children.add_parser("uptime", help="Read Plane keepalive uptime with coverage")
            uptime.add_argument("--bot", metavar="BOT", help="Exact declared bot ID")
            uptime.add_argument("--window", choices=("24h", "7d", "30d"),
                                help="Window (default: all in JSON, 24h in text)")
            uptime.add_argument("--json", action="store_true", help="One schema-1 result object")
            uptime.set_defaults(func=_status_dispatch, public_command="fleet.uptime")
            from ._setup_parsers import register_fleet_setup
            register_fleet_setup(children)
            for action in ("start", "stop", "restart", "reconcile", "reload"):
                route = children.add_parser(action, help=f"{action.capitalize()} selected fleet supervision")
                route.add_argument("--json", action="store_true")
                if action in {"start", "stop", "restart"}:
                    route.add_argument("--workers", action="store_true",
                                       help="Operate workers serially, leaving the manager running")
                route.set_defaults(func=_fleet_runtime_dispatch, public_command=f"fleet.{action}")
            from ._message_write_parsers import register_report_write_subparsers
            register_report_write_subparsers(children)
            inbox = children.add_parser("inbox", help="Read fleet work and a viewer's report attention")
            inbox.add_argument("--json", action="store_true", help="One schema-1 result object")
            inbox.add_argument("--bot", metavar="VIEWER", help="Read as this declared viewer; never changes caller identity")
            inbox.add_argument("--limit", type=int, default=100, metavar="N",
                               help="Maximum items per work/report section (1–1000)")
            inbox.add_argument("--cursor", metavar="TOKEN", help="Continue the same viewer's work/report pages")
            inbox.set_defaults(func=_inbox_dispatch, public_command="fleet.inbox")
        if domain in ("bot", "fleet"):
            usage = children.add_parser("usage", help="Read bounded Claude transcript token counts")
            if domain == "bot":
                usage.add_argument("bot_id", metavar="BOT", help="Exact selected-fleet bot ID")
            usage.add_argument("--since", default="24h", metavar="DURATION",
                               help="Positive window up to 7d; default 24h")
            usage.add_argument("--json", action="store_true", help="One schema-1 result object")
            usage.set_defaults(func=_usage_dispatch, public_command=f"{domain}.usage")
        if domain == "bot":
            status = children.add_parser("status", help="Read one bot's session, native and recorded status")
            status.add_argument("bot_id", metavar="BOT", help="Exact declared bot ID")
            status.add_argument("--json", action="store_true", help="One schema-1 result object")
            status.set_defaults(func=_status_dispatch, public_command="bot.status")
            move = children.add_parser("move", help="Preview or apply a bot move between fleets")
            move.add_argument("bot", metavar="BOT")
            move.add_argument("--to", required=True, metavar="FLEET")
            move.add_argument("--from", dest="from_fleet", metavar="FLEET")
            move.add_argument("--apply", action="store_true", help="Copy state and activate the new placement")
            move.add_argument("--cleanup-source", action="store_true",
                              help="Remove the source bot directory after successful activation")
            move.add_argument("--force", action="store_true", help="Allow a move with an active source session")
            move.add_argument("--json", action="store_true")
            move.set_defaults(func=_move_dispatch, public_command="bot.move")
            automation = children.add_parser("automation", help="Read or control one bot's runner state")
            actions = automation.add_subparsers(dest="automation_action", required=True)
            for action in ("status", "pause", "resume", "record"):
                route = actions.add_parser(action)
                route.add_argument("bot_id", metavar="BOT", help="Exact selected-fleet bot ID")
                if action == "pause":
                    route.add_argument("--reason", required=True, metavar="TEXT")
                if action == "record":
                    route.add_argument("--outcome", required=True,
                                       choices=("completed", "bypassed", "needs-input", "blocked", "partial"))
                    route.add_argument("--pr", metavar="URL")
                    route.add_argument("--issue", metavar="URL")
                if action != "status":
                    route.add_argument("--request-id", required=True, metavar="UUID")
                route.add_argument("--json", action="store_true")
                route.set_defaults(func=_automation_dispatch,
                                   public_command=f"bot.automation.{action}")
        for verb in verbs:
            route = children.add_parser(verb, help=f"{verb.capitalize()} {domain} context")
            route.add_argument("--json", action="store_true", help="One schema-1 result object")
            route.set_defaults(func=_bot_runtime_dispatch if domain == "bot" and verb in {"start", "stop", "restart"}
                               else _dispatch, public_command=f"{domain}.{verb}")
            if domain == "bot" and verb != "list":
                route.add_argument("bot_id", metavar="BOT", help="Exact local bot ID")
                if verb == "restart":
                    route.add_argument("--ceiling", type=int, metavar="SECONDS",
                                       help="Override the per-bot bridge readiness ceiling")
            elif domain == "project" and verb == "show":
                route.add_argument("project_id", metavar="PROJECT", help="Exact project key")
            elif domain == "context":
                route.add_argument("--bot", dest="bot_id", help="Validate this local bot ID")
