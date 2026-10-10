"""Host-local owner authority doors; no credential-bearing CLI arguments."""

from importlib import import_module
from pathlib import Path

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
        ("bind-source", "Attest that this Plane database belongs to this installation (interactive)"),
        ("confirm", "Approve a browser's pairing request at this terminal (interactive)"),
        ("revoke", "Revoke the current pairing and all its sessions (interactive)"),
    ):
        route = actions.add_parser(action, help=help_text)
        route.add_argument("--json", action="store_true", help="Structured result (status only)")
        route.set_defaults(func=_dispatch, public_command=f"host.owner.{action}")
    for capability, label in (("messages", "ordinary messages"), ("nudges", "selected-task nudges")):
        allow = actions.add_parser("allow-" + capability,
            help=f"Allow {label} as a local human in one active fleet (interactive)")
        allow.add_argument("--target-fleet", required=True, help="Explicit active fleet name")
        allow.add_argument("--actor", required=True, help="Explicit local human: actor alias")
        allow.add_argument("--register-actor", action="store_true", help="Separately approve first-contact registration if the actor is absent")
        allow.add_argument("--json", action="store_true", help="Not supported for interactive approval")
        allow.set_defaults(func=_dispatch, public_command="host.owner.allow-" + capability)
        revoke = actions.add_parser("revoke-" + capability,
            help=f"Revoke an exact retained {label} fleet grant (interactive)")
        revoke.add_argument("--fleet-uid", required=True, help="Exact retained fleet UID; active config is not required")
        revoke.add_argument("--json", action="store_true", help="Not supported for interactive approval")
        revoke.set_defaults(func=_dispatch, public_command="host.owner.revoke-" + capability)
    serve = actions.add_parser("serve", help="Serve owner Plane on a private Unix socket (foreground)")
    serve.add_argument("--json", action="store_true", help="Not supported for the foreground server")
    serve.add_argument("--origin", required=True, help="Exact external HTTPS origin configured in Tailscale Serve")
    serve.add_argument("--tailscale", type=Path, required=True, help="Absolute path to the native Tailscale executable")
    serve.add_argument("--socket", type=Path, help="Private Unix socket path; default DATA_ROOT/state/plane/owner.sock")
    serve.set_defaults(func=_dispatch, public_command="host.owner.serve")
