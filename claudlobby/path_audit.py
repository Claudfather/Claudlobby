"""Path-ownership audit — the compositor's guarantee that no emitted file carries
a flat, dangling, or otherwise-improper absolute fleet path.

The generate-time guard (composer) and the freshbox self-containment audit both
call in here, so the definition of "improper" can never drift between the two.
This extends the fresh-box self-containment contract to cover PATHS, the same
shape it already covers for permissions: the compositor *derives* correct,
self-contained wiring rather than trusting hand-written absolute inputs.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import BotConfig, FleetConfig
    from .paths import Paths

# Path anchors the composer exports into bot.conf. A ${VAR} in an MCP fragment,
# or a $VAR in bot.conf, that names one of these resolves — at runtime for
# .mcp.json, at source time for bot.conf — to a composer-derived, migration-safe
# absolute path. They are the blessed way to express an in-fleet absolute path in
# a compose source, so that a raw absolute fleet path stands out as the
# dangling-path smell the guard rejects.
#   CLAUDLOBBY_ROOT — the install root (paths.root)
#   FLEET_ROOT      — the fleet overlay root (paths.fleet_config_dir)
#   BOT_DIR         — this bot's runtime dir (paths.bot_runtime(bot_id))
COMPOSER_PROVIDED_PATH_ANCHORS: tuple[str, ...] = (
    "CLAUDLOBBY_ROOT",
    "FLEET_ROOT",
    "BOT_DIR",
)


@dataclass(frozen=True)
class PathFinding:
    """One improper absolute fleet path in an emitted wiring file."""

    bot_id: str
    file: str  # bot-dir-relative filename
    path: str  # the offending absolute path (anchors already resolved)
    reason: str


# A crude absolute-path token: a run starting with "/" up to whitespace or a
# common delimiter. `<` and `>` delimit too — they cannot occur in a real path,
# so they mark the boundary between an XML tag and a path in a launchd plist
# (``</key><string>/real/path</string>``), keeping the closing tag out of the
# extracted token. Good enough for the machine-generated wiring files scanned
# here (bot.conf, .mcp.json, unit files).
_ABS_TOKEN_RE = re.compile(r"/[^\s'\":;,<>]+")

# Bot-dir-relative wiring files whose absolute paths must resolve for the bot to
# run. Prose (CLAUDE.md) is intentionally excluded — a stale path there does not
# break wiring and the text legitimately carries example paths.
_WIRING_STATIC = ("bot.conf", ".mcp.json", ".claude/settings.local.json")


def _anchor_values(bot: BotConfig, paths: Paths) -> dict[str, str]:
    """Map each composer-provided path anchor to its resolved absolute value."""
    return {
        "CLAUDLOBBY_ROOT": str(paths.root),
        "FLEET_ROOT": str(paths.fleet_config_dir),
        "BOT_DIR": str(paths.bot_runtime(bot.bot_id)),
    }


def _resolve_anchor_tokens(text: str, anchor_values: dict[str, str]) -> str:
    """Expand ``${ANCHOR}`` / ``$ANCHOR`` to the anchor's absolute value, so a path
    written against a blessed anchor is checked at its real resolved location.
    Order-independent: ``${NAME}`` is brace-closed and ``$NAME`` is boundary-
    delimited, so no anchor can partially match inside another."""
    for name, val in anchor_values.items():
        text = text.replace("${" + name + "}", val)
        text = re.sub(r"\$" + re.escape(name) + r"(?![A-Za-z0-9_])", val, text)
    return text


def _fleet_content_roots(paths: Paths) -> list[str]:
    """Trees under which fleet-owned content lives — the ``local/`` overlay and,
    in vault mode, the vault. An absolute path under one of these must resolve
    inside the fleet's own overlay root."""
    roots = [str(paths.root / "local")]
    if paths.vault_root is not None:
        roots.append(str(paths.vault_root))
    return roots


