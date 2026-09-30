"""Drift detection between runtime/ and library/.

A bot may edit its own files in runtime/bots/<name>/ during a session.
`diff` previews the compared artifact families; it does not enumerate every
live effect of generate (in particular skill/command/agent symlinks). `promote` (interactive) routes
drifted content back to library/personas/, library/voices/, or a
new guardrail/protocol.

v1: diff is unified-diff text output; promote is a stub that points
the user at the right library/ file based on heuristics.
"""

from __future__ import annotations
import difflib
import json
from pathlib import Path

from .composer import (
    GH_APP_IDENTITY_FILENAME,
    GITCONFIG_FILENAME,
    compose_bot_gitconfig_app_identity,
    compose_bot_conf,
    compose_bot_gitconfig,
    compose_claude_md,
    compose_mcp_json,
    compose_tool_outputs,
    compose_settings_local,
    compose_systemd_unit,
    compose_launchd_plist,
    compose_access_json,
    bot_boot_delay_s,
    telegram_channel_rel,
)
from .config import FleetConfig
from .paths import Paths

#: The composed.json schema THIS build knows how to read (#1722). A
#: newer record is reported as unread rather than interpreted by field
#: name — 'unchanged' read off a misunderstood file is the false clear
#: the record exists to prevent.
MANIFEST_PROVENANCE_SCHEMA_EXPECTED = 1


def manifest_header(fleet: FleetConfig, paths: Paths) -> str:
    """One line saying whether the COMPOSE INPUTS moved (#1722).

    A diff that shows changes cannot, on its own, say whether the runtime
    drifted or the manifest did — today both read identically, and in the
    outage this comes from it was the manifest that moved, silently, under a
    running fleet. This line separates them before the diff body.
    """
    from .composer import (
        changed_manifest_inputs,
        manifest_change_attribution,
        read_manifest_provenance,
    )

    prov = read_manifest_provenance(paths)
    if prov is None:
        return ("manifest: NO PROVENANCE RECORDED — this runtime was composed by a "
                "claudlobby that did not stamp its inputs; run `generate` to "
                "record them. Nothing below distinguishes an input change from "
                "runtime drift.\n")
    if prov.get("schema") != MANIFEST_PROVENANCE_SCHEMA_EXPECTED:
        return (f"manifest: provenance schema {prov.get('schema')!r} is not the "
                f"{MANIFEST_PROVENANCE_SCHEMA_EXPECTED} this build reads — not "
                "interpreting it. Run `generate` to re-record.\n")
    changed = changed_manifest_inputs(fleet, paths, prov)
    if changed:
        how = manifest_change_attribution(fleet, paths)
        return ("manifest: CHANGED — " + ", ".join(changed) +
                " differ(s) from what this runtime was composed from"
                f" (composed {prov.get('composed_at')}). The inputs moved, not"
                " just the runtime." + (f" {how.capitalize()}." if how else "")
                + "\n")
    return f"manifest: unchanged since compose ({prov.get('composed_at')})\n"


def _artifact_json(path: Path, label: str, parts: list[str]) -> tuple[bool, dict | None]:
    """Read without repair: distinguish absent, invalid and unreadable artifacts."""
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return True, None
    except (ValueError, UnicodeError):
        parts.append(f"\n=== {label}: invalid JSON ===")
        return False, None
    except OSError as exc:
        parts.append(f"\n=== {label}: unreadable ({type(exc).__name__}) ===")
        return False, None
    if not isinstance(value, dict):
        parts.append(f"\n=== {label}: invalid JSON object ===")
        return False, None
    return True, value


def _json_drift(expected: dict, actual: dict, filename: str, bot_name: str,
                parts: list[str], *, note: str = "") -> None:
    expected_text = json.dumps(expected, indent=2, sort_keys=True)
    actual_text = json.dumps(actual, indent=2, sort_keys=True)
    if expected_text == actual_text:
        return
    parts.append(f"\n=== {filename} drift in {bot_name}{note} ===")
    parts.extend(difflib.unified_diff(
        expected_text.splitlines(), actual_text.splitlines(),
        fromfile="library-composed (would be regenerated)",
        tofile=f"{filename} in {bot_name} (current)", lineterm="",
    ))


