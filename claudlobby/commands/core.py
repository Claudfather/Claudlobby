"""Core compositor commands: validate, generate, list-library, diff, promote, status, uptime, warm-cache."""

from __future__ import annotations

import json as _json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..mcp_grammar import GrammarUnavailable, grammar
from ..composer import compose_bot, compose_fleet
from ..diff import diff_bot, promote_bot
from ..source_state import (
    SOURCE_ABSENT,
    probe_source,
    scan_dir,
    unreachable_line,
)
from ..validator import WARNING_CATEGORIES, render_warnings, validate, warning_summary
from ._helpers import _load_env, _load_fleet_or_exit, _resolve_paths
from ._helpers import refuse_unreachable

log = logging.getLogger("claudlobby")


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


def cmd_env_register(args) -> int:
    """The derived credential register (#1214 F6 / #1226).

    DERIVED rather than written: a hand-kept note of "things that work this way"
    is stale the first time someone adds an integration and forgets, and its
    staleness is invisible because it still reads like an answer.

    Reports SHADOWING, not merely resolution. A var resolving to the empty
    string from a more specific tier while a real value sits upstream is
    invisible to every other check — the key is set, so nothing calls it
    missing; a value exists, so nothing calls it unconfigured — and it is the
    state that motivated the whole workstream.
    """
    from ..env_register import ResolverUnavailable, build, exits_nonzero, format_report

    paths = _resolve_paths(args)
    _load_env(paths)
    fleet, _ = _load_fleet_or_exit(paths)

    try:
        reg = build(fleet, paths, bot=getattr(args, "bot", None))
    except ResolverUnavailable as exc:
        # Refuse rather than answer from a copy of the tier order. A register
        # that guessed would be worse than none: its whole claim is that it
        # reports what a boot would actually find.
        print(f"cannot derive the register: {exc}")
        return 2

    if getattr(args, "json", False):
        import json

        print(
            json.dumps(
                {
                    "bot": reg.bot,
                    "tiers": [
                        {"tier": t, "path": p, "state": st} for t, p, st in reg.tiers
                    ],
                    "vars": [r._asdict() for r in reg.rows],
                    "undeclared": list(reg.undeclared),
                },
                indent=2,
            )
        )
    else:
        print(format_report(reg))
    return 1 if exits_nonzero(reg) else 0


def _warn_baseline_gate(report, path: Path, *, write: bool) -> int:
    """``validate --warn-baseline``: fail only on a warning category that is
    new, or has more warnings than the baseline recorded (#1663).

    ``--strict`` fails on every warning, so a fleet that has accepted some can
    never switch it on; this is the gate such a fleet can run. It compares
    category counts, never message text, so rewording a warning cannot trip it,
    and a category that shrinks or disappears never fails. Returns 0 when
    nothing grew, 1 when a category is new or grew, and 2 when the baseline
    cannot be read or written: an unreadable baseline is not an unchanged one.
    """
    current = report.warning_categories
    if write:
        try:
            path.write_text(_json.dumps(dict(sorted(current.items())), indent=2) + "\n")
        except OSError as e:
            log.error("could not write warning baseline %s: %s", path, e)
            return 2
        log.info("wrote warning baseline %s: %s", path,
                 warning_summary(report) if report.warnings else "no warnings")
        return 0
    probe = probe_source(path)
    if probe.unreachable:
        remedy = (f"record one with `claudlobby validate --warn-baseline {path} --write`"
                  if probe.state == SOURCE_ABSENT else "")
        log.error("%s", unreachable_line("the warning baseline", probe, remedy=remedy))
        return 2
    try:
        baseline = _json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log.error("warning baseline %s could not be parsed: %s", path, e)
        return 2
    if not isinstance(baseline, dict) or not all(
        isinstance(k, str) and type(v) is int and v >= 0 for k, v in baseline.items()
    ):
        log.error("warning baseline %s is not a {category: count} map of "
                  "non-negative integers", path)
        return 2
    grew = False
    for kind in sorted(set(current) | set(baseline)):
        before, now = baseline.get(kind, 0), current.get(kind, 0)
        if now > before:
            grew = True
            log.error("%s: %s (%d → %d) — %s; its lines are tagged [%s] above",
                      "new warning category" if before == 0 else "warning category grew",
                      kind, before, now,
                      WARNING_CATEGORIES.get(kind, "not a registered category"), kind)
        elif now < before:
            log.info("warning category shrank: %s (%d → %d) — rerun with --write "
                     "to keep the lower count", kind, before, now)
    if grew:
        return 1
    log.info("warning baseline %s: no category is new or grew", path)
    return 0


