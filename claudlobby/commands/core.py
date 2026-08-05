"""Core compositor commands: validate, generate, list-library, diff, promote, status, doctor, report-back, uptime, warm-cache."""

from __future__ import annotations

import json as _json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..composer import compose_bot, compose_fleet
from ..diff import diff_bot, promote_bot
from ..validator import validate
from ._helpers import _load_env, _load_fleet_or_exit, _resolve_paths

log = logging.getLogger("claudlobby")


def cmd_doctor(args) -> int:
    """Pre-flight fleet health diagnostic."""
    from ..doctor import format_report, run_doctor

    paths = _resolve_paths(args)
    _load_env(paths)
    fleet, _md = _load_fleet_or_exit(paths)
    report = run_doctor(fleet, paths)
    print(format_report(report))
    return 1 if report.has_failures else 0


def cmd_freshbox(args) -> int:
    """Fresh-box self-containment audit (#644 P4): every grant traces to an
    equipped source's contract (no over-grant/orphan), the composed allow covers
    every declared grant (no under-grant), and the Tier-A settings surface
    (enabledPlugins/skip-flags/sandbox) is composed per-bot, not global-inherited.
    """
    from ..freshbox import (
        audit_bot,
        audit_fleet,
        exits_nonzero,
        format_report,
        reap_orphan_units,
    )

    paths = _resolve_paths(args)
    _load_env(paths)
    fleet, _md = _load_fleet_or_exit(paths)

    bot = None
    if args.bot:
        bot = fleet.bots.get(args.bot)
        if bot is None:
            log.error("no such bot: %s", args.bot)
            return 1

    # Reap before auditing so the report reflects the cleaned state.
    if args.reap:
        removed = reap_orphan_units(fleet, paths, [bot] if bot else None)
        for p in removed:
            print(f"reaped orphan unit: {p}")
        if not removed:
            print("no orphan units to reap")

    # The CLI opts into scanning the operator's host-tier ~/.env (a WARN surface);
    # the library default (home=None) never reaches into a personal home.
    home = Path.home()
    findings = (
        audit_bot(bot, fleet, paths, home=home)
        if bot
        else audit_fleet(fleet, paths, home=home)
    )
    print(format_report(fleet, findings))
    return 1 if exits_nonzero(findings, strict=args.strict) else 0


def cmd_validate(args) -> int:
    paths = _resolve_paths(args)
    _load_env(paths)
    fleet, _ = _load_fleet_or_exit(paths)
    report = validate(fleet, paths)

    for e in report.errors:
        log.error("%s", e)
    for w in report.warnings:
        log.warning("%s", w)

    if args.strict and report.has_issues:
        log.error("--strict: warnings count as errors")
        return 1
    if report.has_errors:
        return 1
    if not report.has_issues:
        log.info("fleet.yaml OK (%d bots, %d teams)", len(fleet.bots), len(fleet.teams))
    return 0


def cmd_generate(args) -> int:
    from ..composer import (
        compose_fleet_timers,
        compose_host_bot_handles,
        compose_host_timers,
    )

    paths = _resolve_paths(args)
    _load_env(paths)
    fleet, merged_defaults = _load_fleet_or_exit(paths)
    report = validate(fleet, paths)

    if report.has_errors:
        log.error("validation errors — refusing to generate")
        for e in report.errors:
            log.error("%s", e)
        return 1
    if args.strict and report.warnings:
        log.error("--strict: warnings count as errors — refusing to generate")
        for w in report.warnings:
            log.warning("%s", w)
        return 1
    for w in report.warnings:
        log.warning("%s", w)

    if args.bot:
        bot = fleet.bots.get(args.bot)
        if not bot:
            log.error("bot '%s' not in fleet.yaml", args.bot)
            return 1
        out = compose_bot(bot, fleet, paths)
        log.info("composed %s → %s", args.bot, out)
    else:
        out = compose_fleet(fleet, paths)
        log.info("composed %d bots → %s", len(out), paths.runtime_bots)

    # Fleet-level timer generation (after per-bot loop). Called unconditionally:
    # compose_fleet_timers early-returns cheaply (no mkdir) when nothing is
    # configured, and owns the "prune a removed stanza's stale units" reconcile
    # on that path — a caller-side guard would skip that cleanup.
    timers_dir = compose_fleet_timers(fleet, paths, merged_defaults)
    if timers_dir.is_dir():
        log.info("composed fleet timers → %s", timers_dir)

    # Host-global jobs (system.yaml host:) are platform equipment, not fleet
    # config — composed unconditionally, enrolled only by setup-system.
    host_timers_dir = compose_host_timers(paths)
    if host_timers_dir.is_dir():
        log.info("composed host timers → %s", host_timers_dir)

    # The GitHub mention guard's name list (#1019). Host-wide, so it is written
    # on every generate regardless of which fleet was named — a bot references
    # other fleets' bots constantly, and those are the mentions most likely to
    # be written by someone with no relationship to that bot.
    handles = compose_host_bot_handles(paths)
    log.info("composed bot-handle guard list → %s", handles)

    return 0


