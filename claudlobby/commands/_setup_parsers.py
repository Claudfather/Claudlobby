"""Small public setup route registrations for the existing host/fleet groups."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    args.activation_id = None
    return execute(args.public_command,
                   lambda: import_module(".setup", __package__).dispatch(args),
                   json_output=args.json, request_id=None)


def register_host_setup(hosts):
    parser = hosts.add_parser("setup", help="Prepare a managed Git installation on a cold host")
    for flag in ("installation", "interpreter"):
        parser.add_argument(f"--{flag}", required=True, metavar="PATH")
    parser.add_argument("--remote", required=True, metavar="REMOTE", help="Explicit trusted Git remote")
    parser.add_argument("--ref", required=True, metavar="REF", help="Resolve this ref once for preparation")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=_dispatch, public_command="host.setup")


def register_fleet_setup(children):
    parser = children.add_parser("setup", help="Prepare the first authored fleet through the managed Git updater")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", metavar="FILE")
    source.add_argument("--template", choices=["revenue-web-app"],
                        help="Use the candidate team shipped in the managed source")
    parser.add_argument("--repository", metavar="OWNER/REPO", help="Explicit approved repository for --template")
    parser.add_argument("--goal", metavar="TEXT", help="Explicit approved goal for --template")
    parser.add_argument("--plan-only", action="store_true",
                        help="Prepare the same update operation without native updater enrollment or dispatch")
    parser.add_argument("--install-directory", required=True, metavar="PATH", help="Exact native install directory, including with --plan-only")
    parser.add_argument("--replace-config", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=_dispatch, public_command="fleet.setup")