def _access_owned_view(access: dict, chat_id: str, human_id: str) -> dict:
    """Exactly the fields reconciliation overwrites; runtime additions survive."""
    groups = access.get("groups", {})
    if not isinstance(groups, dict):
        raise ValueError("groups must be an object")
    group = groups.get(chat_id, {})
    if not isinstance(group, dict):
        raise ValueError("configured group must be an object")
    owned = {"dmPolicy": access.get("dmPolicy"),
             "groups": {chat_id: {"requireMention": group.get("requireMention")}}}
    if human_id:
        allowed = access.get("allowFrom", [])
        if not isinstance(allowed, list):
            raise ValueError("allowFrom must be a list")
        owned["declaredHumanAllowed"] = human_id in allowed
    return owned


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

    # .gitconfig — per-org git credential routing. Compositor-owned and pointed
    # at by GIT_CONFIG_GLOBAL, which means `git config --global` inside a bot
    # session writes HERE rather than to ~/.gitconfig. That edit is silently
    # lost on the next generate, so drift detection is the only thing that
    # surfaces it — omitting it would make the loss undetectable.
    expected_gitconfig = compose_bot_gitconfig(bot, paths) or ""
    actual_gitconfig_path = bot_dir / GITCONFIG_FILENAME
    actual_gitconfig = (
        actual_gitconfig_path.read_text() if actual_gitconfig_path.is_file() else ""
    )
    if expected_gitconfig != actual_gitconfig:
        parts.append(f"\n=== .gitconfig drift in {bot_name} ===")
        parts.extend(
            difflib.unified_diff(
                expected_gitconfig.splitlines(),
                actual_gitconfig.splitlines(),
                fromfile="library-composed (would be regenerated)",
                tofile=f"runtime/bots/{bot_name}/{GITCONFIG_FILENAME} (current)",
                lineterm="",
            )
        )

    # .gitconfig-github-app-id — the per-org App identity fragment (#1300),
    # compositor-owned like the .gitconfig it is included from.
    expected_appid = compose_bot_gitconfig_app_identity(bot) or ""
    actual_appid_path = bot_dir / GH_APP_IDENTITY_FILENAME
    actual_appid = (
        actual_appid_path.read_text() if actual_appid_path.is_file() else ""
    )
    if expected_appid != actual_appid:
        parts.append(f"\n=== {GH_APP_IDENTITY_FILENAME} drift in {bot_name} ===")
        parts.extend(
            difflib.unified_diff(
                expected_appid.splitlines(),
                actual_appid.splitlines(),
                fromfile="library-composed (would be regenerated)",
                tofile=f"runtime/bots/{bot_name}/{GH_APP_IDENTITY_FILENAME} (current)",
                lineterm="",
            )
        )

    # tools/ — composited scripts. The whole dir is compositor-owned, so a
    # hand-edited, deleted, or stray file is all drift.
    expected_tools = compose_tool_outputs(bot, fleet, paths, bot_dir)
    tools_dir = bot_dir / "tools"
    actual_tools = (
        {p.name: p.read_text() for p in sorted(tools_dir.iterdir()) if p.is_file()}
        if tools_dir.is_dir()
        else {}
    )
    for tool_name in sorted(set(expected_tools) | set(actual_tools)):
        expected_text = expected_tools.get(tool_name, "")
        actual_text = actual_tools.get(tool_name, "")
        if expected_text == actual_text:
            continue
        parts.append(f"\n=== tools/{tool_name} drift in {bot_name} ===")
        parts.extend(
            difflib.unified_diff(
                expected_text.splitlines(),
                actual_text.splitlines(),
                fromfile="library-composed (would be regenerated)",
                tofile=f"runtime/bots/{bot_name}/tools/{tool_name} (current)",
                lineterm="",
            )
        )

    # Settings are fully overwritten. Surface runtime grants before they vanish.
    label = f"settings.local.json in {bot_name}"
    ok, settings = _artifact_json(bot_dir / ".claude" / "settings.local.json", label, parts)
    if ok:
        _json_drift(compose_settings_local(bot, fleet, paths, list(expected_mcp["mcpServers"])),
                    settings or {}, "settings.local.json", bot_name, parts,
                    note=" (runtime additions are dropped on next generate)")

    # Use generate's host-wide ladder, including preceding sibling fleets.
    units = {
        f"{fleet.service_prefix}.{bot.bot_id}.service": compose_systemd_unit(
            bot, fleet, paths, boot_delay_s=bot_boot_delay_s(bot, fleet, paths)),
        f"{fleet.service_prefix}.{bot.bot_id}.plist": compose_launchd_plist(bot, fleet, paths),
    }
    for name, expected in units.items():
        try:
            actual = (bot_dir / name).read_text()
        except FileNotFoundError:
            actual = ""
        except (OSError, UnicodeError) as exc:
            parts.append(f"\n=== {name}: unreadable ({type(exc).__name__}) ===")
            continue
        if expected != actual:
            parts.append(f"\n=== {name} drift in {bot_name} ===")
            parts.extend(difflib.unified_diff(
                expected.splitlines(), actual.splitlines(),
                fromfile="library-composed (would be regenerated)",
                tofile=f"runtime/bots/{bot_name}/{name} (current)", lineterm=""))

    access_note = "access.json not applicable"
    expected_access = compose_access_json(bot, fleet)
    if expected_access is not None:
        access_path = Path.home() / telegram_channel_rel(bot.telegram.handle) / "access.json"
        ok, actual_access = _artifact_json(access_path, f"access.json in {bot_name}", parts)
        access_note = "access.json owned fields"
        if ok and actual_access is None:
            access_note = "access.json absent (not compared)"
        elif ok:
            chat_id = bot.telegram.chat_id or fleet.telegram_group_chat_id
            try:
                actual_owned = _access_owned_view(actual_access, chat_id, fleet.human_telegram_id)
            except ValueError as exc:
                parts.append(f"\n=== access.json in {bot_name}: invalid structure ({exc}) ===")
            else:
                _json_drift(_access_owned_view(expected_access, chat_id, fleet.human_telegram_id),
                            actual_owned, "access.json", bot_name, parts)
    coverage = ("coverage: CLAUDE.md, .mcp.json, bot.conf, git configuration, tools, "
                f"settings.local.json, bot service/plist, {access_note}; "
                "not compared: skill/command/agent symlinks. "
                "This preview does not enumerate every live effect of generate.\n")
    if not parts:
        return f"no drift in {bot_name}\n" + coverage
    return "\n".join(parts) + "\n" + coverage


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
