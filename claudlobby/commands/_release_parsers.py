"""Dependency-light registration for release preparation and diagnosis."""

from importlib import import_module
from uuid import uuid4

from ..command_result import execute


def _dispatch(args):
    return execute(args.public_command,
                   lambda: import_module(".releases", __package__).dispatch(args),
                   json_output=args.json,
                   request_id=str(uuid4()) if args.public_command == "config.plan" else None)


def _dispatch_host(args):
    args.activation_id = str(uuid4()) if args.public_command == "host.activate" else None
    return execute(args.public_command,
                   lambda: import_module(".host", __package__).dispatch(args),
                   json_output=args.json, request_id=args.activation_id)


def _route(sub, name, command, help):
    parser = sub.add_parser(name, help=help)
    parser.add_argument("--json", action="store_true", help="One schema-1 result object")
    parser.set_defaults(func=_dispatch, public_command=command)
    return parser


def register_release_subparsers(sub):
    host = sub.add_parser("host", help="Host release diagnosis and explicit first activation")
    hosts = host.add_subparsers(dest="host_command", required=True)
    _route(hosts, "releases", "host.releases", "Verify installed releases and report selection")
    status = _route(hosts, "status", "host.status", "Inspect recorded host state; running processes are unobserved")
    status.set_defaults(func=_dispatch_host)
    activate = _route(hosts, "activate", "host.activate", "Activate PLAN_ID from an operator shell")
    activate.description = ("First activation requires explicit global --root. "
                            "Use --adopt-existing only for an unsealed, already running Darwin estate; "
                            "interrupted activations require explicit forward repair.")
    activate.set_defaults(func=_dispatch_host)
    activate.add_argument("plan_id", metavar="PLAN_ID")
    activate.add_argument("--install-directory", required=True, metavar="PATH",
                          help="Absolute native user-unit directory; verified against the OS adapter's search paths")
    activate.add_argument("--adopt-existing", action="store_true",
                          help="First, forward-only adoption of a reviewed unsealed Darwin estate and its existing Plane")

    config = sub.add_parser("config", help="Stage and inspect configuration proposals")
    configs = config.add_subparsers(dest="config_command", required=True)
    plan = _route(configs, "plan", "config.plan", "Stage all declared host fleets using a sealed candidate")
    plan.add_argument("--release", required=True, metavar="ID")
    plan.add_argument("--fleet-path", action="append", default=[], metavar="PATH",
                      help="Explicit external fleet directory or fleet.yaml; repeatable")
    diff = _route(configs, "diff", "config.diff", "Inspect proposed paths and state digests without secret bytes")
    diff.add_argument("plan_id", metavar="PLAN_ID")

    migration = sub.add_parser("migration", help="Preview data migration and inspect recorded progress")
    migrations = migration.add_subparsers(dest="migration_command", required=True)
    plan = _route(migrations, "plan", "migration.plan", "Read-only migration inventory; repeat under quiescence")
    plan.add_argument("--source-release", required=True, metavar="ID")
    plan.add_argument("--target-release", required=True, metavar="ID")
    plan.add_argument("--initialize-empty", action="store_true",
                      help="Record explicit initialization intent only when both DB and WAL are absent")
    status = _route(migrations, "status", "migration.status", "Actual SQL version and recorded activation evidence")
    status.add_argument("--activation", metavar="ID", help="Inspect one named host activation")
