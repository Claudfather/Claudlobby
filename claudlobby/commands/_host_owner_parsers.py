"""Host-local owner authority doors; no credential-bearing CLI arguments."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".host_owner", __package__).dispatch(args),
                   json_output=args.json)


def register_host_owner(hosts):
    owner = hosts.add_parser("owner", help="Locally approve or revoke Plane owner access")
    actions = owner.add_subparsers(dest="owner_action", required=True)
    for action, help_text in (
        ("status", "Inspect this host's owner pairing without changing it"),
        ("initialize", "Prepare owner authority on an existing installation (interactive)"),
        ("confirm", "Approve a browser's pairing request at this terminal (interactive)"),
        ("revoke", "Revoke the current pairing and all its sessions (interactive)"),
    ):
        route = actions.add_parser(action, help=help_text)
        route.add_argument("--json", action="store_true", help="Structured result (status only)")
        route.set_defaults(func=_dispatch, public_command=f"host.owner.{action}")