def cmd_validate(args) -> int:
    paths = _resolve_paths(args)
    baseline = getattr(args, "warn_baseline", None)
    write = getattr(args, "write", False)
    if write and not baseline:
        log.error("--write needs --warn-baseline FILE — it names the file to write")
        return 2
    _load_env(paths)
    fleet, _ = _load_fleet_or_exit(paths)
    report = validate(fleet, paths)

    for e in report.errors:
        log.error("%s", e)
    for line in render_warnings(report):
        log.warning("%s", line)
    if report.warnings:
        log.info("%s", warning_summary(report))
    gate = _warn_baseline_gate(report, Path(baseline), write=write) if baseline else 0

    if args.strict and report.has_issues:
        log.error("--strict: warnings count as errors")
        return 1
    if report.has_errors:
        return 1
    if gate:
        return gate
    if not report.has_issues:
        log.info("fleet.yaml OK (%d bots, %d teams)", len(fleet.bots), len(fleet.teams))
    return 0


def cmd_generate(args) -> int:
    from ..composer import (
        compose_fleet_timers,
        compose_host_bot_handles,
        compose_host_mention_allowlist,
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
        for line in render_warnings(report):
            log.warning("%s", line)
        return 1
    for line in render_warnings(report):
        log.warning("%s", line)

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
    allowlist = compose_host_mention_allowlist(paths)
    log.info("composed mention allowlist → %s", allowlist)

    _warn_unresolvable_skill_refs(paths)

    # Phase 2b: the generate-time registry scan (cause=generate). NON-
    # BLOCKING and dormant: unarmed fleets (no PLANE_EMIT_ENABLED=1 in the
    # fleet env) return None silently, and a scan failure must never break
    # a generate — the composed estate is correct with or without its
    # keyframes; the scan just records what generate produced.
    try:
        from ..plane.registry_emit import run_generate_scan
        summary = run_generate_scan(paths, fleet)
        if summary:
            log.info(
                "registry scan %s: %d entities (%d tombstoned,"
                " complete=%s) — %s",
                summary["scan_id"], summary["entities"],
                summary["tombstoned"], summary["complete"],
                summary["outcomes"])
    except Exception as exc:  # noqa: BLE001 — non-blocking by contract
        log.warning("registry scan failed (generate unaffected): %s", exc)

    return 0


def cmd_host_timers(args) -> int:
    """Compose host-global timer units from system.yaml host.jobs.

    Needs no fleet.yaml — host jobs are package-owned. setup-system runs this
    before enrollment so a cold host (no fleet composed yet) still gets its
    host units.
    """
    from ..composer import (
        compose_host_timers,
    )

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
        voice_root = paths.overlay_voices if tag else paths.base_voices
        log.info("  voices/%s%s", p.relative_to(voice_root), tag)

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
    # #1722: the inputs-moved line comes FIRST and exactly once, whether one bot
    # or the whole fleet is being diffed — it is a fact about the fleet's
    # manifest, not about any bot.
    from ..diff import manifest_header
    sys.stdout.write(manifest_header(fleet, paths))
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
    # A disabled reaction must never be silent (the defaults ruling). Resolving
    # the switches shells the env-tier resolver once; a failure leaves the
    # header unchanged rather than taking the dashboard down with it.
    try:
        from .. import switches as _sw
        switch_states = _sw.resolve(paths, fleet)
    except Exception:  # noqa: BLE001 — status must render regardless
        switch_states = None

    if bot_filter:
        matches = [bs for bs in statuses if bs.name == bot_filter]
        if not matches:
            log.error("bot %r not found in fleet %r", bot_filter, fleet.name)
            return 1
        if use_json:
            sys.stdout.write(format_json(matches, fleet.name, switch_states))
        else:
            sys.stdout.write(format_bot_detail(matches[0]))
        return 0

    if use_json:
        sys.stdout.write(format_json(statuses, fleet.name, switch_states))
    else:
        sys.stdout.write(format_table(statuses, fleet.name, switch_states))
    return 0


def _coverage_line(plane, window_s, family=None) -> str:
    """The coverage statement for an OPEN plane session (#1658).

    The uptime door routes through here so the wording and derivation live in
    `lib/plane-readers.py`, beside the plane's other SQL.

    Degrades to a plain note rather than raising: a door must not lose its
    answer because the sentence describing that answer could not be built. An
    install whose readers predate `coverage()` says so, which is the same
    shape `brief` uses for a matcher older than its caller.
    """
    try:
        first, last, rows = plane.pr.coverage(plane.conn, family)
        return plane.pr.coverage_line(first, last, rows, window_s)
    except AttributeError:
        return ("coverage: unknown — the readers installed at this root predate"
                " the coverage derivation (#1658)")
    except Exception as exc:                       # pragma: no cover - defensive
        return f"coverage: unknown — {exc}"


def cmd_uptime(args) -> int:
    """Per-bot uptime, MTBR, and restart-rate metrics from the plane's
    heartbeat samples and restart transitions (F18 closure R2b)."""
    from ..uptime import WINDOWS, aggregate_fleet, format_json, format_table

    paths = _resolve_paths(args)
    bots_dir = paths.runtime_bots
    # probe_dir, never is_dir()+glob: an unreadable bots dir (or ancestor)
    # made a live fleet render as successful emptiness — "No bots found" at
    # rc 0, the unreachable-vs-empty collapse this module exists to kill
    # (external round 2, probed; source_state named this caller and the
    # audit found it had never been wired).
    # scan_dir, and the returned list IS what aggregate_fleet consumes — a
    # probe followed by aggregate_fleet's own glob re-opened the directory,
    # and glob swallows a mid-iteration OSError: a LIVE bot behind a benign
    # entry vanished at rc 0 (external round 4, probed).
    probe, bot_dirs = scan_dir(bots_dir)
    if not probe.reachable:
        line = unreachable_line("the runtime bots dir", probe)
        print(line, file=sys.stderr if args.json else sys.stdout)
        return 1

    windows = [args.window] if args.window else list(WINDOWS.keys())
    # F18 closure R2b: the plane is the ONLY source — the heartbeat samples,
    # the dead-session fact and the restart transitions keepalive lands
    # there; no keepalive.log, no retirement fact. A plane that cannot
    # answer REFUSES (rc 3): an empty table would read as a fleet that never
    # ran. The readers are the install's own stdlib script, like the bash
    # doors' (never this checkout's copy).
    import sqlite3

    from ..brief import plane_session
    from ..uptime import entries_from_plane
    plane, note = plane_session(paths)
    if plane is None:
        return refuse_unreachable("uptime", note)
    since = (datetime.now(timezone.utc) - max(WINDOWS.values())).isoformat()

    def entries_for(bot_dir):
        return entries_from_plane(plane.pr, plane.conn, plane.fleet, bot_dir.name, since)
    # #1658: the coverage line is per RENDERED window, not per widest window.
    # `uptime` reads back to the widest of 24h/7d/30d and then renders one of
    # them, so a single line derived from `since` above would describe a window
    # the table is not showing -- the same confusion the line exists to remove.
    covs: dict[str, str] = {}
    try:
        results = aggregate_fleet(bots_dir, windows=windows, bot_filter=args.bot,
                                  bot_dirs=bot_dirs, entries_for=entries_for)
        for w in windows:
            covs[w] = _coverage_line(plane, WINDOWS[w].total_seconds())
    except (plane.pr.PlaneUnreachable, sqlite3.Error) as exc:
        return refuse_unreachable("uptime", f"the plane could not answer ({exc})")
    finally:
        plane.close()

    if not results:
        log.info("No bots found in %s", bots_dir)
        return 0

    if args.json:
        sys.stdout.write(format_json(results) + "\n")
        # one per window rendered, on stderr so stdout stays parseable JSON
        for w in windows:
            print(f"{w}: {covs.get(w, 'coverage: unknown')}", file=sys.stderr)
    else:
        display_window = args.window or "24h"
        sys.stdout.write(format_table(results, window=display_window) + "\n")
        sys.stdout.write(covs.get(display_window, "coverage: unknown") + "\n")
    return 0


def cmd_warm_cache(args, *, paths=None, fleet=None, summary=None) -> int:
    """Pre-download the packages referenced by MCP fragments.

    Scans every MCP fragment the fleet uses and runs each package's own
    `--help` through the package manager that fetches it -- `npx` for Node
    servers, `uvx` for Python ones -- so the on-disk caches are warm before bot
    startup instead of paying the download on first connect.

    A cold fetch on either runtime can exceed Claude Code's 30s MCP connect
    budget on its own, so a server whose cache is cold loses deterministically
    rather than only under a boot storm. That is what makes this a warm rather
    than a nice-to-have.

    Which token of a server's args names its package is NOT decided here: that
    grammar is `lib/mcp-package-grammar.py`, shared with the composer's binary
    swap and with `check-npx-cache.sh`, the probe that gates this command.
    """
    paths = paths if paths is not None else _resolve_paths(args)
    _load_env(paths)
    if fleet is None:
        fleet, _md = _load_fleet_or_exit(paths)
    try:
        g = grammar(paths)
    except GrammarUnavailable as e:
        log.error("%s", e)
        return 1

    # Keyed on (runtime, argv) so a package reached by two bots is warmed once,
    # and so a pinned spec plus its separate entry point survives the round
    # trip. A set of bare names cannot represent `--from <spec> <entry>`, which
    # is the shape two of the three shipped uvx fragments use.
    targets: dict[tuple[str, tuple[str, ...]], str] = {}
    unreadable: set[str] = set()
    for bot in fleet.bots.values():
        for entry in bot.mcp:
            frag_path = paths.find_library_file("mcp", entry.name, ".json")
            if frag_path is None:
                continue
            try:
                frag = _json.loads(frag_path.read_text())
            except _json.JSONDecodeError:
                continue
            for k, v in g.servers_in(frag):
                runtime = v.get("command")
                if runtime not in g.WARM_RUNTIMES or "args" not in v:
                    continue
                target = g.warm_prefix(runtime, v["args"])
                if target is None:
                    unreadable.add(f"{entry.name}:{k}")
                    continue
                pkg, prefix = target
                targets[(runtime, tuple(prefix))] = pkg

    if unreadable:
        # Coverage honesty: these servers are NOT warmed and will pay the cold
        # cost at boot. Named rather than dropped -- the remedy is a fragment
        # edit, and nothing else on the estate would surface the gap.
        log.warning(
            "%d MCP server(s) on a cache-backed runtime name no package this "
            "can identify — not warmed: %s",
            len(unreadable),
            ", ".join(sorted(unreadable)),
        )

    if summary is not None:
        summary.update(packages=[{"package": pkg, "runtime": runtime}
                                 for (runtime, _prefix), pkg in sorted(targets.items())],
                       unreadable=sorted(unreadable), failed=[], dry_run=args.dry_run)

    if not targets:
        log.info("no npx- or uvx-based MCP packages found in fleet")
        return 0

    log.info("warming cache for %d package(s):", len(targets))
    failed: list[str] = []
    missing_runtimes: set[str] = set()
    # Keys are (runtime, prefix), so sorting them is already runtime-major.
    for (runtime, prefix), pkg in sorted(targets.items()):
        log.info("  %s (%s)", pkg, runtime)
        if args.dry_run:
            continue
        if runtime in missing_runtimes:
            # Already reported below; an absent toolchain is not per-package news.
            failed.append(pkg)
            continue
        # Run the package's own --help: enough to force the download without
        # starting the server.
        try:
            proc = subprocess.run(
                [runtime, *prefix, "--help"],
                capture_output=True,
                timeout=120,
                text=True,
            )
        except subprocess.TimeoutExpired:
            log.warning("  timeout warming %s (120s) — may still have cached", pkg)
        except FileNotFoundError:
            # The toolchain is absent, not the package. Record it and carry on:
            # a bare `return 1` here let one missing toolchain stop the OTHER
            # ecosystem warming at all, and a fleet mixing npx and uvx must
            # still warm the half it can. The set also keeps this one
            # diagnostic from repeating once per package.
            log.error("%s not found — %s", runtime, g.WARM_RUNTIMES[runtime])
            missing_runtimes.add(runtime)
            failed.append(pkg)
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
        if summary is not None:
            summary["failed"] = failed
        log.warning(
            "%d of %d packages failed to warm: %s",
            len(failed),
            len(targets),
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


# Warn-only rung of the #1253 resolution gate. Deliberately NOT routed through
# the validator report: --strict escalates those to a hard refusal, and a docs
# typo blocking `generate` on a running fleet is a worse failure than the one
# being prevented. This is also the only layer that sees a fleet overlay —
# `local/*/library/` is gitignored, so CI is blind to it by design.
_REF_WARN_CAP = 10


def _warn_unresolvable_skill_refs(paths) -> None:
    from ..skill_refs import scan_composable

    try:
        findings = scan_composable(paths.base_library, paths.overlay_library)
    except Exception as exc:  # never let the advisory rung break composition
        log.debug("skill-ref scan skipped: %s", exc)
        return

    live = [f for f in findings if not f.deferred_to]
    deferred = len(findings) - len(live)
    if not live:
        return

    log.warning(
        "%d backticked /ref(s) resolve to nothing invocable (#1253) — "
        "advisory, composition continued",
        len(live),
    )
    for f in live[:_REF_WARN_CAP]:
        log.warning("  %s:%d: %s", f.path, f.lineno, f.token)
    if len(live) > _REF_WARN_CAP:
        log.warning("  ... and %d more not shown", len(live) - _REF_WARN_CAP)
    if deferred:
        log.warning("  (%d known-deferred ref(s) suppressed)", deferred)
