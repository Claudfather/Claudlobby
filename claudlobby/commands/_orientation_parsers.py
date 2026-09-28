"""Dependency-light registration for read-only context and equipment views."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".orientation", __package__).dispatch(args),
                   json_output=args.json)


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