def _fleet_layout_needles(paths: Paths) -> list[str]:
    """Slash-bounded fragments that mark a path as this fleet's own content by
    *shape* — what a fleet-owned path keeps even when it is written against a
    foreign install root, which a prefix test against this install's roots
    (``_fleet_content_roots``) cannot recognize.

    A path is fleet-owned by shape when it contains, at segment boundaries:

    * this fleet's overlay dir at its real depth (``…/local/home/tl/…``),
      derived from the fleet config dir so it tracks the layout rather than
      restating it
    * a flat ``local/<fleet>`` overlay — a flat-layout fleet, or a leftover husk
      of one that lives deeper (kept as its own fragment so a flat path is
      caught even when this fleet is nested)
    * a bot runtime tree (``…/runtime/bots/…``)

    Each fragment is gated on this fleet's own directory name or the runtime
    marker, so a bare ``local`` segment (``/usr/local/bin``) or a package token
    never matches; the leading/trailing ``/`` enforce the segment boundary.
    """
    fleet_cfg = paths.fleet_config_dir
    needles = {f"/local/{fleet_cfg.name}/", "/runtime/bots/"}
    if fleet_cfg.is_relative_to(paths.root):
        rel = fleet_cfg.relative_to(paths.root).parts
        if rel:
            needles.add("/" + "/".join(rel) + "/")
    return sorted(needles)


def _traverses_fleet_layout(path: str, needles: list[str]) -> bool:
    """True if *path* contains any fleet-layout fragment at a segment boundary."""
    hay = path + "/"
    return any(n in hay for n in needles)


def improper_fleet_paths(
    text: str, bot: BotConfig, paths: Paths
) -> list[tuple[str, str]]:
    """Return ``[(path, reason)]`` for improper absolute fleet paths in *text*.

    After resolving composer path anchors, an absolute path is improper when it
    is fleet-owned — under a fleet-content root, or shaped like this fleet's own
    overlay / bot-runtime layout at any root — yet does NOT resolve inside
    ``paths.fleet_config_dir``. That covers a flat or dangling husk, a cross-fleet
    leak, and a stale absolute hand-typed against a foreign install root that
    dangles the moment the fleet runs elsewhere. A nested-correct absolute path
    (what the composer itself emits, e.g. FLEET_MISSION_FILE) is fine; the rule
    is correctness, not "no absolutes".
    """
    resolved = _resolve_anchor_tokens(text, _anchor_values(bot, paths))
    content_roots = _fleet_content_roots(paths)
    layout_needles = _fleet_layout_needles(paths)
    fleet_root = str(paths.fleet_config_dir)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _ABS_TOKEN_RE.finditer(resolved):
        p = m.group(0).rstrip("/.,:;\"')}")
        if p in seen:
            continue
        under_content_root = any(
            p == r or p.startswith(r + os.sep) for r in content_roots
        )
        if not (under_content_root or _traverses_fleet_layout(p, layout_needles)):
            continue  # not fleet-owned (system path, $HOME, /tmp, package token, …)
        norm = os.path.normpath(p)
        if norm == fleet_root or norm.startswith(fleet_root + os.sep):
            continue  # resolves inside the fleet's real overlay root — correct
        seen.add(p)
        out.append(
            (
                p,
                f"absolute fleet path that does not resolve inside the fleet "
                f"overlay root {fleet_root} — a flat, dangling, or foreign-rooted "
                "layout, or a cross-fleet leak",
            )
        )
    return out


def _wiring_files(bot: BotConfig, fleet: FleetConfig) -> list[str]:
    return [
        *_WIRING_STATIC,
        f"{fleet.service_prefix}.{bot.bot_id}.service",
        f"{fleet.service_prefix}.{bot.bot_id}.plist",
    ]


def audit_bot_paths(
    bot: BotConfig, fleet: FleetConfig, paths: Paths
) -> list[PathFinding]:
    """Scan a bot's emitted wiring files for improper absolute fleet paths."""
    bot_dir = paths.bot_runtime(bot.bot_id)
    findings: list[PathFinding] = []
    for rel in _wiring_files(bot, fleet):
        try:
            text = (bot_dir / rel).read_text()
        except (OSError, UnicodeDecodeError):
            continue  # file absent (e.g. no .mcp.json) or binary — nothing to scan
        for path, reason in improper_fleet_paths(text, bot, paths):
            findings.append(PathFinding(bot.bot_id, rel, path, reason))
    return findings


def assert_bot_paths(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> None:
    """Fail loudly if any emitted wiring file carries an improper fleet path.

    The generate-time half of the path-ownership guarantee: a hand-typed flat or
    dangling absolute fleet path in any compose source surfaces here as a hard
    error, never a silent dangle.
    """
    findings = audit_bot_paths(bot, fleet, paths)
    if not findings:
        return
    detail = "\n".join(f"  {f.file}: {f.path}\n      {f.reason}" for f in findings)
    raise ValueError(
        f"bot {bot.bot_id!r}: improper absolute fleet path(s) in composed wiring — "
        "derive the path from a composer anchor (FLEET_ROOT / BOT_DIR / "
        "CLAUDLOBBY_ROOT), never hand-type it:\n" + detail
    )