def cmd_host_timers(args) -> int:
    """Compose host-global timer units from system.yaml host.jobs.

    Needs no fleet.yaml — host jobs are package-owned. setup-system runs this
    before enrollment so a cold host (no fleet composed yet) still gets its
    host units.
    """
    from ..composer import compose_host_bot_handles, compose_host_timers

    paths = _resolve_paths(args)
    host_timers_dir = compose_host_timers(paths)
    if host_timers_dir.is_dir():
        log.info("composed host timers → %s", host_timers_dir)
    else:
        log.info("no host jobs declared — nothing composed")
    return 0


def cmd_list_library(args) -> int:
    paths = _resolve_paths(args)

    def _list_md(label: str, kind: str):
        """Walk overlay → base recursively. Display nested files as `dir/name`."""
        log.info("%s:", label)
        seen: dict[str, str] = {}  # rel_key (no .md) → "[overlay]" or "[base]"
        for d in paths.library_search_dirs(kind):
            if not d.is_dir():
                continue
            tag = (
                "[overlay]"
                if (paths.overlay_library and d == paths.overlay_library / kind)
                else "[base]"
            )
            for p in sorted(d.rglob("*.md")):
                if p.stem.lower().startswith("readme"):
                    continue
                rel_key = str(p.relative_to(d).with_suffix(""))
                if rel_key not in seen:
                    seen[rel_key] = tag
        for rel_key, tag in sorted(seen.items()):
            marker = " (override)" if tag == "[overlay]" else ""
            log.info("  %s%s", rel_key, marker)

    _list_md("Expertise", "expertise")

    log.info("MCP fragments (base only):")
    if paths.base_mcp.is_dir():
        for p in sorted(paths.base_mcp.glob("*.json")):
            log.info("  %s", p.stem)

    _list_md("Integrations", "integrations")
    _list_md("Protocols", "protocols")
    _list_md("Guardrails", "guardrails")
    _list_md("Resources", "resources")
    _list_md("Lessons", "lessons")
    _list_md("Post-actions", "post_actions")

    log.info("Skills:")
    seen_skills: dict[str, str] = {}  # rel_key → tag
    for d in paths.library_search_dirs("skills"):
        if not d.is_dir():
            continue
        tag = (
            "[overlay]"
            if (paths.overlay_library and d == paths.overlay_library / "skills")
            else "[base]"
        )
        for sub in sorted(d.rglob("*")):
            if not sub.is_dir():
                continue
            if not (sub / "SKILL.md").is_file():
                continue
            rel_key = str(sub.relative_to(d))
            if rel_key not in seen_skills:
                seen_skills[rel_key] = tag
    for rel_key, tag in sorted(seen_skills.items()):
        marker = " (override)" if tag == "[overlay]" else ""
        log.info("  %s%s", rel_key, marker)

    log.info("Tools:")
    for name, is_overlay in sorted(
        paths.library_dir_names("tools", "tool.yaml").items()
    ):
        log.info("  %s%s", name, " (override)" if is_overlay else "")

    log.info("Voices:")
    seen_voices: dict[str, Path] = {}
    if paths.overlay_voices and paths.overlay_voices.is_dir():
        for p in sorted(paths.overlay_voices.rglob("*.md")):
            seen_voices[p.name] = p
    if paths.base_voices.is_dir():
        for p in sorted(paths.base_voices.rglob("*.md")):
            seen_voices.setdefault(p.name, p)
    for name in sorted(seen_voices):
        p = seen_voices[name]
        try:
            tag = (
                " (override)"
                if (paths.overlay_voices and p.is_relative_to(paths.overlay_voices))
                else ""
            )
        except ValueError:
            tag = ""
        log.info("  %s%s", p.relative_to(paths.root), tag)

    if paths.fleet_dir:
        log.info("[fleet overlay: %s]", paths.fleet_dir.relative_to(paths.root))
    else:
        log.info("[no fleet overlay — root mode. Use --fleet <name> for overlay mode.]")
    return 0


