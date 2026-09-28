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


def _automation_dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".automation", __package__).dispatch(args),
                   json_output=args.json, request_id=getattr(args, "request_id", None))


def register_orientation_subparsers(sub):
    for domain, verbs in (("context", ("show",)),
                          ("bot", ("list", "show", "capabilities")),
                          ("fleet", ("show",)),
                          ("project", ("list", "show"))):
        group = sub.add_parser(domain, help=f"Read {domain} declarations and available evidence")
        children = group.add_subparsers(dest=f"{domain}_command", required=True)
        if domain == "fleet":
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
            route.set_defaults(func=_dispatch, public_command=f"{domain}.{verb}")
            if domain == "bot" and verb != "list":
                route.add_argument("bot_id", metavar="BOT", help="Exact local bot ID")
            elif domain == "project" and verb == "show":
                route.add_argument("project_id", metavar="PROJECT", help="Exact project key")
            elif domain == "context":
                route.add_argument("--bot", dest="bot_id", help="Validate this local bot ID")
