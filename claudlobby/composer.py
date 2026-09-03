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
import platform
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

import jinja2
import yaml
from jinja2.sandbox import SandboxedEnvironment

from . import defaults, dotenv, tool_resolve
from .config import (
    GITHUB_APP_ENV_VARS,
    BotConfig,
    FleetConfig,
    load_fleet,
    load_host_jobs,
)
from .known_values import ENV_TIERS, HEADLESS_TRIM_VARS, SHELL_IDENT_RE
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
from .paths import Paths, _iter_fleet_dirs


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


def _load_mcp_fragment(name: str, paths: Paths) -> dict | None:
    """Load + JSON-parse an MCP fragment by name, or ``None`` if it is absent.

    The single loader for a raw fragment dict — shared by :func:`compose_mcp_json`
    (which wires the servers) and the L1 source guard (which classifies the
    fragment's leaves before any output is written). Malformed JSON is a hard
    error: a fragment that will not parse cannot compose."""
    frag_path = paths.find_library_file("mcp", name, ".json")
    if frag_path is None:
        return None
    try:
        return json.loads(frag_path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        raise ValueError(f"invalid JSON in MCP fragment {frag_path}: {e}") from e


def _load_bot_fragments(bot: BotConfig, paths: Paths) -> dict[str, dict]:
    """Every present MCP fragment for a bot, keyed by name — the ``{name: frag}``
    dict the L1 source guard classifies. The one assembly point shared by
    compose_bot, the validator, and freshbox (they import it from here, so the
    fragment library layout stays a composer concern)."""
    return {
        entry.name: frag
        for entry in bot.mcp
        if (frag := _load_mcp_fragment(entry.name, paths)) is not None
    }


def compose_mcp_json(bot: BotConfig, paths: Paths) -> dict:
    """Compose one bot's .mcp.json from its merged MCP fragments; returns the config dict."""
    import shutil

    merged: dict = {"mcpServers": {}}
    for entry in bot.mcp:
        frag = _load_mcp_fragment(entry.name, paths)
        if frag is None:
            continue
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


def _has_telegram_channel(channels: list[str]) -> bool:
    """True when a bot launches with a Telegram channel.

    A channel entry names the channel either bare (``telegram``) or as a
    marketplace-pinned plugin ref (``plugin:telegram@claude-plugins-official``);
    both mean the same channel. ``channels: []`` — the documented way to declare
    a bot with no channel — yields False.
    """
    return any(
        chan.removeprefix("plugin:").split("@", 1)[0] == "telegram" for chan in channels
    )


def telegram_handle(bot: BotConfig) -> str | None:
    """This bot's Telegram handle, or None when it is not a channel bot.

    ``handle`` is optional and ``fleet.yaml`` documents the default plainly —
    "handle defaults to bot_id". TELEGRAM_STATE_DIR has applied that default
    since #43; the readers of the handle VARIABLE never did. So a fleet using
    the documented default composed a working state dir, a live bridge, and no
    TELEGRAM_BOT_HANDLE — and every consumer keyed off that variable concluded
    the bot had no channel at all: bridge_state returned `no_handle` for a
    provably live bridge, halting rolling-restart on its first bot (#1107), and
    creds-check's Telegram probe skipped every bot on the host (#1097). One
    resolver, so the default cannot drift apart again.

    Returns None for a bot with no Telegram channel AND no declared handle —
    the genuine non-channel class, which must stay expressible because
    `bridge_state`'s `no_handle` and the restart gates' non-channel contract
    (#901) both rest on it. An explicitly declared handle still wins outright,
    channel or not: it names an existing state dir, and silently re-pointing
    that path would orphan a live channel.
    """
    if bot.telegram.handle:
        return bot.telegram.handle
    if _has_telegram_channel(bot.channels):
        return bot.bot_id
    return None


GITCONFIG_FILENAME = ".gitconfig"
# Sibling of the composed .gitconfig holding ONLY the App [user] block, pulled
# in by a per-org `includeIf hasconfig:remote.*.url` from the main file when an
# App declares `orgs:` + an identity (#1300). Kept separate because includeIf
# includes a FILE; referenced relatively so no absolute path is baked.
GH_APP_IDENTITY_FILENAME = ".gitconfig-github-app-id"

# GitHub ignores the username when the password is a PAT; this is its documented
# placeholder. Hardcoding a real login here would be both PII and wrong for any
# other operator, and asking fleet.yaml for a username would be a field that can
# only ever hold one value.
_GIT_CRED_USERNAME = "x-access-token"


@functools.cache
def _resolve_gh_executable() -> str | None:
    """Absolute ``gh`` path for the composed gitconfig fallback helper, or None.

    Cached for the same reason as _resolve_claudron_executable: the location is
    host-invariant, so the per-bot compose loop resolves it once per process
    instead of re-walking PATH for every bot. Returns None rather than a bare
    ``gh`` because this lands in a gitconfig consulted by a non-login git
    process, where an unresolvable helper is a silent auth failure — omitting
    the fallback line entirely is the honest degradation.
    """
    return shutil.which("gh")


def _operator_gitconfig() -> Path:
    """The operator's own ~/.gitconfig, included by each composed bot gitconfig.

    A named seam rather than an inline Path.home() so tests can point the include
    at a fixture instead of string-editing composed output.
    """
    return Path.home() / ".gitconfig"


def _gh_app_user_block(app: "GithubAppConfig") -> str:
    """The App ``[user]`` name/email lines — the one place the ``<slug>[bot]``
    noreply format lives, shared by the inline (host-generic) and fragment
    (per-org) identity paths so they cannot drift."""
    return (
        "[user]\n"
        f"\tname = {app.slug}[bot]\n"
        f"\temail = {app.bot_user_id}+{app.slug}[bot]@users.noreply.github.com\n"
    )


def compose_bot_gitconfig_app_identity(bot: BotConfig) -> str | None:
    """The App ``[user]`` fragment for an ORG-SCOPED App identity, or None.

    Returned as its own file only when the bot's ``github_app`` declares BOTH
    an identity (slug + bot_user_id) AND ``orgs`` — the per-org case, where
    ``compose_bot_gitconfig`` references it from a ``includeIf`` keyed on the
    repo remote. A host-generic App keeps its identity inline in the main
    gitconfig (no fragment), and a bot with no App identity has none.
    """
    app = bot.github_app
    if not (app and app.composes_identity and app.orgs):
        return None
    return (
        "# Generated by claudlobby — do not hand-edit. Pulled in by a per-org\n"
        "# includeIf from .gitconfig for repos whose remote is an App org.\n"
        + _gh_app_user_block(app)
    )


def compose_bot_gitconfig(bot: BotConfig, paths: Paths) -> str | None:
    """Render one bot's git credential routing, or None when it declares none.

    Covers BOTH declaration surfaces: per-org PAT routing (``git_credentials``)
    and GitHub App routing (``github_app``, App-auth P3 #1273). The App block
    references ``lib/git-credential-github-app`` by BAKED ABSOLUTE PATH (the
    same precedent as the operator-gitconfig include and the resolved gh
    fallback — gitconfig files do not expand env vars), layers ``cache
    --timeout=3000`` in front so a mint is amortized across git operations,
    adds the ssh→https ``insteadOf`` rewrite (App tokens are HTTPS-only; an
    ssh remote would silently bypass the helper), and — iff ``slug`` AND
    ``bot_user_id`` are both declared — overrides the commit identity to
    ``<slug>[bot]`` AFTER the operator include (later keys win; F-identity
    variant a). PAT-beats-App falls out of section ORDER (first-both-fields
    wins): explicit per-org PAT sections precede the App block.

    The gh fallback is emitted ONLY when the App block is org-scoped: for a
    host-generic App, gh could only ever answer after an App-helper failure —
    silently substituting the operator identity, the exact class D11/quit=1
    exists to stop. (The helper also emits ``quit=1`` on hard failure, so even
    org-scoped chains fail loudly for App-covered URLs.)

    Why this exists: fine-grained PATs are single-resource-owner, so a bot that
    pushes to two orgs needs two tokens — and ``gh auth setup-git`` installs a
    host-wide helper for ``https://github.com`` that answers first for every org,
    presenting the wrong token as a misleading 403. Full root cause:
    documentation/plans/2026-07-27-per-org-git-credential-routing.md.

    Three ORDERING properties are load-bearing; a reorder breaks routing
    SILENTLY, so tests/test_composer.py pins each one separately:

    1. ``[include]`` of the operator's config FIRST — preserves user.name /
       user.email per the git-identity-no-overrides guardrail. Replacing global
       config instead of extending it would violate it.
    2. An empty ``helper =`` reset AFTER that include — the include drags in
       gh's own reset+helper pair, and without re-resetting, gh's helper is
       first in the list and wins for every org.
    3. Org-scoped helper BEFORE the generic fallback — git takes the first
       helper that answers. Ordering *is* the selection mechanism; there is no
       most-specific-wins rule.

    Ordering-sensitivity is safe to build on because both semantics it rests on
    are git's DOCUMENTED contract, not incidental behaviour — ``gitcredentials``
    specifies first-both-fields-wins and empty-resets-the-list, with wording
    unchanged from v2.20.0 to today, and ``credential_apply_config()`` sets
    ``select_fn = select_all``, explicitly opting credential config out of
    urlmatch's most-specific-wins arbitration.

    ``useHttpPath = true`` is a fourth line but is NOT one of the three: it does
    not decide whether an org-scoped section matches. The repo path is already in
    the credential context at match time, because ``credential_apply_config()``
    reads config — and does its URL matching — BEFORE the ``!use_http_path`` strip
    that follows it. That read-then-strip order is a git *internal*, though, where
    the two semantics above are documented; setting ``useHttpPath`` turns the one
    undocumented dependency in this design into a configured guarantee. Its actual
    effect is to forward ``path=`` to the helper. In App mode that is no longer
    free of consequence: the ``cache`` layer IS a storage-backed helper, and
    with the path forwarded it keys one cached entry per repo — so the mint
    amortization is per-repo, not per-host (D3). Accepted: correctness is
    unchanged (every entry is a valid token) and the extra mints are bounded
    by repo count per 50-minute window.

    The helper embeds ``$VAR``, never a token: the value is read from the
    process env at push time, so this file carries no secret.

    The ``[include]`` target is an EXTERNAL host file this composed artifact
    depends on. git ignores a missing include path silently, which would leave
    routing working while ``user.email`` vanished, so its existence is asserted
    by freshbox (fail) and by the validator (warn) rather than left to surface as
    a confusing ``rc=128`` at the first commit.
    """
    if not (bot.git_credentials or bot.github_app):
        return None

    out = f"""# Generated by claudlobby — do not hand-edit (regenerated by `claudlobby generate`).
# Per-org / GitHub App git credential routing. See
# documentation/plans/2026-07-27-per-org-git-credential-routing.md

# The operator's own config first — identity, aliases, everything else.
[include]
\tpath = {_operator_gitconfig()}

# Keep the repo path in the credential context and forward it to helpers, then
# discard whatever helper the include installed (gh auth setup-git writes its
# own reset + helper pair in there).
[credential "https://github.com"]
\tuseHttpPath = true
\thelper =
"""
    for org, env_name in sorted(bot.git_credentials.items()):
        out += f"""
# {org} -> ${env_name} (value read from the env at push time).
[credential "https://github.com/{org}"]
\thelper = "!f(){{ echo username={_GIT_CRED_USERNAME}; echo password=${env_name}; }};f"
"""
    app = bot.github_app
    if app:
        app_helper = paths.root / "lib" / "git-credential-github-app"
        # One fork for every scoped-vs-generic artifact below: None = all of
        # github.com. App tokens are HTTPS-only either way, which is what the
        # insteadOf rewrites are for.
        scopes = sorted(app.orgs) or [None]
        out += """
# GitHub App routing: cache amortizes the ~1h installation-token mint across
# git operations; the helper is referenced by absolute path so nothing needs
# to be on PATH. On hard failure it emits quit=1 (loud, never a fall-through
# to another identity).
"""
        for org in scopes:
            target = f"https://github.com/{org}" if org else "https://github.com"
            out += f"""[credential "{target}"]
\thelper = cache --timeout=3000
\thelper = {app_helper}
"""
        if app.composes_identity:
            if app.orgs:
                # PER-ORG identity (#1300): the App identity applies ONLY in
                # repos whose remote is one of the App's orgs; every other repo
                # keeps the operator identity from the include above. Keyed on
                # the repo REMOTE via `includeIf hasconfig:remote.*.url` (git
                # >= 2.36), not the checkout path — so a multi-org bot commits
                # as <slug>[bot] on the App's org and as the operator elsewhere,
                # WITHOUT the App needing access to those other orgs. The
                # identity lives in a sibling fragment file (compose_bot writes
                # it); the include is relative so no absolute path is baked.
                for org in sorted(app.orgs):
                    # hasconfig matches the RAW remote.*.url, NOT the
                    # insteadOf-rewritten transport — so a checkout whose remote
                    # is still ssh-form (git@github.com:org/… or ssh://…) pushes
                    # fine via the App helper but would otherwise author as the
                    # operator. Match all three spellings so identity follows the
                    # org regardless of how the remote was cloned.
                    for url in (
                        f"https://github.com/{org}/**",
                        f"git@github.com:{org}/**",
                        f"ssh://git@github.com/{org}/**",
                    ):
                        out += f"""
# App commit identity, scoped to {org} repos by their remote URL.
[includeIf "hasconfig:remote.*.url:{url}"]
\tpath = ./{GH_APP_IDENTITY_FILENAME}
"""
            else:
                # Host-generic App: the whole host is the App, so the identity
                # is global (AFTER the operator include, so these keys win). The
                # noreply address maps to the App bot user, so commits attribute
                # to the App, not the operator.
                out += (
                    "\n# App commit identity (F-identity variant a) — AFTER the "
                    "operator include, so these keys win.\n"
                    + _gh_app_user_block(app)
                )
        for org in scopes:
            prefix = f"{org}/" if org else ""
            # Both remote spellings bypass the https credential layer: scp-form
            # (git@github.com:org/) and ssh:// (ssh://git@github.com/org/).
            out += f"""
[url "https://github.com/{prefix}"]
\tinsteadOf = git@github.com:{prefix}
\tinsteadOf = ssh://git@github.com/{prefix}
"""

    # Omitted when gh is absent rather than emitting a bare name a non-login git
    # process would fail to resolve (_resolve_claudron_executable pattern).
    # Also omitted for a HOST-GENERIC App block: there gh could only answer
    # after an App-helper failure — a silent operator-identity substitution.
    gh_bin = _resolve_gh_executable()
    if gh_bin and not (app and app.host_generic):
        out += f"""
# Every other org keeps the host default (gh's own credential helper).
[credential "https://github.com"]
\thelper = !{gh_bin} auth git-credential
"""
    return out


def compose_bot_conf(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> str:
    """Render one bot's bot.conf (env exports sourced at startup); returns the file text."""
    # Defense-in-depth: validate identifiers embedded in double-quoted lines
    # that require shell variable expansion ($HOME, $CLAUDLOBBY_ROOT).
    if not _SAFE_NAME_RE.match(bot.bot_id):
        raise ValueError(f"bot_id {bot.bot_id!r} contains shell-unsafe characters")
    if not _SAFE_NAME_RE.match(bot.name):
        raise ValueError(f"bot name {bot.name!r} contains shell-unsafe characters")
    # Resolved once, consumed by both the state-dir path and the handle export
    # below — a second hand-rolled `handle or bot_id` here is exactly the drift
    # that produced #1097/#1107. `tg_channel_handle` is None for a bot with no
    # Telegram channel; `tg_handle` is the slug, which the state-dir path always
    # needs a value for even then.
    tg_channel_handle = telegram_handle(bot)
    tg_handle = tg_channel_handle or bot.bot_id
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
        # Exported, unlike the bot.conf vars above: lib/ reads those from the
        # file via bot_conf_get, but this one must reach the `claude` CHILD
        # process environment. The telegram plugin resolves its state dir from
        # process.env.TELEGRAM_STATE_DIR and silently falls back to the shared
        # ~/.claude/channels/telegram when unset — which parks bot.pid outside
        # this bot's dir, and the plugin enforces a singleton per state dir, so
        # only one bot per host can then hold a poller. lib-common.sh's ownership
        # check greps the poller's environ for this exact KEY=VALUE, so the
        # export is a contract between compositor and consumer (#976).
        f'export TELEGRAM_STATE_DIR="$HOME/.claude/channels/telegram-{tg_handle}"',
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
    # Resolved, not raw: a fleet that relies on the documented "handle defaults
    # to bot_id" otherwise composes no handle at all, and every consumer reading
    # this variable then classifies a live channel bot as non-channel (#1097,
    # #1107). A bot with no Telegram channel still emits nothing — that is the
    # signal `bridge_state`'s `no_handle` and #901's gate contract rest on.
    if tg_channel_handle:
        lines.append(f"export TELEGRAM_BOT_HANDLE={_shq(tg_channel_handle)}")

    # The DECLARED BotFather @username — emitted ONLY when the fleet spelled a
    # handle out, and deliberately a separate variable from TELEGRAM_BOT_HANDLE.
    # The handle is channel identity (a state-dir slug that now defaults to
    # bot_id); a slug is not a username. creds-check's cross-wire check compares
    # getMe's answer against a username, so it must read a value that is absent
    # when we were never told one — otherwise it compares a slug and declares a
    # correctly-wired bot cross-wired, edge-alerting once per bot (#1095).
    # "Was a username declared?" is a fact known here; making bash re-derive it
    # by comparing the handle against BOT_ID would couple it to this default,
    # and silently resume false-paging if the default ever moved.
    if bot.telegram.handle:
        lines.append(f"export TELEGRAM_BOT_USERNAME={_shq(bot.telegram.handle)}")

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
            obs.unassigned_check,
            obs.unassigned_threshold,
            obs.unassigned_max_age,
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
        if obs.unassigned_check is not None:
            # Same shell-boolean rule as bridge_heal above: fleet-pulse gates on
            # the string "1", so _shq(bool) would render "True" and leave the
            # check silently off.
            lines.append(
                f"export OBSERVABILITY_UNASSIGNED_CHECK={'1' if obs.unassigned_check else '0'}"
            )
        if obs.unassigned_threshold is not None:
            lines.append(
                f"export OBSERVABILITY_UNASSIGNED_THRESHOLD={_shq(obs.unassigned_threshold)}"
            )
        if obs.unassigned_max_age is not None:
            lines.append(
                f"export OBSERVABILITY_UNASSIGNED_MAX_AGE={_shq(obs.unassigned_max_age)}"
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
        if bot_is_vault_wired(bot):
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

    # A well-formed anchored path (${FLEET_ROOT}/x, $BOT_DIR/y — anchor head, then
    # only path-safe segments) emits DOUBLE-quoted so the shell expands the
    # composer anchor at source time — mirrors the secret_files precedent below.
    # The default single-quoted _shq sink would freeze the anchor as a literal,
    # leaving the 'anchor it' remediation inert. Gating on is_safe_anchored_path
    # (not the looser is_anchor_headed) is the emission-side of #731: an
    # anchor-headed value with a shell metacharacter after the anchor is denied at
    # the source guard, but if one ever reaches here it falls back to the frozen
    # _shq sink rather than a double-quoted line that would execute on source.
    # Every non-anchor value stays _shq (byte-identical to before).
    from .path_audit import is_safe_anchored_path

    for k, v in bot.env.items():
        if not _SHELL_IDENT_RE.match(k):
            raise ValueError(f"bot.env key {k!r} is not a valid shell identifier")
        rhs = f'"{v}"' if is_safe_anchored_path(v) else _shq(v)
        lines.append(f"export {k}={rhs}")

    # Secret-file paths: fleet-relative by contract, anchored on FLEET_ROOT so the
    # path is composer-derived and moves with the fleet. An absolute (or
    # parent-escaping) value is the exact hand-typed dangling-path smell this
    # closes → reject it loudly rather than bake it in. The path is emitted
    # double-quoted, so a shell metacharacter in the subpath would execute when
    # bot.conf is sourced — is_safe_relative_subpath closes that too (#731).
    from .path_audit import is_safe_relative_subpath

    for var, subpath in bot.secret_files.items():
        if not _SHELL_IDENT_RE.match(var):
            raise ValueError(
                f"bot.secret_files key {var!r} is not a valid shell identifier"
            )
        if (
            subpath.startswith(("/", "~"))
            or ".." in Path(subpath).parts
            or not is_safe_relative_subpath(subpath)
        ):
            raise ValueError(
                f"bot.secret_files[{var!r}] must be a fleet-relative path of plain "
                f"path segments, got {subpath!r} — no absolute/~ head, no '..', and "
                "no shell metacharacter (the composer anchors it on FLEET_ROOT and "
                "emits it double-quoted; a $() or backtick would execute on source)"
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

    # Git credential routing — point git at the composed per-org gitconfig. Only
    # when the bot declares credentials, so fleets that declare none compose
    # byte-identically to before. The composed file re-includes the operator's
    # ~/.gitconfig, so this extends global config rather than replacing it.
    if bot.git_credentials or bot.github_app:
        lines.append("")
        lines.append("# Git — per-org / GitHub App credential routing (composed .gitconfig)")
        lines.append(f'export GIT_CONFIG_GLOBAL="$BOT_DIR/{GITCONFIG_FILENAME}"')
    if bot.github_app:
        # The composed tools/gh shim must SHADOW the system gh so per-call
        # App minting is mechanical, not remembered (#1283 R6).
        lines.append("# tools/gh shim shadows system gh — per-call App minting")
        lines.append('export PATH="$BOT_DIR/tools:$PATH"')

    lines.append("")
    if bot.startup_prompt:
        rendered = _render_startup_prompt(bot.startup_prompt, bot, fleet)
        lines.append(f"STARTUP_PROMPT={json.dumps(rendered)}")
    else:
        lines.append(
            'STARTUP_PROMPT="Welcome back. Read your CLAUDE.md. Idle and await Telegram messages."'
        )

    read_flags = _read_flag_env(paths, fleet.name)
    if read_flags:
        lines.append("")
        lines.append("# Cutover read flags (chunk 5): the SESSION carrier for PLANE_READ_* —")
        lines.append("# resolved from the fleet .env tier at compose time, because start-bot.sh")
        lines.append("# exports bot.conf and sources the tiers without export. A flag here is")
        lines.append("# half a flip: the matcher serves the plane only once `plane cutover` declared it.")
        for k, v in sorted(read_flags.items()):
            lines.append(f"export {k}={_shq(v)}")
        lines.append("")
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


def _scheduler_tool_path(root: Path | None = None) -> str:
    """PATH for composed timer units so their jobs can resolve fleet tools.

    systemd and launchd hand a scheduled job a minimal PATH that excludes
    Homebrew and the per-user npm/bun/.local bins where ``claude`` and
    ``claudlobby`` install — so a timer that shells out to them (e.g.
    reload-fleet's ``claude plugin update``) dies 'command not found' and fires
    a false failure alert. Mirror lib/start-bot.sh:49's runtime construction so
    a tool resolves identically under a timer as in a bot session, resolved at
    compose time: ``$HOME`` → the composing user's home; Homebrew → the host
    layout (lib/lib-common.sh detect_os — /opt/homebrew on Apple Silicon,
    /usr/local on Intel, absent off macOS). On Intel the Homebrew segment
    intentionally re-collides with the leading /usr/local/bin, matching
    start-bot.sh's own duplication — a harmless, deliberate mirror.

    ``root`` appends ``<root>/.venv/bin`` (#805). The user-prefix segments above
    cover ``claude``, but ``claudlobby`` is a *console script* whose location
    depends on which python ran ``pip install -e .`` — with a repo-local venv it
    lands in ``.venv/bin`` and on no system PATH at all, so a timer fixed for
    ``claude`` alone still died one line later on ``claudlobby generate``.
    """
    home = Path.home()
    segments = [
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        f"{home}/.local/bin",
        f"{home}/.bun/bin",
        f"{home}/.npm-global/bin",
    ]
    if platform.system() == "Darwin":
        segments.append(
            "/opt/homebrew/bin" if Path("/opt/homebrew").is_dir() else "/usr/local/bin"
        )
    if root is not None:
        segments.append(f"{root}/.venv/bin")
    return ":".join(segments)


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
#
# LOAD-BEARING BEYOND CLEANUP: Type=simple + a spawner ExecStart that exits +
# RemainAfterExit=yes is what makes SubState a boot-progress signal, and
# service_is_starting (lib-common.sh) reads it as one:
#   activating      ExecStartPre — the boot stagger sleep
#   active/running  start-bot.sh executing; tmux session not up yet
#   active/exited   steady state — spawner done, tmux running underneath
# If ExecStart ever execs into a foreground process, or RemainAfterExit is
# dropped, active/running becomes the STEADY state and keepalive's dead-session
# watchdog silently stops restarting anything — a failure shaped exactly like a
# healthy fleet. tests/test_composer.py asserts this triple; do not relax it
# without reading service_is_starting first.
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
        params = tool_resolve.resolve_tool_params(
            entry.name, manifest, entry.params, bot.external_paths
        )
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

    # App-mode gh shim (App-auth P3, #1283 R6): per-call minting must be
    # MECHANICAL, not remembered — a forgotten GH_TOKEN prefix on an App bot
    # silently rides the operator PAT otherwise. Composer-owned content (like
    # the gitconfig), injected here so tools/ reconciliation manages its
    # lifecycle: rendered while github_app is declared, reaped when dropped.
    if bot.github_app:
        if "gh" in outputs:
            raise ValueError(
                f"bot '{bot.bot_id}': github_app composes a tools/gh shim, but a "
                "declared tool also renders 'gh' — rename the tool or disable "
                "github_app for this bot"
            )
        outputs["gh"] = _GH_APP_SHIM
    return outputs


# The composed tools/gh shim. Runtime-only logic (no Jinja): the mint CLI
# resolves through $CLAUDLOBBY_ROOT (always exported first in bot.conf), and
# the REAL gh is found by stripping this shim's own directory from PATH.
# Mint failure is LOUD (exit 1) — falling through to gh's own keychain auth
# would be the silent operator-identity substitution the helper's quit=1
# exists to stop, one surface over.
_GH_APP_SHIM = """#!/bin/bash
# Generated by claudlobby — do not hand-edit (regenerated by generate).
# gh with the App identity, mechanically (App-auth P3). Mints per call (F5):
# the token dies in about an hour, so nothing is exported at boot.
set -euo pipefail
# Real gh resolved at RUNTIME, deliberately not the composer-baked path the
# gitconfig fallback uses (_resolve_gh_executable): the shim only ever runs
# after being FOUND via PATH, so PATH is the full session PATH by
# construction — and a bake would go dead (or compose no shim at all) on a
# host that installs gh after generate, while runtime resolution self-heals.
# Resolved FIRST so a gh-less host fails fast instead of burning a network
# mint and a live token on a doomed call. The tr/grep strip removes EVERY
# occurrence of this dir; a naive ${PATH//...} single pass leaves one entry
# behind on adjacent duplicates and exec self-loops (measured).
_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_clean="$(printf '%s' "$PATH" | tr ':' '\\n' | grep -v -F -x "$_self_dir" | tr '\\n' ':')"
_clean="${_clean%:}"
_real="$(PATH="$_clean" command -v gh || true)"
if [ -z "$_real" ]; then
    printf 'gh (App shim): no real gh on PATH beyond this shim\\n' >&2
    exit 127
fi
if [ -z "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]; then
    if ! GH_TOKEN="$("$CLAUDLOBBY_ROOT/lib/mint-github-token.sh")"; then
        printf 'gh (App shim): mint failed — refusing to fall through to another identity; see stderr above\\n' >&2
        exit 1
    fi
    export GH_TOKEN
fi
exec "$_real" "$@"
"""


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
    # DELIBERATELY the raw handle, not telegram_handle(bot) — do not "fix" this
    # to match the other three sites without reading #1107 first. Composing
    # access.json for a fleet that never had one is not additive: the reconcile
    # path below OVERWRITES groups.<chat_id>.requireMention from fleet config.
    # Measured on a live 9-bot fleet, applying the default here flipped the
    # manager's own group from requireMention:false to true — a manager that no
    # longer sees un-mentioned messages is a dispatch outage. Extending the
    # default here therefore needs each fleet's require_mention reconciled to
    # its live access.json FIRST. Tracked separately; the other sites only ever
    # add capability, which is why they lead and this one does not.
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


def resolve_effective_protocols(
    bot: BotConfig, fleet: FleetConfig, paths: Paths, *, is_manager: bool
) -> list[str]:
    """The protocols a bot is ACTUALLY composed with: the declared list plus
    the system defaults that are available to this fleet (manager-role
    defaults only for a manager), then the fleet-fit exclusions applied to
    the FINAL list. ONE definition: the compose path and the plane's
    registry keyframe both call this, so what the registry records is what
    the bot runs with — a keyframe of the declared-only list read every
    default protocol as "unused" on every overlay fleet (#1405 gauntlet).

    The exclusions run OUTSIDE the defaults branch, because the hazard is
    the DECLARED half — which the availability gate never sees. Measured
    before that line existed: a vault-wired fleet declaring
    `shared-documentation` composed the hand-scan form, the vault form and
    the template's vault section — three `Shared Documentation` headings
    with opposite instructions (#1172 in a worse form). Opting out of the
    defaults must not re-admit a declared form that does not fit the fleet.
    """
    facts = defaults.Facts(
        shared_docs=paths.shared_docs is not None,
        vault_wired=bot_is_vault_wired(bot),
    )
    protocol_names = list(bot.protocols)
    sd = fleet.system_defaults
    if sd.enabled and sd.protocols:
        roles = (defaults.ROLE_MANAGER,) if is_manager else ()
        for name in defaults.resolve("protocols", roles):
            if defaults.available(name, facts) and name not in protocol_names:
                protocol_names.append(name)
    return defaults.resolve_exclusions(protocol_names, facts)


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
    """Compose one bot's CLAUDE.md from expertise, voice, protocols, and guardrails; returns the markdown."""
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

    teams = fleet.teams_for_manager(bot.bot_id)
    org_structure = _compose_org_structure(bot, fleet)

    is_manager = bot.bot_id in fleet.manager_bots()

    # INSTRUCT-tier protocol defaults, resolved from defaults.REGISTRY rather
    # than named here. This used to be a literal `append("shared-documentation")`
    # sitting downstream of the merge the registry feeds, which made it a default
    # no fleet could see and none could switch off (#1168 Phase 3, finding 1).
    #
    # Applied HERE and not in load_fleet's merge — where `guardrails` is applied
    # — because these entries are availability-gated on facts the config layer
    # never receives. Root mode has no shared docs, so an ungated default would
    # newly instruct every root-mode bot.
    #
    # `vault_wired` reads the same field `claude.md.j2` branches on, and the two
    # must not diverge on WHICH FORM a bot is told to use: divergence means the
    # vault section beside the hand-scan protocol, which is #1172. The template
    # carries the reciprocal note.
    #
    # They are NOT the same condition, deliberately. The template is
    # `vault else shared_docs`; this gate is `shared_docs and vault`. Root mode
    # plus a vault therefore composes the door section and no protocol — correct,
    # since root mode has no fleet doc tree, and stated here because a comment
    # claiming the two predicates are identical would mislead exactly when it
    # matters. `tests/test_shared_docs_default.py` pins the agreement on the
    # composed FILE rather than at the predicate, so it holds however either
    # side is spelled.
    protocol_names = resolve_effective_protocols(
        bot, fleet, paths, is_manager=is_manager)

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


# --- #904 M1: the SessionStart boot brief (epic #1102 R3, locked fork R3-F1) --


# The platform default hook timeout is 600s; an explicit budget keeps a hung
# door from delaying context assembly for ten minutes. 10s is ~11x the measured
# warm floor; whether it survives host-reboot load is what the canary's
# per-boot hook p95 measures (R3-F1 correction: measure-then-gate — if p95
# approaches this, the held #1123 lazy-import branch lands before arming).
BRIEF_HOOK_TIMEOUT_S = 10


@functools.cache
def _brief_cli_probe() -> tuple[str | None, str]:
    """(certified executable, "") — or (None, why) when arming must refuse.

    The compose-time arming gate: a subprocess probe of the PATH-resolved
    binary, never an import check of THIS package — composed settings outlive
    installs on this estate (the merged-but-not-installed gap was measured
    live during R3-F1 ratification, #1102), so probing our own parser would
    certify the wrong artifact. The returned path is composed into the hook
    verbatim (the C2 absolute-path contract `_resolve_claudron_executable`
    states for this same settings file), so the certified artifact and the
    runtime artifact are one object.

    The ``--boot`` containment check reads argparse's own generated help — the
    option's self-description, not a source grep. Cached: host-invariant
    within one compose process, and it runs per armed bot across generate,
    validate, and freshbox otherwise.
    """
    import shutil
    import subprocess

    exe = shutil.which("claudlobby")
    if exe is None:
        return None, "no `claudlobby` on PATH at compose time"
    try:
        r = subprocess.run(
            [exe, "brief", "--help"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"probing `claudlobby brief --help` failed: {e}"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        return None, (
            tail[-1] if tail else f"`claudlobby brief --help` exited {r.returncode}"
        )
    if "--boot" not in r.stdout:
        return None, "installed `claudlobby brief` predates the --boot mode"
    return exe, ""


def _with_brief_boot_hook(
    hooks: dict[str, list[dict[str, Any]]],
    bot_id: str,
    exe: str,
    fleet: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return a copy of the flat fleet.yaml-shaped hooks with the boot-brief
    SessionStart entries appended (matchers ``startup`` and ``compact`` only —
    resume is #986's lane, /clear is deferred with an un-cut condition, and
    ``fork`` is excluded deliberately; fork R3-F1 rule 9).

    The command is fail-open BY SHAPE: the platform cannot let a SessionStart
    hook block the session, but a non-zero exit discards stdout — so the door
    failing must itself exit 0 with one line naming the door, or the boot
    carries nothing at all. `$?` expands at hook runtime, in the hook's shell.

    ``fleet`` is the overlay name for overlay-mode composes, None in root
    mode. It must be threaded into the command as the global ``--fleet``
    flag: fleet selection has no env or cwd fallback (#1197), so a flagless
    door on an overlay estate resolves root-mode and exits 1 on every boot.
    """
    # The EXECUTED path is the certified absolute exe (C2: hook context is not
    # a login shell, PATH frequently omits a venv/pipx install — see
    # _resolve_claudron_executable); the human-facing fallback text keeps the
    # bare name, since it is an instruction to a reader, not an invocation —
    # but both carry --fleet, or the instruction teaches a broken door (#1197).
    fleet_arg = f" --fleet {fleet}" if fleet else ""
    fallback = (
        f'echo "fleet-brief unavailable (rc $?) — '
        f'claudlobby{fleet_arg} brief --bot {bot_id}"'
    )
    cmd = f"{exe}{fleet_arg} brief --bot {bot_id} --boot || {fallback}"
    out = {k: list(v) for k, v in hooks.items()}
    entries = out.setdefault("SessionStart", [])
    for matcher in ("startup", "compact"):
        entries.append(
            {"command": cmd, "matcher": matcher, "timeout": BRIEF_HOOK_TIMEOUT_S}
        )
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


def bot_is_vault_wired(bot: BotConfig) -> bool:
    """Whether this bot reaches knowledge through a Claudron vault.

    Trivial, and it exists because its ABSENCE was the finding (#1172). Three
    places decide this one fact — `claude.md.j2`'s Shared Documentation branch,
    the `CLAUDRON_VAULT_PATH` export in `bot.conf`, and the availability gate on
    the `shared-documentation*` protocol defaults — and they must agree. When
    they disagree the bot is told to use the vault door AND to hand-scan the
    tree the door forbids opening, which is the defect this named the fix for.

    NOT `_session_loop_enabled`, which is a different question wearing a similar
    shape: that one is tri-state and an explicit `claudron_session_loop: false`
    wins over a wired vault. Gating the protocol on it would give a vault-wired
    bot with the session loop off `shared_docs and not vault_wired` — the
    HAND-SCAN form. That does not merely drop the vault protocol; it re-creates
    #1172 exactly, on the bots least likely to be looked at.
    """
    return bool(bot.claudron_vault_path)


def _session_loop_enabled(bot: BotConfig) -> bool:
    """Resolve the tri-state ``claudron_session_loop``: an explicit value wins;
    unset defaults True exactly when the bot is vault-wired.

    The fallback goes through ``bot_is_vault_wired`` so "exactly when the bot is
    vault-wired" is true by construction rather than by two copies of the same
    expression agreeing today. `validator.py` already resolves a vault a richer
    way (`detect_vault`); if that ever becomes the definition, this follows.
    """
    if bot.claudron_session_loop is not None:
        return bot.claudron_session_loop
    return bot_is_vault_wired(bot)


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

    Keyed off the CHANNEL, not the handle: the precondition for granting the
    Telegram plugin tools is that the plugin is loaded, which is what `channels`
    says. Gating on the handle field was wrong in both directions — it granted
    nothing to a fleet using the documented handle default (these tools reached
    zero bots, masked only by permission-mode auto), and it would over-grant to
    a bot that keeps a declared handle while turning its channel off.
    """
    tools: list[str] = []
    if _has_telegram_channel(bot.channels):
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
    # dir. Two rules, both DOUBLE-slash, and every part of that shape is a fix
    # for a measured defect (#1312, #873).
    #
    # WHY `//` (#1312). A permission-rule path with a SINGLE leading slash anchors
    # at the SETTINGS SOURCE, not the filesystem root — so an absolute path names
    # a directory that never exists and the rule is inert. Measured directly in a
    # scratch project with a trust seed and a no-rule control:
    #
    #   Read(/target/**)      vs  <project>/target/inside.txt  -> BLOCKED
    #       ^ the mechanism: a bare /path really is project-relative
    #   Read(/abs/target/**)  vs  /abs/target/secret.txt       -> NOT blocked
    #   Read(//abs/target/**) vs  /abs/target/secret.txt       -> BLOCKED
    #   Read(//abs/target/**) vs  /abs/target/nested/deep.txt  -> BLOCKED
    #
    # WHY NO `Write(...)` (#873). Claude Code accepts a Write(path) rule and never
    # consults it — file permission checks read Read and Edit only, and it says so
    # in a warning on every boot, once per sibling. Dropping it removes N-1
    # warnings per bot per boot and loses no enforcement: Edit rules cover every
    # file-editing tool, Write included.
    #
    # WHAT #873's "isolation is intact" GOT HALF RIGHT, measured: bare-absolute
    # EDIT is NOT inert — it blocks (twice, with a no-rule control proving the rule
    # caused it, not a workspace boundary). So on a trusted workspace the integrity
    # half held all along via the Edit rule; it was the confidentiality half that
    # was missing, because bare-absolute Read is inert. Read and Edit do not
    # resolve a bare-absolute path the same way.
    siblings = [bid for bid in fleet.bots if bid != bot.bot_id]
    for sibling in siblings:
        sibling_dir = str(paths.bot_runtime(sibling))
        deny_patterns.extend(
            (
                f"Read(/{sibling_dir}/**)",
                f"Edit(/{sibling_dir}/**)",
            )
        )

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

    # L1 source guard (#702) — an ALLOW grant carrying an absolute path must
    # anchor it (${FLEET_ROOT}/…) or declare it (external_paths), same as any
    # other source value: granting access to a raw absolute is the self-containment
    # violation (it dangles the moment the fleet runs elsewhere). Deny grants are
    # restrictions, not wiring-to-a-path, so they are not classified — which also
    # sidesteps the composer-derived sibling-isolation denies above (a resolved
    # absolute the composer emits, never a source). No composer-derived allow layer
    # emits an absolute, so every flagged allow traces to a real source. Fires
    # before the settings file is written; the whole-bot no-partial-output
    # guarantee is the dataclass+fragment gate before mkdir, and generate is
    # idempotent (mkdir exist_ok, writes overwrite) with compose_fleet surfacing
    # every offending bot, so a late-caught grant re-generates cleanly once fixed.
    from .path_audit import (
        SourceFinding,
        classify_grant_paths,
        source_findings_error,
    )

    grant_findings: list[SourceFinding] = []
    for grant in allow_patterns:
        for denied in classify_grant_paths(grant, bot.external_paths):
            grant_findings.append(
                SourceFinding(
                    bot_id=bot.bot_id,
                    source=f"bots.{bot.bot_id} grant {grant}",
                    value=grant,
                    path=denied,
                    reason="grant path",
                )
            )
    if grant_findings:
        raise source_findings_error(bot.bot_id, grant_findings)

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
    bot_hooks = bot.hooks
    if bot.brief_on_start:
        exe, why = _brief_cli_probe()
        if exe is None:
            raise ValueError(
                f"bot {bot.bot_id}: brief.on_start is set but the installed "
                f"CLI cannot serve the hook ({why}). Composed settings outlive "
                "installs on this estate — arming now would compose a hook "
                "whose every boot prints a failure line instead of a brief. "
                "Pull/install a claudlobby with `brief --boot`, or disarm the "
                "knob."
            )
        # Bare overlay name, not fleet.name: --fleet resolves the DIRECTORY
        # (F5 global uniqueness is _find_fleet_dir's own contract), and the
        # two are not guaranteed equal.
        bot_hooks = _with_brief_boot_hook(
            bot_hooks,
            bot.bot_id,
            exe,
            fleet=paths.fleet_dir.name if paths.fleet_dir else None,
        )
    hooks = _compose_hooks(bot_hooks)
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
    bot: BotConfig,
    fleet: FleetConfig,
    paths: Paths,
    log=None,
    *,
    boot_delay_s: int | None = None,
) -> Path:
    """Compose one bot's full runtime dir (CLAUDE.md, bot.conf, .mcp.json, units, skill symlinks); returns bot_dir.

    ``boot_delay_s`` defaults to this bot's own rung on the host boot ladder,
    DERIVED rather than threaded: every caller that composes a single bot
    (``generate --bot``, ``move-bot``, ``new-bot``) would otherwise silently
    write a unit with no stagger at all, collapsing that bot to rung 0 and
    re-creating the collision the ladder exists to prevent (#1002). Pass an
    explicit value only to compose a unit off the host ladder, e.g. in tests.
    """
    if boot_delay_s is None:
        boot_delay_s = bot_boot_delay_s(bot, fleet, paths)
    bot_dir = paths.bot_runtime(bot.bot_id)
    # L1 source guard (#702) — deny an unanchored, undeclared absolute path in any
    # compose source (bot config leaves + loaded MCP fragments) BEFORE the first
    # disk write, so a failing bot leaves no partial wiring behind.
    from .path_audit import assert_bot_sources

    assert_bot_sources(bot, fleet, paths, _load_bot_fragments(bot, paths))

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

    # Per-org git credential routing. None ⇒ the bot declares no credentials, so
    # remove any file a previous compose left behind rather than stranding stale
    # routing after the fleet.yaml declaration is dropped.
    gitconfig = compose_bot_gitconfig(bot, paths)
    gitconfig_path = bot_dir / GITCONFIG_FILENAME
    if gitconfig is None:
        gitconfig_path.unlink(missing_ok=True)
    else:
        gitconfig_path.write_text(gitconfig)

    # Sibling App-identity fragment (#1300), pulled in by a per-org includeIf.
    # Written only for an org-scoped App identity; reaped otherwise, same
    # lifecycle as the gitconfig itself.
    app_id_fragment = compose_bot_gitconfig_app_identity(bot)
    app_id_path = bot_dir / GH_APP_IDENTITY_FILENAME
    if app_id_fragment is None:
        app_id_path.unlink(missing_ok=True)
    else:
        app_id_path.write_text(app_id_fragment)

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
    #: Placement DEFAULT, not the resolution location — see ContractVar.
    #: One of known_values.ENV_TIERS.
    default_tier: str
    source: str  # e.g. "mcp/github", "integration/neon", "telegram"
    #: Bots that attach this var's declaring equipment THEMSELVES rather than
    #: inheriting it from fleet defaults. Empty = fleet-wide equipment.
    bot_attached: frozenset[str] = frozenset()

    def scaffold_tier(self) -> str:
        """Which ``.env`` this var's STUB belongs in. Not where it resolves from.

        Two inputs, most-specific-wins — the same rule the runtime cascade uses,
        so a reader who understands one understands the other:

        * the equipment is attached to a BOT → that bot's ``.env`` (#1214 F3a).
          A GitHub App given to one bot is that bot's equipment, and its stub
          belongs where the equipment is, not in a file every bot sources.
        * otherwise the declared ``default_tier``. A var declared ``bot`` stays
          per-bot even on fleet-wide equipment — a Telegram token is one
          BotFather bot per bot no matter who declared the MCP.

        ``host`` and ``root`` come back unchanged and are NOT written: the
        composer owns the fleet and bot tiers only. That refusal is load-bearing
        rather than a limitation. Writing a stub for a host-tier var would
        emit ``export VAR=`` at the fleet tier, and under shell assignment
        semantics that BLANKS the value in ``~/.env`` the declaration was
        pointing at — manufacturing the exact #1213 failure out of an attempt
        to be helpful.
        """
        if self.bot_attached:
            return "bot"
        return self.default_tier


def _attaching_bots(fleet: FleetConfig, equipment: str) -> frozenset[str]:
    """Bots that declare *equipment* themselves rather than inheriting it.

    Returns the attaching bots only when they are a STRICT SUBSET of the fleet;
    equipment every bot carries comes back empty, meaning fleet-wide.

    The spec's line is "a bot-level MCP declaration puts its var stub in that
    bot's .env". Read literally that keys on the SPELLING, and the spelling is
    not what it is trying to distinguish — most fleets list `mcp: [github]`
    under every bot rather than in `fleet.defaults`, which has identical scope
    and identical meaning. Taken literally the rule moves one fleet-tier stub
    into N per-bot stubs, each landing at the MOST specific tier there is,
    where a bare `export VAR=` outranks every other assignment of that key on
    the machine. That multiplies exposure to the very shadowing this phase
    exists to contain.

    So the test is SCOPE, which is the fact the spec is reaching for: equipment
    held by some bots and not others is that bot's equipment — a GitHub App you
    gave to one bot — and its credential belongs with it. Equipment every bot
    holds is fleet infrastructure however it was spelled. A one-bot fleet is
    therefore fleet-tier, not bot-tier, which is also the right answer.

    Deliberately the WHOLE fleet rather than the bot being walked: this answer
    must not depend on which bot happened to reach the var first, or a var
    shared by a fleet-attached bot and a self-attached one would scaffold
    differently depending on dict order.
    """
    attaching = frozenset(
        name
        for name, bot in fleet.bots.items()
        if equipment in bot.bot_attached_equipment
    )
    if not fleet.bots or attaching == set(fleet.bots):
        return frozenset()
    return attaching


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
                    # FIRST-write-wins, keyed on canonical name, silently. Safe
                    # today because EnvVar carries no `secret`: what a skipped
                    # duplicate loses is a description and a source label.
                    #
                    # It stops being safe the moment `secret` is added to
                    # EnvVar (#1214 Phase 3). 11 vars are declared on BOTH
                    # surfaces, and which record lands here depends on bot walk
                    # order — the MCP loop runs before the integration loop
                    # within a bot, but a bot equipping only the integration is
                    # walked before one equipping the fragment. Take the value
                    # from mcp_resolve._reconcile_secret rather than from
                    # whichever surface happened to arrive first.
                    continue
                inst_label = (
                    f" ({cv.instance})" if cv.instance not in (None, "default") else ""
                )
                vars[cv.canonical_name] = EnvVar(
                    name=cv.canonical_name,
                    description=f"{cv.description}{inst_label}",
                    default_tier=cv.default_tier,
                    source=f"mcp/{entry.name}",
                    bot_attached=_attaching_bots(fleet, entry.name),
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
                default_tier = meta.get("default_tier", "fleet")
                if var_name not in vars:
                    vars[var_name] = EnvVar(
                        name=var_name,
                        description=meta.get("description", ""),
                        default_tier=default_tier,
                        source=f"integration/{int_name}",
                        bot_attached=_attaching_bots(fleet, int_name),
                    )

        # Telegram token_env — but never the plugin's OWN read var. A
        # self-referential token_env (== TELEGRAM_BOT_TOKEN) would scaffold a
        # defined-but-empty export the poller treats as authoritative, skipping its
        # channel-dir .env fallback and dying before bot.pid (#749/#750). Skip it
        # here (its home is that fallback); a real value an operator adds to the bot
        # .env is still preserved by _scaffold_env_merge's existing-key merge.
        if (
            bot.telegram
            and bot.telegram.token_env
            and not bot.telegram.token_env_is_self_referential
        ):
            var_name = bot.telegram.token_env
            if var_name not in vars:
                vars[var_name] = EnvVar(
                    name=var_name,
                    description="Telegram bot token",
                    default_tier="bot",
                    source="telegram",
                )

        # Per-org git tokens. Registered here — not just consumed by the composer —
        # because this list is the SSOT three other subsystems read: scaffold_env_files
        # writes the stub, `doctor` reports it missing, and freshbox flags a per-bot
        # secret leaked into a host-shared .env tier. A declaration surface that skips
        # this registry gets none of that.
        for org, var_name in sorted(bot.git_credentials.items()):
            if var_name not in vars:
                vars[var_name] = EnvVar(
                    name=var_name,
                    description=f"GitHub token for the {org} org (git push)",
                    # fleet tier: an org token is shared by every bot pushing to
                    # that org, and freshbox FAILs a *bot*-tier var found in a
                    # host-shared .env — mislabelling it would false-alarm on the
                    # normal placement.
                    default_tier="fleet",
                    source="git_credentials",
                )

        # GitHub App routing (App-auth P3). Registered so an App-only bot —
        # one that declares github_app: without equipping the github-app MCP
        # fragment — still gets the .env stub, the doctor line, and freshbox
        # coverage for vars its composed git routing hard-requires. Deduped
        # by name against the fragment contract (`if var_name not in vars`).
        if bot.github_app:
            for var_name, desc in GITHUB_APP_ENV_VARS.items():
                if var_name not in vars:
                    vars[var_name] = EnvVar(
                        name=var_name,
                        description=desc,
                        default_tier="fleet",
                        source="github_app",
                    )

    return list(vars.values())


def _scaffold_env_merge(
    env_path: Path,
    header: str,
    required: list[EnvVar],
    log=None,
    provided_upstream: frozenset[str] = frozenset(),
) -> None:
    """Idempotent .env scaffold: preserve existing values, append new stubs.

    Reads the file (if it exists), keeps every existing line verbatim, then
    appends stub entries for any contract vars not already present.
    Re-running is safe: no values are lost and new vars always surface.

    ``provided_upstream`` names vars that already resolve to a NON-EMPTY value at
    a higher .env tier. Their stubs are emitted commented-out, because
    start-bot.sh sources the bot tier *last* — so a live ``export FOO=`` here
    silently blanks a value the operator already set at the fleet tier. That is
    not hypothetical: following the /setup skill exactly (which writes the
    Telegram token to the fleet-tier .env) produced a bot whose token was wiped
    by its own scaffold, leaving a dead inbound bridge with nothing in the logs
    to explain it. A commented stub still documents the per-bot override point,
    which is the real reason bot-tier stubs exist (one BotFather bot per bot).
    """
    existing_keys: set[str] = set()
    existing_content = ""
    rewrote_stub = False
    if env_path.is_file():
        existing_content = env_path.read_text()
        existing_keys = set(dotenv.read(env_path).keys())
        # dotenv.read skips comment lines, so a stub this function previously
        # commented out is invisible to it and would be appended again on the
        # next run — unbounded growth, once per generate, and reload-fleet.sh
        # runs generate on a daily fleet-wide timer. Count them as present.
        existing_keys |= set(_COMMENTED_STUB_RE.findall(existing_content))
        # Neutralise a stub that was written live BEFORE its var gained an
        # upstream value. `provided_upstream` is otherwise consulted only when a
        # key is first scaffolded, so the ordinary dev loop — generate while
        # iterating on fleet.yaml, obtain the token, fill it in, generate again —
        # leaves the original blanking stub in place forever and reproduces the
        # bug in full. Only an untouched `export FOO=` is rewritten: an empty
        # value the operator typed themselves is indistinguishable from this on
        # disk, so anything carrying a value, a comment, or different spacing is
        # left exactly as found.
        if provided_upstream:
            # split("\n"), never splitlines(): the latter also breaks on \x0b,
            # \x0c, \x1c-\x1e, \x85, \u2028 and \u2029, none of which end a
            # line in sh. A secret containing any of them was rewritten as two
            # lines — destroyed in place, and the .env left unsourceable.
            rewritten = [
                (
                    f"# {ln}   # already set at a higher .env tier; "
                    "uncomment to override for this bot"
                    if (_m := _PRISTINE_STUB_RE.fullmatch(ln.strip()))
                    and _m.group(1) in provided_upstream
                    else ln
                )
                for ln in existing_content.split("\n")
            ]
            rewrote_stub = rewritten != existing_content.split("\n")
            existing_content = "\n".join(rewritten)

    new_vars = [
        ev
        for ev in sorted(required, key=lambda e: e.name)
        if ev.name not in existing_keys
    ]
    if not new_vars and existing_content and not rewrote_stub:
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
            if ev.name in provided_upstream:
                # Commented, not live — see the docstring on provided_upstream.
                # The hint must not itself be a blanking stub: uncommenting
                # `# export FOO=` yields an empty value and reproduces exactly
                # the bug this branch exists to prevent. Make it unusable until
                # a value is supplied.
                lines.append(
                    f"# Set at a higher .env tier. To override for this bot only,"
                    f" uncomment and supply a value:\n# export {ev.name}=<value>"
                )
            else:
                lines.append(f"export {ev.name}=")

    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)
    if new_vars:
        new_names = ", ".join(ev.name for ev in new_vars)
        msg = f"  {env_path.name}: added {len(new_vars)} new vars ({new_names})"
        _log.info("%s: added %d new vars (%s)", env_path.name, len(new_vars), new_names)
        if log is not None:
            log(msg)


# Anchored so only an untouched scaffold stub matches. `rstrip("=")` also ate a
# real value of "==" (export TOK===), silently dropping a per-bot override.
_PRISTINE_STUB_RE = re.compile(r"export ([A-Za-z_]\w*)=")
_COMMENTED_STUB_RE = re.compile(r"^#\s*export ([A-Za-z_]\w*)=", re.MULTILINE)


def _upstream_env_names(paths: Paths, *, for_tier: str = "bot") -> frozenset[str]:
    """Vars carrying a NON-EMPTY value in any tier sourced ABOVE ``for_tier``.

    start-bot.sh sources $HOME/.env, then the (deprecated) repo-root .env, then
    the fleet .env, then the bot .env — so all three of the first are upstream of
    a bot-tier stub and any of them can be blanked by one. Reading only the fleet
    tier missed a secret placed in ~/.env, which is where lib-common.sh's own
    deprecation notice tells operators to put it, and missed the repo-root tier
    entirely in overlay mode (paths.env_file resolves to the fleet file once one
    exists). Names only; values are never retained.

    ``for_tier`` names the tier being SCAFFOLDED, and the fleet case is not
    cosmetic (#1206). ``paths.env_file`` resolves to the FLEET file in overlay
    mode, so including it while scaffolding that same file makes a stub count as
    its own upstream: every name is "already set", the guard finds nothing to
    protect, and it no-ops silently — the fix would land, the test would pass
    against a bot-tier fixture, and the destructive behaviour would remain. The
    fleet tier therefore reads only the two tiers strictly above it.

    An unknown tier raises rather than defaulting: the wrong tier set here is
    invisible at runtime (it produces a guard that merely does less), so a typo
    must fail loudly at the call site instead of degrading to no protection.
    """
    # WHICH tiers are upstream is sliced out of ENV_TIERS, which is pinned by
    # test against what the runtime resolver emits — so adding a tier to the
    # cascade cannot leave this guard silently reading a short list.
    #
    # The tier -> PATH mapping below is the one place in the compositor that
    # still restates something the resolver knows, and it is deliberate rather
    # than overlooked: reading it from lib/env-tiers.sh means a bash subprocess
    # inside `generate`, and no test fixture root carries a lib/. The order —
    # the part that actually drifted and caused #1226 — now comes from the
    # pinned registry; only the leaf names are local. Tracked as the residual.
    if for_tier not in ENV_TIERS:
        raise ValueError(
            f"_upstream_env_names: unknown tier {for_tier!r} "
            f"(expected one of {', '.join(ENV_TIERS)})"
        )
    tier_path = {
        "host": Path.home() / ".env",
        "root": paths.root / ".env",
        "fleet": paths.env_file,
    }
    above = ENV_TIERS[: ENV_TIERS.index(for_tier)]
    tiers: tuple[Path, ...] = tuple(
        tier_path[t] for t in above if t in tier_path
    )

    names: set[str] = set()
    for tier in tiers:
        try:
            if tier.is_file():
                names |= {n for n, v in dotenv.read(tier).items() if v}
        except (OSError, UnicodeDecodeError):
            # UnicodeDecodeError is a ValueError, not an OSError: one latin-1
            # byte in a password in ~/.env would otherwise abort the whole
            # generate, which is exactly what this guard exists to prevent.
            continue
    return frozenset(names)


def scaffold_env_files(fleet: FleetConfig, paths: Paths, log=None) -> None:
    """Idempotent .env scaffolding at fleet and bot tiers.

    Preserves existing key=value pairs, appends stubs for any new
    contract vars discovered from MCP fragments and integrations.
    """
    env_vars = collect_env_contracts(fleet, paths)
    fleet_vars = [ev for ev in env_vars if ev.scaffold_tier() == "fleet"]
    bot_vars = [ev for ev in env_vars if ev.scaffold_tier() == "bot"]

    # host/root-tier vars are NOT scaffolded, and saying so is the point. The
    # composer owns the fleet and bot .env files; emitting `export VAR=` at the
    # fleet tier for a var whose home is ~/.env would BLANK the value that
    # declaration points at, because the more specific empty assignment wins.
    # That is #1213 manufactured out of helpfulness, so the stub is withheld and
    # the operator is told where the value goes instead.
    unscaffolded = [ev for ev in env_vars if ev.scaffold_tier() in ("host", "root")]
    if unscaffolded and log is not None:
        for ev in sorted(unscaffolded, key=lambda e: e.name):
            log(
                f"  {ev.name}: declared {ev.default_tier}-tier — no stub written "
                f"(a stub here would blank it); set it in the {ev.default_tier} .env"
            )

    if paths.fleet_dir:
        # Guarded exactly as the bot tier is (#1206). start-bot.sh sources the
        # fleet tier AFTER $HOME/.env and the repo-root .env, so an empty fleet
        # stub is not a neutral placeholder — it is an assignment that blanks a
        # working credential. Measured live on one fleet: three real tokens
        # present in both upstream tiers, destroyed by their own fleet stubs.
        # for_tier="fleet" is load-bearing; see _upstream_env_names.
        _scaffold_env_merge(
            paths.fleet_dir / ".env",
            f"# Fleet environment for: {fleet.name}",
            fleet_vars,
            log=log,
            provided_upstream=_upstream_env_names(paths, for_tier="fleet"),
        )

    if bot_vars:
        # Vars already carrying a real value upstream must not be re-stubbed live
        # at the bot tier — start-bot.sh sources the bot tier last, so an empty
        # export would win over the operator's actual value. Unchanged, and it
        # matters MORE now: routing a var to one bot's .env moves its stub to
        # the most specific tier there is, where a bare stub outranks every
        # other assignment of that key on the machine.
        upstream = _upstream_env_names(paths)
        for bot_name in fleet.bots:
            # A var whose equipment this bot attaches ITSELF goes only here. One
            # with no attaching bot is fleet-wide equipment carrying a per-bot
            # value (a Telegram token: one BotFather bot per bot) and still goes
            # to every bot, as before.
            mine = [
                ev
                for ev in bot_vars
                if not ev.bot_attached or bot_name in ev.bot_attached
            ]
            if not mine:
                continue
            _scaffold_env_merge(
                paths.bot_runtime(bot_name) / ".env",
                f"# Bot environment for: {bot_name}",
                mine,
                log=log,
                provided_upstream=upstream,
            )


_BOOT_STAGGER_SECONDS = 3  # delay between each bot's startup on fleet boot


def _fleet_manager_worker_counts(fleet_yaml: Path) -> tuple[int, int]:
    """(managers, workers) from a fleet.yaml, sized for another fleet's rungs.

    Manager detection is DELEGATED to ``FleetConfig.manager_bots`` and never
    restated here. A private copy of that rule is exactly how the ladder came
    to disagree with the rest of the composer: this helper read only ``teams:``
    while ``manager_bots`` reads ``teams:`` AND ``bots.<id>.manages:``, so a
    fleet whose manager is declared solely by ``manages:`` counted ZERO
    managers and shifted every later fleet off its rung. Its own docstring
    asserted the two were "the same declaration", which was true when written
    and stopped being true forty minutes later.

    That is the same shape as #892/#1143 — a predicate fixed centrally while a
    consumer kept its own copy — which is the second occurrence in this repo
    and the argument for fixing the seam rather than patching the symptom.

    A sibling fleet's manifest is read here only to size its rung blocks, so a
    manifest that is missing, unparseable, or mid-edit contributes (0, 0)
    rather than raising: one fleet's broken config must never block another
    fleet's generate. The cost of the soft failure is a stale offset — the
    un-offset behaviour, not a new failure mode.

    The caught set is NAMED, never a bare ``Exception``, and that is load
    bearing rather than tidiness. The version of this helper being replaced
    warned that a blanket catch "would also swallow a NameError in this
    function and report every fleet as empty" — and while writing the
    replacement I did exactly that: the import of ``load_fleet`` was dropped as
    unused between two edits, every call raised NameError, the blanket catch
    turned it into (0, 0), and every fleet on the host silently lost its
    stagger. The suite caught it; a narrower catch would have raised on the
    first call instead. NameError is absent from this tuple deliberately.
    """
    try:
        fleet, _ = load_fleet(fleet_yaml)
    except (OSError, yaml.YAMLError, ValueError, TypeError, KeyError):
        # missing, unparseable, or shaped in a way coercion rejects
        return 0, 0
    # Only bots the manifest actually lists: a team naming an absent bot would
    # otherwise inflate the manager tier and shift every later fleet.
    bots = set(fleet.bots)
    managers = fleet.manager_bots() & bots
    return len(managers), len(bots) - len(managers)


@functools.cache
def _host_boot_rung_bases(paths: Paths, own_managers: int) -> tuple[int, int]:
    """This fleet's first manager rung and first worker rung on the host ladder.

    Each fleet ladders ``ExecStartPre=/bin/sleep`` from zero. On a host running
    several fleets that puts one bot from *every* fleet on every early rung, so
    a host reboot fires N simultaneous cold starts. Laddering the fleets into a
    single host-global sequence fixes that: one bot per rung, ordered.

    Within that sequence the host boots in two tiers — **every manager on the
    host first, then every worker**. The reason is recovery latency, not boot
    success: a stranded worker is recoverable because its manager notices, while
    a stranded manager means nothing is watching that fleet at all. Front-loading
    the managers puts the whole observation layer up in the first few rungs, so a
    strand anywhere is noticed sooner. It does **not** make any bot less likely
    to strand — rung is not predictive of strand (#1002) — and it costs nothing:
    same one-bot-per-rung property, same total spread.

    The order within each tier is the sorted overlay enumeration shared with the
    collision scan and move-bot autodetect (``_iter_fleet_dirs``), so the
    assignment is deterministic and identical no matter which fleet is being
    generated. Worker rungs start past *every* manager on the host, which is why
    the walk continues after this fleet is found rather than returning early.

    Bases are computed from the manifests present at generate time, so ADDING,
    REMOVING or RENAMING a fleet — or promoting a bot to manager — reshuffles the
    rungs of other fleets, and those fleets keep their old ladder until *they*
    regenerate. Until then some rungs can collide again, degrading to today's
    behaviour, never worse. ``lib/setup-fleets`` regenerates every fleet in one
    pass, which is the sanctioned way to re-converge the ladder.

    A fleet with no place in the host ladder — root mode, or an explicitly-
    pointed overlay outside ``local/`` — ladders standalone off ``own_managers``,
    still manager-tier first. That keeps "managers occupy the first block" true
    in one place rather than leaving the caller to restate it.

    Cached for the process: every bot in a fleet asks the same question, and the
    walk parses every sibling manifest to answer it. No shipped path mutates a
    manifest and re-composes without re-exec — ``generate`` is one-shot,
    ``setup-fleets`` forks per fleet, and ``move-bot`` requires the target stanza
    to exist before it runs.
    """
    standalone = (0, own_managers)
    if paths.fleet_dir is None:  # root mode: one fleet, nothing to ladder past
        return standalone
    here = paths.fleet_dir.resolve()
    managers_before = workers_before = None
    managers_total = workers_total = 0
    for fleet_dir in _iter_fleet_dirs(paths.root / "local"):
        if fleet_dir.resolve() == here:
            managers_before, workers_before = managers_total, workers_total
        # A container or non-fleet dir has no manifest and contributes (0, 0) —
        # the same answer _fleet_manager_worker_counts already gives for an
        # unreadable file, so what a non-fleet dir is worth is decided in
        # exactly one place.
        managers, workers = _fleet_manager_worker_counts(fleet_dir / "fleet.yaml")
        managers_total += managers
        workers_total += workers
    if managers_before is None:
        return standalone  # not under local/: no placement in the host ladder
    return managers_before, managers_total + workers_before


def bot_boot_delay_s(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> int:
    """This bot's rung on the host-global boot ladder, in seconds.

    Derived from (manager or worker, fleet position on the host, position among
    that fleet's same-tier bots), so every compose path lands on the same rung
    without threading an index through its callers.

    Manager-ness comes from ``FleetConfig.manager_bots`` — the same declaration
    that decides what ``MANAGER_TMUX`` is composed to. It is deliberately not
    re-derived from the composed value, which carries a trailing comment that a
    naive parse gets wrong.
    """
    # Intersected with the bot list so this fleet's tier is sized by the same
    # rule _fleet_manager_worker_counts applies to every sibling: a team naming
    # an absent bot must not inflate one side of the ladder and not the other.
    managers = fleet.manager_bots() & set(fleet.bots)
    is_manager = bot.bot_id in managers
    tier = [b for b in fleet.bots if (b in managers) == is_manager]
    try:
        index = tier.index(bot.bot_id)
    except ValueError:  # composing a bot the fleet does not list (scaffolding)
        index = 0
    manager_base, worker_base = _host_boot_rung_bases(paths, len(managers))
    base = manager_base if is_manager else worker_base
    return (base + index) * _BOOT_STAGGER_SECONDS


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


# The system.yaml fleet job whose script reads the FLEET_PULSE_* knobs (#1120).
_FLEET_PULSE_JOB = "fleet-pulse"


def _write_service_units(
    timers_dir: Path,
    service_name: str,
    name: str,
    script: str,
    paths: Paths,
) -> None:
    """Write the .service/.plist for a RESIDENT host service — no .timer
    (nothing schedules it; supervision restarts it). systemd Restart=always /
    launchd KeepAlive are the v1 supervision shape (socket activation is a
    deferred systemd-only enhancement — launchd's needs launch_activate_socket
    via ctypes, and a platform-asymmetric v1 buys complexity first).

    Emitted ONLY for armed jobs — see the compose_host_timers branch for why
    dormancy must be compose-time for services."""
    from .path_audit import SourceFinding, denied_source_paths, source_findings_error

    _svc_id = f"host service {name}"
    _svc_findings = [
        SourceFinding(
            bot_id=_svc_id,
            source=f"jobs.{name}.script",
            value=script,
            path=denied,
            reason="host service script path",
        )
        for denied in denied_source_paths(script, [])
    ]
    if _svc_findings:
        raise source_findings_error(_svc_id, _svc_findings)

    script_expanded = script.replace("$CLAUDLOBBY_ROOT", str(paths.root))
    tool_path = _scheduler_tool_path(paths.root)

    service_lines = [
        "# Generated by claudlobby — do not hand-edit.",
        "[Unit]",
        f"Description=claudlobby {name} (host service)",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={paths.root}",
        f"Environment=CLAUDLOBBY_ROOT={paths.root}",
        f"Environment=PATH={tool_path}",
        f"ExecStart={script_expanded}",
        "Restart=always",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=default.target",
    ]
    (timers_dir / f"{service_name}.service").write_text(
        "\n".join(service_lines) + "\n"
    )

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
    ]
    plist_lines.extend(
        f"    <string>{part}</string>" for part in shlex.split(script_expanded)
    )
    plist_lines.extend(
        [
            "  </array>",
            "  <key>EnvironmentVariables</key>",
            "  <dict>",
            "    <key>CLAUDLOBBY_ROOT</key>",
            f"    <string>{paths.root}</string>",
            "    <key>PATH</key>",
            f"    <string>{tool_path}</string>",
            "  </dict>",
            "  <key>WorkingDirectory</key>",
            f"  <string>{paths.root}</string>",
            "  <key>RunAtLoad</key>",
            "  <true/>",
            "  <key>KeepAlive</key>",
            "  <true/>",
            "</dict>",
            "</plist>",
        ]
    )
    (timers_dir / f"{service_name}.plist").write_text(
        "\n".join(plist_lines) + "\n"
    )


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
    fleet_pulse_env: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
    abandon_children: bool = False,
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
    # L1 source guard (#702) — a timer's script is a compose source. A
    # $CLAUDLOBBY_ROOT-anchored script (the shape system.yaml / fleet jobs use)
    # passes; a raw absolute hand-typed into a fleet timer's script is denied.
    from .path_audit import SourceFinding, denied_source_paths, source_findings_error

    _timer_id = f"{fleet_name or 'host'} timer {name}"
    _timer_findings = [
        SourceFinding(
            bot_id=_timer_id,
            source=f"jobs.{name}.script",
            value=script,
            path=denied,
            reason="timer script path",
        )
        for denied in denied_source_paths(script, [])
    ]
    if _timer_findings:
        raise source_findings_error(_timer_id, _timer_findings)

    scope = fleet_name if fleet_name is not None else "host"
    script_expanded = script.replace("$CLAUDLOBBY_ROOT", str(paths.root))
    exec_start = f"{script_expanded} {fleet_name}" if fleet_name else script_expanded
    if exec_args:
        exec_start = f"{exec_start} {' '.join(exec_args)}"

    # Compute the tool PATH once so systemd and launchd emit an identical value
    # (see _scheduler_tool_path, #798); the parity test asserts they match.
    tool_path = _scheduler_tool_path(paths.root)

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
        # Carry the fleet tool PATH into the timer env (#798).
        f"Environment=PATH={tool_path}",
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
    # #1120: the fleet-pulse escalation knobs. Same reasoning as the line above
    # and the same door — a scheduler starts with almost no environment, and
    # this unit sources no .env, so an Environment= line is the only thing the
    # script can actually read. Scoped to the one job whose script reads them
    # rather than every fleet timer: the vars are FLEET_PULSE_-prefixed and no
    # other job consumes them, so a wider grant would buy nothing.
    if fleet_name and name == _FLEET_PULSE_JOB and fleet_pulse_env:
        for var, value in fleet_pulse_env.items():
            service_lines.append(f"Environment={var}={value}")
    # Caller-scoped extra env (gauntlet round; first tenant: PLANE_EMIT_ENABLED
    # on briefing timers). Same mechanism as the two blocks above — a scheduler
    # env is closed, so an Environment= line is the only thing the script can
    # read — generalized so the next timer-reachable var is a call-site dict,
    # not a fourth hand-rolled block.
    for var, value in (extra_env or {}).items():
        service_lines.append(f"Environment={var}={value}")
    if abandon_children:
        # The script backgrounds work that must outlive the job (keepalive's
        # per-bot plane emit, reaped on its own clock). With the default
        # control-group kill, the LAST bot's emit dies at sweep exit every
        # tick — measured live: the alphabetically-last bot landed 1
        # heartbeat in 7 days while every sibling landed ~1300, and a manual
        # tick outside the supervisor landed at once. Same rationale as the
        # bot service unit's KillMode=process.
        service_lines.append("KillMode=process")
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
    ]
    # launchd's ProgramArguments[0] is the executable PATH; systemd's ExecStart=
    # is a command LINE that it splits on whitespace. A job whose `script`
    # carries flags (data-sweep's `--purge`) is therefore valid on Linux and, if
    # passed through whole, becomes a file that does not exist on macOS — the
    # job then silently never execs. Split here so both platforms receive the
    # same argv, flags before the fleet name exactly as systemd would pass them
    # (#969).
    plist_lines.extend(
        f"    <string>{part}</string>" for part in shlex.split(script_expanded)
    )
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
            # Carry the fleet tool PATH into the timer env (#798).
            "    <key>PATH</key>",
            f"    <string>{tool_path}</string>",
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
    # #1120 — parity with the systemd half above; the env-parity test compares
    # the two, so emitting on one platform only would fail there rather than in
    # production eight weeks later.
    if fleet_name and name == _FLEET_PULSE_JOB and fleet_pulse_env:
        for var, value in fleet_pulse_env.items():
            plist_lines.extend(
                [f"    <key>{var}</key>", f"    <string>{value}</string>"]
            )
    # Parity with the systemd extra_env block — the env-parity test compares
    # the two platforms, so emitting on one only fails there, not in production.
    for var, value in (extra_env or {}).items():
        plist_lines.extend(
            [f"    <key>{var}</key>", f"    <string>{value}</string>"]
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
    if abandon_children:
        # launchd kills the job's process group at exit unless told to abandon
        # it — see the KillMode=process note above (same live measurement)
        plist_lines.extend(["  <key>AbandonProcessGroup</key>", "  <true/>"])
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


# Fleet jobs whose SCRIPT reads an arming flag (the closed-scheduler-env class
# #1383): the composer resolves each through the runtime tier cascade and
# stamps it on exactly that unit. Add a row per dormant door.
FLEET_JOB_ARMING: dict[str, tuple[str, ...]] = {
    "keepalive": ("PLANE_EMIT_ENABLED",),
    "plane-shadow": ("PLANE_SHADOW_ENABLED",),
    # fleet-pulse reads TWO plane flags: the chunk-4 divergence bridge
    # (PLANE_SHADOW_ENABLED — the timer unit sources no .env, so without this
    # stamp the bridge could never arm from a timer) and the chunk-5 flip of
    # its overdue reader (PLANE_READ_OVERDUE — per reader, never one
    # fleet-wide flag). The one genuinely multi-flag job.
    "fleet-pulse": ("PLANE_SHADOW_ENABLED", "PLANE_READ_OVERDUE"),
}

# The cutover read flags also need a SESSION carrier: start-bot.sh exports
# bot.conf (`set -a`) but sources the .env tiers WITHOUT export, so a bare
# `PLANE_READ_OPEN=1` in the fleet tier never reaches report-back.sh or brief
# inside a bot session (measured in bash and zsh — spec lens, chunk 5).
# compose_bot_conf therefore carries the armed read flags, resolved from the
# same cascade the timers use, memoized per (root, fleet) so a 21-bot generate
# pays the env-tiers subprocess once.
_READ_FLAG_MEMO: dict[tuple[str, str], dict[str, str]] = {}


def _read_flag_env(paths: Paths, fleet_name: str | None) -> dict[str, str]:
    key = (str(paths.root), fleet_name or "")
    if key not in _READ_FLAG_MEMO:
        from . import env_tiers as _env_tiers
        from .plane.cutover import READ_FLAGS
        env: dict[str, str] = {}
        try:
            cascade = _env_tiers.cascade(_env_tiers.read_tiers(paths, fleet_name=fleet_name))
            env = {f: "1" for f in READ_FLAGS.values() if _env_tiers.armed(cascade, f)}
        except _env_tiers.ResolverUnavailable as exc:
            _log.warning("cutover read flags unresolved (%s) — bot.conf composes them UNSET", exc)
        _READ_FLAG_MEMO[key] = env
    return _READ_FLAG_MEMO[key]


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

    # Scheduler-run scripts live in a CLOSED env — the fleet-tier .env a bot
    # session sources never reaches them, so PLANE_EMIT_ENABLED composed
    # nowhere and a timer-run door was UNREACHABLE in production (#1383,
    # first found on briefing; the keepalive presence door is the second
    # consumer — its per-minute tick runs from the keepalive job unit).
    # Resolve the fleet arming ONCE through the runtime tier cascade and
    # stamp it as an Environment= line on exactly the jobs whose scripts
    # read it; a resolver failure composes UNARMED and says so.
    plane_extra_env: dict[str, str] | None = None
    from . import env_tiers as _env_tiers

    # Which fleet job reads which arming flag — a TABLE, so the next dormant
    # door adds a row rather than a branch; each flag resolves through the
    # one cascade read below. The shadow comparison (cutover chunk 3) rides
    # its OWN carrier: a recorded fact must not start being written because
    # emission is on.
    job_extra_env: dict[str, dict[str, str]] = {}
    try:
        _cascade = _env_tiers.cascade(
            _env_tiers.read_tiers(paths, fleet_name=fleet.name)
        )
        for _job, _flags in FLEET_JOB_ARMING.items():
            _armed = {f: "1" for f in _flags if _env_tiers.armed(_cascade, f)}
            if _armed:
                job_extra_env[_job] = _armed
        plane_extra_env = job_extra_env.get("keepalive")
    except _env_tiers.ResolverUnavailable as exc:
        _log.warning(
            "plane arming unresolved (%s) — timer-run plane doors compose"
            " UNARMED",
            exc,
        )

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
                abandon_children=bool(cfg.get("abandon_children", False)),
                randomized_delay=int(cfg.get("randomized_delay") or 0),
                telegram_group_chat_id=fleet.telegram_group_chat_id,
                fleet_pulse_env=(
                    fleet.fleet_pulse.env() if fleet.fleet_pulse else None
                ),
                extra_env=job_extra_env.get(name),
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
    # Briefing timers run in the scheduler's CLOSED env — the fleet-tier .env a
    # bot session sources never reaches them, so PLANE_EMIT_ENABLED composed
    # nowhere and the briefing door's plane emission was UNREACHABLE in
    # production (gauntlet round; the arming-mechanics half is #1383). Resolve
    # the fleet's arming through the runtime's OWN tier cascade (env_tiers —
    # Paths.env_file is the write tier, and reading it as the answer was the
    # #1226 defect) and carry it as an Environment= line. Arming lands on
    # briefing timers at the NEXT generate; a resolver failure composes
    # UNARMED (the pre-fix state) and says so.
    briefing_extra_env = plane_extra_env
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
                extra_env=briefing_extra_env,
            )
            composed_briefing.add(unit)
    _reconcile_briefing_units(
        timers_dir, prefix, composed_briefing, len(expected_briefing)
    )

    return timers_dir


_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def compose_host_mention_allowlist(
    paths: Paths, *, output_dir: Path | None = None
) -> Path:
    """Handles a bot MAY @-mention on GitHub. Everything else is rewritten.

    The inversion (#1019 follow-up). The merged guard matched a denylist of
    composed bot names, which is correct as far as it goes and provably does not
    go far enough: of the eleven real accounts we notified, `Botfather`,
    `latest` and `216` are on no bot-name list and never could be. The harm
    class is "any @word that happens to be a real handle" — unbounded, and it
    grows without us doing anything, so it cannot be enumerated.

    So the guard defaults to rewriting and this file is the exception list. It
    is bounded where a denylist is not: the set of people we intend to notify
    from a bot is tiny and changes rarely, while the set of strings that happen
    to be real handles is neither.

    Declared per fleet under ``fleet.github.mention_allowlist`` and unioned
    across the host, because a handle worth notifying from one fleet is worth
    notifying from all of them. Empty by default: no mention notifies anyone
    until someone says so, in writing, in a manifest.

    A composed BOT name always wins over this list — see the deny-override in
    lib/mention-rewrite.py. Without that, someone eventually allowlists a bot's
    name meaning our bot and silently re-arms the original bug.
    """
    names: set[str] = set()
    for fleet_dir in _iter_fleet_dirs(paths.root / "local"):
        manifest = fleet_dir / "fleet.yaml"
        if not manifest.is_file():
            continue
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            gh = (data.get("fleet") or {}).get("github") or {}
            if isinstance(gh, dict):
                names.update(gh.get("mention_allowlist") or [])

    base = output_dir if output_dir is not None else paths.root / "runtime" / "_host"
    base.mkdir(parents=True, exist_ok=True)
    target = base / "mention-allowlist"
    safe = sorted(n for n in names if _HANDLE_RE.match(n or ""))
    target.write_text("".join(f"{n}\n" for n in safe), encoding="utf-8")
    return target


def compose_host_bot_handles(paths: Paths, *, output_dir: Path | None = None) -> Path:
    """Write every bot name on the HOST, one per line, for the mention guard.

    Consumed by ``lib/gh-mention-guard.sh`` (#1019), which rewrites ``@<name>``
    out of GitHub-bound tool calls. Every one of this estate's bot names is also
    a real GitHub account — all 21 resolve, 19 of them to real people and 2 to
    organizations — so an unguarded teammate reference emails a stranger. One of
    them asked us to stop. There is no safe name.

    COMPOSED, never hardcoded: a literal list re-breaks the moment a bot is
    added, which is #1009's defect class exactly.

    HOST-WIDE, not fleet-scoped, and that is load-bearing rather than tidy.
    Cross-fleet references are routine — this fleet's issues name bots from
    three other fleets constantly — so a fleet-scoped list would miss precisely
    the mentions written by someone with no relationship to that bot. Built by
    enumerating every fleet under ``local/`` via the same ``_iter_fleet_dirs``
    the collision scan and move-bot autodetect use.

    A sibling fleet whose manifest is missing or unparseable contributes
    nothing rather than raising: one fleet's broken config must not stop another
    fleet's generate. The cost is a narrower guard, which the hook reports.
    """
    names: set[str] = set()
    for fleet_dir in _iter_fleet_dirs(paths.root / "local"):
        manifest = fleet_dir / "fleet.yaml"
        if not manifest.is_file():  # a container or a non-fleet dir
            continue
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            # A sibling fleet's unreadable manifest must not stop this generate.
            # Narrow ON PURPOSE: the first cut caught bare Exception, so a
            # missing `import yaml` raised NameError on EVERY fleet and was
            # swallowed as "all four manifests are broken" — the guard composed
            # an empty list and would have protected nothing, silently.
            continue
        if isinstance(data, dict):
            names.update((data.get("fleet") or {}).get("bots") or {})
    base = output_dir if output_dir is not None else paths.root / "runtime" / "_host"
    base.mkdir(parents=True, exist_ok=True)
    target = base / "bot-handles"
    # Only names safe to drop into the hook's regex alternation. Deliberately
    # NOT SHELL_IDENT_RE, which forbids hyphens — `worker-1` is a real bot name
    # shape (fleet.yaml.example uses it), and excluding it would leave exactly
    # those bots unguarded while looking covered. This charset carries no regex
    # metacharacters, and matches the hook's own `grep -Ex` filter so the two
    # cannot disagree about which names are admissible.
    safe = sorted(n for n in names if _HANDLE_RE.match(n or ""))
    target.write_text("".join(f"{n}\n" for n in safe), encoding="utf-8")
    return target


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
        if cfg.get("unit") == "service":
            # Resident host services (first tenant: the plane ingest daemon).
            # Dormancy is COMPOSE-time, not enroll-time, and that is a safety
            # property rather than a style choice: setup-system's macOS leg
            # enrolls every claudlobby-*.plist it finds, so a composed-but-
            # dormant service plist would be started by the next setup run —
            # a root pull silently activating a new resident process, the
            # exact no-silent-switches violation. Unarmed = no files at all,
            # INCLUDING leftovers: an armed→unarmed transition must PRUNE the
            # previously composed units, or the next setup run re-enrolls the
            # supposedly dormant service from the stale files (measured).
            # Pruning stops future (re)enrollment only — an already-INSTALLED
            # unit keeps running until disabled; the disarm recipe is printed
            # beside the knob in system.yaml.
            if cfg.get("enroll") is True:
                _write_service_units(
                    timers_dir, f"claudlobby-{name}", name,
                    cfg.get("script", ""), paths,
                )
            else:
                for ext in ("service", "plist"):
                    leftover = timers_dir / f"claudlobby-{name}.{ext}"
                    if leftover.exists():
                        leftover.unlink()
                        _log.info(
                            "pruned dormant service unit %s (armed→unarmed)",
                            leftover.name,
                        )
            continue
        sched = _resolve_timer_schedule(cfg, {})
        # Arming carrier for a self-gated host door (chunk 3a.1): a host
        # timer starts with a CLOSED env, so a door that self-gates on a
        # flag (plane-prune on PLANE_PRUNE_ENABLED) needs it stamped as an
        # Environment= line — the same closed-scheduler-env problem the
        # keepalive door solved for fleet timers (#1383). Resolved ONCE
        # through the host tier cascade (no fleet name → host+root tiers)
        # and stamped only on the job that reads it; a resolver failure
        # composes UNARMED (the safe default for a DELETE door).
        # Host doors that emit into the plane self-gate on a closed
        # scheduler env, so the flag is stamped from the host tier cascade
        # (chunk 3a.1). plane-prune uses its OWN flag (a DELETE door earns
        # a dedicated arm); plane-host-probe is read-only emission and uses
        # the standard PLANE_EMIT_ENABLED.
        if name == "plane-prune":
            extra_env = _host_job_plane_arming(paths, "PLANE_PRUNE_ENABLED")
        elif name == "plane-host-probe":
            extra_env = _host_job_plane_arming(paths, "PLANE_EMIT_ENABLED")
        elif name == "plane-expire":
            extra_env = _host_job_plane_arming(paths, "PLANE_EXPIRE_ENABLED")
        else:
            extra_env = None
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
            abandon_children=bool(cfg.get("abandon_children", False)),
            randomized_delay=int(cfg.get("randomized_delay") or 0),
            extra_env=extra_env,
        )
    return timers_dir


def _host_job_plane_arming(paths: Paths, flag: str) -> dict[str, str] | None:
    """Resolve a plane arming flag from the host tier cascade for a
    self-gated host door's Environment= line. None = unarmed (the safe
    default: a resolver failure never arms). `flag` is the door's own
    variable (PLANE_PRUNE_ENABLED for the DELETE door, PLANE_EMIT_ENABLED
    for read-only emission)."""
    from . import env_tiers as _env_tiers
    try:
        res = _env_tiers.cascade(_env_tiers.read_tiers(paths)).get(flag)
    except _env_tiers.ResolverUnavailable as exc:
        _log.warning("host-job arming unresolved for %s (%s) — composes"
                     " UNARMED", flag, exc)
        return None
    if res is not None and res.value == "1":
        return {flag: "1"}
    return None


def compose_fleet(fleet: FleetConfig, paths: Paths, log=None) -> dict[str, Path]:
    """Compose every bot in the fleet; returns a dict of bot_id -> bot_dir."""
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
    # Collect every bot's compose failure rather than aborting on the first (G1):
    # one generate should surface all offenders, so an operator fixes the fleet in
    # a single pass instead of whack-a-mole.
    failures: list[tuple[str, str]] = []
    for bot_name, bot in fleet.bots.items():
        _log.info("composing %s...", bot_name)
        if log is not None:
            log(f"composing {bot_name}...")
        try:
            # boot_delay_s is deliberately NOT passed: compose_bot derives every
            # bot's rung the same way for every entry point, so `generate --bot`
            # and move-bot cannot compose a unit that disagrees with this one.
            # The sibling-manifest walk behind that derivation is cached for the
            # process (see _host_boot_rung_bases), so the re-derivation costs one
            # walk per fleet rather than one per bot.
            out[bot_name] = compose_bot(bot, fleet, paths, log=log)
        except ValueError as e:
            failures.append((bot_name, str(e)))
    if failures:
        detail = "\n\n".join(f"[{name}] {err}" for name, err in failures)
        raise ValueError(f"{len(failures)} bot(s) failed to compose:\n\n{detail}")

    scaffold_env_files(fleet, paths, log=log)

    return out