def cmd_diff(args) -> int:
    from ..diff import diff_fleet_timers

    paths = _resolve_paths(args)
    _load_env(paths)
    fleet, merged_defaults = _load_fleet_or_exit(paths)
    if args.bot:
        sys.stdout.write(diff_bot(args.bot, fleet, paths))
    else:
        for name in fleet.bots:
            sys.stdout.write(diff_bot(name, fleet, paths))
        # Fleet-level timer drift
        timer_drift = diff_fleet_timers(fleet, paths, merged_defaults)
        if timer_drift:
            sys.stdout.write(timer_drift)
    return 0


def cmd_promote(args) -> int:
    paths = _resolve_paths(args)
    fleet, _md = _load_fleet_or_exit(paths)
    sys.stdout.write(promote_bot(args.bot, fleet, paths))
    return 0


def cmd_status(args) -> int:
    """Fleet health dashboard — live snapshot from tmux, systemd, fleet-state."""
    from ..status import (
        collect_fleet_status,
        format_bot_detail,
        format_json,
        format_table,
    )

    paths = _resolve_paths(args)
    _load_env(paths)
    fleet, _md = _load_fleet_or_exit(paths)

    bot_filter = getattr(args, "bot", None)
    use_json = getattr(args, "json", False)

    statuses = collect_fleet_status(fleet, paths)

    if bot_filter:
        matches = [bs for bs in statuses if bs.name == bot_filter]
        if not matches:
            log.error("bot %r not found in fleet %r", bot_filter, fleet.name)
            return 1
        if use_json:
            sys.stdout.write(format_json(matches, fleet.name))
        else:
            sys.stdout.write(format_bot_detail(matches[0]))
        return 0

    if use_json:
        sys.stdout.write(format_json(statuses, fleet.name))
    else:
        sys.stdout.write(format_table(statuses, fleet.name))
    return 0


def cmd_report_back(args) -> int:
    """Query the report-back ledger — human-readable table of bot work events."""
    paths = _resolve_paths(args)

    # Resolve ledger path (fleet_state owns the overlay-vs-root rule; the shell
    # twin is fleet_runtime_dir in lib-common.sh, used by report-back.sh)
    ledger = paths.fleet_state / "report-back.jsonl"

    if not ledger.is_file():
        log.info("no report-back ledger found at %s", ledger)
        return 0

    # Parse --since into a cutoff timestamp
    cutoff = None
    if args.since:
        raw = args.since.strip()
        now = datetime.now(timezone.utc)
        if raw.endswith("h"):
            cutoff = now - timedelta(hours=int(raw[:-1]))
        elif raw.endswith("d"):
            cutoff = now - timedelta(days=int(raw[:-1]))
        elif raw.endswith("m"):
            cutoff = now - timedelta(minutes=int(raw[:-1]))
        else:
            try:
                cutoff = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                log.error(
                    "cannot parse --since '%s' (use e.g. 24h, 7d, 30m, or ISO date)",
                    raw,
                )
                return 1

    # Read and filter entries
    entries = []
    for line in ledger.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if args.bot and entry.get("bot") != args.bot:
            continue
        if args.status and entry.get("status") != args.status:
            continue
        if cutoff:
            try:
                ts = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
                if ts < cutoff:
                    continue
            except (KeyError, ValueError):
                continue
        entries.append(entry)

    if not entries:
        log.info("no matching events")
        return 0

    if args.json:
        for e in entries:
            print(_json.dumps(e))
        return 0

    # Table output
    print(f"{'TIMESTAMP':<22} {'BOT':<12} {'STATUS':<10} {'SUMMARY':<50} {'PR'}")
    print("-" * 110)
    for e in entries:
        ts = e.get("ts", "")[:19]
        bot = e.get("bot", "")[:11]
        status = e.get("status", "")[:9]
        summary = e.get("summary", "")[:49]
        pr = e.get("pr_url", "")
        print(f"{ts:<22} {bot:<12} {status:<10} {summary:<50} {pr}")

    print(f"\n{len(entries)} event(s)")
    return 0


