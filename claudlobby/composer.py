"""Template-driven composition.

Reads `templates/claude.md.j2`, loads library files via the frontmatter-aware
loader, renders the bot's CLAUDE.md. Library files are pure content — the
template owns all top-level structure.
"""

from __future__ import annotations
import copy
import functools
import json
import logging
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

import jinja2
from jinja2.sandbox import SandboxedEnvironment

from . import dotenv, tool_resolve
from .config import BotConfig, FleetConfig, load_host_jobs
from .known_values import HEADLESS_TRIM_VARS, SHELL_IDENT_RE
from .loader import (
    ExpertisePermissions,
    LibraryItem,
    _demote_headings,
    iter_guardrail_permissions,
    iter_skill_grants,
    load_library_items_overlay,
    load_voice,
    parse_expertise_file,
)
from .mcp_resolve import iter_operator_contract_vars, resolve_placeholders
from .paths import Paths


# ----------------------------------------------------------------------
# Templating
# ----------------------------------------------------------------------


def _expand(text: str, ctx: dict[str, str]) -> str:
    """Replace {{KEY}} placeholders. Used for non-Jinja substitution within
    library bodies (e.g., `{{BOT_NAME}}` inside a protocol). Missing keys
    are left as-is."""
    out = text
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def _bot_template_context(
    bot: BotConfig, fleet: FleetConfig, paths: Paths
) -> dict[str, str]:
    bot_dir = paths.bot_runtime(bot.bot_id)
    return {
        "BOT_ID": bot.bot_id,
        "BOT_ID_UPPER": bot.bot_id.upper(),
        "BOT_NAME": bot.name,
        "BOT_NAME_UPPER": bot.name.upper(),
        "FLEET_NAME": fleet.name,
        "SERVICE_PREFIX": fleet.service_prefix,
        "CLAUDLOBBY_ROOT": str(paths.root),
        "BOT_DIR": str(bot_dir),
        "TELEGRAM_GROUP_CHAT_ID": (
            bot.telegram.chat_id or fleet.telegram_group_chat_id or ""
        ),
        "SHARED_DOCS_PATH": str(paths.shared_docs) if paths.shared_docs else "",
    }


def _expand_item(item: LibraryItem, ctx: dict[str, str]) -> LibraryItem:
    return LibraryItem(
        title=_expand(item.title, ctx),
        description=_expand(item.description, ctx) if item.description else None,
        body=_expand(item.body, ctx),
        source_path=item.source_path,
    )


# ----------------------------------------------------------------------
# Expertise composition (special — provides H1 label + base body)
# ----------------------------------------------------------------------


def _compose_expertise(
    bot: BotConfig, paths: Paths, ctx: dict[str, str]
) -> tuple[str | None, str]:
    """Return (title_label, expertise_body).

    First expertise file's H1 (if present) provides the title_label. Subsequent
    expertise files' H1s are stripped; their bodies are concatenated.

    Overlay-aware: looks in `local/<fleet>/library/expertise/` first, then
    in the public `library/expertise/`.
    """
    if not bot.expertise:
        raise ValueError(f"bot '{bot.bot_id}': expertise list is empty")

    first_label: str | None = None
    body_chunks: list[str] = []
    for i, area in enumerate(bot.expertise):
        path = paths.find_library_file("expertise", area, ".md")
        if path is None:
            continue
        item = parse_expertise_file(path)
        if item is None:
            continue
        if i == 0:
            first_label = item.title_label
        body_chunks.append(item.body)
    expertise_body = "\n\n".join(b for b in body_chunks if b).rstrip()
    return first_label, _expand(expertise_body, ctx)


# ----------------------------------------------------------------------
# Jinja2 environment
# ----------------------------------------------------------------------


