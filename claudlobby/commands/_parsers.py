"""Argparse registration: command implementations load only on dispatch."""

from __future__ import annotations

from importlib import import_module

import argparse


def _command(module: str, name: str):
    """Defer implementation imports until argparse has accepted the call."""
    def dispatch(args):
        handler = getattr(import_module(f".{module}", __package__), name)
        return handler(args)

    return dispatch


def register_subparsers(sub) -> None:
    """Register all CLI subcommands on the given subparsers action."""

    from ._release_parsers import register_release_subparsers
    register_release_subparsers(sub)
    from ._orientation_parsers import register_orientation_subparsers
    register_orientation_subparsers(sub)


    pfb = sub.add_parser(
        "freshbox",
        help="Fresh-box self-containment audit — grants trace to sources, "
        "Tier-A composed (#644 P4)",
    )
    pfb.add_argument("--bot", help="Audit only one bot")
    pfb.add_argument(
        "--strict", action="store_true", help="Fail on advisory warnings too"
    )
    pfb.set_defaults(func=_command("core", "cmd_freshbox"))

    pg = sub.add_parser(
        "generate", help="Compose runtime/bots/ from fleet.yaml + library/"
    )
    pg.add_argument("--bot", help="Generate only one bot")
    pg.add_argument(
        "--strict", action="store_true", help="Refuse to generate on warnings"
    )
    pg.set_defaults(func=_command("core", "cmd_generate"))

    pht = sub.add_parser(
        "host-timers",
        help="Compose host-global timer units from system.yaml host.jobs",
    )
    pht.set_defaults(func=_command("core", "cmd_host_timers"))

    from ._library_parsers import register_library_subparsers
    register_library_subparsers(sub)

    pd = sub.add_parser(
        "diff",
        help="Show drift between runtime/bots/<bot>/ and what generate would produce",
    )
    pd.add_argument("--bot", help="Diff only one bot (default: all)")
    pd.set_defaults(func=_command("core", "cmd_diff"))

    pp = sub.add_parser("promote", help="Promote runtime drift back to library/")
    pp.add_argument("bot", help="Bot name")
    pp.set_defaults(func=_command("core", "cmd_promote"))

    pb = sub.add_parser(
        "brief",
        help="Read a bot's active fleet mission, canonical work, workstreams, reports and alerts",
    )
    pb.add_argument("--bot", help="Read this active-fleet bot's view (defaults to caller or fleet manager)")
    pb.add_argument("--json", action="store_true", help="One schema-1 result containing a schema-2 brief")
    pb.add_argument("--usage-since", metavar="DURATION",
                    help="Include bounded viewer transcript usage for up to 7d (for example 24h)")
    pb.add_argument(
        "--boot",
        action="store_true",
        help="Render the SessionStart boot payload (#1102 R3/M1): work "
        "lines + empty-state provenance + door line, token-capped — the "
        "composed hook's mode, never the full brief",
    )
    from ..command_result import execute

    def _brief_dispatch(args):
        return execute("brief", lambda: _command("brief_read", "dispatch")(args),
                       json_output=args.json)

    pb.set_defaults(func=_brief_dispatch, public_command="brief")

    from ._workstream_parsers import register_workstream_subparsers
    register_workstream_subparsers(sub)

    from ._checkin_parsers import register_checkin_subparsers
    register_checkin_subparsers(sub)

    pt = sub.add_parser("task", help="Read and operate on fleet-owned work")
    t_sub = pt.add_subparsers(dest="task_action", required=True)

    from ._task_read_parsers import register_task_read_subparsers
    assignment_children = register_task_read_subparsers(sub, t_sub)
    from ._task_write_parsers import register_task_write_subparsers
    register_task_write_subparsers(t_sub, assignment_children)

    tick = sub.add_parser("_task-recheck-tick", help=argparse.SUPPRESS)
    tick.add_argument("tick_fleet", metavar="FLEET")
    tick.set_defaults(func=_command("task_recheck", "tick"))

    from ._message_read_parsers import register_message_read_subparsers
    message_children = register_message_read_subparsers(sub)
    from ._message_write_parsers import register_message_write_subparsers
    register_message_write_subparsers(message_children)
    from ._request_read_parsers import register_request_read_subparsers
    register_request_read_subparsers(sub)

    from ._event_read_parsers import register_event_read_subparsers
    register_event_read_subparsers(sub)

    # --- observable plane (Phase 1 kernel) ---
    pp = sub.add_parser("plane", help="Observable-plane operations")
    psub = pp.add_subparsers(dest="plane_action", required=True)
    for action in ("emit", "emit-batch"):
        pe = psub.add_parser(action, help="Validated Plane ingest" if action == "emit"
                             else "Atomic multi-event Plane ingest")
        if action == "emit":
            pe.add_argument("event_type", help="Plane event family")
        pe.add_argument("--file", required=True, help="Request JSON path, or '-' for stdin")
        pe.add_argument("--require-commit", action="store_true",
                        help="Refuse instead of spooling a conditional mutation")
        pe.add_argument("--json", action="store_true", help="Schema-1 result")

        def _emit_dispatch(args):
            from ..command_result import execute
            action = args.plane_action
            return execute(f"plane.{action}",
                           lambda: _command("plane_emit", "dispatch")(args),
                           json_output=args.json)

        pe.set_defaults(func=_emit_dispatch, public_command=f"plane.{action}")
    ps = psub.add_parser("status", help="Kernel health: db, counts, spool")
    ps.set_defaults(func=_command("plane", "cmd_plane_status"))
    pd = psub.add_parser("doctor", help="Kernel health rungs (exit 1 on attention)")
    pd.set_defaults(func=_command("plane", "cmd_plane_doctor"))
    pv = psub.add_parser("serve", help="Run the ingest daemon (foreground)")
    pv.add_argument("--socket", help="Socket path override (default: state/plane/ingest.sock)")
    pv.add_argument("--drain-interval", default="600",
                    help="Seconds between spool drains (default 600)")
    pv.set_defaults(func=_command("plane", "cmd_plane_serve"))
    pvw = psub.add_parser("view", help="Run the operator-plane view daemon (read-only UI)")
    pvw.add_argument("--host", default="127.0.0.1",
                     help="Bind address (default 127.0.0.1 — Tailscale Serve fronts it; a raw address is the dev fallback)")
    pvw.add_argument("--port", type=int, default=8899, help="Bind port (default 8899)")
    pvw.set_defaults(func=_command("plane", "cmd_plane_view"))
    po = psub.add_parser("open", help="Print/launch the operator plane URL")
    po.add_argument("--port", type=int, default=8899, help="View daemon port (default 8899)")
    po.add_argument("--no-browser", action="store_true", help="Print the URL only")
    po.set_defaults(func=_command("plane", "cmd_plane_open"))
    psc = psub.add_parser("schema", help="Export JSON Schemas (envelope + families)")
    psc.set_defaults(func=_command("plane", "cmd_plane_schema"))
    ppr = psub.add_parser(
        "prune",
        help="Age out raw metric_samples past the retention window (30d;"
        " family-scoped, never the ledger)")
    ppr.add_argument("--days", type=int, default=None,
                     help="Retention window in days (default 30)")
    ppr.add_argument("--dry-run", action="store_true",
                     help="Report the count without deleting")
    ppr.set_defaults(func=_command("plane", "cmd_plane_prune"))
    pex = psub.add_parser(
        "expire",
        help="Attention expiry sweep: emit `expired` for assignments overdue"
        " past the horizon (7d; a Lane-B fact through ingest, idempotent)")
    pex.add_argument("--after-days", type=int, default=None,
                     help="Days an overdue assignment must be QUIET (no task"
                     " event) before it expires (default 7; 0 = anything overdue"
                     " and silent right now — sharp)")
    pex.add_argument("--dry-run", action="store_true",
                     help="Report the count without emitting")
    pex.set_defaults(func=_command("plane", "cmd_plane_expire"))
    prg = psub.add_parser(
        "registry",
        help="Registry lane reads: current state, history, changes, verify")
    prg.add_argument("--type", choices=(
        "host", "vault", "fleet", "bot", "project", "library_item"),
        help="Filter current listing by entity type")
    # DELIBERATELY NOT --fleet: that dest is the global overlay selector
    # (_resolve_paths consumes it), and sharing it made a legal db-scope
    # query refuse unless a whole overlay existed by that name (gauntlet,
    # probed). --verify uses the GLOBAL --fleet, which it genuinely needs.
    prg.add_argument("--scope", dest="scope_fleet", metavar="FLEET",
                     help="Filter the listing by fleet scope (a db fact —"
                     " needs no overlay)")
    mode = prg.add_mutually_exclusive_group()
    mode.add_argument("--show", metavar="ALIAS",
                      help="One entity's current payload (alias or uid)")
    mode.add_argument("--history", metavar="ALIAS",
                      help="One entity's SCD windows (alias or uid)")
    mode.add_argument("--changes", type=int, nargs="?", const=20,
                      metavar="N", help="Recent field-level changes"
                      " (default 20)")
    mode.add_argument("--verify", action="store_true",
                      help="Hash-verify the projection against the"
                      " re-derived estate (root-mode fleet.yaml, or the"
                      " global --fleet <name> for an overlay)")
    prg.set_defaults(func=_command("plane", "cmd_plane_registry"))
    psp = psub.add_parser("spool", help="Inspect/drain the emit spool")
    psp.add_argument("spool_action", choices=["list", "inspect", "retry", "quarantine"])
    psp.add_argument("name", nargs="?", help="Spool file name (inspect/quarantine)")
    psp.set_defaults(func=_command("plane", "cmd_plane_spool"))
