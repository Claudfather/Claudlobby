"""Small public setup route registrations for the existing host/fleet groups."""

from importlib import import_module
from uuid import uuid4

from ..command_result import execute


def _dispatch(args):
    args.activation_id = str(uuid4()) if args.public_command == "fleet.setup" else None
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
    parser.add_argument("--config", required=True, metavar="FILE")
    parser.add_argument("--install-directory", required=True, metavar="PATH")
    parser.add_argument("--replace-config", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=_dispatch, public_command="fleet.setup")
