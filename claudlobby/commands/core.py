"""Core compositor commands still used by private composition and fleet operations."""

from __future__ import annotations

import json as _json
import logging
import subprocess
from pathlib import Path

from .. import mcp_direct
from ..mcp_grammar import GrammarUnavailable, grammar
from ..composer import compose_bot, compose_fleet
from ..source_state import (
    SOURCE_ABSENT,
    probe_source,
    unreachable_line,
)
from ..validator import WARNING_CATEGORIES, render_warnings, validate, warning_summary
from ._helpers import _load_env, _load_fleet_or_exit, _resolve_paths

log = logging.getLogger("claudlobby")


def _warn_baseline_gate(report, path: Path, *, write: bool) -> int:
    """``config validate --warn-baseline``: fail only on a warning category that is
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
        remedy = (f"record one with `claudlobby config validate --warn-baseline {path} --write`"
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
    grammar is `claudlobby/_runtime_scripts/mcp-package-grammar.py`, shared with the composer's binary
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
    # is the shape three of the four shipped uvx fragments use.
    targets: dict[tuple[str, tuple[str, ...]], str] = {}
    unreadable: set[str] = set()
    # #1604: spec -> (bare, version) for the copies an ARMED bot launches from.
    # Only armed bots contribute, so a fleet that armed nobody installs nothing.
    direct: dict[str, tuple[str, str]] = {}
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
                if bot.mcp_direct_launch and runtime == "npx":
                    pin = mcp_direct.pinned(g, pkg)
                    if pin is not None:
                        direct[pkg] = pin

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

    # The copies armed bots launch directly (#1604), after the npx warm: with
    # `--prefer-offline` the install resolves from the tarballs that warm left.
    install_failed: list[str] = []
    if direct:
        log.info("direct-launch copies for %d package(s):", len(direct))
    for spec, (bare, version) in sorted(direct.items()):
        if args.dry_run:
            there = mcp_direct.entry_point(
                mcp_direct.package_dir(paths.root, bare, version), bare
            )[0]
            log.info("  %s: %s", spec, "present" if there else "would install")
            continue
        outcome, detail = mcp_direct.install(paths.root, bare, version)
        if outcome == "failed":
            log.warning("  failed to install %s for direct launch: %s", spec, detail)
            install_failed.append(spec)
        elif outcome == "unusable":
            # Not a failed warm: the npx launch still works, it only keeps
            # its wrapper. Said, so the forgone saving is visible.
            log.warning("  %s cannot launch directly (%s); its bots keep npx", spec, detail)
        else:
            log.info("  %s: %s", spec, outcome)

    if args.dry_run:
        log.info("(dry run — no downloads)")
    elif failed or install_failed:
        if summary is not None:
            summary["failed"] = sorted(set(failed + install_failed))
        if failed:
            log.warning(
                "%d of %d packages failed to warm: %s",
                len(failed), len(targets), ", ".join(failed),
            )
        if install_failed:
            log.warning(
                "%d of %d direct-launch copies failed to install: %s",
                len(install_failed), len(direct), ", ".join(install_failed),
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
