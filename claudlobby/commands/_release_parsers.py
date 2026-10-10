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
    args.activation_id = (getattr(args, "resume", None) or str(uuid4())) if args.public_command == "host.activate" else None
    return execute(args.public_command,
                   lambda: import_module(".host", __package__).dispatch(args),
                   json_output=args.json, request_id=args.activation_id)


def _dispatch_doctor(args):
    return execute(args.public_command,
                   lambda: import_module(".host_doctor", __package__).dispatch(args),
                   json_output=args.json)


def _dispatch_host_job(args):
    return execute(args.public_command,
                   lambda: import_module(".host_jobs", __package__).dispatch(args),
                   json_output=args.json)


def _dispatch_host_env_cache(args):
    return execute(args.public_command,
                   lambda: import_module(".host_env_cache", __package__).dispatch(args),
                   json_output=args.json)


def _dispatch_host_credentials(args):
    return execute(args.public_command,
                   lambda: import_module(".host_credentials", __package__).dispatch(args),
                   json_output=args.json)


def _dispatch_host_channels(args):
    return execute(args.public_command,
                   lambda: import_module(".host_channels", __package__).dispatch(args),
                   json_output=args.json)


def _dispatch_host_github_app(args):
    return execute(args.public_command,
                   lambda: import_module(".host_github_app", __package__).dispatch(args),
                   json_output=args.json)


def _dispatch_host_update(args):
    return execute(args.public_command,
                   lambda: import_module(".host_update", __package__).dispatch(args),
                   json_output=args.json)


def _dispatch_host_supervision(args):
    return execute(args.public_command,
                   lambda: import_module(".host_supervision", __package__).dispatch(args),
                   json_output=args.json)


def _dispatch_config_explain(args):
    return execute(args.public_command,
                   lambda: import_module(".config_explain", __package__).dispatch(args),
                   json_output=args.json)


def _dispatch_config_validate(args):
    return execute(args.public_command,
                   lambda: import_module(".config_validation", __package__).dispatch(args),
                   json_output=args.json)


def _route(sub, name, command, help):
    parser = sub.add_parser(name, help=help)
    parser.add_argument("--json", action="store_true", help="One schema-1 result object")
    parser.set_defaults(func=_dispatch, public_command=command)
    return parser


