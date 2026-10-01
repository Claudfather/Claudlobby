"""Dependency-light public library authoring grammar."""

from importlib import import_module

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".library", __package__).dispatch(args),
                   json_output=args.json)


def register_library_subparsers(sub):
    group = sub.add_parser("library", help="List or create fleet library components")
    children = group.add_subparsers(dest="library_command", required=True)
    listing = children.add_parser("list", help="List installed and overlay library components")
    listing.add_argument("--json", action="store_true", help="One schema-1 result object")
    listing.set_defaults(func=_dispatch, public_command="library.list")

    create = children.add_parser("create", help="Create a skill or guardrail in the writable overlay")
    create.add_argument("--kind", required=True, choices=("skill", "guardrail"))
    create.add_argument("--name", help="Lowercase slug")
    create.add_argument("--description", help="One-line description")
    create.add_argument("--title", help="Guardrail title; defaults to title-cased name")
    create.add_argument("--argument-hint", help="Optional skill argument hint")
    create.add_argument("--interactive", action="store_true", help="Prompt for authoring fields")
    create.add_argument("--dry-run", action="store_true", help="Preview without writing")
    create.add_argument("--json", action="store_true", help="One schema-1 result object; never prompts")
    create.set_defaults(func=_dispatch, public_command="library.create")
