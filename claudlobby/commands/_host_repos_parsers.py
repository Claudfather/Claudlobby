"""Small registration surface for the operator's selected repo pull."""

from . import lazy

_dispatch = lazy(".host_repos")


def register_host_repos(hosts):
    repos = hosts.add_parser("repos", help="Request a selected bot's project repository update")
    actions = repos.add_subparsers(dest="repos_command", required=True)
    pull = actions.add_parser("pull", help="Fast-forward one bot's immediate project repos")
    pull.add_argument("--bot", required=True, metavar="BOT", help="Exact declared bot ID")
    pull.add_argument("--json", action="store_true", help="One schema-1 result object")
    pull.set_defaults(func=_dispatch, public_command="host.repos.pull")
