"""Dependency-light registration for reads of a repository's open PRs (#2833)."""

from . import lazy

_holders = lazy(".pr_holders")


def register_pr_subparsers(sub):
    pr = sub.add_parser("pr", help="Read a GitHub repository's open pull requests")
    children = pr.add_subparsers(dest="pr_command", required=True)
    holders = children.add_parser(
        "holders", help="List the open PRs that change given paths",
        description="List each open PR of OWNER/REPO that changes a PATH, matched on each "
                    "changed file's name and its previous name (a rename or copy), with its "
                    "number, draft state, base branch and last update. PRs that change a "
                    "file with the same file name at another path are listed apart, not "
                    "counted: a PR written before a directory move changes the file at its "
                    "old path. Reads GitHub only, through gh. Exits 6, naming each PR, when "
                    "the listing or any open PR's file list cannot be read in full.")
    holders.add_argument("paths", nargs="+", metavar="PATH",
                         help="A file's path from the repository root")
    holders.add_argument("--repo", required=True, metavar="OWNER/REPO")
    holders.add_argument("--json", action="store_true", help="One schema-1 result object")
    holders.set_defaults(func=_holders, public_command="pr.holders")