def _build_jinja_env(paths: Paths) -> jinja2.Environment:
    """Jinja env with overlay-aware template loader.

    A fleet can override the master template by placing
    `local/<fleet>/templates/claude.md.j2`.
    """
    search = []
    if paths.fleet_dir and (paths.fleet_dir / "templates").is_dir():
        search.append(str(paths.fleet_dir / "templates"))
    search.append(str(paths.root / "templates"))
    env = SandboxedEnvironment(
        loader=jinja2.FileSystemLoader(search),
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    env.filters["quote_backtick"] = lambda s: f"`{s}`"
    return env


# ----------------------------------------------------------------------
# .mcp.json merge
# ----------------------------------------------------------------------


def compose_mcp_json(bot: BotConfig, paths: Paths) -> dict:
    import shutil

    merged: dict = {"mcpServers": {}}
    for entry in bot.mcp:
        frag_path = paths.find_library_file("mcp", entry.name, ".json")
        if frag_path is None:
            continue
        try:
            frag = json.loads(frag_path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            raise ValueError(f"invalid JSON in MCP fragment {frag_path}: {e}") from e
        contract = frag.pop("_env_contract", {})
        global_binary = frag.pop("_global_binary", None)

        # Find the server config (the non-underscore key)
        server_key = None
        server_config = None
        for k, v in frag.items():
            if not k.startswith("_") and isinstance(v, dict):
                server_key = k
                server_config = v
                break
        if server_key is None:
            continue

        # Generate one server entry per instance
        for instance in entry.instances:
            output_name = entry.output_name(instance)

            instance_config = copy.deepcopy(server_config)

            # Use global binary if available (saves ~0.8s npx overhead per server)
            resolved_binary = shutil.which(global_binary) if global_binary else None
            if resolved_binary and instance_config.get("command") == "npx":
                instance_config["command"] = "node"
                # Replace npx args ([-y, pkg, ...rest]) with [binary, ...rest]
                npx_args = instance_config.get("args", [])
                rest_args = []
                skip_next = False
                for a in npx_args:
                    if a == "-y":
                        skip_next = True
                        continue
                    if skip_next:
                        skip_next = False
                        continue
                    rest_args.append(a)
                instance_config["args"] = [resolved_binary] + rest_args

            # Resolve ${VAR} placeholders (instance-scoped vars get prefixed)
            for field in ["env", "url", "args", "headers"]:
                if field in instance_config:
                    instance_config[field] = resolve_placeholders(
                        instance_config[field], contract, entry, instance
                    )

            merged["mcpServers"][output_name] = instance_config

    return merged


def _load_mcp_contract(paths: Paths, name: str) -> dict | None:
    """Load an MCP fragment's validated ``_permissions_contract``, or ``None`` when absent.

    Missing fragment and malformed JSON (logged) both resolve to ``None`` so
    callers treat the server as contract-less rather than failing compose.

    A declared ``read_only_tools`` subset must sit inside the declared
    ``tools`` universe: an entry outside it is a typo or an upstream tool
    rename, and would silently grant a tool that does not exist — so every
    reader gets the invariant checked here, at load, and compose fails loudly.
    """
    frag_path = paths.find_library_file("mcp", name, ".json")
    if frag_path is None:
        return None
    try:
        frag = json.loads(frag_path.read_text())
    except json.JSONDecodeError as exc:
        _log.warning(
            "skipping MCP permissions for %s: malformed JSON: %s", frag_path, exc
        )
        return None
    contract = frag.get("_permissions_contract")
    if contract:
        unknown = sorted(
            set(contract.get("read_only_tools") or []) - set(contract.get("tools", []))
        )
        if unknown:
            raise ValueError(
                f"mcp fragment {name!r}: read_only_tools entries {unknown} are not in "
                "the contract's tools list — declare the full tool universe in tools "
                "and keep read_only_tools a subset of it."
            )
    return contract


def _resolve_mcp_permissions(bot: BotConfig, paths: Paths) -> list[str]:
    """Resolve MCP permission patterns from fragment _permissions_contract fields.

    For each MCP entry the bot uses, reads the fragment contract and generates
    permission patterns per instance:

    - ``read_only_tools`` declared — one exact ``mcp__<server>__<tool>`` entry
      per read-only tool. The server-level wildcard is never emitted for these
      servers: it would auto-approve the contract's remaining (write) tools,
      which must keep prompting (#661).
    - otherwise, non-empty ``tools`` — a single ``mcp__<server>__*`` wildcard.
      This keeps settings.local.json compact and prevents staleness when the
      server adds new tools.
    """
    patterns: list[str] = []
    for entry in bot.mcp:
        contract = _load_mcp_contract(paths, entry.name)
        if not contract:
            continue
        read_only = contract.get("read_only_tools")
        for instance in entry.instances:
            output_name = entry.output_name(instance)
            if read_only is not None:
                patterns.extend(f"mcp__{output_name}__{tool}" for tool in read_only)
            elif contract.get("tools"):
                patterns.append(f"mcp__{output_name}__*")
    return patterns


def _assert_read_only_grants(name: str, tool_grants: list[str], paths: Paths) -> None:
    """Fail compose unless an integration's grants mirror its read-only set exactly.

    A fragment contract that declares ``read_only_tools`` splits its tool
    universe into reads (auto-grantable) and writes (must keep prompting).
    The paired integration's ``tool_grants`` is the composed grant surface, so
    it must be the exact ``mcp__<name>__<tool>`` mirror of the read set:

    - an entry beyond it (server wildcard, write tool) would silently
      auto-approve mutations for every bot that equips the integration;
    - a missing read entry would leave that safe read prompting forever —
      the wedged-headless-worker symptom this contract exists to end (#661).

    The equality check is what keeps the mirror maintained once the legacy
    ``_resolve_mcp_permissions`` grant role (and its superset gate) is cut:
    add a tool upstream and the directional error here names the fix. A bot
    that genuinely needs a write tool gets it explicitly via fleet.yaml
    ``tools.allow``; that operator path is untouched here.
    """
    contract = _load_mcp_contract(paths, name)
    if not contract:
        return
    read_only = contract.get("read_only_tools")
    if read_only is None:
        return
    mirror = {f"mcp__{name}__{tool}" for tool in read_only}
    extra = sorted(set(tool_grants) - mirror)
    if extra:
        raise ValueError(
            f"integration {name!r}: tool_grants {extra} not auto-grantable — the mcp "
            f"fragment declares read_only_tools, so grants must be exact "
            f"mcp__{name}__<tool> entries from that read-only set. Writes stay "
            "prompt-gated; grant one per-bot via tools.allow if genuinely needed."
        )
    missing = sorted(mirror - set(tool_grants))
    if missing:
        raise ValueError(
            f"integration {name!r}: tool_grants is missing read entries {missing} — "
            f"add them so the fragment's read_only_tools compose (a missing read "
            "prompts forever on headless bots)."
        )


def _assert_no_write_autoallows(
    bot: BotConfig, paths: Paths, allow_patterns: list[str]
) -> None:
    """Fail compose when ANY library-derived allow covers a write of a read-only server.

    :func:`_assert_read_only_grants` polices the integration grant surface, but
    expertise ``permissions.allow``, guardrail allows, and skill ``tool_grants``
    merge into the same allow list — a skill declaring ``mcp__shopify__*``
    would otherwise compose every catalog write into each equipping bot. This
    is the union-layer invariant: for every attached server whose contract
    declares ``read_only_tools``, no accumulated pattern may reach beyond the
    read set. Runs before fleet.yaml ``tools.allow`` is appended — that layer
    is the operator's deliberate, auditable escape hatch and stays exempt.
    """
    for entry in bot.mcp:
        contract = _load_mcp_contract(paths, entry.name)
        if not contract:
            continue
        read_only = contract.get("read_only_tools")
        if read_only is None:
            continue
        for instance in entry.instances:
            server = entry.output_name(instance)
            prefix = f"mcp__{server}__"
            permitted = {f"{prefix}{tool}" for tool in read_only}
            bad = sorted(
                p
                for p in allow_patterns
                if (p == f"mcp__{server}" or p.startswith(prefix))
                and p not in permitted
            )
            if bad:
                raise ValueError(
                    f"bot {bot.bot_id!r}: allow patterns {bad} cover non-read tools "
                    f"of read-only server {server!r} — no library-derived layer "
                    "(integration, skill, expertise, guardrail) may auto-allow a "
                    "write. Grant it per-bot via fleet.yaml tools.allow if "
                    "genuinely needed."
                )


def _resolve_integration_grants(bot: BotConfig, paths: Paths) -> list[str]:
    """Resolve MCP tool-permission grants from equipped integrations' ``tool_grants``.

    Reads ``tool_grants`` frontmatter from each of the bot's effective
    integrations (see :func:`resolve_effective_integrations`) and expands it
    into settings.local.json allow entries. One rule produces three shapes,
    keyed on whether a ``bot.mcp`` entry shares the integration's name (a
    fragment-backed integration is named after its mcp server):

    - **fragment-backed** with a matching ``bot.mcp`` entry — the
      ``mcp__<name>__`` prefix is rewritten per instance (``default`` →
      ``mcp__<name>__``; named → ``mcp__<name>-<instance>__``), reproducing
      :func:`_resolve_mcp_permissions` output byte-for-byte;
    - **fragment-backed equipped without a matching mcp entry** — the grant is
      emitted literally (the default ``mcp__<name>__*``); the validator warns
      that the paired server is not configured;
    - **connector-backed** (no matching mcp entry, e.g. ``mcp__claude_ai_*``) —
      emitted literally;
    - **CLI-backed** (no ``tool_grants``) — nothing.
    """
    from .loader import iter_integration_grants

    grants: list[str] = []
    # Folder-aware (dir/ expansion) so generate resolves the same grant set the
    # validator shape-checks — the reader is shared with validator.py.
    for name, tool_grants in iter_integration_grants(
        paths, resolve_effective_integrations(bot, paths)
    ):
        if not tool_grants:
            continue
        _assert_read_only_grants(name, tool_grants, paths)
        entry = next((e for e in bot.mcp if e.name == name), None)
        if entry is None:
            grants.extend(tool_grants)
            continue
        prefix = f"mcp__{name}__"
        for grant in tool_grants:
            if grant.startswith(prefix):
                rest = grant[len(prefix) :]
                for instance in entry.instances:
                    grants.append(f"mcp__{entry.output_name(instance)}__{rest}")
            else:
                grants.append(grant)
    return grants


# ----------------------------------------------------------------------
# bot.conf
# ----------------------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_SHELL_IDENT_RE = SHELL_IDENT_RE  # canonical in known_values (shared with config)
# Telegram handle: must start alnum/underscore, then alnum/underscore/dash.
# One canonical rule used by both compose_bot_conf and compose_bot.
_TELEGRAM_HANDLE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*\Z")

# Pinned fleet-wide tmux tmpdir. Per-bot sockets are reached via
# `tmux -L <socket>`, which resolves to "$TMUX_TMPDIR/tmux-$(id -u)/<socket>".
# Pinning one explicit value (tmux's own built-in default is /tmp) guarantees
# every script and unit resolves the same `-L <name>` to the same server —
# drift here would silently spawn a second server for the same socket name.
_TMUX_TMPDIR = "/tmp"


def _shq(v: object) -> str:
    """Shell-quote a value for safe embedding in sourced bash scripts.

    Uses shlex.quote (single-quoting) so $, `, \\, and " are literal.
    """
    return shlex.quote(str(v))


def _root_anchored(path: Path, paths: Paths) -> str:
    """Shell RHS for a fleet path: anchored on ``$CLAUDLOBBY_ROOT`` when it lives
    under the install root, else an absolute ``_shq`` value (vault mode). The one
    place the "anchor a fleet path for bot.conf" rule lives — BOT_DIR and
    FLEET_ROOT both route through it, so the two anchors path_audit trusts to
    resolve to the composer's real locations cannot silently drift apart."""
    try:
        rel = path.relative_to(paths.root)
    except ValueError:
        return _shq(str(path))
    # rel == "." only for a root-mode fleet (fleet_config_dir == root); BOT_DIR,
    # always runtime/bots/<id>, never hits it.
    return '"$CLAUDLOBBY_ROOT"' if rel == Path(".") else f'"$CLAUDLOBBY_ROOT/{rel}"'


def _channel_plugins(channels: list[str]) -> list[str]:
    """Plugin install-IDs a bot's ``--channels`` flag depends on.

    A channel entry ``plugin:<name>@<marketplace>`` means the session launches
    with that plugin, so it must be installed for the channel to work on a cold
    box. Returns the ``<name>@<marketplace>`` install-IDs in order, skipping any
    channel that is not a marketplace-pinned plugin ref. The marketplace itself
    must still be declared in ``plugins.marketplaces`` (its source repo cannot
    be inferred from the ref) for start-bot to register it.
    """
    plugins: list[str] = []
    for chan in channels:
        if chan.startswith("plugin:"):
            ref = chan.removeprefix("plugin:")
            if "@" in ref and ref not in plugins:
                plugins.append(ref)
    return plugins


def compose_bot_conf(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> str:
    # Defense-in-depth: validate identifiers embedded in double-quoted lines
    # that require shell variable expansion ($HOME, $CLAUDLOBBY_ROOT).
    if not _SAFE_NAME_RE.match(bot.bot_id):
        raise ValueError(f"bot_id {bot.bot_id!r} contains shell-unsafe characters")
    if not _SAFE_NAME_RE.match(bot.name):
        raise ValueError(f"bot name {bot.name!r} contains shell-unsafe characters")
    tg_handle = (bot.telegram.handle if bot.telegram else None) or bot.bot_id
    if not _TELEGRAM_HANDLE_RE.match(tg_handle):
        raise ValueError(
            f"telegram handle {tg_handle!r} contains shell-unsafe characters"
        )

    ctx = _bot_template_context(bot, fleet, paths)
    bot_dir = paths.bot_runtime(bot.bot_id)
    account_dir = fleet.accounts.get(
        bot.account, fleet.accounts.get("default", "~/.claude")
    )

    bot_dir_line = f"BOT_DIR={_root_anchored(bot_dir, paths)}"
    # BOT_SERVICE is the bot's host-wide-unique, fleet-prefixed identity. It
    # names the systemd/launchd unit AND the per-bot tmux server socket — one
    # value, two of the three identity axes (the third is the dir-slug session).
    bot_service = f"{fleet.service_prefix}.{bot.bot_id}"
    lines = [
        "# Generated by claudlobby — do not hand-edit. Edit fleet.yaml + library/, then re-run `claudlobby generate`.",
        f"# Bot: {bot.bot_id}",
        "",
        f"export CLAUDLOBBY_ROOT={_shq(str(paths.root))}",
        "",
        f"export BOT_ID={_shq(bot.bot_id)}",
        f"BOT_NAME={_shq(bot.name)}",
        f"BOT_SERVICE={_shq(bot_service)}",
        # Per-bot tmux server socket (the `-L` name) — equals BOT_SERVICE so one
        # server's death drops only this bot. Resolved for peer bots via
        # tmux_socket_for_bot() (lib/lib-common.sh).
        f"TMUX_SOCKET={_shq(bot_service)}",
        f"BOT_LABEL={_shq(bot.bot_id.upper())}",
        bot_dir_line,
        f'TELEGRAM_STATE_DIR="$HOME/.claude/channels/telegram-{bot.telegram.handle or bot.bot_id}"',
        "",
        "# Claude Code config dir (multi-account support)",
    ]
    if bot.account != "default":
        lines.append(f"CLAUDE_CONFIG_DIR={_shq(account_dir)}")
    else:
        lines.append(f"# CLAUDE_CONFIG_DIR={_shq(account_dir)}  # default account")
    lines.append("")

    # Assemble the full claude CLI flag set. lib/start-bot.sh reads
    # CLAUDE_FLAGS verbatim and appends only --name <session>.
    flags: list[str] = []
    for ch in bot.channels:
        flags.append(f"--channels {ch}")
    if bot.remote_control:
        flags.append("--remote-control")
    # Permission model, in precedence order:
    #   1. explicit permission_mode always wins;
    #   2. else an explicit dangerously_skip_permissions opt-in bypasses prompts;
    #   3. else the conservative default — acceptEdits auto-accepts edits while
    #      still honoring the composed allow/deny lists (headless-safe, and unlike
    #      dangerously-skip it does not bypass the cross-bot read isolation).
    if bot.permission_mode:
        flags.append(f"--permission-mode {bot.permission_mode}")
    elif bot.dangerously_skip_permissions:
        flags.append("--dangerously-skip-permissions")
    else:
        flags.append("--permission-mode acceptEdits")
    if bot.model:
        flags.append(f"--model {bot.model}")
    if bot.effort:
        flags.append(f"--effort {bot.effort}")
    flags.extend(bot.extra_flags)
    lines.append(f"CLAUDE_FLAGS={_shq(' '.join(flags))}")
    lines.append("")

    # Prompt suggestions — off by default for headless bots.
    lines.append(
        f"export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION={_shq(str(bot.prompt_suggestions).lower())}"
    )
    # Headless traffic trim — kills the satisfaction survey, /bug, Sentry
    # error reporting, and the auto-updater (claudlobby self-manages updates).
    # Granular by design: see HEADLESS_TRIM_VARS / RC_KILLING_ENV_VARS for why
    # the umbrella vars stay off (they break remote-control, #533). Presence
    # flags — an override to false omits them, never emits "=0".
    if bot.disable_nonessential_traffic:
        for var in HEADLESS_TRIM_VARS:
            lines.append(f"export {var}=1")
    lines.append("")
    lines.append("# Exports for skills + scripts")
    lines.append(f"export FLEET_NAME={_shq(fleet.name)}")
    lines.append(f"export SERVICE_PREFIX={_shq(fleet.service_prefix)}")
    # Pin the tmux tmpdir so `tmux -L <socket>` resolves to the same server in
    # every bot process (start parent, session, watchdog, cross-socket peers) —
    # drift would silently spawn a duplicate server for the same socket name.
    lines.append(f"export TMUX_TMPDIR={_shq(_TMUX_TMPDIR)}")
    lines.append('export FLEET_STATE_PATH="$CLAUDLOBBY_ROOT/state/fleet-state.json"')
    # FLEET_ROOT — the fleet-scoped sibling of CLAUDLOBBY_ROOT. It is the fleet
    # overlay root (the dir holding fleet.yaml, nested-aware), so every
    # fleet-relative path (secret-file paths, per-bot MCP build dirs) anchors on
    # it and moves with the fleet instead of dangling after a re-nest.
    lines.append(f"export FLEET_ROOT={_root_anchored(paths.fleet_config_dir, paths)}")
    if fleet.mission_file and fleet.mission:
        # Gated on the PAIR, mirroring the CLAUDE.md section: in the
        # pairing-forbidden state (file without paragraph) no composed prose
        # references the var, so emitting it would dangle. Resolved at
        # compose time (the config field stays fleet-relative): consumers
        # just read the path — no bot has to re-derive the fleet layout,
        # which breaks in vault mode. Same anchored-path pattern as
        # FLEET_STATE_PATH above.
        charter_path = paths.fleet_config_dir / fleet.mission_file
        lines.append(f"export FLEET_MISSION_FILE={_shq(str(charter_path))}")
    chat_id = ctx["TELEGRAM_GROUP_CHAT_ID"]
    if chat_id:
        lines.append(f"export TELEGRAM_GROUP_CHAT_ID={_shq(str(chat_id))}")
    if bot.telegram.token_env:
        lines.append(f"export TELEGRAM_TOKEN_ENV_NAME={_shq(bot.telegram.token_env)}")
    if bot.telegram.require_mention is not None:
        lines.append(
            f"export TELEGRAM_REQUIRE_MENTION={_shq(str(bot.telegram.require_mention).lower())}"
        )
    if bot.telegram.handle:
        lines.append(f"export TELEGRAM_BOT_HANDLE={_shq(bot.telegram.handle)}")

    # Model strategy — config-driven model escalation / compaction / subagent models.
    if bot.model_strategy:
        ms = bot.model_strategy
        lines.append("")
        lines.append("# Model strategy")
        if ms.base:
            lines.append(f"export MODEL_STRATEGY_BASE={_shq(ms.base)}")
        if ms.escalate_to:
            lines.append(f"export MODEL_STRATEGY_ESCALATE_TO={_shq(ms.escalate_to)}")
        if ms.escalate_when:
            lines.append(
                f"export MODEL_STRATEGY_ESCALATE_WHEN={_shq(ms.escalate_when)}"
            )
        if ms.compact_when:
            lines.append(f"export MODEL_STRATEGY_COMPACT_WHEN={_shq(ms.compact_when)}")
        # Subagent model preferences (from raw extras)
        for key in ("explore", "plan", "general"):
            val = ms.raw.get(key)
            if val:
                lines.append(f"export MODEL_STRATEGY_{key.upper()}={_shq(val)}")

    # Observability — pulse interval and event retention.
    # Values may be None when system defaults are disabled via system_defaults: false.
    obs = bot.observability
    if any(
        v is not None
        for v in [
            obs.pulse_interval,
            obs.reap_days,
            obs.activity_stuck_threshold,
            obs.dispatch_deadline,
            obs.bridge_heal,
            obs.bridge_heal_max_attempts,
        ]
    ):
        lines.append("")
        lines.append("# Observability")
        if obs.pulse_interval is not None:
            lines.append(
                f"export OBSERVABILITY_PULSE_INTERVAL={_shq(obs.pulse_interval)}"
            )
        if obs.reap_days is not None:
            lines.append(f"export OBSERVABILITY_REAP_DAYS={_shq(obs.reap_days)}")
        if obs.activity_stuck_threshold is not None:
            lines.append(
                f"export OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD={_shq(obs.activity_stuck_threshold)}"
            )
        if obs.dispatch_deadline is not None:
            lines.append(
                f"export OBSERVABILITY_DISPATCH_DEADLINE={_shq(obs.dispatch_deadline)}"
            )
        if obs.bridge_heal is not None:
            # keepalive.sh gates on the string "1"; emit a shell boolean (1/0),
            # NOT _shq(bool) which renders "True"/"False" and would leave the
            # gate closed.
            lines.append(
                f"export OBSERVABILITY_BRIDGE_HEAL={'1' if obs.bridge_heal else '0'}"
            )
        if obs.bridge_heal_max_attempts is not None:
            lines.append(
                f"export BRIDGE_HEAL_MAX_ATTEMPTS={_shq(obs.bridge_heal_max_attempts)}"
            )

    # Project validation tiers (projects.yaml) — the repo -> closure-bar map,
    # emitted into EVERY bot's conf: any sprint/runner bot must resolve a
    # working repo's tier locally (there is no "sprint owner" concept).
    if fleet.projects:
        lines.append("")
        lines.append("# Projects (projects.yaml) — repo -> validation tier")
        for key, project in sorted(fleet.projects.items()):
            tier_var = f"PROJECT_TIER_{project.env_slug}"
            if not _SHELL_IDENT_RE.match(tier_var):
                raise ValueError(
                    f"project key '{key}' does not yield a valid env name "
                    f"(run claudlobby validate)"
                )
            for repo in project.repos:
                # Emit-time corruption backstop (mirrors the slug raise
                # above): the value is a space-delimited list by contract,
                # so an entry containing whitespace cannot be represented.
                if any(c.isspace() for c in repo):
                    raise ValueError(
                        f"project '{key}': repos entry '{repo}' contains "
                        f"whitespace (run claudlobby validate)"
                    )
            lines.append(f"export {tier_var}={_shq(project.validation.tier)}")
            lines.append(
                f"export PROJECT_REPOS_{project.env_slug}="
                f"{_shq(' '.join(project.repos))}"
            )

    # Workstream registry bounds (fleet.workstreams). Read from the env by the
    # single-writer helper (lib/workstream-update.sh) at open/renew time.
    # Emitted into EVERY bot.conf rather than manager-gated so the fleet-wide
    # cap/lease applies regardless of team topology — a teamless fleet would
    # silently lose its config under a manager-only gate. Defaults (12/14) apply
    # when the fleet omits the block.
    lines.append("")
    lines.append("# Workstream registry (fleet.workstreams)")
    lines.append(f"export WORKSTREAM_MAX_ACTIVE={_shq(fleet.workstreams.max_active)}")
    lines.append(f"export WORKSTREAM_LEASE_DAYS={_shq(fleet.workstreams.lease_days)}")

    # Rolling code-audit sweep — emitted only into the owner bot's conf, so the
    # fleet-level selector (lib/code-audit-sweep.sh) resolves exactly one owner.
    # repos default to the owner's scope.repos when sweep.repos is unset.
    if fleet.sweep_enabled() and fleet.sweep.owner_bot == bot.bot_id:
        sweep_repos = fleet.sweep.repos or (bot.scope.repos if bot.scope else [])
        lines.append("")
        lines.append("# Code-audit sweep (owner)")
        lines.append(f"export SWEEP_OWNER_BOT={_shq(bot.bot_id)}")
        lines.append(f"export SWEEP_REPOS={_shq(' '.join(sweep_repos))}")
        lines.append(f"export SWEEP_LABEL={_shq(fleet.sweep.label)}")
        lines.append(
            f"export SWEEP_AUDIT_TYPES={_shq(' '.join(fleet.sweep.audit_types))}"
        )

    # Equippable briefing (bots.<bot>.briefing) — emitted into the equipped bot's
    # own conf, mirroring SWEEP_*. The composed timer passes the slot to the
    # trigger as an ExecStart arg; these vars let the in-session /briefing skill
    # read the bot's personalization. Per-slot sections use an UPPER-CASED slot
    # suffix (BRIEFING_SECTIONS_<SLOT>) per shell-var convention — the skill
    # upper-cases the dispatched slot name to read the matching var.
    if bot.briefing:
        lines.append("")
        lines.append("# Briefing (equipped via bots.<bot>.briefing)")
        lines.append(f"export BRIEFING_SLOTS={_shq(' '.join(bot.briefing.slots))}")
        if bot.briefing.sources:
            lines.append(
                f"export BRIEFING_SOURCES={_shq(' '.join(bot.briefing.sources))}"
            )
        for slot, sections in bot.briefing.sections.items():
            if sections:
                lines.append(
                    f"export BRIEFING_SECTIONS_{slot.upper()}="
                    f"{_shq(' '.join(sections))}"
                )

    # Ecosystem — clauDNA version pin, Claudron vault, Claudosseum tenant
    if bot.claudna_version or bot.claudron_vault_path or bot.claudosseum_tenant_id:
        lines.append("")
        lines.append("# Ecosystem")
        if bot.claudna_version:
            lines.append(f"export CLAUDNA_VERSION={_shq(bot.claudna_version)}")
        if bot.claudron_vault_path:
            lines.append(f"export CLAUDRON_VAULT_PATH={_shq(bot.claudron_vault_path)}")
        if bot.claudosseum_tenant_id:
            lines.append(
                f"export CLAUDOSSEUM_TENANT_ID={_shq(bot.claudosseum_tenant_id)}"
            )

    # Plugin sync — restore third-party plugins on session start. Union the
    # plugins this bot's channels pin (see _channel_plugins) so a cold box
    # installs them alongside the fleet's declared plugins.
    plugins_required = list(fleet.plugins.required)
    for _chan_plugin in _channel_plugins(bot.channels):
        if _chan_plugin not in plugins_required:
            plugins_required.append(_chan_plugin)
    if plugins_required:
        lines.append('export CLAUDE_CODE_SYNC_PLUGIN_INSTALL="1"')
        lines.append(
            f"export FLEET_PLUGINS_REQUIRED={_shq(' '.join(plugins_required))}"
        )
    if fleet.plugins.marketplaces:
        # Encode as "Name=github:Owner/Repo Name2=github:Owner2/Repo2"
        pairs = []
        for name, meta in fleet.plugins.marketplaces.items():
            src = meta.get("source", {})
            src_type = src.get("source", "github")
            src_repo = src.get("repo", "")
            pairs.append(f"{name}={src_type}:{src_repo}")
        lines.append(f"export FLEET_PLUGINS_MARKETPLACES={_shq(' '.join(pairs))}")

    for k, v in bot.env.items():
        if not _SHELL_IDENT_RE.match(k):
            raise ValueError(f"bot.env key {k!r} is not a valid shell identifier")
        lines.append(f"export {k}={_shq(v)}")

    # Secret-file paths: fleet-relative by contract, anchored on FLEET_ROOT so the
    # path is composer-derived and moves with the fleet. An absolute (or
    # parent-escaping) value is the exact hand-typed dangling-path smell this
    # closes → reject it loudly rather than bake it in.
    for var, subpath in bot.secret_files.items():
        if not _SHELL_IDENT_RE.match(var):
            raise ValueError(
                f"bot.secret_files key {var!r} is not a valid shell identifier"
            )
        if subpath.startswith(("/", "~")) or ".." in Path(subpath).parts:
            raise ValueError(
                f"bot.secret_files[{var!r}] must be fleet-relative, got {subpath!r} "
                "— declare it relative to the fleet root so the composer anchors it "
                "on FLEET_ROOT (never hand-type an absolute fleet path)"
            )
        lines.append(f'export {var}="$FLEET_ROOT/{subpath}"')

    lines.append("")

    for team in fleet.teams.values():
        if bot.bot_id in team.workers:
            lines.append(f"export MANAGER_TMUX={_shq(team.manager)}")
            # The manager's private tmux socket — mirrors MANAGER_TMUX, mapped to
            # the manager's BOT_SERVICE so report-back / pulse / sprint sends
            # reach the manager's own server (see bot_tmux_send).
            lines.append(
                f"export MANAGER_TMUX_SOCKET={_shq(f'{fleet.service_prefix}.{team.manager}')}"
            )
            break
    if bot.bot_id in fleet.manager_bots():
        lines.append(f"export MANAGER_TMUX={_shq(bot.bot_id)}  # this bot is a manager")
        lines.append(f"export MANAGER_TMUX_SOCKET={_shq(bot_service)}")

    lines.append("")
    if bot.startup_prompt:
        rendered = _render_startup_prompt(bot.startup_prompt, bot, fleet)
        lines.append(f"STARTUP_PROMPT={json.dumps(rendered)}")
    else:
        lines.append(
            'STARTUP_PROMPT="Welcome back. Read your CLAUDE.md. Idle and await Telegram messages."'
        )

    return "\n".join(lines) + "\n"


def _render_startup_prompt(prompt: str, bot: BotConfig, fleet: FleetConfig) -> str:
    """Render jinja placeholders in startup_prompt against fleet/bot context.

    Exposed variables (each is an empty string when not configured):
      - {{ bot_name }}                — bot.name
      - {{ fleet_name }}              — fleet.name
      - {{ telegram_group_chat_id }}  — fleet.telegram_group_chat_id
      - {{ telegram_handle }}         — bot.telegram.handle

    Lets fleet.yaml authors stop hand-duplicating identifiers — e.g.
    chat_id literals that already live in fleet.telegram_group_chat_id.
    """
    env = SandboxedEnvironment()
    return env.from_string(prompt).render(
        bot_id=bot.bot_id,
        bot_name=bot.name,
        fleet_name=fleet.name,
        telegram_group_chat_id=fleet.telegram_group_chat_id or "",
        telegram_handle=(bot.telegram.handle if bot.telegram else "") or "",
    )


# ----------------------------------------------------------------------
# Service units
# ----------------------------------------------------------------------


def compose_systemd_unit(
    bot: BotConfig, fleet: FleetConfig, paths: Paths, *, boot_delay_s: int = 0
) -> str:
    bot_dir = paths.bot_runtime(bot.bot_id)
    bot_service = f"{fleet.service_prefix}.{bot.bot_id}"
    stagger = f"\nExecStartPre=/bin/sleep {boot_delay_s}" if boot_delay_s > 0 else ""
    return f"""# Generated by claudlobby — do not hand-edit.
[Unit]
Description=claudlobby bot: {bot.name} ({fleet.name})
After=network-online.target

[Service]
Type=simple
# start-bot.sh exits 0 after spawning tmux. Without these two, the default
# control-group cleanup kills the tmux server we just started. KillMode=process
# limits the kill to the main process; RemainAfterExit=yes keeps the unit
# "active" while tmux runs underneath.
RemainAfterExit=yes
KillMode=process
WorkingDirectory={bot_dir}{stagger}
ExecStart={paths.lib}/start-bot.sh {bot_dir}
# kill-server (not kill-session): this bot owns its tmux server. kill-server
# tears the whole server down deterministically; kill-session leaves the emptied
# server orphaned unless tmux's exit-empty default reaps it.
ExecStop=/bin/sh -c 'tmux -L {bot_service} kill-server 2>/dev/null || true'
ExecStopPost=/bin/rm -f {bot_dir}/.tmux-env
# Restart= here only fires on non-zero exit of start-bot.sh — i.e., a config
# failure before tmux ever spawned. Tmux dying after we've gone "active" is
# detected by lib/keepalive.sh, NOT by systemd, because exit 0 + RemainAfterExit
# leaves the unit looking healthy regardless of what tmux is doing.
Restart=on-failure
RestartSec=5
Environment=CLAUDLOBBY_ROOT={paths.root}
Environment=TMUX_TMPDIR={_TMUX_TMPDIR}

[Install]
WantedBy=default.target
"""


# NOTE: launchd has no ExecStartPre equivalent for boot stagger.
# On macOS, fleet boot contention is less of an issue (Mac Mini has
# more cores/RAM than Pi). If needed, stagger can be added via a
# BOOT_DELAY env var honored by start-bot.sh itself.
def compose_launchd_plist(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> str:
    bot_dir = paths.bot_runtime(bot.bot_id)
    label = f"{fleet.service_prefix}.{bot.bot_id}"
    log_dir = paths.lib / "logs"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by claudlobby — do not hand-edit. -->
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{paths.lib}/start-bot.sh</string>
        <string>{bot_dir}</string>
    </array>
    <key>WorkingDirectory</key><string>{bot_dir}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key><false/>
    </dict>
    <key>StandardOutPath</key><string>{log_dir}/{bot.bot_id}.out.log</string>
    <key>StandardErrorPath</key><string>{log_dir}/{bot.bot_id}.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CLAUDLOBBY_ROOT</key><string>{paths.root}</string>
        <key>TMUX_TMPDIR</key><string>{_TMUX_TMPDIR}</string>
        <key>PATH</key><string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key><string>{Path.home()}</string>
    </dict>
</dict>
</plist>
"""


# ----------------------------------------------------------------------
# Skill symlinks
# ----------------------------------------------------------------------


def link_skills(bot: BotConfig, paths: Paths, log) -> None:
    """Symlink each skill into the bot's runtime .claude/skills/.

    Overlay-aware: looks in `local/<fleet>/library/skills/` first, then in
    public `library/skills/`.

    Each skill entry can be:
      - `name`       — single skill at `skills/name/`
      - `dir/name`   — nested skill at `skills/dir/name/`
      - `dir/`       — folder expansion: every skill subdir under `skills/dir/`
                       (recursive; overlay wins per leaf name).

    Symlinks are created under the skill's leaf name in `.claude/skills/`
    (Claude Code looks up skills by directory name, not nested path).
    Collisions (two source dirs with the same leaf) are first-wins; the
    second is logged and skipped.
    """
    bot_skills_dir = paths.bot_runtime(bot.bot_id) / ".claude" / "skills"
    if bot_skills_dir.exists():
        for entry in bot_skills_dir.iterdir():
            if entry.is_symlink():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry)
    bot_skills_dir.mkdir(parents=True, exist_ok=True)

    linked: dict[str, Path] = {}  # leaf name → source dir, for collision detection

    def _add(leaf: str, src: Path) -> None:
        if leaf in linked:
            log(f"  skill '{leaf}' already linked from {linked[leaf]} — skipping {src}")
            return
        linked[leaf] = src
        (bot_skills_dir / leaf).symlink_to(src.resolve())

    for skill in bot.skills:
        if skill.endswith("/"):
            dir_name = skill.rstrip("/")
            collected = paths.expand_skill_folder(dir_name)
            if not collected:
                log(f"  skill folder '{skill}' empty or missing — skipped")
                continue
            for leaf, src in collected.items():
                _add(leaf, src)
        else:
            src = paths.find_library_dir("skills", skill)
            if src is None:
                log(f"  skill '{skill}' missing — skipped")
                continue
            _add(src.name, src)


def link_mounts(bot: BotConfig, bot_dir: Path, log) -> None:
    """Create symlinks for declared mounts.

    Each mount maps a local name to an absolute host path.
    Symlinks are placed under bot_dir/mounts/<name>.
    Stale symlinks (removed from config) are cleaned up.
    """
    mounts_dir = bot_dir / "mounts"
    mounts_dir.mkdir(exist_ok=True)

    # Clean stale symlinks
    for entry in mounts_dir.iterdir():
        if entry.is_symlink() and entry.name not in bot.mounts:
            entry.unlink()

    for name, target in bot.mounts.items():
        target_path = Path(target).expanduser()
        try:
            resolved = target_path.resolve()
            if not resolved.is_relative_to(Path.home()) and not resolved.is_relative_to(
                bot_dir
            ):
                log(
                    f"  mount '{name}': target {target_path} escapes home and bot dir — skipping"
                )
                continue
        except (ValueError, OSError):
            log(f"  mount '{name}': could not resolve target {target_path} — skipping")
            continue
        link = mounts_dir / name
        if link.is_symlink():
            if link.resolve() == target_path.resolve():
                continue  # already correct
            link.unlink()
        elif link.exists():
            log(f"  mount '{name}': non-symlink already exists at {link} — skipping")
            continue
        if not target_path.exists():
            log(
                f"  mount '{name}' target does not exist: {target_path} — creating dangling symlink"
            )
        link.symlink_to(target_path)


# ----------------------------------------------------------------------
# Tools — composited scripts (library/tools/<name>/ → bot_dir/tools/)


def compose_tool_outputs(
    bot: BotConfig, fleet: FleetConfig, paths: Paths, bot_dir: Path
) -> dict[str, str]:
    """Render the bot's attached library tools → {target_filename: content}.

    Pure (no writes) so generate and diff share one implementation. Tool
    templates render with StrictUndefined — a missing/undeclared variable is
    a hard error, never a silently-empty executable. Params are compose-time
    structure; secrets never enter the context (rendered files are 0755 —
    tools read secrets from os.environ at runtime, declared in tool.yaml
    `env:`).
    """
    outputs: dict[str, str] = {}
    env = SandboxedEnvironment(
        undefined=jinja2.StrictUndefined, keep_trailing_newline=True
    )
    for entry in bot.tools:
        tool_dir = paths.find_library_dir("tools", entry.name)
        if tool_dir is None:
            raise ValueError(
                f"bot '{bot.bot_id}': tool '{entry.name}' not in any library/tools/"
            )
        manifest = tool_resolve.load_tool_manifest(tool_dir)
        template_path = tool_resolve.tool_template_path(tool_dir, manifest)
        target_name = tool_resolve.tool_target_name(template_path)
        if target_name in outputs:
            raise ValueError(
                f"bot '{bot.bot_id}': tools render duplicate target '{target_name}'"
            )
        params = tool_resolve.resolve_tool_params(entry.name, manifest, entry.params)
        context = tool_resolve.tool_context(
            params,
            bot_id=bot.bot_id,
            bot_name=bot.name,
            fleet_name=fleet.name,
            bot_dir=str(bot_dir),
            data_dir=str(bot_dir / "data"),
        )
        try:
            content = env.from_string(template_path.read_text()).render(context)
        except jinja2.exceptions.UndefinedError as e:
            raise ValueError(
                f"tool '{entry.name}': template references an "
                f"undeclared/unset variable — {e}"
            ) from None
        outputs[target_name] = content
    return outputs


def compose_tools(
    bot: BotConfig, fleet: FleetConfig, paths: Paths, bot_dir: Path
) -> None:
    """Write rendered tools into bot_dir/tools/ (0755) and reconcile.

    tools/ is compositor-owned like CLAUDE.md — never hand-edited. Files for
    detached tools are removed on every generate; a tool's runtime outputs
    (snapshots, ledgers) belong in data/, which this never touches.
    """
    outputs = compose_tool_outputs(bot, fleet, paths, bot_dir)
    tools_dir = bot_dir / "tools"
    if outputs:
        tools_dir.mkdir(exist_ok=True)
    for target_name, content in outputs.items():
        target = tools_dir / target_name
        target.write_text(content)
        target.chmod(0o755)
    if tools_dir.is_dir():
        for existing in sorted(tools_dir.iterdir()):
            if existing.is_file() and existing.name not in outputs:
                existing.unlink()


# ----------------------------------------------------------------------
# Org structure
# ----------------------------------------------------------------------


def _compose_org_structure(bot: BotConfig, fleet: FleetConfig) -> str | None:
    """Render the org-structure block for CLAUDE.md when reports_to/manages is set."""
    if not bot.reports_to and not bot.manages:
        return None

    def _ref(bot_id: str) -> str:
        b = fleet.bots.get(bot_id)
        if b and b.name != b.bot_id:
            return f"{b.name} (`{bot_id}`)"
        return bot_id

    lines: list[str] = []
    if bot.reports_to:
        lines.append(f"- **Reports to:** {_ref(bot.reports_to)}")
    if bot.manages:
        lines.append("- **Direct reports:**")
        for mid in bot.manages:
            lines.append(f"  - {_ref(mid)}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Telegram access.json
# ----------------------------------------------------------------------


def compose_access_json(bot: BotConfig, fleet: FleetConfig) -> dict | None:
    """Generate Telegram channel access.json from fleet.yaml.

    Returns None if the bot has no telegram config. The generated config
    controls DM policy, group requireMention, and human allowlisting.

    Written to ~/.claude/channels/telegram-<handle>/access.json so the
    Telegram plugin picks up correct settings on first boot — preventing
    the default-config bug where requireMention defaults to false.
    """
    if not bot.telegram or not bot.telegram.handle:
        return None

    chat_id = bot.telegram.chat_id or fleet.telegram_group_chat_id
    if not chat_id:
        return None

    access: dict = {
        "dmPolicy": "allowlist",
        "allowFrom": [],
        "groups": {
            chat_id: {
                "requireMention": bot.telegram.require_mention,
                "allowFrom": [],
            }
        },
        "pending": {},
    }

    if fleet.human_telegram_id:
        access["allowFrom"] = [fleet.human_telegram_id]

    return access


# ----------------------------------------------------------------------
# Integration resolution
# ----------------------------------------------------------------------


def resolve_effective_integrations(bot: BotConfig, paths: Paths) -> list[str]:
    """Return the list of integration names to use for a bot.

    Returns the UNION of explicit ``bot.integrations`` and the auto-paired
    mcp names (any MCP entry whose name has a matching integrations/<name>.md
    file in the overlay or base library), with explicit integrations first and
    order preserved. When ``bot.integrations`` is unset this is exactly the
    auto-paired set.

    The union is load-bearing: returning ``bot.integrations`` verbatim would
    strip the mcp-derived permission grants the moment a bot sets integrations:
    explicitly, because grant resolution keys off the effective-integration set.
    Explicit connector integrations must be *additive* to the mcp-paired set,
    never a replacement.
    """
    auto_paired = [
        e.name
        for e in bot.mcp
        if paths.find_library_file("integrations", e.name, ".md") is not None
    ]
    if not bot.integrations:
        return auto_paired
    result = list(bot.integrations)
    for name in auto_paired:
        if name not in result:
            result.append(name)
    return result


# ----------------------------------------------------------------------
# CLAUDE.md composition (template-driven)
# ----------------------------------------------------------------------


def compose_claude_md(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> str:
    ctx = _bot_template_context(bot, fleet, paths)

    title_label, expertise_body = _compose_expertise(bot, paths, ctx)

    voice_item: LibraryItem | None = None
    if bot.voice:
        voice_path = paths.find_voice_file(bot.voice)
        if voice_path is not None:
            voice_item = load_voice(voice_path)
            if voice_item is not None:
                voice_item = _expand_item(voice_item, ctx)

    def _items(names: list[str], kind: str) -> list[LibraryItem]:
        return [
            _expand_item(it, ctx)
            for it in load_library_items_overlay(names, paths, kind)
        ]

    integration_names = resolve_effective_integrations(bot, paths)

    # Auto-include shared-documentation protocol when shared docs are available
    protocol_names = list(bot.protocols)
    if paths.shared_docs and "shared-documentation" not in protocol_names:
        protocol_names.append("shared-documentation")

    teams = fleet.teams_for_manager(bot.bot_id)
    org_structure = _compose_org_structure(bot, fleet)

    is_manager = bot.bot_id in fleet.manager_bots()

    # Projects table composes for managers only (F6-style context budget:
    # workers resolve tiers from the bot.conf env map, not prose).
    projects = (
        sorted(fleet.projects.values(), key=lambda p: p.key) if is_manager else []
    )
    for p in projects:
        # Emit-time corruption backstop (validator owns the UX error): a
        # newline in a title would inject fake sections into the composed
        # instructions; a pipe breaks the table.
        if "\n" in p.title or "|" in p.title:
            raise ValueError(
                f"project '{p.key}': title contains newline or '|' — refusing "
                f"to render it into CLAUDE.md (run claudlobby validate)"
            )

    # Fleet-mission extra content under the paragraph, decided in ONE place:
    # managers compose the full charter body (headings demoted, like every
    # composed library body); workers — and a manager whose charter file is
    # missing (validator already warned; benign absence) — get a pointer to
    # $FLEET_MISSION_FILE. None when no mission_file is configured.
    fleet_mission_extra = None
    if fleet.mission and "\n" in fleet.mission.strip():
        # Emit-time corruption backstop (validator owns the UX error): a
        # multi-line mission would inject sections into EVERY bot's
        # composed instructions.
        raise ValueError(
            "fleet.mission contains newlines — refusing to render it into "
            "CLAUDE.md (run claudlobby validate)"
        )
    if fleet.mission_file:
        charter = paths.fleet_config_dir / fleet.mission_file
        if is_manager and charter.is_file():
            # Double demote: the body nests under the H2 "## Fleet Mission"
            # section, so the charter's own H1 must land at H3 (a single
            # demote would leave it an H2 sibling, escaping the section).
            fleet_mission_extra = _demote_headings(
                _demote_headings(charter.read_text())
            )
        else:
            fleet_mission_extra = (
                "Full charter: read $FLEET_MISSION_FILE when picking or "
                "prioritizing work."
            )

    env = _build_jinja_env(paths)
    template = env.get_template("claude.md.j2")
    rendered = template.render(
        bot=bot,
        fleet=fleet,
        title_label=title_label,
        expertise_body=expertise_body,
        voice=voice_item,
        teams=teams,
        projects=projects,
        fleet_mission_extra=fleet_mission_extra,
        org_structure=org_structure,
        shared_docs_path=str(paths.shared_docs) if paths.shared_docs else None,
        resources=_items(bot.resources, "resources"),
        integrations=_items(integration_names, "integrations"),
        principles=_items(bot.principles, "principles"),
        permissions=_items(bot.permissions, "permissions"),
        protocols=_items(protocol_names, "protocols"),
        guardrails=_items(bot.guardrails, "guardrails"),
        lessons=_items(bot.lessons, "lessons"),
        post_actions=_items(bot.post_actions, "post_actions"),
    )
    # Collapse 3+ blank lines → 2 to keep output tidy.
    while "\n\n\n\n" in rendered:
        rendered = rendered.replace("\n\n\n\n", "\n\n\n")
    return rendered


# ----------------------------------------------------------------------
# Main entry: compose one bot
# ----------------------------------------------------------------------


def _compose_hooks(hooks: dict[str, list[dict[str, Any]]]) -> dict[str, list]:
    """Transform fleet.yaml hook entries into Claude Code settings.local.json format.

    Fleet.yaml uses a flat format per entry:
        {command: "script.sh", matcher: "Bash", timeout: 10}

    Claude Code expects nested matcher groups:
        [{matcher: "Bash", hooks: [{type: "command", command: "script.sh", timeout: 10}]}]

    Entries with the same matcher within one event are grouped together.
    """
    if not hooks:
        return {}
    out: dict[str, list] = {}
    for event, entries in hooks.items():
        if not entries:
            continue
        # Group entries by matcher value
        by_matcher: dict[str, list[dict]] = {}
        for entry in entries:
            matcher = entry.get("matcher", "")
            if matcher not in by_matcher:
                by_matcher[matcher] = []
            # Build the hook object — everything except "matcher" is a hook field
            hook = {k: v for k, v in entry.items() if k != "matcher"}
            if "type" not in hook:
                hook["type"] = "command"
            by_matcher[matcher].append(hook)
        # Build matcher groups
        groups = []
        for matcher, hook_list in by_matcher.items():
            group: dict[str, Any] = {"hooks": hook_list}
            if matcher:
                group["matcher"] = matcher
            groups.append(group)
        out[event] = groups
    return out


# ----------------------------------------------------------------------
# Claudron session loop (L2) — engine hooks + narrow verb grants per vault-wired
# bot. The hook entries are a RENDERED COPY of a Claudron-owned contract surface
# (CLI_CONTRACT.md §Session-loop protocol, "The hook-settings snippet — normative
# shape"); tests/test_claudron_loop.py carries the required drift gate against the
# pinned engine's `claudron.hooks.settings_snippet()` (register rule R3).
# ----------------------------------------------------------------------

# Narrow, per-verb grants for the model-initiated CLI calls the loop enables (the
# query-before wedge; /claudna:capture shelling `claudron capture`). NEVER a
# Bash(claudron *) wildcard — that would grant the human-gated curation verbs
# (promote/plug/unplug/config/migrate; boundary spec §8) and defeat curation. The
# settings-installed hooks are harness-executed and pass through no permission
# check, so they need no grant; these cover only calls the model itself makes.
CLAUDRON_LOOP_GRANTS = [
    "Bash(claudron lookup *)",
    "Bash(claudron recall *)",
    "Bash(claudron capture *)",
    "Bash(claudron status *)",
]

# The three session-loop events → the engine's `hook <event>` dispatch verb.
_CLAUDRON_HOOK_EVENTS = {
    "SessionStart": "session-start",
    "PreCompact": "pre-compact",
    "SessionEnd": "session-end",
}


def _session_loop_enabled(bot: BotConfig) -> bool:
    """Resolve the tri-state ``claudron_session_loop``: an explicit value wins;
    unset defaults True exactly when the bot is vault-wired (has a
    ``claudron_vault_path``), False otherwise."""
    if bot.claudron_session_loop is not None:
        return bot.claudron_session_loop
    return bool(bot.claudron_vault_path)


@functools.cache
def _resolve_claudron_executable() -> tuple[str, str | None]:
    """Absolute ``claudron`` path for the composed hook commands, resolved on the
    compose host. Returns ``(executable, warning)``.

    Hook context is not a login shell — PATH frequently omits a venv/pipx install
    — so an absolute path is the contract (C2). A bare-``claudron`` fallback is
    permitted only *with* a warning, because the loop would be wired-but-dead if
    PATH does not carry it at runtime. Resolution is `shutil.which` at compose
    time; we never shell out to ``claudron`` itself (composition must work on a
    CLI-less host).

    Cached: the executable's PATH location is host-invariant, so the per-bot
    compose loop resolves it once per process instead of re-walking PATH for
    every vault-wired bot. The caller emits the warning per bot, so operators
    still see which bots are affected.
    """
    resolved = shutil.which("claudron")
    if resolved:
        return resolved, None
    return (
        "claudron",
        "claudron is not on PATH at compose time — the session-loop hooks fall "
        "back to a bare 'claudron' command that may not resolve in hook context "
        "(hook PATH is not login-shell PATH). Install claudron on the compose/run "
        "host so the hook command resolves to an absolute executable.",
    )


def _claudron_hook_entries(executable: str) -> dict[str, list]:
    """The engine's session-loop hook entries in Claude Code settings shape.

    Emitted INLINE (never by shelling ``claudron hooks install``) so composition
    works on a CLI-less host. Byte-for-byte identical to the pinned engine's
    ``claudron.hooks.settings_snippet(executable)["hooks"]`` — the parity gate
    in tests/test_claudron_loop.py enforces that.
    """
    return {
        event: [
            {
                "matcher": "",
                "hooks": [
                    {"type": "command", "command": f"{executable} hook {event_cmd}"}
                ],
            }
        ]
        for event, event_cmd in _CLAUDRON_HOOK_EVENTS.items()
    }


def _is_claudron_hook_entry(group: dict, event_cmd: str) -> bool:
    """A claudron entry's identity is its ``hook <event>`` command SUFFIX, not the
    full string (mirrors the engine's ``merge_settings`` key). Keying on the full
    path would append a duplicate whenever the resolved executable moved."""
    return any(
        str(h.get("command", "")).endswith(f"hook {event_cmd}")
        for h in (group.get("hooks") or [])
    )


def _merge_claudron_hooks(hooks: dict[str, list], executable: str) -> dict[str, list]:
    """Merge the engine's session-loop entries into a composed hooks block.

    Self-replacing per event (a stale claudron entry for the same event is
    dropped, so a moved executable path never accumulates) while foreign entries
    — a fleet's own SessionStart hook, say — are preserved. Mirrors the engine's
    ``claudron.hooks.merge_settings`` so an engine-installed loop and a
    composer-installed loop converge on the same file. Idempotent.
    """
    merged = dict(hooks)
    for event, entries in _claudron_hook_entries(executable).items():
        event_cmd = _CLAUDRON_HOOK_EVENTS[event]
        kept = [
            g
            for g in (merged.get(event) or [])
            if not _is_claudron_hook_entry(g, event_cmd)
        ]
        merged[event] = kept + entries
    return merged


_TELEGRAM_PLUGIN_TOOLS = [
    "mcp__plugin_telegram_telegram__reply",
    "mcp__plugin_telegram_telegram__edit_message",
    "mcp__plugin_telegram_telegram__react",
    "mcp__plugin_telegram_telegram__download_attachment",
]


def _resolve_channel_permissions(bot: BotConfig) -> list[str]:
    """Auto-derive tool permissions from configured channels.

    When a bot has a Telegram handle set, include the 4 plugin tools.
    """
    tools: list[str] = []
    if bot.telegram.handle:
        tools.extend(_TELEGRAM_PLUGIN_TOOLS)
    return tools


def _resolve_skill_permissions(bot: BotConfig) -> list[str]:
    """Auto-derive Skill() permission patterns from bot's skill list.

    Each skill needs both Skill(<name>) and Skill(<name>:*) for full operation.
    """
    patterns: list[str] = []
    for skill in bot.skills:
        patterns.append(f"Skill({skill})")
        patterns.append(f"Skill({skill}:*)")
    return patterns


def _resolve_skill_grants(bot: BotConfig, paths: Paths) -> list[str]:
    """Additive ``tool_grants`` declared by the bot's equipped skills (F2/F6).

    :func:`_resolve_skill_permissions` grants only ``Skill(<name>)`` invocation;
    a skill's ``SKILL.md`` separately declares the ``Bash(...)`` / ``mcp__...`` /
    bare tools its body actually runs. This resolves those additive grants —
    folder entries (``dir/``) expanded to every member — so a skill ships
    self-contained with the tools it needs. Joins integrations on the additive
    ``tool_grants`` path; de-duplication against the allow list happens in
    :func:`compose_settings_local`.
    """
    return [
        grant
        for _name, grants in iter_skill_grants(paths, bot.skills)
        for grant in grants
    ]


# Full tool set an ``allow_all`` permission profile expands to.
ALL_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Agent",
    "Grep",
    "Glob",
    "WebFetch",
    "WebSearch",
    "NotebookEdit",
]


def _append_unique(target: list[str], items: Iterable[str]) -> None:
    """Append each item to ``target`` in order, skipping ones already present.

    The composed allow/deny lists are order-preserving de-duplicated unions;
    this is the single expression of that idiom across the permission layers.
    """
    for item in items:
        if item not in target:
            target.append(item)


def _merge_permission_profiles(
    profiles: Iterable[ExpertisePermissions | None],
) -> tuple[list[str], list[str]]:
    """Fold deny-capable permission profiles into (allow, deny) pattern lists.

    Shared by expertise and guardrail resolution — both declare the same
    deny-capable ``permissions:{}`` schema (F2). ``allow`` and ``deny`` are
    unioned across profiles, ``allow_all`` expands to :data:`ALL_TOOLS`,
    ``bash_allow`` entries become ``Bash(<cmd> *)`` allows, and deny wins over
    allow within the merged set (CC also enforces deny-wins at runtime across
    layers). ``None`` profiles (prose-only sources) are skipped.
    """
    merged_allow: list[str] = []
    merged_deny: list[str] = []
    merged_bash: list[str] = []
    has_allow_all = False

    for p in profiles:
        if p is None:
            continue
        if p.allow_all:
            has_allow_all = True
        _append_unique(merged_allow, p.allow)
        _append_unique(merged_deny, p.deny)
        _append_unique(merged_bash, p.bash_allow)

    if has_allow_all:
        _append_unique(merged_allow, ALL_TOOLS)

    # Deny wins within the merged set.
    merged_allow = [t for t in merged_allow if t not in merged_deny]

    allow_patterns = list(merged_allow)
    for cmd in merged_bash:
        allow_patterns.append(f"Bash({cmd} *)")
    return allow_patterns, list(merged_deny)


def _resolve_expertise_permissions(
    bot: BotConfig,
    paths: Paths,
) -> tuple[list[str], list[str]]:
    """Merge permission profiles from all expertise files for a bot.

    Returns (allow_patterns, deny_patterns) — see :func:`_merge_permission_profiles`
    for the fold semantics (allow/deny union, allow_all expansion, bash_allow,
    deny-wins).
    """
    profiles: list[ExpertisePermissions | None] = []
    for area in bot.expertise:
        path = paths.find_library_file("expertise", area, ".md")
        if path is None:
            continue
        item = parse_expertise_file(path)
        if item is None:
            continue
        profiles.append(item.permissions)
    return _merge_permission_profiles(profiles)


def _resolve_guardrail_permissions(
    bot: BotConfig,
    paths: Paths,
) -> tuple[list[str], list[str]]:
    """Merge deny-capable ``permissions:{}`` blocks from the bot's guardrails (F2).

    Guardrails share the expertise permission schema; a guardrail typically
    declares only ``deny`` (a safety rule). Prose-only guardrails contribute
    nothing. Folder entries (``dir/``) are expanded so nested guardrails are not
    silently skipped. Joins expertise on the deny-capable ``permissions:{}`` path.
    """
    profiles = [
        perms for _name, perms in iter_guardrail_permissions(paths, bot.guardrails)
    ]
    return _merge_permission_profiles(profiles)


# Minimal base tools every bot needs for read-only operation.
# Expertise profiles add Write, Edit, Bash, etc. in Phase 2.
BASE_TOOLS = ["Read", "Grep", "Glob"]


def _assert_grant_superset(bot_id: str, legacy: list[str], new: list[str]) -> None:
    """Fail generation unless the new integration ``tool_grants`` cover every legacy grant.

    The migration keeps both grant sources live (see :func:`compose_settings_local`),
    so no live grant can vanish mid-window even if the new resolver has an edge bug.
    This gate proves the *stronger* property the ``_permissions_contract`` cut depends
    on: the new :func:`_resolve_integration_grants` output alone is already a superset
    of :func:`_resolve_mcp_permissions`. Compared as normalized sets (order-independent)
    and raised before any settings.local.json is written.
    """
    missing = sorted(set(legacy) - set(new))
    if missing:
        raise ValueError(
            f"bot {bot_id!r}: integration tool_grants would drop legacy MCP grants "
            f"{missing} — every fragment-backed mcp entry needs a paired integration "
            "file whose tool_grants cover it before the _permissions_contract cut."
        )


def compose_settings_local(
    bot: BotConfig,
    fleet: FleetConfig,
    paths: Paths,
    mcp_server_names: list[str] | None = None,
) -> dict:
    """Generate .claude/settings.local.json with memory dir, sibling isolation, tools, sandbox, and hooks.

    ``mcp_server_names`` is the bot's composed project MCP-server set (the keys of
    :func:`compose_mcp_json`), threaded in from :func:`compose_bot` so it is computed
    once. When non-empty it is emitted as ``enabledMcpjsonServers`` — a per-server trust
    allowlist that pre-approves exactly the fleet-configured servers, so a headless
    ``claude`` boot never stalls on the interactive MCP-approval prompt (``--permission-mode
    auto`` does not answer it). Because this file is fully overwritten every generate, this
    key is what makes MCP trust durable: it is re-derived each run rather than preserved as
    runtime state. The set comes from fleet config, not the on-disk ``.mcp.json``, so a
    server absent from fleet config stays untrusted and a regenerate drops any entry added
    to the on-disk file (fails closed). The blanket ``enableAllProjectMcpServers`` is
    deliberately NOT emitted: it would trust any server present on disk, including one
    carried by a checked-out repo's ``.mcp.json``.
    """
    bot_dir = paths.bot_runtime(bot.bot_id)
    memory_dir = str(bot_dir / "memory")

    settings: dict = {
        "autoMemoryDirectory": memory_dir,
    }

    # Build permissions block — layered composition
    deny_patterns: list[str] = []

    # Layer 0: Sibling isolation — deny reading OR mutating another bot's runtime
    # dir. Read alone left a cross-bot Write/Edit gap (R9); all three are denied so
    # a bot cannot touch a sibling's files.
    siblings = [bid for bid in fleet.bots if bid != bot.bot_id]
    for sibling in siblings:
        sibling_dir = str(paths.bot_runtime(sibling))
        deny_patterns.append(f"Read({sibling_dir}/**)")
        deny_patterns.append(f"Write({sibling_dir}/**)")
        deny_patterns.append(f"Edit({sibling_dir}/**)")

    # Layer 1: Guardrail permissions (deny-capable safety rules; shared expertise
    # schema). Guardrails are usually deny-only; their rare allows join Layer 2.
    guardrail_allow, guardrail_deny = _resolve_guardrail_permissions(bot, paths)
    deny_patterns.extend(guardrail_deny)

    # Layer 2: Expertise permissions (from library/expertise/ frontmatter)
    expertise_allow, expertise_deny = _resolve_expertise_permissions(bot, paths)
    deny_patterns.extend(expertise_deny)

    # Layer 7: Bot-level deny (from fleet.yaml tools.deny) — wins over everything
    for tool in bot.tool_permissions.deny:
        deny_patterns.append(tool)

    # Build allow list: layers 2-7
    allow_patterns: list[str] = []

    # Layer 2: Expertise allow (plain tool names + bash command patterns)
    allow_patterns.extend(expertise_allow)

    # Layer 1: Guardrail allow (rare — guardrails are usually deny-only)
    _append_unique(allow_patterns, guardrail_allow)

    # Layer 3: MCP tool grants — union of the legacy fragment _permissions_contract
    # resolver and the new integration tool_grants resolver. Both stay live through
    # the migration window (belt-and-suspenders — no live grant can vanish even if the
    # new resolver has an edge bug). _assert_grant_superset proves the new resolver
    # alone already covers legacy, the precondition for cutting _permissions_contract.
    legacy_grants = _resolve_mcp_permissions(bot, paths)
    integration_grants = _resolve_integration_grants(bot, paths)
    _assert_grant_superset(bot.bot_id, legacy_grants, integration_grants)
    _append_unique(allow_patterns, (*legacy_grants, *integration_grants))

    # Layer 4: Channel/plugin tools (auto-derived from config)
    allow_patterns.extend(_resolve_channel_permissions(bot))

    # Layer 5: Skill patterns (auto-derived from bot.skills)
    allow_patterns.extend(_resolve_skill_permissions(bot))

    # Layer 5b: Skill tool_grants — the Bash/mcp/bare tools a skill body actually
    # runs, declared on its SKILL.md (F2/F6). Joins integration grants on the
    # additive path; Skill(<name>) above only grants invocation.
    _append_unique(allow_patterns, _resolve_skill_grants(bot, paths))

    # Layer 5c: Claudron session-loop verb grants (L2) — the NARROW allowlist for
    # the model-initiated CLI calls the loop enables (query wedge, /claudna:capture).
    # Never a Bash(claudron *) wildcard (see CLAUDRON_LOOP_GRANTS). Gated on the
    # same switch as the hooks below, so `claudron_session_loop: false` composes
    # neither — a clean, single off-switch for a bot's Claudron loop wiring.
    if _session_loop_enabled(bot):
        _append_unique(allow_patterns, CLAUDRON_LOOP_GRANTS)

    # Union-layer write guardrail: with every library-derived layer accumulated
    # (and before the operator's tools.allow escape hatch below), nothing may
    # cover a non-read tool of a read-only-contracted server (#661).
    _assert_no_write_autoallows(bot, paths, allow_patterns)

    # Layer 6/7: Explicit tools.allow from fleet defaults + bot config
    _append_unique(allow_patterns, bot.tool_permissions.allow)

    # Bot-level deny wins over all allow layers
    bot_deny_plain = set(bot.tool_permissions.deny)
    allow_patterns = [p for p in allow_patterns if p not in bot_deny_plain]

    # Ensure base tools are present whenever allow list is non-empty
    # (settings.local.json allow REPLACES global — without these, bots lose basic access)
    if allow_patterns:
        for t in BASE_TOOLS:
            if t not in allow_patterns and t not in bot_deny_plain:
                allow_patterns.insert(0, t)

    if deny_patterns:
        permissions: dict = {"deny": deny_patterns}
        if allow_patterns:
            permissions["allow"] = allow_patterns
        settings["permissions"] = permissions
    elif allow_patterns:
        settings["permissions"] = {
            "allow": allow_patterns,
        }

    # Sandbox: enabled toggle + network + filesystem allowlists + bash auto-allow
    sandbox_cfg: dict = {}
    sandbox = bot.sandbox
    # Sandbox is disabled by default as a low-friction system default; a fleet or
    # bot opts in with `sandbox.enabled: true`. None is the internal unset/inherit
    # sentinel, resolved to the system default (False) here at the compose boundary
    # so `enabled` is always emitted (a fresh box never runs unsandboxed by omission).
    sandbox_cfg["enabled"] = sandbox.enabled if sandbox.enabled is not None else False
    if sandbox.network_allowed_domains:
        sandbox_cfg["network"] = {"allowedDomains": sandbox.network_allowed_domains}
    if sandbox.filesystem_allow_write:
        sandbox_cfg["filesystem"] = {"allowWrite": list(sandbox.filesystem_allow_write)}
    if sandbox.auto_allow_bash is not None:
        sandbox_cfg["autoAllowBashIfSandboxed"] = sandbox.auto_allow_bash

    # Inject shared docs write access
    if paths.shared_docs:
        fs = sandbox_cfg.setdefault("filesystem", {})
        aw = fs.setdefault("allowWrite", [])
        shared_str = str(paths.shared_docs)
        if shared_str not in aw:
            aw.append(shared_str)

    if sandbox_cfg:
        settings["sandbox"] = sandbox_cfg

    # Hooks: fleet.yaml lifecycle hooks (PreToolUse/PostToolUse/…) plus, when the
    # bot is wired to the Claudron session loop (L2), the engine's
    # SessionStart/PreCompact/SessionEnd entries merged in per the C2 contract's
    # normative snippet shape. The merge preserves a fleet's own entries for those
    # events and replaces only a stale claudron entry (self-replacing by suffix).
    # No claim env is composed: F1 is STRUCTURAL — the single capture prompt is
    # claimed by clauDNA's hook detecting the engine's `hook pre-compact` entry,
    # not by any composed CLAUDRON_CAPTURE_OWNER-style variable.
    hooks = _compose_hooks(bot.hooks)
    if _session_loop_enabled(bot):
        executable, warning = _resolve_claudron_executable()
        if warning:
            _log.warning("bot %s: %s", bot.bot_id, warning)
        hooks = _merge_claudron_hooks(hooks, executable)
    if hooks:
        settings["hooks"] = hooks

    # Plugins — enabledPlugins + extraKnownMarketplaces from fleet config
    if fleet.plugins.required:
        settings["enabledPlugins"] = {plugin: True for plugin in fleet.plugins.required}
    if fleet.plugins.marketplaces:
        settings["extraKnownMarketplaces"] = dict(fleet.plugins.marketplaces)

    # MCP trust allowlist — see docstring for the derive-and-fail-closed rationale.
    if mcp_server_names:
        settings["enabledMcpjsonServers"] = sorted(mcp_server_names)

    # Headless UX defaults (fleet.yaml-overridable per bot): silence the spinner
    # tip lines, disable the notification prompts, and prefer reduced motion — safe
    # now that keepalive liveness is marker-based rather than spinner-based.
    settings["spinnerTipsEnabled"] = bot.spinner_tips_enabled
    settings["preferredNotifChannel"] = bot.preferred_notif_channel
    settings["prefersReducedMotion"] = bot.prefers_reduced_motion

    # First-run consent skip-flags (fleet.yaml-overridable per bot): suppress the
    # interactive first-run permission prompts so a headless bot boots without
    # hanging. Distinct from the --dangerously-skip-permissions CLI flag, which
    # composes into CLAUDE_FLAGS.
    settings["skipAutoPermissionPrompt"] = bot.skip_auto_permission_prompt
    settings["skipDangerousModePermissionPrompt"] = (
        bot.skip_dangerous_mode_permission_prompt
    )

    return settings


def _reconcile_access_json(
    access_path: Path,
    fresh: dict,
    bot: BotConfig,
    fleet: FleetConfig,
    log=None,
) -> None:
    """Update fleet-derived fields in an existing access.json, preserving runtime state.

    Fleet-controlled fields (updated every generate):
      - dmPolicy, groups.<chat_id>.requireMention, allowFrom (human ID)

    Runtime state (preserved):
      - pending, extra groups added at runtime, extra allowFrom entries
    """
    try:
        existing = json.loads(access_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        msg = f"  WARNING: {access_path} is unreadable ({exc}), leaving unchanged"
        _log.warning("%s is unreadable: %s, leaving unchanged", access_path, exc)
        if log is not None:
            log(msg)
        return

    if not isinstance(existing, dict):
        msg = f"  WARNING: {access_path} is not a JSON object, leaving unchanged"
        _log.warning("%s is not a JSON object, leaving unchanged", access_path)
        if log is not None:
            log(msg)
        return

    chat_id = bot.telegram.chat_id or fleet.telegram_group_chat_id
    existing["dmPolicy"] = fresh["dmPolicy"]

    if chat_id:
        existing.setdefault("groups", {})
        if chat_id in existing["groups"]:
            existing["groups"][chat_id]["requireMention"] = bot.telegram.require_mention
        else:
            existing["groups"][chat_id] = {
                "requireMention": bot.telegram.require_mention,
                "allowFrom": [],
            }

    if fleet.human_telegram_id:
        allow = existing.setdefault("allowFrom", [])
        if fleet.human_telegram_id not in allow:
            allow.append(fleet.human_telegram_id)

    access_path.write_text(json.dumps(existing, indent=2) + "\n")


def compose_bot(
    bot: BotConfig, fleet: FleetConfig, paths: Paths, log=None, *, boot_delay_s: int = 0
) -> Path:
    bot_dir = paths.bot_runtime(bot.bot_id)
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / ".claude").mkdir(exist_ok=True)
    (bot_dir / "memory").mkdir(exist_ok=True)
    (bot_dir / "projects").mkdir(exist_ok=True)
    (bot_dir / "data").mkdir(exist_ok=True)
    (bot_dir / "data" / "events").mkdir(exist_ok=True)
    (bot_dir / "logs").mkdir(exist_ok=True)

    (bot_dir / "CLAUDE.md").write_text(compose_claude_md(bot, fleet, paths))

    mcp = compose_mcp_json(bot, paths)
    (bot_dir / ".mcp.json").write_text(json.dumps(mcp, indent=2) + "\n")

    (bot_dir / "bot.conf").write_text(compose_bot_conf(bot, fleet, paths))

    settings_local = compose_settings_local(
        bot, fleet, paths, list(mcp["mcpServers"].keys())
    )
    (bot_dir / ".claude" / "settings.local.json").write_text(
        json.dumps(settings_local, indent=2) + "\n"
    )

    _emit = log if log is not None else _log.info
    link_skills(bot, paths, _emit)
    link_mounts(bot, bot_dir, _emit)
    compose_tools(bot, fleet, paths, bot_dir)

    # Telegram access.json — write to channel state dir so the plugin
    # picks up correct requireMention/dmPolicy on first boot.
    access = compose_access_json(bot, fleet)
    if access is not None:
        handle = bot.telegram.handle
        # compose_bot_conf already enforced this rule and raised; defensive re-check.
        if not _TELEGRAM_HANDLE_RE.match(handle):
            _log.warning(
                "bot %s has invalid telegram handle %r, skipping access.json",
                bot.bot_id,
                handle,
            )
            if log is not None:
                log(
                    f"  WARNING: bot {bot.bot_id} has invalid telegram handle {handle!r}, skipping access.json"
                )
        else:
            channel_dir = Path.home() / ".claude" / "channels" / f"telegram-{handle}"
            channel_dir.mkdir(parents=True, exist_ok=True)
            access_path = channel_dir / "access.json"
            if access_path.exists():
                _reconcile_access_json(access_path, access, bot, fleet, log)
            else:
                access_path.write_text(json.dumps(access, indent=2) + "\n")

    (bot_dir / f"{fleet.service_prefix}.{bot.bot_id}.service").write_text(
        compose_systemd_unit(bot, fleet, paths, boot_delay_s=boot_delay_s)
    )
    (bot_dir / f"{fleet.service_prefix}.{bot.bot_id}.plist").write_text(
        compose_launchd_plist(bot, fleet, paths)
    )

    # Path-ownership guarantee: fail loud if any composed wiring file carries a
    # flat/dangling/improper absolute fleet path (a hand-typed path that would not
    # survive a fleet move), rather than letting it dangle silently. See path_audit.
    from .path_audit import assert_bot_paths

    assert_bot_paths(bot, fleet, paths)

    return bot_dir


@dataclass
class EnvVar:
    """A single env var requirement from a library contract."""

    name: str
    description: str
    tier: str  # "fleet" or "bot"
    source: str  # e.g. "mcp/github", "integration/neon", "telegram"


def collect_env_contracts(fleet: FleetConfig, paths: Paths) -> list[EnvVar]:
    """Walk fleet config, collect all env var contracts. Instance-aware."""
    from .loader import parse_frontmatter

    vars: dict[str, EnvVar] = {}

    seen_mcp: set[str] = set()
    for bot in fleet.bots.values():
        for entry in bot.mcp:
            if entry.name in seen_mcp:
                continue
            seen_mcp.add(entry.name)
            frag_path = paths.find_library_file("mcp", entry.name, ".json")
            if frag_path is None:
                continue
            frag = json.loads(frag_path.read_text())
            contract = frag.get("_env_contract", {})
            # Operator-facing vars only: the shared kernel skips
            # provided_by:composer (a composer-emitted var like CLAUDRON_VAULT_PATH
            # would otherwise scaffold a misleading empty .env stub and false-alarm
            # doctor) and applies instance naming — one home so validate + this
            # collection can never drift again (#568, finishes #233). Dedup by
            # canonical name and the .env description label are this consumer's job.
            for cv in iter_operator_contract_vars(contract, entry):
                if cv.canonical_name in vars:
                    continue
                inst_label = (
                    f" ({cv.instance})" if cv.instance not in (None, "default") else ""
                )
                vars[cv.canonical_name] = EnvVar(
                    name=cv.canonical_name,
                    description=f"{cv.description}{inst_label}",
                    tier=cv.tier,
                    source=f"mcp/{entry.name}",
                )

        # Integration doc contracts
        integration_names = resolve_effective_integrations(bot, paths)
        for int_name in integration_names:
            int_path = paths.find_library_file("integrations", int_name, ".md")
            if int_path is None:
                continue
            fm, _ = parse_frontmatter(int_path.read_text())
            contract = fm.get("env_contract", {})
            if not contract:
                continue
            for var_name, meta in contract.items():
                tier = meta.get("tier", "fleet")
                if var_name not in vars:
                    vars[var_name] = EnvVar(
                        name=var_name,
                        description=meta.get("description", ""),
                        tier=tier,
                        source=f"integration/{int_name}",
                    )

        # Telegram token_env
        if bot.telegram and bot.telegram.token_env:
            var_name = bot.telegram.token_env
            if var_name not in vars:
                vars[var_name] = EnvVar(
                    name=var_name,
                    description="Telegram bot token",
                    tier="bot",
                    source="telegram",
                )

    return list(vars.values())


def _scaffold_env_merge(
    env_path: Path,
    header: str,
    required: list[EnvVar],
    log=None,
) -> None:
    """Idempotent .env scaffold: preserve existing values, append new stubs.

    Reads the file (if it exists), keeps every existing line verbatim, then
    appends stub entries for any contract vars not already present.
    Re-running is safe: no values are lost and new vars always surface.
    """
    existing_keys: set[str] = set()
    existing_content = ""
    if env_path.is_file():
        existing_content = env_path.read_text()
        existing_keys = set(dotenv.read(env_path).keys())

    new_vars = [
        ev
        for ev in sorted(required, key=lambda e: e.name)
        if ev.name not in existing_keys
    ]
    if not new_vars and existing_content:
        env_path.chmod(0o600)
        _log.debug("%s: up to date", env_path.name)
        if log is not None:
            log(f"  {env_path.name}: up to date")
        return

    lines: list[str] = []
    if existing_content:
        lines.append(existing_content.rstrip("\n"))
    else:
        lines.append(header)
        lines.append("# Generated by claudlobby generate — fill in real values.")
        lines.append("")

    if new_vars:
        lines.append("")
        for ev in new_vars:
            source_note = f" (from {ev.source})" if ev.source else ""
            lines.append(f"# {ev.description}{source_note}")
            lines.append(f"export {ev.name}=")

    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)
    if new_vars:
        new_names = ", ".join(ev.name for ev in new_vars)
        msg = f"  {env_path.name}: added {len(new_vars)} new vars ({new_names})"
        _log.info("%s: added %d new vars (%s)", env_path.name, len(new_vars), new_names)
        if log is not None:
            log(msg)


def scaffold_env_files(fleet: FleetConfig, paths: Paths, log=None) -> None:
    """Idempotent .env scaffolding at fleet and bot tiers.

    Preserves existing key=value pairs, appends stubs for any new
    contract vars discovered from MCP fragments and integrations.
    """
    env_vars = collect_env_contracts(fleet, paths)
    fleet_vars = [ev for ev in env_vars if ev.tier != "bot"]
    bot_vars = [ev for ev in env_vars if ev.tier == "bot"]

    if paths.fleet_dir:
        _scaffold_env_merge(
            paths.fleet_dir / ".env",
            f"# Fleet environment for: {fleet.name}",
            fleet_vars,
            log=log,
        )

    if bot_vars:
        for bot_name in fleet.bots:
            _scaffold_env_merge(
                paths.bot_runtime(bot_name) / ".env",
                f"# Bot environment for: {bot_name}",
                bot_vars,
                log=log,
            )


_BOOT_STAGGER_SECONDS = 3  # delay between each bot's startup on fleet boot


# ---------------------------------------------------------------------------
# Fleet-level timer generation
# ---------------------------------------------------------------------------


def _resolve_timer_schedule(timer_cfg: dict, merged_defaults: dict) -> dict:
    """Resolve timer scheduling from config.

    Returns a dict describing the schedule type:
      {"type": "interval", "seconds": 300}
      {"type": "calendar", "expression": "*-*-* 06:00:00"}
    """
    if "schedule" in timer_cfg:
        return {"type": "calendar", "expression": timer_cfg["schedule"]}
    if "interval_from" in timer_cfg:
        ref = timer_cfg["interval_from"]
        section, _, field = ref.partition(".")
        if section == "observability":
            obs = merged_defaults.get("observability", {})
            val = obs.get(field)
            if val is not None:
                return {"type": "interval", "seconds": int(val)}
    return {"type": "interval", "seconds": timer_cfg.get("interval", 300)}


def _write_timer_units(
    timers_dir: Path,
    service_name: str,
    name: str,
    sched: dict,
    script: str,
    svc_type: str,
    fleet_name: str | None,
    paths: Paths,
    *,
    persistent: bool = False,
    randomized_delay: int = 0,
    exec_args: list[str] | None = None,
    telegram_group_chat_id: str | None = None,
) -> None:
    """Write the .service/.timer/.plist units for a single timer.

    One emitter shared by the system_defaults timer loop, the opt-in
    fleet.sweep branch, and the host-global jobs (consolidate, don't fork).

    ``service_name`` is the full unit basename, computed by the caller:
    ``<service_prefix>.<name>`` for fleet timers, ``claudlobby-<name>`` for
    host singletons. ``fleet_name=None`` emits a host-scoped unit: no
    CLAUDLOBBY_FLEET env and no fleet argument on ExecStart. ``persistent`` /
    ``randomized_delay`` map to the systemd ``Persistent=`` /
    ``RandomizedDelaySec=`` timer knobs; launchd has no equivalent, so the
    plist ignores them. ``exec_args`` appends extra positional arguments after
    the fleet name on both the systemd ``ExecStart`` and the launchd
    ``ProgramArguments`` (the per-(bot,slot) briefing timers pass ``<bot> <slot>``).
    """
    scope = fleet_name if fleet_name is not None else "host"
    script_expanded = script.replace("$CLAUDLOBBY_ROOT", str(paths.root))
    exec_start = f"{script_expanded} {fleet_name}" if fleet_name else script_expanded
    if exec_args:
        exec_start = f"{exec_start} {' '.join(exec_args)}"

    # --- systemd service unit ---
    service_lines = [
        "# Generated by claudlobby — do not hand-edit.",
        "[Unit]",
        f"Description=claudlobby {name} ({scope})",
        "",
        "[Service]",
        f"Type={svc_type}",
        # Pin cwd to the install root so jobs never depend on the
        # supervisor's spawn cwd.
        f"WorkingDirectory={paths.root}",
        f"Environment=CLAUDLOBBY_ROOT={paths.root}",
    ]
    if fleet_name:
        service_lines.append(f"Environment=CLAUDLOBBY_FLEET={fleet_name}")
    # Fleet timers run in a minimal scheduler env (systemd/launchd start with
    # almost nothing). Carry the fleet Telegram group so a scheduled job can
    # deliver an alert from that env — creds-check's tg-post exits without it,
    # silently dropping the dead-credential alert while the unit still exits 0
    # (false-healthy). See lib/creds-check.sh record_and_alert.
    if fleet_name and telegram_group_chat_id:
        service_lines.append(
            f"Environment=TELEGRAM_GROUP_CHAT_ID={telegram_group_chat_id}"
        )
    service_lines.append(f"ExecStart={exec_start}")
    (timers_dir / f"{service_name}.service").write_text("\n".join(service_lines) + "\n")

    # --- systemd timer unit ---
    timer_lines = [
        "# Generated by claudlobby — do not hand-edit.",
        "[Unit]",
    ]
    if sched["type"] == "interval":
        secs = sched["seconds"]
        timer_lines.append(
            f"Description=claudlobby {name} timer ({scope}) -- tick every {secs}s"
        )
        timer_lines.extend(
            [
                "",
                "[Timer]",
                f"OnBootSec={secs}",
                f"OnUnitActiveSec={secs}",
                "AccuracySec=10",
            ]
        )
    else:
        expr = sched["expression"]
        timer_lines.append(f"Description=claudlobby {name} timer ({scope}) -- {expr}")
        timer_lines.extend(
            [
                "",
                "[Timer]",
                f"OnCalendar={expr}",
                "AccuracySec=60",
            ]
        )
    if persistent:
        timer_lines.append("Persistent=true")
    if randomized_delay:
        timer_lines.append(f"RandomizedDelaySec={randomized_delay}")
    timer_lines.extend(
        [
            "",
            "[Install]",
            "WantedBy=timers.target",
        ]
    )
    (timers_dir / f"{service_name}.timer").write_text("\n".join(timer_lines) + "\n")

    # --- launchd plist ---
    plist_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "  <key>Label</key>",
        f"  <string>{service_name}</string>",
        "  <key>ProgramArguments</key>",
        "  <array>",
        f"    <string>{script_expanded}</string>",
    ]
    if fleet_name:
        plist_lines.append(f"    <string>{fleet_name}</string>")
    for arg in exec_args or []:
        plist_lines.append(f"    <string>{arg}</string>")
    plist_lines.extend(
        [
            "  </array>",
            "  <key>EnvironmentVariables</key>",
            "  <dict>",
            "    <key>CLAUDLOBBY_ROOT</key>",
            f"    <string>{paths.root}</string>",
        ]
    )
    if fleet_name:
        plist_lines.extend(
            [
                "    <key>CLAUDLOBBY_FLEET</key>",
                f"    <string>{fleet_name}</string>",
            ]
        )
    if fleet_name and telegram_group_chat_id:
        plist_lines.extend(
            [
                "    <key>TELEGRAM_GROUP_CHAT_ID</key>",
                f"    <string>{telegram_group_chat_id}</string>",
            ]
        )
    plist_lines.append("  </dict>")
    plist_lines.extend(
        [
            "  <key>WorkingDirectory</key>",
            f"  <string>{paths.root}</string>",
        ]
    )
    if sched["type"] == "interval":
        plist_lines.extend(
            [
                "  <key>StartInterval</key>",
                f"  <integer>{sched['seconds']}</integer>",
            ]
        )
    else:
        # Parse HH:MM from OnCalendar expression (e.g. "*-*-* 06:00:00").
        cal_match = re.search(r"(\d{1,2}):(\d{2})", sched["expression"])
        hour = int(cal_match.group(1)) if cal_match else 6
        minute = int(cal_match.group(2)) if cal_match else 0
        cal_interval = ["  <key>StartCalendarInterval</key>", "  <dict>"]
        # A leading systemd weekday (e.g. "Sun *-*-* 05:00:00") maps to a launchd
        # Weekday so a weekly schedule stays weekly on macOS — without it launchd
        # fires daily at the same HH:MM. Only a single weekday is mapped; systemd
        # ranges/lists (e.g. Mon..Fri) have no single-dict launchd equivalent and
        # fall back to firing daily.
        weekdays = {
            "Sun": 0,
            "Mon": 1,
            "Tue": 2,
            "Wed": 3,
            "Thu": 4,
            "Fri": 5,
            "Sat": 6,
        }
        wd_match = re.match(r"\s*(Sun|Mon|Tue|Wed|Thu|Fri|Sat)\b", sched["expression"])
        if wd_match:
            cal_interval += [
                "    <key>Weekday</key>",
                f"    <integer>{weekdays[wd_match.group(1)]}</integer>",
            ]
        cal_interval += [
            "    <key>Hour</key>",
            f"    <integer>{hour}</integer>",
            "    <key>Minute</key>",
            f"    <integer>{minute}</integer>",
            "  </dict>",
        ]
        plist_lines.extend(cal_interval)
    plist_lines.extend(
        [
            "</dict>",
            "</plist>",
        ]
    )
    (timers_dir / f"{service_name}.plist").write_text("\n".join(plist_lines) + "\n")


def _reconcile_briefing_units(
    timers_dir: Path, prefix: str, composed: set[str], n_expected: int
) -> list[str]:
    """Prune stale ``<prefix>.briefing-*`` unit files — glob-bounded, with a
    verify-before-disable partial/degenerate guard (F3).

    The briefing family is the first dynamic per-(bot,slot) timer set, so a
    renamed/removed slot leaves an enrolled orphan that fires forever unless
    pruned. This is the generate-side half: after the current units are written,
    delete any existing ``<prefix>.briefing-*`` {service,timer,plist} absent from
    ``composed``.

    ``composed`` is the set of unit basenames just written this generate.
    ``n_expected`` is how many (bot,slot) briefing units the fleet config
    declares — the config truth a composition bug can't fake.

    Abort-on-partial (modeled on ``migrate_legacy_keepalive``'s
    verify-before-disable): if fewer units composed than config declares
    (``len(composed) < n_expected``, of which a fully-empty set is the limit
    case), a bug or an interrupted generate dropped part of the set — SKIP the
    prune, leave every unit untouched, and warn loudly, so a compose shortfall
    can never wholesale-delete live briefing timers. A legitimate full removal
    passes ``n_expected == 0`` and prunes everything. The glob bound means it can
    never touch a non-briefing unit. Returns the pruned unit basenames.
    """
    if not timers_dir.is_dir():
        return []
    existing = {p.stem for p in timers_dir.glob(f"{prefix}.briefing-*")}
    stale = existing - composed
    if not stale:
        return []
    # Partial/degenerate: config declares n_expected (bot,slot) units but fewer
    # composed — a torn or interrupted compose, not an intended teardown. Refuse
    # the prune so a shortfall can never wholesale-delete live timers. Empty is
    # the limit case (0 < n_expected); a real full removal passes n_expected == 0.
    if len(composed) < n_expected:
        _log.warning(
            "briefing reconcile: PARTIAL composed set (%d of %d declared unit(s)) "
            "— SKIPPING prune of %d existing unit(s) to avoid a compose-shortfall "
            "wholesale delete; units left untouched (re-run once composition is "
            "fixed)",
            len(composed),
            n_expected,
            len(stale),
        )
        return []
    pruned: list[str] = []
    for base in sorted(stale):
        for ext in ("service", "timer", "plist"):
            f = timers_dir / f"{base}.{ext}"
            if f.exists():
                f.unlink()
        pruned.append(base)
    return pruned


def _write_timers_manifest(
    timers_dir: Path, name: str, header: list[str], units: set[str] | list[str]
) -> None:
    """Atomically write a timers-dir sidecar manifest (``DORMANT``,
    ``BRIEFING_EXPECTED``): comment ``header`` lines followed by one unit
    basename per line, sorted.

    The setup backbone reads these mid-run (``unit_is_dormant``,
    ``reconcile_briefing_timers``), so the write is atomic (temp + ``replace``) —
    a torn file would under-list and either wrongly enroll a dormant unit or
    under-count the reconcile guard. A no-op when the timers dir does not exist
    (nothing composed, so nothing to annotate).
    """
    if not timers_dir.is_dir():
        return
    body = "\n".join([*header, *sorted(units)]) + "\n"
    tmp = timers_dir / f"{name}.tmp"
    tmp.write_text(body)
    tmp.replace(timers_dir / name)


def _write_briefing_manifest(timers_dir: Path, expected: set[str]) -> None:
    """Write the config-truth ``BRIEFING_EXPECTED`` manifest — the declared
    ``<prefix>.briefing-<bot>-<slot>`` basenames setup-fleet's reconcile reads as
    the independent expected count (a composed dir shorter than this is a
    partial/torn generate, and the live-timer disable is refused). Thin
    briefing-header wrapper over the shared atomic manifest writer.
    """
    _write_timers_manifest(
        timers_dir,
        "BRIEFING_EXPECTED",
        [
            "# Config-declared briefing (bot,slot) units. setup-fleet reconcile",
            "# refuses to disable live briefing timers when fewer than these compose.",
        ],
        expected,
    )


def compose_fleet_timers(
    fleet: FleetConfig,
    paths: Paths,
    merged_defaults: dict,
    *,
    output_dir: Path | None = None,
) -> Path:
    """Generate fleet-level systemd/launchd timer units into runtime/fleet/timers/.

    Emits the merged ``defaults.jobs`` (when system_defaults timers are enabled)
    and, independently, the opt-in ``code-audit-sweep`` timer (when fleet.sweep
    is enabled).  Returns the timers dir.

    Jobs flagged ``enroll: false`` are composed-but-dormant: their units are
    emitted like any other, but their basenames land in the ``DORMANT``
    manifest beside the units, and the setup backbone (setup-fleet,
    reconcile's job-drift audit) skips them. A fleet opts in per job via
    ``defaults.jobs.<name>.enroll: true`` in fleet.yaml.

    ``output_dir`` overrides the destination directory (the ``timers/`` subdir is
    written beneath it); it defaults to ``paths.runtime_fleet``. ``diff`` passes a
    temp dir here so it can hand this function the real ``Paths`` — keeping the
    diff and generate code paths on an identical ``Paths`` surface — while writing
    the expected units somewhere other than ``runtime/``.
    """
    timers = merged_defaults.get("jobs", {})
    sd = fleet.system_defaults
    emit_defaults = bool(sd.enabled and sd.timers and timers)
    sweep_on = fleet.sweep_enabled()
    briefing_bots = [
        (bot_id, bot)
        for bot_id, bot in fleet.bots.items()
        if bot.briefing and bot.briefing.slots
    ]
    briefing_on = bool(briefing_bots)

    base_dir = output_dir if output_dir is not None else paths.runtime_fleet
    timers_dir = base_dir / "timers"
    if not emit_defaults and not sweep_on and not briefing_on:
        # Nothing to emit — but a prior generate may have left briefing units a
        # now-removed stanza should prune. Reconcile only if the dir exists, and
        # record zero declared units so setup-fleet confirms the teardown is
        # intended (config truth) rather than a torn compose.
        _reconcile_briefing_units(timers_dir, fleet.service_prefix, set(), 0)
        _write_briefing_manifest(timers_dir, set())
        return timers_dir

    timers_dir.mkdir(parents=True, exist_ok=True)
    prefix = fleet.service_prefix

    if emit_defaults:
        for name, cfg in timers.items():
            sched = _resolve_timer_schedule(cfg, merged_defaults)
            script = cfg.get("script", "")
            svc_type = cfg.get("type", "oneshot")
            _write_timer_units(
                timers_dir,
                f"{prefix}.{name}",
                name,
                sched,
                script,
                svc_type,
                fleet.name,
                paths,
                persistent=bool(cfg.get("persistent", False)),
                randomized_delay=int(cfg.get("randomized_delay") or 0),
                telegram_group_chat_id=fleet.telegram_group_chat_id,
            )
        dormant = [
            f"{prefix}.{n}" for n, c in timers.items() if not c.get("enroll", True)
        ]
        _write_timers_manifest(
            timers_dir,
            "DORMANT",
            [
                "# Composed-but-dormant units — the setup backbone does not enroll",
                "# these. Opt in via fleet.yaml defaults.jobs.<name>.enroll: true.",
            ],
            dormant,
        )

    if sweep_on:
        # Opt-in sweep timer: synthesized from fleet.sweep, not system_defaults.
        _write_timer_units(
            timers_dir,
            f"{prefix}.code-audit-sweep",
            "code-audit-sweep",
            {"type": "calendar", "expression": fleet.sweep.schedule},
            "$CLAUDLOBBY_ROOT/lib/code-audit-sweep.sh",
            "oneshot",
            fleet.name,
            paths,
            telegram_group_chat_id=fleet.telegram_group_chat_id,
        )

    # Equippable briefing (bots.<bot>.briefing) — the first dynamic
    # per-(bot,slot) timer family. Each equipped (bot,slot) becomes a
    # <prefix>.briefing-<bot>-<slot> OnCalendar unit whose ExecStart hands the
    # slash-aware trigger `<fleet> <bot> <slot>`. Reconcile runs unconditionally
    # (even with zero briefing bots) so a removed stanza's stale units are pruned;
    # the count-check guard blocks a wholesale delete when fewer units compose
    # than config declares (a torn/partial generate), of which "none composed" is
    # only the limit case. The BRIEFING_EXPECTED manifest — the config truth a
    # partial compose can't fake — is emitted BEFORE the per-unit write loop so an
    # interrupted generate still leaves the full declared count for setup-fleet's
    # reconcile to compare a short timers dir against.
    expected_briefing = {
        f"{prefix}.briefing-{bot_id}-{slot}"
        for bot_id, bot in briefing_bots
        for slot in bot.briefing.slots
    }
    _write_briefing_manifest(timers_dir, expected_briefing)
    composed_briefing: set[str] = set()
    for bot_id, bot in briefing_bots:
        for slot, expr in bot.briefing.slots.items():
            unit = f"{prefix}.briefing-{bot_id}-{slot}"
            _write_timer_units(
                timers_dir,
                unit,
                f"briefing-{bot_id}-{slot}",
                {"type": "calendar", "expression": expr},
                "$CLAUDLOBBY_ROOT/lib/briefing-trigger.sh",
                "oneshot",
                fleet.name,
                paths,
                exec_args=[bot_id, slot],
                telegram_group_chat_id=fleet.telegram_group_chat_id,
            )
            composed_briefing.add(unit)
    _reconcile_briefing_units(
        timers_dir, prefix, composed_briefing, len(expected_briefing)
    )

    return timers_dir


def compose_host_timers(paths: Paths, *, output_dir: Path | None = None) -> Path:
    """Emit host-global singleton units from system.yaml ``host.jobs``.

    Host jobs are platform equipment with a fixed ``claudlobby-<name>``
    identity (no fleet prefix): one instance per host, package-owned, NOT
    layered through the fleet defaults merge. ``generate`` (and the
    ``host-timers`` subcommand) compose them; ``setup-system`` enrolls them.

    Returns the host timers dir (``runtime/_host/timers/`` under the repo
    root), only created when host jobs are declared.
    """
    host_jobs = load_host_jobs()
    base_dir = (
        output_dir if output_dir is not None else (paths.root / "runtime" / "_host")
    )
    timers_dir = base_dir / "timers"
    if not host_jobs:
        return timers_dir

    timers_dir.mkdir(parents=True, exist_ok=True)
    for name, cfg in host_jobs.items():
        sched = _resolve_timer_schedule(cfg, {})
        _write_timer_units(
            timers_dir,
            f"claudlobby-{name}",
            name,
            sched,
            cfg.get("script", ""),
            cfg.get("type", "oneshot"),
            None,
            paths,
            persistent=bool(cfg.get("persistent", False)),
            randomized_delay=int(cfg.get("randomized_delay") or 0),
        )
    return timers_dir


def compose_fleet(fleet: FleetConfig, paths: Paths, log=None) -> dict[str, Path]:
    paths.runtime_bots.mkdir(parents=True, exist_ok=True)

    # Scaffold shared documentation directories
    if paths.shared_docs:
        for subdir in [
            "planning/active",
            "planning/completed",
            "decisions",
            "knowledge",
            "runbooks",
        ]:
            (paths.shared_docs / subdir).mkdir(parents=True, exist_ok=True)

    out: dict[str, Path] = {}
    for i, (bot_name, bot) in enumerate(fleet.bots.items()):
        _log.info("composing %s...", bot_name)
        if log is not None:
            log(f"composing {bot_name}...")
        out[bot_name] = compose_bot(
            bot, fleet, paths, log=log, boot_delay_s=i * _BOOT_STAGGER_SECONDS
        )

    scaffold_env_files(fleet, paths, log=log)

    return out
