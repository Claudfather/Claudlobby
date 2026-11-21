"""The workstream registry import and backfill under the public migration grammar."""

from __future__ import annotations

from . import lazy

_run = lazy(".migration_converters")


def _route(children, name, help_text):
    parser = children.add_parser(name, help=help_text)
    parser.add_argument("--json", action="store_true", help="One schema-1 result object")
    parser.set_defaults(func=_run, public_command=f"migration.{name}")
    return parser


def register_converter_subparsers(children):
    workstreams = _route(children, "workstreams", "Import a residual workstream registry")
    workstreams.add_argument("--file", help="Registry file; defaults to selected fleet state")
    mode = workstreams.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Emit events; default is preview")
    mode.add_argument("--dry-run", action="store_true", help="Print the full envelope plan without emitting")
    workstreams.add_argument("--archive", action="store_true", help="Archive source after committed apply")

    # A one-time step after the update that gives every task a workstream (#1053): it files each
    # open task with no open workstream into its own. It records through the workstream writer,
    # so its owner is `commands/workstream.py`, not a legacy converter.
    backfill = children.add_parser(
        "workstream-backfill",
        help="File each open task that has no open workstream into its own; default is preview")
    backfill.add_argument("--json", action="store_true", help="One schema-1 result object")
    backfill.add_argument("--apply", action="store_true", help="Record the backfill; default is preview")
    backfill.add_argument("--request-id", metavar="UUID",
                          help="Required with --apply; reuse it only to retry the same backfill")
    backfill.set_defaults(func=lazy(".workstream", request_id=True),
                          public_command="migration.workstream-backfill", workstream_action="backfill")