def register_release_subparsers(sub):
    host = sub.add_parser("host", help="Host release diagnosis and explicit first activation")
    hosts = host.add_subparsers(dest="host_command", required=True)
    from ._setup_parsers import register_host_setup
    register_host_setup(hosts)
    from ._host_repos_parsers import register_host_repos
    register_host_repos(hosts)
    from ._host_owner_parsers import register_host_owner
    register_host_owner(hosts)
    supervision = hosts.add_parser("supervision", help="Inspect selected-fleet supervision cleanup")
    supervision_actions = supervision.add_subparsers(dest="supervision_command", required=True)
    reap = supervision_actions.add_parser("reap-orphans", help="Reap stale units in declared bot directories")
    reap.add_argument("--bot", metavar="BOT", help="Limit to one declared bot")
    mode = reap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="List stale units without removing them")
    mode.add_argument("--apply", action="store_true", help="Remove stale units")
    reap.add_argument("--json", action="store_true", help="One schema-1 result object")
    reap.set_defaults(func=_dispatch_host_supervision, public_command="host.supervision.reap-orphans")
    env = hosts.add_parser("env", help="Inspect the runtime's environment tier paths")
    envs = env.add_subparsers(dest="env_command", required=True)
    tiers = envs.add_parser("tiers", help="List host, root, fleet, and bot .env tiers without values")
    tiers.add_argument("--bot", help="Declared bot whose .env tier to include")
    tiers.add_argument("--json", action="store_true", help="One schema-1 result object")
    tiers.set_defaults(func=_dispatch_host_env_cache, public_command="host.env.tiers")
    cache = hosts.add_parser("cache", help="Prepare host-shared MCP package caches")
    caches = cache.add_subparsers(dest="cache_command", required=True)
    warm = caches.add_parser("warm", help="Pre-download npx and uvx packages used by the fleet")
    warm.add_argument("--dry-run", action="store_true", help="Identify packages without downloading")
    warm.add_argument("--json", action="store_true", help="One schema-1 result object")
    warm.set_defaults(func=_dispatch_host_env_cache, public_command="host.cache.warm")
    job = hosts.add_parser("job", help="Inspect or request selected host jobs")
    jobs = job.add_subparsers(dest="job_command", required=True)
    for action in ("list", "show"):
        route = jobs.add_parser(action, help=f"{action.capitalize()} effective host jobs")
        route.add_argument("--json", action="store_true", help="One schema-1 result object")
        if action == "show":
            route.add_argument("name", help="Packaged host job name, e.g. plane-prune")
        route.set_defaults(func=_dispatch_host_job, public_command=f"host.job.{action}")
    run = jobs.add_parser("run", help="Request one selected, enabled host timer job")
    run.add_argument("name", help="Packaged host timer job name")
    run.add_argument("--json", action="store_true", help="One schema-1 result object")
    run.set_defaults(func=_dispatch_host_job, public_command="host.job.run")
    credentials = hosts.add_parser("credentials", help="Check or reconcile selected-fleet credentials")
    credential_actions = credentials.add_subparsers(dest="credential_command", required=True)
    reconcile = credential_actions.add_parser(
        "reconcile", help="Compare declared credentials, stored tiers and equipped consumers")
    reconcile.add_argument("--json", action="store_true", help="One schema-1 result object")
    reconcile.set_defaults(func=_dispatch_host_credentials, public_command="host.credentials.reconcile")
    check = credential_actions.add_parser(
        "check", help="Run the selected fleet's credential probe and transition alerts once")
    check.add_argument("--json", action="store_true", help="One schema-1 result object")
    check.set_defaults(func=_dispatch_host_credentials, public_command="host.credentials.check")
    channels = hosts.add_parser("channels", help="Inspect or explicitly approve managed channel plugins")
    channel_actions = channels.add_subparsers(dest="channel_command", required=True)
    for action in ("check", "approve"):
        route = channel_actions.add_parser(action, help=(
            "Inspect managed Telegram channel approvals" if action == "check" else
            "Add official and fork Telegram approvals to managed settings"))
        route.add_argument("--json", action="store_true", help="One schema-1 result object")
        route.set_defaults(func=_dispatch_host_channels, public_command=f"host.channels.{action}")
    github_app = hosts.add_parser("github-app", help="Configure or mint a host GitHub App identity")
    github_actions = github_app.add_subparsers(dest="github_app_command", required=True)
    setup = github_actions.add_parser("setup", help="Validate App identity and write its host config")
    for flag in ("app-id", "installation-id", "private-key", "slug"):
        setup.add_argument(f"--{flag}", required=True)
    setup.add_argument("--config-path", metavar="PATH")
    setup.add_argument("--no-write-config", action="store_true")
    setup.add_argument("--json", action="store_true", help="One schema-1 result object")
    setup.set_defaults(func=_dispatch_host_github_app, public_command="host.github-app.setup")
    token = github_actions.add_parser("token", help="Print one fresh App installation token")
    token.add_argument("--json", action="store_true", help="One schema-1 result object containing the token")
    token.set_defaults(func=_dispatch_host_github_app, public_command="host.github-app.token")
    update = hosts.add_parser("update", help="Run selected host runtime or sibling update owners")
    updates = update.add_subparsers(dest="update_command", required=True)
    runtime = updates.add_parser("runtime", help="Run the configured Claude Code binary update once")
    runtime.add_argument("--json", action="store_true", help="One schema-1 result object")
    runtime.set_defaults(func=_dispatch_host_update, public_command="host.update.runtime")
    siblings = updates.add_parser("siblings", help="Fast-forward guarded sibling release checkouts")
    siblings.add_argument("--dry-run", action="store_true", help="Report without a checkout merge or notices")
    siblings.add_argument("--json", action="store_true", help="One schema-1 result object")
    siblings.set_defaults(func=_dispatch_host_update, public_command="host.update.siblings")
    doctor = _route(hosts, "doctor", "host.doctor", "Diagnose configured fleets on this host")
    doctor.set_defaults(func=_dispatch_doctor)
    doctor.add_argument("--switches", action="store_true", help="Only show resolved opt-in/out switches")
    doctor.add_argument("--markdown", action="store_true", help="With --switches, render documentation tables")
    doctor.add_argument("--no-delivery", dest="delivery", action="store_false", default=True,
                        help="Skip repository delivery probes, reporting that evidence as unchecked")
    _route(hosts, "releases", "host.releases", "Verify installed releases and report selection")
    status = _route(hosts, "status", "host.status", "Inspect recorded state and passive cold-host prerequisites; no setup effects")
    status.set_defaults(func=_dispatch_host)
    activate = _route(hosts, "activate", "host.activate", "Activate PLAN_ID from an operator shell")
    activate.description = ("First activation requires explicit global --root. "
                            "Use --adopt-existing only for an unsealed, already running estate. "
                            "--resume ID can fix forward supported recorded bootstrap, pause/quiesce, "
                            "and candidate-start stages with durable receipts. It never repeats a "
                            "recorded bot handoff or start; a handoff or start begun without a "
                            "recorded result requires manual recovery evidence.")
    activate.set_defaults(func=_dispatch_host)
    activate.add_argument("plan_id", metavar="PLAN_ID")
    activate.add_argument("--install-directory", required=True, metavar="PATH",
                          help="Absolute native user-unit directory; verified against the OS adapter's search paths")
    activate.add_argument("--adopt-existing", action="store_true",
                          help="First, forward-only adoption of a reviewed unsealed estate and its existing Plane")
    activate.add_argument("--resume", metavar="ACTIVATION_ID",
                          help="Fix forward the same recorded activation at a supported step with required evidence")
    repair = _route(hosts, "repair-start", "host.repair-start",
                    "Archive one verified-dead bot's unresolved activation start")
    repair.description = ("Operator only. Starts nothing. Requires the selected pending bots_started "
                          "activation, the named bot's start without a result, identical frozen unit "
                          "bytes and an inactive unit with no private tmux server. The attempt and its "
                          "evidence stay in the activation record; then run the sealed candidate's "
                          "host activate --resume once to start that bot again.")
    repair.set_defaults(func=_dispatch_host)
    repair.add_argument("repair_activation_id", metavar="ACTIVATION_ID")
    repair.add_argument("--fleet", dest="repair_fleet", required=True, metavar="FLEET")
    repair.add_argument("--bot", required=True, metavar="BOT")
    repair.add_argument("--reason", required=True, metavar="TEXT")
    abort = _route(hosts, "abort-adoption", "host.abort-adoption",
                   "Abort an unsealed first adoption stopped while pausing producers")
    abort.description = ("Operator-only, from a sealed release CLI. Restores only the original producer "
                         "files and native states of a Linux first adoption with no completed step, "
                         "handoff, start, selection or migration. Not general rollback: bots, ingest, "
                         "selection and SQL are untouched.")
    abort.set_defaults(func=_dispatch_host)
    abort.add_argument("abort_activation_id", metavar="ACTIVATION_ID")
    abort.add_argument("--reason", required=True, metavar="TEXT", help="Recorded operator reason")
    abort.add_argument("--expected-sql-version", required=True, type=int, metavar="INT",
                       help="Plane user_version from the operator's retained pre-activation preflight")

    config = sub.add_parser("config", help="Stage and inspect configuration proposals")
    configs = config.add_subparsers(dest="config_command", required=True)
    explain = configs.add_parser("explain", help="Explain environment tiers or supported fleet/bot scalar sources without values")
    explain.add_argument("key", nargs="?", metavar="KEY",
                         help="Environment variable or fleet.FIELD / bot.FIELD; omit to list environment variables")
    explain.add_argument("--bot", metavar="BOT", help="Select a declared bot for bot fields or its environment tier")
    explain.add_argument("--json", action="store_true", help="One schema-1 result object")
    explain.set_defaults(func=_dispatch_config_explain, public_command="config.explain")
    validate = configs.add_parser("validate", help="Validate authored config, or audit composed runtime with --runtime")
    validate.add_argument("--strict", action="store_true", help="Fail on warnings")
    validate.add_argument("--runtime", action="store_true",
                          help="Audit rendered grants, isolation and runtime-owned sources instead of authored config")
    validate.add_argument("--bot", metavar="BOT", help="Limit --runtime audit to one declared bot")
    validate.add_argument("--warn-baseline", metavar="FILE",
                          help="Fail when a warning category is new or has grown since FILE was written")
    validate.add_argument("--write", action="store_true",
                          help="With --warn-baseline, record warning categories to FILE")
    validate.add_argument("--json", action="store_true", help="One schema-1 result object")
    validate.set_defaults(func=_dispatch_config_validate, public_command="config.validate")
    plan = _route(configs, "plan", "config.plan", "Stage all declared host fleets using a sealed candidate")
    plan.add_argument("--release", required=True, metavar="ID")
    plan.add_argument("--fleet-path", action="append", default=[], metavar="PATH",
                      help="Explicit external fleet directory or fleet.yaml; repeatable")
    diff = _route(configs, "diff", "config.diff", "Inspect staged changes or current rendered drift without secret bytes")
    diff.add_argument("plan_id", nargs="?", metavar="PLAN_ID",
                      help="Staged plan to inspect; omit for current rendered drift")
    diff.add_argument("--bot", metavar="BOT", help="Limit current drift to one declared bot")

    migration = sub.add_parser("migration", help="Preview data migration and inspect recorded progress")
    migrations = migration.add_subparsers(dest="migration_command", required=True)
    plan = _route(migrations, "plan", "migration.plan", "Read-only migration inventory; repeat under quiescence")
    plan.add_argument("--source-release", required=True, metavar="ID")
    plan.add_argument("--target-release", required=True, metavar="ID")
    plan.add_argument("--initialize-empty", action="store_true",
                      help="Record explicit initialization intent only when both DB and WAL are absent")
    status = _route(migrations, "status", "migration.status", "Actual SQL version and recorded activation evidence")
    status.add_argument("--activation", metavar="ID", help="Inspect one named host activation")
    from ._migration_parsers import register_converter_subparsers
    register_converter_subparsers(migrations)
