"""Legacy data converters under the public migration grammar."""

from __future__ import annotations

from importlib import import_module

from ..command_result import execute


def _run(args):
    return execute(args.public_command,
                   lambda: import_module(".migration_converters", __package__).dispatch(args),
                   json_output=args.json)


def _route(children, name, help_text):
    parser = children.add_parser(name, help=help_text)
    parser.add_argument("--json", action="store_true", help="One schema-1 result object")
    parser.set_defaults(func=_run, public_command=f"migration.{name}")
    return parser


def _source_args(parser):
    parser.add_argument("--source", required=True, help="Existing bot fleet directory")
    parser.add_argument("--map", action="append", default=[],
                        help="Fleet bot=legacy directory; repeatable")
    parser.add_argument("--apply", action="store_true", help="Write changes; default is preview")


def register_converter_subparsers(children):
    env = _route(children, "env", "Move legacy secrets into tiered .env files")
    _source_args(env)

    data = _route(children, "data", "Copy legacy bot data directories")
    _source_args(data)
    data.add_argument("--include", help="Comma-separated subdirectories to include")
    data.add_argument("--exclude", help="Comma-separated subdirectories to skip")

    cron = _route(children, "cron", "Rewrite legacy crontab paths")
    _source_args(cron)

    memory = _route(children, "memory", "Copy legacy Claude memory files")
    memory.add_argument("--map", nargs="*", help="Source-pattern:bot mappings")
    memory.add_argument("--force", action="store_true", help="Overwrite existing memory files on apply")
    memory.add_argument("--apply", action="store_true", help="Copy files; default is preview")

    lessons = _route(children, "lessons", "Capture referential lessons in the Claudron vault")
    lessons.add_argument("--apply", action="store_true", help="Capture lessons; default is preview")
    lessons.add_argument("--vault", help="Target vault path for --apply")
    lessons.add_argument("--vault-fleet", dest="fleet_scope", help="Capture in a fleet tier")
    lessons.add_argument("--claudron-bin", dest="claudron_bin", help="Claudron executable")

    workstreams = _route(children, "workstreams", "Import a residual workstream registry")
    workstreams.add_argument("--file", help="Registry file; defaults to selected fleet state")
    mode = workstreams.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Emit events; default is preview")
    mode.add_argument("--dry-run", action="store_true", help="Print the full envelope plan without emitting")
    workstreams.add_argument("--archive", action="store_true", help="Archive source after committed apply")
