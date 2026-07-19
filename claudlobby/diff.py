"""Drift detection between runtime/ and library/.

A bot may edit its own files in runtime/bots/<name>/ during a session
(skills auto-sync via symlink; CLAUDE.md does not). `diff` shows what
would change if `generate` ran now. `promote` (interactive) routes
drifted content back to library/personas/, library/voices/, or a
new guardrail/protocol.

v1: diff is unified-diff text output; promote is a stub that points
the user at the right library/ file based on heuristics.
"""

from __future__ import annotations
import difflib
import json
from pathlib import Path

from .composer import compose_bot_conf, compose_claude_md, compose_mcp_json
from .config import FleetConfig
from .paths import Paths


def diff_bot(bot_name: str, fleet: FleetConfig, paths: Paths) -> str:
    bot = fleet.bots.get(bot_name)
    if not bot:
        return f"bot '{bot_name}' not in fleet.yaml\n"

    bot_dir = paths.bot_runtime(bot_name)
    if not bot_dir.is_dir():
        return f"runtime/bots/{bot_name}/ does not exist — run `claudlobby generate` first\n"

    parts: list[str] = []

    # CLAUDE.md
    expected_md = compose_claude_md(bot, fleet, paths)
    actual_md_path = bot_dir / "CLAUDE.md"
    actual_md = actual_md_path.read_text() if actual_md_path.is_file() else ""
    if expected_md != actual_md:
        parts.append(f"=== CLAUDE.md drift in {bot_name} ===")
        parts.extend(
            difflib.unified_diff(
                expected_md.splitlines(),
                actual_md.splitlines(),
                fromfile="library-composed (would be regenerated)",
                tofile=f"runtime/bots/{bot_name}/CLAUDE.md (current)",
                lineterm="",
            )
        )

    # .mcp.json
    expected_mcp = compose_mcp_json(bot, paths)
    actual_mcp_path = bot_dir / ".mcp.json"
    actual_mcp = (
        json.loads(actual_mcp_path.read_text()) if actual_mcp_path.is_file() else {}
    )
    if expected_mcp != actual_mcp:
        parts.append(f"\n=== .mcp.json drift in {bot_name} ===")
        parts.extend(
            difflib.unified_diff(
                json.dumps(expected_mcp, indent=2).splitlines(),
                json.dumps(actual_mcp, indent=2).splitlines(),
                fromfile="library-composed",
                tofile=f"runtime/bots/{bot_name}/.mcp.json",
                lineterm="",
            )
        )

    # bot.conf — a generated file (env vars sourced by the bot session AND the
    # supervisor scripts, e.g. keepalive). A fleet.yaml change that adds an env
    # var drifts it; omitting it here lets a stale bot.conf read as "no drift"
    # while runtime is out of sync.
    expected_conf = compose_bot_conf(bot, fleet, paths)
    actual_conf_path = bot_dir / "bot.conf"
    actual_conf = actual_conf_path.read_text() if actual_conf_path.is_file() else ""
    if expected_conf != actual_conf:
        parts.append(f"\n=== bot.conf drift in {bot_name} ===")
        parts.extend(
            difflib.unified_diff(
                expected_conf.splitlines(),
                actual_conf.splitlines(),
                fromfile="library-composed (would be regenerated)",
                tofile=f"runtime/bots/{bot_name}/bot.conf (current)",
                lineterm="",
            )
        )

    if not parts:
        return f"no drift in {bot_name}\n"
    return "\n".join(parts) + "\n"