def cmd_workstreams(args) -> int:
    """Read-only view of the fleet workstream registry. Writes go exclusively
    through lib/workstream-update.sh and the /workstream manager skill."""
    from ..workstreams import format_list, format_show, load_workstreams

    paths = _resolve_paths(args)
    workstreams = load_workstreams(paths)
    if getattr(args, "ws_command", "list") == "show":
        entry = workstreams.get(args.id)
        if not entry:
            log.error("no such workstream: %s", args.id)
            return 1
        print(format_show(entry))
    else:
        print(format_list(workstreams))
    return 0


def cmd_uptime(args) -> int:
    """Per-bot uptime, MTBR, and restart-rate metrics from keepalive logs."""
    from ..uptime import WINDOWS, aggregate_fleet, format_json, format_table

    paths = _resolve_paths(args)
    bots_dir = paths.runtime_bots
    if not bots_dir.is_dir():
        log.error("runtime bots dir not found: %s", bots_dir)
        return 1

    windows = [args.window] if args.window else list(WINDOWS.keys())
    results = aggregate_fleet(bots_dir, windows=windows, bot_filter=args.bot)

    if not results:
        log.info("No bots found in %s", bots_dir)
        return 0

    if args.json:
        sys.stdout.write(format_json(results) + "\n")
    else:
        display_window = args.window or "24h"
        sys.stdout.write(format_table(results, window=display_window) + "\n")
    return 0


def cmd_warm_cache(args) -> int:
    """Pre-download npx packages referenced by MCP fragments.

    Scans all MCP fragments used by the fleet and runs `npx -y <pkg> --help`
    for each unique npx-based package. This ensures ~/.npm/_npx/ is warm
    before bot startup, avoiding 30-60s cold-download delays on Pi hardware.
    """
    paths = _resolve_paths(args)
    _load_env(paths)
    fleet, _md = _load_fleet_or_exit(paths)

    # Collect unique npx packages from all bot MCP configs
    npx_packages: set[str] = set()
    for bot in fleet.bots.values():
        for entry in bot.mcp:
            frag_path = paths.find_library_file("mcp", entry.name, ".json")
            if frag_path is None:
                continue
            try:
                frag = _json.loads(frag_path.read_text())
            except _json.JSONDecodeError:
                continue
            for k, v in frag.items():
                if k.startswith("_") or not isinstance(v, dict):
                    continue
                if v.get("command") == "npx" and "args" in v:
                    args_list = v["args"]
                    # Extract package name: first arg after "-y"
                    for i, a in enumerate(args_list):
                        if a == "-y" and i + 1 < len(args_list):
                            npx_packages.add(args_list[i + 1])
                            break

    if not npx_packages:
        log.info("no npx-based MCP packages found in fleet")
        return 0

    log.info("warming npx cache for %d packages:", len(npx_packages))
    failed = []
    for pkg in sorted(npx_packages):
        log.info("  %s", pkg)
        if args.dry_run:
            continue
        # Use --help or a quick-exit to trigger download without running the server
        try:
            proc = subprocess.run(
                ["npx", "-y", pkg, "--help"],
                capture_output=True,
                timeout=120,
                text=True,
            )
        except subprocess.TimeoutExpired:
            log.warning("  timeout warming %s (120s) — may still have cached", pkg)
        except FileNotFoundError:
            log.error("npx not found — install Node.js first")
            return 1
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("  failed to warm %s: %s", pkg, e)
            failed.append(pkg)
        else:
            # Nothing above fires when the child simply exits non-zero: run()
            # without check=True does not raise, so without this branch a failed
            # warm fell through to "cache warm complete". capture_output holds
            # the diagnostic that explains it -- report it rather than discard it.
            if proc.returncode != 0:
                out = (proc.stderr or proc.stdout or "").strip().splitlines()
                log.warning(
                    "  %s exited %d: %s",
                    pkg,
                    proc.returncode,
                    out[-1].strip() if out else "(no output)",
                )
                failed.append(pkg)

    if args.dry_run:
        log.info("(dry run — no downloads)")
    elif failed:
        log.warning(
            "%d of %d packages failed to warm: %s",
            len(failed),
            len(npx_packages),
            ", ".join(failed),
        )
        # Exit non-zero so a caller cannot read silence as success. Note what
        # this still cannot tell you: a non-zero child does NOT prove the cache
        # is unpopulated -- a package whose CLI rejects `--help` (mcp-remote
        # parses its first positional as a URL) may well have downloaded first.
        # The status is reported; neither outcome is claimed.
        return 1
    else:
        log.info("cache warm complete")
    return 0
