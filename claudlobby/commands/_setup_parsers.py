"""Small public setup route registrations for the existing host/fleet groups."""

from importlib import import_module
from uuid import uuid4

from ..command_result import execute


def _dispatch(args):
    args.activation_id = str(uuid4()) if args.public_command == "fleet.setup" and not args.plan_only else None
    return execute(args.public_command,
                   lambda: import_module(".setup", __package__).dispatch(args),
                   json_output=args.json, request_id=args.activation_id)


def register_host_setup(hosts):
    parser = hosts.add_parser("setup", help="Assemble a sealed release on a cold host")
    for flag in ("wheel", "dependency-lock", "wheelhouse", "interpreter"):
        parser.add_argument(f"--{flag}", required=True, metavar="PATH")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=_dispatch, public_command="host.setup")


def register_fleet_setup(children):
    parser = children.add_parser("setup", help="Install an authored fleet and activate its sealed configuration")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", metavar="FILE")
    source.add_argument("--template", choices=["revenue-web-app"],
                        help="Use the candidate team shipped in the selected release")
    parser.add_argument("--repository", metavar="OWNER/REPO", help="Explicit approved repository for --template")
    parser.add_argument("--goal", metavar="TEXT", help="Explicit approved goal for --template")
    parser.add_argument("--plan-only", action="store_true",
                        help="Prepare a frozen configuration plan without activation or supervision changes")
    parser.add_argument("--install-directory", metavar="PATH", help="Required when activating (without --plan-only)")
    parser.add_argument("--replace-config", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=_dispatch, public_command="fleet.setup")
