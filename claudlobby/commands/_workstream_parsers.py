"""Canonical workstream grammar, loaded without domain imports."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".workstream", __package__).dispatch(args),
                   json_output=args.json, request_id=getattr(args, "request_id", None))


def register_workstream_subparsers(sub):
    parent = sub.add_parser("workstream", help="Read or update selected-fleet workstreams")
    children = parent.add_subparsers(dest="workstream_action", required=True)
    for verb in ("list", "show", "open", "progress", "renew", "block", "unblock", "close", "prune"):
        parser = children.add_parser(verb)
        if verb in ("show", "progress", "renew", "block", "unblock", "close"):
            parser.add_argument("id", metavar="WORKSTREAM_ID")
        if verb == "open":
            parser.add_argument("title", metavar="TITLE")
            parser.add_argument("--id")
            parser.add_argument("--project")
            parser.add_argument("--owner")
            parser.add_argument("--next")
        if verb == "progress":
            parser.add_argument("--next")
        if verb in ("renew", "block", "unblock"):
            parser.add_argument("--note", required=True)
        if verb == "block":
            parser.add_argument("--on", required=True, metavar="ACTOR")
        if verb == "close":
            parser.add_argument("--status", choices=("done", "abandoned"), default="done")
        if verb not in ("list", "show"):
            parser.add_argument("--request-id", required=True, metavar="UUID")
            parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=_dispatch, public_command=f"workstream.{verb}")