def diff_fleet_timers(fleet: FleetConfig, paths: Paths, merged_defaults: dict) -> str:
    """Diff fleet-level timer units against what generate would produce."""
    from .composer import compose_fleet_timers
    import tempfile

    sd = fleet.system_defaults
    sweep_on = fleet.sweep_enabled()
    if (
        (not sd.enabled or not sd.timers)
        and not sweep_on
        and not fleet.briefing_enabled()
    ):
        return ""

    timers_dir = paths.runtime_fleet / "timers"
    if not timers_dir.is_dir():
        return "=== fleet timers: runtime/fleet/timers/ does not exist — run `claudlobby generate`\n"

    # Generate expected timers to a temp dir, then compare. Pass the REAL Paths
    # (redirecting output via output_dir) so diff and generate exercise an
    # identical Paths surface — a partial shadow would AttributeError here, but
    # not in generate, the moment the timer path reads a new paths.* attribute.
    with tempfile.TemporaryDirectory() as tmpdir:
        parts: list[str] = []

        expected_fleet = Path(tmpdir) / "expected_fleet"
        compose_fleet_timers(fleet, paths, merged_defaults, output_dir=expected_fleet)

        expected_timers_dir = expected_fleet / "timers"
        if not expected_timers_dir.is_dir():
            return ""

        expected_files = {f.name for f in expected_timers_dir.iterdir()}
        actual_files = {f.name for f in timers_dir.iterdir()}

        for fname in sorted(expected_files | actual_files):
            expected_path = expected_timers_dir / fname
            actual_path = timers_dir / fname

            expected_text = expected_path.read_text() if expected_path.is_file() else ""
            actual_text = actual_path.read_text() if actual_path.is_file() else ""

            if expected_text != actual_text:
                parts.append(f"\n=== fleet timer drift: {fname} ===")
                parts.extend(
                    difflib.unified_diff(
                        expected_text.splitlines(),
                        actual_text.splitlines(),
                        fromfile=f"expected ({fname})",
                        tofile=f"runtime/fleet/timers/{fname} (current)",
                        lineterm="",
                    )
                )

        if not parts:
            return ""
        return "\n".join(parts) + "\n"


def promote_bot(bot_name: str, fleet: FleetConfig, paths: Paths) -> str:
    """Interactive promote — v1: report intent, point user at files."""
    bot = fleet.bots.get(bot_name)
    if not bot:
        return f"bot '{bot_name}' not in fleet.yaml\n"

    expertise_paths = [paths.base_expertise / f"{a}.md" for a in bot.expertise]
    voice_path = paths.root / bot.voice if bot.voice else None
    bot_md = paths.bot_runtime(bot_name) / "CLAUDE.md"

    expertise_lines = (
        "\n".join(f"     • {p}" for p in expertise_paths)
        if expertise_paths
        else "     (none — set expertise in fleet.yaml)"
    )

    return (
        f"Promote workflow for '{bot_name}' (v1 — manual):\n"
        f"\n"
        f"1. Review drift:    claudlobby diff {bot_name}\n"
        f"2. Decide what to keep, then edit the source:\n"
        f"   - Expertise content →\n{expertise_lines}\n"
        + (
            f"   - Voice / personality → {voice_path}\n"
            if voice_path
            else "   - Voice / personality → create a voices/<name>.md and reference it in fleet.yaml\n"
        )
        + f"   - Mission (one paragraph) → fleet.yaml `bots.{bot_name}.mission`\n"
        f"   - Scope override → fleet.yaml `bots.{bot_name}.scope`\n"
        f"   - Shared resource → new file under {paths.base_resources}/\n"
        f"   - Integration / MCP usage doc → new file under {paths.base_integrations}/ (paired with mcp fragment)\n"
        f"   - Cross-cutting protocol → new file under {paths.base_protocols}/\n"
        f"   - New guardrail → new file under {paths.base_guardrails}/\n"
        f"   - Lesson / 'learned the hard way' → new file under {paths.base_lessons}/\n"
        f"3. After editing library/, run: claudlobby generate\n"
        f"   (Runtime CLAUDE.md is overwritten; library/ is now the source of truth.)\n"
        f"\n"
        f"Runtime file:  {bot_md}\n"
        f"Interactive promote (with picker) — coming in v2.\n"
    )
