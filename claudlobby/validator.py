"""fleet.yaml validation.

Permissive by default: warnings let `generate` proceed; errors block it.
Pass `--strict` to make warnings into errors (CI-friendly).
"""

from __future__ import annotations
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

from . import dotenv, tool_resolve
from .claudron_compat import CLAUDRON_INTEGRATION_URL
from .config import _PROJECT_VALIDATION_KEYS, FleetConfig, is_pos_int
from .known_values import (
    AUTO_ELIGIBLE_SKILLS,
    BYPASS_ACTIONS,
    EXPERTISE_CORE_TOOLS,
    KNOWN_HOOK_EVENTS,
    KNOWN_MODELS,
    OUTCOME_ACTIONS,
    OUTCOME_KEYS,
    PROJECT_KEYS,
    RC_KILLING_ENV_VARS,
    VALID_TIERS,
    closest_match,
    hint,
)
from .mcp_resolve import required_vars as _mcp_required_vars
from .paths import Paths, _iter_fleet_dirs, detect_vault

_CADENCE_RE = re.compile(r"^\d+[mhd]$")
_ORG_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def _env_has_value(effective_env: dict[str, str], var: str) -> bool:
    """True iff ``var`` resolves to a non-empty value in ``effective_env``.

    Value-based, not presence-based: the compositor scaffolds required env vars
    as empty ``export VAR=`` stubs and dotenv parses those to ``""``, so a bare
    ``var in effective_env`` is True-but-empty. A warning that keyed off mere
    presence fired once (the first cold generate, before the stub existed) then
    went permanently silent even if the operator never filled in a real value
    (#755). The single predicate behind all four "requires VAR but it is not
    set" validator warnings — MCP contract vars, tool env vars, telegram
    token_env, git_credentials — so a fifth site cannot reintroduce the fork.
    """
    return bool(effective_env.get(var))


def _operator_git_identity_problem() -> str | None:
    """Why the composed ``.gitconfig``'s ``[include]`` would not yield a git
    identity, or None when it is fine.

    ``git_credentials`` composes an include of the operator's own ``~/.gitconfig``
    rather than a per-bot identity (the git-identity-no-overrides guardrail), and
    git ignores a missing or identity-less include SILENTLY: credential routing
    keeps working, ``user.email`` is simply never set, and the first commit dies
    with ``Author identity unknown``. Nothing in that failure points back here, so
    it is worth naming at validate time.

    Delegates the read to ``git config --file --includes`` rather than parsing the
    file, because an operator's identity legitimately lives one ``[include]``
    deeper and a hand-rolled parse would cry wolf. ``--includes`` is not optional
    here: it is off by default for ``--file`` reads, but ON when git reads the
    same file as global config — which is exactly how the bot will read it — so
    omitting it would warn about identities that resolve perfectly at runtime.
    """
    from .composer import _operator_gitconfig  # local: composer imports config, not us

    operator = _operator_gitconfig()
    if not operator.is_file():
        return f"{operator} does not exist"
    if not shutil.which("git"):
        return None  # cannot check; not the validator's business to guess
    probe = subprocess.run(
        ["git", "config", "--file", str(operator), "--includes", "--get", "user.email"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return f"{operator} sets no user.email"
    return None


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def has_issues(self) -> bool:
        return bool(self.errors or self.warnings)


def _available_names(paths: Paths, kind: str, ext: str = ".md") -> set[str]:
    """Scan library search dirs for available file stems of a given kind."""
    names: set[str] = set()
    for d in paths.library_search_dirs(kind):
        if d.is_dir():
            names |= {p.stem for p in d.glob(f"*{ext}")}
    return names


# Grant grammar (F3(a)): a well-formed grant is exactly one of three kinds —
#   1. an exact-prefix ``mcp__`` glob whose only wildcard, if any, is a trailing
#      ``*`` (F5: no mid-string wildcards);
#   2. a scoped ``Bash(<command pattern>)`` grant;
#   3. a bare CamelCase tool name (e.g. ``Read``, ``WebFetch``, ``Bash``).
# One regex per kind keeps each shape independently checkable.
_GRANT_MCP_RE = re.compile(r"^mcp__[^*]*\*?$")
_GRANT_BASH_RE = re.compile(r"^Bash\(.+\)$")
_GRANT_BARE_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")


def _grant_wellformed(grant: object) -> bool:
    """True when ``grant`` matches the F3(a) grant grammar (mcp glob | Bash(..) | bare tool)."""
    return isinstance(grant, str) and bool(
        _GRANT_MCP_RE.match(grant)
        or _GRANT_BASH_RE.match(grant)
        or _GRANT_BARE_RE.match(grant)
    )


def _grant_shape_warnings(
    bot_name: str,
    source_kind: str,
    source_name: str,
    grants: list[str],
    *,
    allow_side: bool,
) -> list[str]:
    """Grammar (F3(a)) + missing-scoped-Bash warnings for a list of declared grants.

    ``allow_side`` gates the bare-``Bash`` warning: *granting* bare ``Bash`` is
    over-broad (a specific ``Bash(<cmd> *)`` is missing), but *denying* bare
    ``Bash`` (deny-all shell) is legitimate, so it is not flagged.
    """
    out: list[str] = []
    for grant in grants:
        if not _grant_wellformed(grant):
            out.append(
                f"bot '{bot_name}': {source_kind} '{source_name}' grant '{grant}' is "
                "malformed — must be an mcp__ glob, a Bash(...) grant, or a bare tool name"
            )
        elif allow_side and grant == "Bash":
            out.append(
                f"bot '{bot_name}': {source_kind} '{source_name}' grants bare 'Bash' — "
                "scope it to Bash(<cmd> *) so the contract is specific (missing scoped Bash(...))"
            )
    return out


def _mcp_contract(frag_path: Path) -> dict:
    """An MCP fragment's ``_permissions_contract`` (``{}`` on missing/malformed).

    Remove with the P8 ``_permissions_contract`` cut of the wildcard path — it
    only exists to power the migration-gap warning below, which is dead once
    the legacy contract's grant role is gone.
    """
    try:
        frag = json.loads(frag_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return frag.get("_permissions_contract") or {}


def _validate_bots(
    fleet: FleetConfig,
    paths: Paths,
    fleet_env: dict[str, str],
    report: ValidationReport,
) -> None:
    """Per-bot validation: expertise, voice, skills, MCP, env vars, integrations, etc."""
    # Pre-compute available names for suggestion hints (avoids per-bot re-scan)
    avail_expertise = _available_names(paths, "expertise")
    avail_mcp = _available_names(paths, "mcp", ext=".json")
    avail_tools = set(paths.library_dir_names("tools", "tool.yaml"))

    # The claudron CLI door is host state, not per-bot state — probe once.
    claudron_on_path = shutil.which("claudron") is not None
    # Same shape: the operator's git identity is one host file every bot's
    # composed .gitconfig includes, and the probe shells out — so resolve it once,
    # and only when some bot actually declares git_credentials.
    git_identity_problem = (
        _operator_git_identity_problem()
        if any(b.git_credentials for b in fleet.bots.values())
        else None
    )
    # Vault resolution is a full walk-up + scan per call, and `claudron_vault_path`
    # falls back to a fleet-wide default (config.py) — so every bot in a fleet
    # typically resolves the *same* path. Memo per run, same reason as above.
    vault_resolutions: dict[str, bool] = {}

    # Grant-contract readers (folder-aware; shared with the P2 composer resolvers).
    from .loader import (
        integration_tool_grants,
        iter_guardrail_permissions,
        iter_integration_grants,
        iter_skill_grants,
    )

    # The L1 source guard runs at generate-time (composer.compose_bot); mirror it
    # here so `validate` ≡ `generate` for the deny-by-default path rule — an
    # unanchored, undeclared absolute in a bot source is a hard error, surfaced
    # before generate is ever attempted. Two source classes need two calls: the
    # dataclass / MCP-fragment leaves via audit_bot_sources, and the grant paths
    # (which live in the composed settings.local.json allow-list, not on BotConfig)
    # via compose_settings_local — the same pure builder generate raises from (#704).
    from .composer import _load_bot_fragments, compose_settings_local
    from .path_audit import audit_bot_sources

    for bot_name, bot in fleet.bots.items():
        bot_env = dotenv.read(paths.bot_runtime(bot_name) / ".env")
        effective_env: dict[str, str] = {**os.environ, **fleet_env, **bot_env}

        for sf in audit_bot_sources(bot, fleet, paths, _load_bot_fragments(bot, paths)):
            report.errors.append(
                f"bot '{bot_name}': {sf.source} = {sf.value!r} — {sf.reason}: "
                f"{sf.path} (anchor on FLEET_ROOT/BOT_DIR/CLAUDLOBBY_ROOT, declare "
                "in external_paths, or drop any shell metacharacter)"
            )

        # Grant-path parity: the allow-list L1 deny (a foreign absolute in
        # tool_permissions.allow or an expertise/guardrail/skill/integration grant)
        # fires inside the composer's settings assembly, never in audit_bot_sources
        # — grant strings are EXEMPT on the walk, classified at the settings choke.
        # Run that pure, zero-write builder so the census catches what generate
        # would; collect-all — convert its raise to a report error, never abort.
        try:
            compose_settings_local(bot, fleet, paths)
        except ValueError as exc:
            report.errors.append(f"bot '{bot_name}': {exc}")

        # Expertise — at least one must exist (HARD)
        if not bot.expertise:
            report.errors.append(
                f"bot '{bot_name}': expertise list is empty — need at least one entry from library/expertise/"
            )
        for area in bot.expertise:
            if paths.find_library_file("expertise", area, ".md") is None:
                suggestion = closest_match(area, avail_expertise)
                hint = f" — did you mean '{suggestion}'?" if suggestion else ""
                report.errors.append(
                    f"bot '{bot_name}': expertise '{area}' not found in overlay or base library{hint}"
                )

        # Voice (warn)
        if bot.voice:
            if paths.find_voice_file(bot.voice) is None:
                report.warnings.append(
                    f"bot '{bot_name}': voice file '{bot.voice}' not found — bare expertise will be used"
                )

        # Skills (warn). Accepts:
        #   name        — skills/name/
        #   dir/name    — skills/dir/name/
        #   dir/        — folder expansion (skills/dir/**)
        for skill in bot.skills:
            if skill.endswith("/"):
                dir_name = skill.rstrip("/")
                if not paths.expand_skill_folder(dir_name):
                    report.warnings.append(
                        f"bot '{bot_name}': skill folder '{skill}' empty or missing in any library/skills/ — no skills will be linked"
                    )
            elif paths.find_library_dir("skills", skill) is None:
                report.warnings.append(
                    f"bot '{bot_name}': skill '{skill}' not in any library/skills/ — symlink will be skipped"
                )

        # MCP fragment existence (warn). bot.mcp is list[McpEntry]; the file
        # on disk is named after .name regardless of how many instances the
        # entry composes into .mcp.json.
        for mcp in bot.mcp:
            frag_path = paths.find_library_file("mcp", mcp.name, ".json")
            if frag_path is None:
                suggestion = closest_match(mcp.name, avail_mcp)
                hint = f" — did you mean '{suggestion}'?" if suggestion else ""
                report.warnings.append(
                    f"bot '{bot_name}': mcp fragment '{mcp.name}.json' not found — server will not be configured{hint}"
                )
            else:
                # Migration-gap warning (remove with the P8 _permissions_contract cut):
                # a fragment that still grants tools whose paired integration hasn't
                # been given covering tool_grants would be dropped by the eventual
                # cut. A read-only-split fragment must NOT be covered by a wildcard —
                # the hint mirrors what compose will actually accept.
                contract = _mcp_contract(frag_path)
                if contract.get("tools") and not integration_tool_grants(
                    paths, mcp.name
                ):
                    hint = (
                        f"mirror its read_only_tools as exact mcp__{mcp.name}__<tool> entries"
                        if contract.get("read_only_tools") is not None
                        else f'add tool_grants: ["mcp__{mcp.name}__*"]'
                    )
                    report.warnings.append(
                        f"bot '{bot_name}': mcp '{mcp.name}' grants tools via _permissions_contract "
                        f"but the paired integration '{mcp.name}.md' has no tool_grants — the grant "
                        f"won't migrate ({hint})"
                    )

        # MCP env-contract check (warn) — uses the canonical instance-renamed
        # var names (the same names composer puts into the rendered
        # `.mcp.json`), and looks across the full 3-tier env (host →
        # fleet/.env → bot/.env). Replaces a fragile placeholder-scan that
        # didn't know about instance scoping or bot-tier .env files.
        for var, tier, source, instance in _mcp_required_vars(bot, paths):
            if _env_has_value(effective_env, var):
                continue
            inst_note = f" (instance: {instance})" if instance else ""
            report.warnings.append(
                f"bot '{bot_name}': {source}{inst_note} requires {var} but it's not set — "
                f"add to {tier}-tier .env (MCP server will fail at runtime)"
            )

        # Integrations (warn). Accepts `name`, `dir/name`, or `dir/`.
        for integ in bot.integrations:
            if integ.endswith("/"):
                dir_name = integ.rstrip("/")
                if not paths.expand_library_folder("integrations", dir_name):
                    report.warnings.append(
                        f"bot '{bot_name}': integration folder '{integ}' empty or missing in any library/integrations/ — skipped"
                    )
            elif paths.find_library_file("integrations", integ, ".md") is None:
                report.warnings.append(
                    f"bot '{bot_name}': integration '{integ}' not in any library/integrations/ — skipped"
                )

        # Grant contracts on equipped sources — integrations (additive tool_grants),
        # skills (additive tool_grants), guardrails (deny-capable permissions:) — all
        # validated against the single F3(a) grammar via _grant_shape_warnings.
        # iter_integration_grants folder-expands dir/ equips so a contract nested in
        # an expanded folder is not silently skipped (same guarantee as skills).
        from .composer import resolve_effective_integrations

        for name, grants in iter_integration_grants(
            paths, resolve_effective_integrations(bot, paths)
        ):
            report.warnings.extend(
                _grant_shape_warnings(
                    bot_name, "integration", name, grants, allow_side=True
                )
            )
        for name, grants in iter_skill_grants(paths, bot.skills):
            report.warnings.extend(
                _grant_shape_warnings(bot_name, "skill", name, grants, allow_side=True)
            )
        for name, gperms in iter_guardrail_permissions(paths, bot.guardrails):
            if gperms is None:
                continue
            report.warnings.extend(
                _grant_shape_warnings(
                    bot_name, "guardrail", name, gperms.allow, allow_side=True
                )
            )
            report.warnings.extend(
                _grant_shape_warnings(
                    bot_name, "guardrail", name, gperms.deny, allow_side=False
                )
            )

        # Briefing source coverage (warn). A briefing-equipped bot with no
        # integrations and no mcp servers has nothing to summarize — the skill
        # would render only self-derivable sections. Parse-time already
        # hard-rejects malformed slot names / cron (config._coerce_briefing).
        if bot.briefing and not bot.integrations and not bot.mcp:
            report.warnings.append(
                f"bot '{bot_name}': briefing equipped but no integrations/mcp "
                "source coverage — sections that read external data will be empty"
            )

        # Guardrails / protocols / resources / lessons / post_actions (warn).
        # Each entry can be `name`, `dir/name`, or `dir/` (folder expansion).
        for ref, kind in [
            (bot.guardrails, "guardrails"),
            (bot.protocols, "protocols"),
            (bot.resources, "resources"),
            (bot.lessons, "lessons"),
            (bot.post_actions, "post_actions"),
        ]:
            for item in ref:
                if item.endswith("/"):
                    dir_name = item.rstrip("/")
                    if not paths.expand_library_folder(kind, dir_name):
                        report.warnings.append(
                            f"bot '{bot_name}': {kind[:-1]} folder '{item}' empty or missing in any library/{kind}/ — no items will be loaded"
                        )
                elif paths.find_library_file(kind, item, ".md") is None:
                    report.warnings.append(
                        f"bot '{bot_name}': {kind[:-1]} '{item}' not in any library/{kind}/ — section will be skipped"
                    )

        # Tools (library/tools/ refs). Ref/manifest/param defects are HARD
        # errors — generate would raise on the same defect, and a bad param
        # means a broken 0755 executable. The env contract mirrors the MCP
        # check above: warn only, the script fails at runtime.
        tool_targets: dict[str, str] = {}  # target filename → tool name
        for tool_entry in bot.tools:
            tool_dir = paths.find_library_dir("tools", tool_entry.name)
            if tool_dir is None:
                suggestion = closest_match(tool_entry.name, avail_tools)
                hint = f" — did you mean '{suggestion}'?" if suggestion else ""
                report.errors.append(
                    f"bot '{bot_name}': tool '{tool_entry.name}' not in any library/tools/{hint}"
                )
                continue
            try:
                manifest = tool_resolve.load_tool_manifest(tool_dir)
                template_path = tool_resolve.tool_template_path(tool_dir, manifest)
                tool_resolve.resolve_tool_params(
                    tool_entry.name, manifest, tool_entry.params, bot.external_paths
                )
            except ValueError as e:
                report.errors.append(f"bot '{bot_name}': {e}")
                continue
            target = tool_resolve.tool_target_name(template_path)
            if target in tool_targets:
                report.errors.append(
                    f"bot '{bot_name}': tools '{tool_targets[target]}' and "
                    f"'{tool_entry.name}' both render '{target}' — generate would fail"
                )
            else:
                tool_targets[target] = tool_entry.name
            for var in manifest.get("env") or []:
                if not _env_has_value(effective_env, var):
                    report.warnings.append(
                        f"bot '{bot_name}': tool '{tool_entry.name}' requires {var} but it's not set — "
                        f"add to a .env tier (script will fail at runtime)"
                    )

        # Telegram token env (warn). Value-based, not mere presence: warn when the
        # token_env resolves EMPTY or absent, so a scaffolded-but-unfilled stub
        # still nudges. A presence check fires once (the first cold generate,
        # before the stub exists) then goes permanently silent even if the operator
        # never fills in a real value (#755). Reading effective_env lets bot-tier
        # .env values count (the common case — per-bot Telegram tokens live in
        # runtime/bots/<bot>/.env so multi-bot fleets don't cross-wire). Skip the
        # self-referential case (token_env == TELEGRAM_BOT_TOKEN): the compositor
        # deliberately does not scaffold it and its home is the plugin's channel-dir
        # .env, which is not a tier we inspect — so the check would false-alarm on
        # the documented default (#750).
        if (
            bot.telegram.token_env
            and not bot.telegram.token_env_is_self_referential
            and not _env_has_value(effective_env, bot.telegram.token_env)
        ):
            report.warnings.append(
                f"bot '{bot_name}': telegram.token_env '{bot.telegram.token_env}' not set in any tier of .env — bot won't connect to Telegram"
            )

        # Git credential env (warn, never fail). Same value-based shape as
        # telegram.token_env above: a declared org whose token is missing or empty
        # composes valid routing that then presents no credential, so git silently
        # falls through to the host default and the push fails with a 403 that reads
        # like a permissions problem. A missing token is an operator gap, not a
        # composition error — warn and still generate.
        for org, env_name in sorted(bot.git_credentials.items()):
            if not _env_has_value(effective_env, env_name):
                report.warnings.append(
                    f"bot '{bot_name}': git_credentials['{org}'] names '{env_name}', "
                    f"not set in any tier of .env — pushes to {org} will fall back to "
                    f"the host credential helper and may 403"
                )

        # The other half of the same declaration: routing composes an [include] of
        # the operator's ~/.gitconfig for identity, and git ignores a missing or
        # identity-less include silently — so the bot pushes fine and then cannot
        # commit at all. Warn (never fail): it is an operator-side host gap, same
        # severity as the missing token above.
        if bot.git_credentials and git_identity_problem:
            report.warnings.append(
                f"bot '{bot_name}': git_credentials composes an [include] for git "
                f"identity, but {git_identity_problem} — credential routing will work "
                f"while every commit fails 'Author identity unknown'"
            )

        # Observability config (warn). Fields may be None (= use hardcoded default);
        # only validate when explicitly set.
        obs = bot.observability
        if obs.pulse_interval is not None:
            if obs.pulse_interval <= 0:
                report.warnings.append(
                    f"bot '{bot_name}': observability.pulse_interval must be > 0 (got {obs.pulse_interval})"
                )
            elif obs.pulse_interval > 3600:
                report.warnings.append(
                    f"bot '{bot_name}': observability.pulse_interval > 3600s (1h) is unusually long — got {obs.pulse_interval}"
                )
        if obs.reap_days is not None:
            if obs.reap_days <= 0:
                report.warnings.append(
                    f"bot '{bot_name}': observability.reap_days must be > 0 (got {obs.reap_days})"
                )
            elif obs.reap_days > 365:
                report.warnings.append(
                    f"bot '{bot_name}': observability.reap_days > 365 is unusually long — got {obs.reap_days}"
                )
        if obs.bridge_heal_max_attempts is not None and not (
            1 <= obs.bridge_heal_max_attempts <= 10
        ):
            report.warnings.append(
                f"bot '{bot_name}': observability.bridge_heal_max_attempts must be "
                f"1..10 (got {obs.bridge_heal_max_attempts})"
            )

        # Model validation (warn + pass-through)
        if bot.model and bot.model not in KNOWN_MODELS:
            suggestion = closest_match(bot.model, KNOWN_MODELS)
            hint = f" — did you mean '{suggestion}'?" if suggestion else ""
            report.warnings.append(
                f"bot '{bot_name}': model '{bot.model}' not in known models{hint}. "
                f"Known: {', '.join(sorted(KNOWN_MODELS))}. "
                f"Passing through as-is (may be a new model)."
            )

        # model_strategy.base and escalate_to (warn + suggest)
        if bot.model_strategy:
            for field_name, val in [
                ("base", bot.model_strategy.base),
                ("escalate_to", bot.model_strategy.escalate_to),
            ]:
                if val and val not in KNOWN_MODELS:
                    suggestion = closest_match(val, KNOWN_MODELS)
                    hint = f" — did you mean '{suggestion}'?" if suggestion else ""
                    report.warnings.append(
                        f"bot '{bot_name}': model_strategy.{field_name} '{val}' not in known models{hint}"
                    )

        # Hook event keys (warn)
        for event in bot.hooks:
            if event not in KNOWN_HOOK_EVENTS:
                suggestion = closest_match(event, KNOWN_HOOK_EVENTS)
                hint = f" — did you mean '{suggestion}'?" if suggestion else ""
                report.warnings.append(
                    f"bot '{bot_name}': hook event '{event}' not recognized{hint}. "
                    f"Known events: {', '.join(sorted(KNOWN_HOOK_EVENTS))}. "
                    f"This hook will be silently ignored by Claude Code."
                )

        # RC-killing env vs remote-control/channels (error, #533). extra_flags
        # is checked too so a raw "--remote-control" there gets the same guard.
        # See RC_KILLING_ENV_VARS for why these vars break channel replies.
        # Scope: bot.env is exactly what composes today (config.py doesn't merge
        # defaults.env; the composer emits only bot.env) — if defaults.env ever
        # merges, this check must follow it or it becomes a silent hole.
        needs_rc = (
            bot.remote_control
            or bot.channels
            or any("--remote-control" in f for f in bot.extra_flags)
        )
        if needs_rc:
            for var in RC_KILLING_ENV_VARS:
                if var in bot.env:
                    report.errors.append(
                        f"bot '{bot_name}': env sets {var} but the bot uses "
                        f"remote-control/channels — this var disables Claude Code's "
                        f"feature-flag evaluation and with it remote-control, so "
                        f"channel replies are silently dropped (#533). Remove it, or "
                        f"set remote_control: false and channels: [] if this bot "
                        f"genuinely needs it."
                    )

        # Ecosystem: the Claudron door a vault-wired bot actually walks through
        # is the CLI (Claudron's CLI_CONTRACT is the consumption ABI) — never an
        # MCP server. Warn, never error: a fleet is legitimately composed on a
        # host that has no claudron installed yet.
        if bot.claudron_vault_path:
            if not claudron_on_path:
                report.warnings.append(
                    f"bot '{bot_name}': claudron_vault_path is set but the claudron CLI "
                    f"is not on PATH — bots reach the vault through the CLI "
                    f"(see {CLAUDRON_INTEGRATION_URL})"
                )
            vault_path = Path(bot.claudron_vault_path).expanduser()
            if not vault_path.is_dir():
                report.warnings.append(
                    f"bot '{bot_name}': claudron_vault_path "
                    f"'{bot.claudron_vault_path}' is not a directory on this host — "
                    f"the bot will get no vault (see {CLAUDRON_INTEGRATION_URL})"
                )
            else:
                # Memo the scan, not just its result: guard the call so
                # detect_vault (a full walk-up) runs once per distinct path, not
                # once per bot. `setdefault(k, detect_vault(...))` evaluates the
                # default eagerly every iteration and would not save the scan.
                key = bot.claudron_vault_path
                if key not in vault_resolutions:
                    vault_resolutions[key] = detect_vault(vault_path) is not None
                if not vault_resolutions[key]:
                    report.warnings.append(
                        f"bot '{bot_name}': claudron_vault_path "
                        f"'{bot.claudron_vault_path}' does not resolve to a vault — no "
                        f"'_shared/' (or 'shared/') marker found walking up "
                        f"(see {CLAUDRON_INTEGRATION_URL})"
                    )

        # Ecosystem: the session loop (L2) needs a vault to run against. An
        # explicit `claudron_session_loop: true` with no `claudron_vault_path` is
        # an error — its hooks (pull/recall/push) and narrow verb grants are
        # meaningless without one. Left UNSET the loop defaults on only when a
        # vault is wired (composer._session_loop_enabled), so this never fires on
        # the default path — only on an opt-in that cannot work. (Loop enabled +
        # vault set but claudron CLI absent is covered by the L1 PATH warning above.)
        if bot.claudron_session_loop is True and not bot.claudron_vault_path:
            report.errors.append(
                f"bot '{bot_name}': claudron_session_loop is true but "
                f"claudron_vault_path is unset — the session loop has no vault to "
                f"pull, recall, or push against. Set claudron_vault_path, or remove "
                f"claudron_session_loop (see {CLAUDRON_INTEGRATION_URL})"
            )

        # Account (warn)
        if bot.account not in fleet.accounts:
            report.warnings.append(
                f"bot '{bot_name}': account '{bot.account}' not in fleet.accounts — falling back to 'default'"
            )

        # Tool deny vs expertise conflict (warn). Flag when a denied tool is
        # core to the bot's expertise — the bot won't be able to do its job.
        if bot.tool_permissions.deny:
            denied = set(bot.tool_permissions.deny)
            for area in bot.expertise:
                core = EXPERTISE_CORE_TOOLS.get(area, set())
                conflict = denied & core
                if conflict:
                    report.warnings.append(
                        f"bot '{bot_name}': tools.deny includes {sorted(conflict)} "
                        f"but expertise '{area}' typically requires them"
                    )
            # Also warn if same tool appears in both allow and deny
            if bot.tool_permissions.allow:
                overlap = denied & set(bot.tool_permissions.allow)
                if overlap:
                    report.warnings.append(
                        f"bot '{bot_name}': tools {sorted(overlap)} appear in both allow and deny lists"
                    )

        # Autonomous-runner block (Phase 4). Soft validation: mostly warnings
        # since the wrapper at runtime is more authoritative than this static
        # checker. One hard error: github_issues picker without a label.
        ar = bot.autonomous_runner
        if ar is not None:
            if ar.skill not in AUTO_ELIGIBLE_SKILLS:
                suggestion = closest_match(ar.skill, AUTO_ELIGIBLE_SKILLS)
                hint = f" — did you mean '{suggestion}'?" if suggestion else ""
                report.warnings.append(
                    f"bot '{bot_name}': autonomous_runner.skill '{ar.skill}' is not on the "
                    f"--auto-eligible list — the wrapper will still invoke it, but unknown "
                    f"clauDNA skills may not emit a structured result{hint}"
                )

            if not _CADENCE_RE.match(ar.cadence):
                report.warnings.append(
                    f"bot '{bot_name}': autonomous_runner.cadence '{ar.cadence}' doesn't match "
                    f"<N><m|h|d> — the bot may not fire on the expected interval"
                )

            if "/" not in ar.target_repo or ar.target_repo.count("/") != 1:
                report.warnings.append(
                    f"bot '{bot_name}': autonomous_runner.target_repo '{ar.target_repo}' "
                    f"should be 'org/repo' format"
                )

            if ar.picker is not None:
                if ar.picker.type == "github_issues" and not ar.picker.label:
                    report.errors.append(
                        f"bot '{bot_name}': autonomous_runner.picker.label is required when "
                        f"type='github_issues'"
                    )

            if ar.bypass is not None:
                if ar.bypass.on_bypass not in BYPASS_ACTIONS:
                    report.warnings.append(
                        f"bot '{bot_name}': autonomous_runner.bypass.on_bypass "
                        f"'{ar.bypass.on_bypass}' not in known set "
                        f"({sorted(BYPASS_ACTIONS)})"
                    )

            for k, v in ar.on_outcome.items():
                if k not in OUTCOME_KEYS:
                    report.warnings.append(
                        f"bot '{bot_name}': autonomous_runner.on_outcome key '{k}' is not a "
                        f"known outcome (expected one of {sorted(OUTCOME_KEYS)})"
                    )
                if v not in OUTCOME_ACTIONS:
                    report.warnings.append(
                        f"bot '{bot_name}': autonomous_runner.on_outcome action '{v}' is not "
                        f"a known action (expected one of {sorted(OUTCOME_ACTIONS)})"
                    )

            for hook in ar.pre_hooks + ar.post_hooks:
                if not hook.startswith("/claudna:"):
                    report.warnings.append(
                        f"bot '{bot_name}': autonomous_runner hook '{hook}' is not a "
                        f"/claudna: skill — hooks should be clauDNA skill names"
                    )


def _validate_teams(fleet: FleetConfig, report: ValidationReport) -> None:
    """Org structure integrity and team membership checks."""
    # Org structure integrity (warn — bot_ids may reference other fleets)
    for bot_name, bot in fleet.bots.items():
        if bot.reports_to and bot.reports_to not in fleet.bots:
            report.warnings.append(
                f"bot '{bot_name}': reports_to '{bot.reports_to}' not found in fleet.bots"
            )
        if bot.manages:
            for managed_id in bot.manages:
                if managed_id not in fleet.bots:
                    report.warnings.append(
                        f"bot '{bot_name}': manages '{managed_id}' not found in fleet.bots"
                    )

    # Team integrity (warn)
    for team in fleet.teams.values():
        if team.manager not in fleet.bots:
            report.warnings.append(
                f"team '{team.name}': manager '{team.manager}' is not in fleet.bots"
            )
        for worker in team.workers:
            if worker not in fleet.bots:
                report.warnings.append(
                    f"team '{team.name}': worker '{worker}' is not in fleet.bots"
                )


def _check_relative_file(
    label: str, value: str, base: Path, report: ValidationReport, *, hard: bool = False
) -> None:
    """Shared check for config paths defined as relative-to-their-config-file
    (fleet.mission_file, project mission_file): warn on absolute (pathlib's
    / operator would silently discard base), on `..` components (escapes the
    overlay the same way), and on a missing target.

    ``hard=True`` routes the absolute-path case to ``report.errors`` instead of
    ``warnings`` — the L1 deny-by-default posture for fleet.mission_file, whose
    absolute the composer emits into every bot's CLAUDE.md. The `..`/missing
    branches stay warnings (both hard and soft callers share them)."""
    p = Path(value)
    if p.is_absolute():
        sink = report.errors if hard else report.warnings
        sink.append(f"{label} '{value}' is absolute — must be relative to {base}")
    elif ".." in p.parts:
        report.warnings.append(
            f"{label} '{value}' contains '..' — must stay under {base}"
        )
    elif not (base / value).is_file():
        report.warnings.append(f"{label} '{value}' not found under {base}")


def _validate_mission(
    fleet: FleetConfig, paths: Paths, report: ValidationReport
) -> None:
    """Fleet mission — pairing rule: the charter file requires the paragraph,
    so the every-bot anchor can never be starved by a file-only config."""
    if fleet.mission_file and not fleet.mission:
        report.errors.append(
            "fleet.mission_file requires fleet.mission (the one-paragraph "
            "anchor every bot receives)"
        )
    if fleet.mission and "\n" in fleet.mission.strip():
        # .strip(): a YAML folded scalar (mission: >) legitimately ends with
        # a chomped newline — only INTERIOR newlines are the corruption.
        report.errors.append(
            "fleet.mission must be a single paragraph without newlines — it "
            "is rendered into every bot's composed CLAUDE.md (put long-form "
            "content in mission_file)"
        )
    if fleet.mission_file:
        _check_relative_file(
            "fleet.mission_file",
            fleet.mission_file,
            paths.fleet_config_dir,
            report,
            hard=True,
        )


_WORKSTREAMS_KEYS = {"max_active", "lease_days"}


def _validate_workstreams(fleet: FleetConfig, report: ValidationReport) -> None:
    """fleet.workstreams — positive-int bounds; unknown keys warn. The loader is
    tolerant (a bad value falls back to the default); this is where the operator
    hears about it."""
    raw = fleet.workstreams.raw
    if not raw:
        return
    for key in sorted(_WORKSTREAMS_KEYS):
        if key in raw and not is_pos_int(raw[key]):
            report.errors.append(
                f"fleet.workstreams.{key} must be a positive integer, got "
                f"{raw[key]!r} (the default was used instead)"
            )
    for unknown in sorted(k for k in raw if k not in _WORKSTREAMS_KEYS):
        report.warnings.append(
            f"fleet.workstreams: unknown key '{unknown}'"
            f"{hint(unknown, _WORKSTREAMS_KEYS)}"
        )


def _validate_fleet(fleet: FleetConfig, report: ValidationReport) -> None:
    """Fleet-level dependency checks."""
    # Warn about disabling defaults — unusual, worth flagging
    if not fleet.plugins.include_defaults:
        report.warnings.append(
            "plugins.include_defaults is false — default plugins (claudna) will not be installed"
        )

    # Plugin name format: must be name@marketplace
    for plugin in fleet.plugins.required:
        if not re.match(r"^[\w-]+@[\w-]+$", plugin):
            report.warnings.append(
                f"plugin '{plugin}' does not match expected name@marketplace format"
            )

    # Marketplace source format: github repos must be org/repo
    for mp_name, mp_config in fleet.plugins.marketplaces.items():
        src = mp_config.get("source", {})
        if isinstance(src, dict):
            src_type = src.get("source", "")
            src_repo = src.get("repo", "")
            if src_type == "github" and not _ORG_REPO_RE.match(src_repo):
                report.warnings.append(
                    f"marketplace '{mp_name}': repo '{src_repo}' does not match "
                    "expected <org>/<repo> format"
                )

    if not fleet.plugins.required:
        return

    # Check installed_plugins.json for declared plugins
    config_dir = Path.home() / ".claude"
    installed_path = config_dir / "plugins" / "installed_plugins.json"

    if not installed_path.is_file():
        report.warnings.append(
            f"plugins declared in fleet.yaml but {installed_path} not found — "
            "run 'claude plugin install <plugin>' for each declared plugin, or "
            "set CLAUDE_CODE_SYNC_PLUGIN_INSTALL=1 to auto-install on first bot start"
        )
        return

    try:
        installed = json.loads(installed_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        report.warnings.append(
            f"could not read {installed_path}: {exc} — plugin state unknown"
        )
        return

    installed_plugins = installed.get("plugins", {})
    for plugin in fleet.plugins.required:
        if plugin not in installed_plugins:
            # Say when it resolves itself. Declared-but-absent is the NORMAL
            # state before a bot has ever started: start-bot.sh installs declared
            # plugins at launch. Without that context the warning reads as a
            # contradiction of the documented "auto-installed as a fleet default,
            # no manual setup needed", and sends users to install by hand.
            report.warnings.append(
                f"plugin '{plugin}' declared in fleet.yaml but not installed yet — "
                "bots install declared plugins at startup, so this usually clears "
                "on first start; to install now, run "
                f"'claude plugin install {plugin}'"
            )


# projects.yaml keys become PROJECT_TIER_<SLUG> env names — same charset as
# bot ids so ProjectConfig.env_slug always yields a shell identifier.
_PROJECT_KEY_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _validate_projects(
    fleet: FleetConfig, paths: Paths, report: ValidationReport
) -> None:
    """Validate the optional projects.yaml tier (goal-aware fleet, P2)."""
    if fleet.projects:
        # bot env: blocks are emitted AFTER the projects tier map in
        # bot.conf, so an env: key in this namespace silently overrides the
        # project's declared closure bar at source time (last assignment
        # wins — a human-tier project flips to auto with zero warning).
        # Reserved namespace, hard error.
        for bot_name, bot in fleet.bots.items():
            for env_key in bot.env:
                if env_key.startswith(("PROJECT_TIER_", "PROJECT_REPOS_")):
                    report.errors.append(
                        f"bot '{bot_name}': env key '{env_key}' is in the "
                        f"reserved projects namespace — it would clobber the "
                        f"tier map composed from projects.yaml"
                    )

    repo_owners: dict[str, str] = {}
    for key, project in fleet.projects.items():
        label = f"project '{key}'"

        if not _PROJECT_KEY_RE.match(key):
            report.errors.append(
                f"project key '{key}' is invalid — use lowercase kebab-case "
                f"([a-z][a-z0-9-]*), it becomes the PROJECT_TIER_* env name"
            )

        # title renders verbatim into the manager's composed CLAUDE.md table:
        # a newline is a prompt-injection surface (fake sections in the
        # agent's own instructions), a pipe breaks the table. Corruption
        # class -> error, with a composer backstop for unvalidated paths.
        if "\n" in project.title or "|" in project.title:
            report.errors.append(
                f"{label}: title must not contain newlines or '|' — it is "
                f"rendered into the composed CLAUDE.md projects table"
            )

        if project.validation.tier not in VALID_TIERS:
            report.errors.append(
                f"{label}: validation.tier '{project.validation.tier}' is not "
                f"one of {'/'.join(VALID_TIERS)}"
                f"{hint(project.validation.tier, VALID_TIERS)}"
            )

        if not project.repos:
            report.errors.append(
                f"{label}: repos is empty — repos is the join key that maps "
                f"work to this project's closure bar"
            )
        for repo in project.repos:
            if any(c.isspace() for c in repo):
                # PROJECT_REPOS_* is word-split when consumed in shell — an
                # embedded space silently corrupts the list.
                report.errors.append(
                    f"{label}: repos entry '{repo}' contains whitespace — "
                    f"it would word-split when PROJECT_REPOS_* is consumed"
                )
            elif not _ORG_REPO_RE.match(repo):
                report.warnings.append(
                    f"{label}: repos entry '{repo}' does not match <org>/<repo> format"
                )
            elif repo in repo_owners and repo_owners[repo] != key:
                report.warnings.append(
                    f"repo '{repo}' is claimed by both '{repo_owners[repo]}' "
                    f"and '{key}' — tier resolution is ambiguous"
                )
            else:
                repo_owners[repo] = key

        if project.mission_file:
            _check_relative_file(
                f"{label}: mission_file",
                project.mission_file,
                paths.fleet_config_dir,
                report,
            )

        for unknown in sorted(project.raw):
            if unknown == "metrics":
                report.warnings.append(
                    f"{label}: 'metrics' is reserved for the metrics plan and "
                    f"not part of the v1 schema — ignored"
                )
            else:
                report.warnings.append(
                    f"{label}: unknown key '{unknown}'{hint(unknown, PROJECT_KEYS)}"
                )

        for unknown in sorted(project.validation.raw):
            report.warnings.append(
                f"{label}: unknown validation key "
                f"'{unknown}'{hint(unknown, _PROJECT_VALIDATION_KEYS)}"
            )


def _validate_sweep(fleet: FleetConfig, report: ValidationReport) -> None:
    """Validate the opt-in fleet.sweep (rolling code-audit) block."""
    sweep = fleet.sweep
    if sweep is None or not sweep.enabled:
        return

    # owner_bot must name a real bot in this fleet
    if not sweep.owner_bot:
        report.errors.append("sweep.owner_bot is required when sweep is enabled")
    elif sweep.owner_bot not in fleet.bots:
        suggestion = closest_match(sweep.owner_bot, set(fleet.bots))
        hint = f" — did you mean '{suggestion}'?" if suggestion else ""
        report.errors.append(
            f"sweep.owner_bot '{sweep.owner_bot}' is not a bot in this fleet{hint}"
        )

    # Repos: an explicit sweep.repos list OR the owner's scope.repos must exist,
    # else the selector has nothing to audit.
    owner = fleet.bots.get(sweep.owner_bot) if sweep.owner_bot else None
    owner_scope = owner.scope.repos if (owner and owner.scope) else []
    if not sweep.repos and not owner_scope:
        report.errors.append(
            f"sweep is enabled but has no repos — set sweep.repos or give owner "
            f"'{sweep.owner_bot}' a scope.repos list"
        )

    for repo in sweep.repos:
        if not _ORG_REPO_RE.match(repo):
            report.warnings.append(
                f"sweep.repos entry '{repo}' does not match <org>/<repo> format"
            )

    # schedule is a systemd OnCalendar expression — light sanity check only.
    if not re.search(r"\d{1,2}:\d{2}", sweep.schedule):
        report.warnings.append(
            f"sweep.schedule '{sweep.schedule}' has no HH:MM time — expected a "
            f"systemd OnCalendar expression like '*-*-* 03:00:00'"
        )


def _validate_cross_fleet_collisions(
    fleet: FleetConfig, paths: Paths, report: ValidationReport
) -> None:
    """Warn when bot names collide with bots in other fleets on the same host.

    tmux session names are derived from the bot directory basename, so two
    fleets with a bot named 'alex' would fight over the same tmux session.
    Fleets are enumerated at both depths (flat ``local/<fleet>/`` and nested
    ``local/<system>/<fleet>/``), so a nested sibling is not invisible to the
    scan.
    """
    local_dir = paths.root / "local"
    if not local_dir.is_dir():
        return

    current_fleet = paths.fleet_dir.name if paths.fleet_dir else None
    bot_names = set(fleet.bots)

    for fleet_dir in _iter_fleet_dirs(local_dir):
        if fleet_dir.name == current_fleet:
            continue
        other_bots_dir = fleet_dir / "runtime" / "bots"
        if not other_bots_dir.is_dir():
            continue
        for bot_dir in sorted(other_bots_dir.iterdir()):
            if not bot_dir.is_dir() or not (bot_dir / "bot.conf").is_file():
                continue
            if bot_dir.name in bot_names:
                report.warnings.append(
                    f"bot '{bot_dir.name}' also exists in fleet '{fleet_dir.name}' "
                    f"— tmux session names will collide on this host"
                )


def _validate_timers(fleet: FleetConfig, report: ValidationReport) -> None:
    """Grant `validate` ≡ `generate` for timer scripts: a raw absolute in a fleet
    job's ``script`` fails generate (compose_fleet_timers), so the census must catch
    it too. Reads the fleet's own merged defaults (``fleet.defaults`` — the dict
    ``load_fleet`` builds and stores on the fleet), so the check runs on every
    surface that calls ``validate`` and cannot be silently skipped by a caller that
    forgets to thread it in. Gated on the composer's own emit condition (system
    defaults enabled + timers on) so validate never flags a job generate would not
    emit — a false positive there would itself break the zero-FP bar (#704)."""
    sd = fleet.system_defaults
    if not (sd.enabled and sd.timers):
        return
    from .path_audit import timer_script_findings

    for sf in timer_script_findings(fleet.defaults.get("jobs", {})):
        report.errors.append(
            f"{sf.source} = {sf.value!r} — {sf.reason}: {sf.path} "
            "(anchor the script on $CLAUDLOBBY_ROOT)"
        )


def _validate_library_frontmatter(paths: Paths, report: ValidationReport) -> None:
    """Fail loud on malformed YAML frontmatter in any library ``.md`` file.

    ``loader.parse_frontmatter`` degrades safely at compose (it drops an
    unparseable block), so a malformed library file would otherwise silently
    lose its frontmatter — and, before the loader was hardened, leak the raw
    ``---`` block into every equipping bot's composed CLAUDE.md. Catching it
    here blocks ``validate`` (and ``generate``) until the file is fixed.

    Scans every ``.md`` under the base library and the fleet overlay (READMEs
    excepted); ``.json`` MCP fragments and ``tool.yaml`` templates carry no
    markdown frontmatter and are out of scope.
    """
    from .loader import frontmatter_error

    roots = [paths.base_library, paths.overlay_library]
    seen = set()
    for root in roots:
        if root is None or not root.is_dir():
            continue
        for md in sorted(root.rglob("*.md")):
            if md.name.startswith("README"):
                continue
            resolved = md.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                text = md.read_text(encoding="utf-8")
            except OSError as exc:
                report.warnings.append(f"library file unreadable: {md} ({exc})")
                continue
            err = frontmatter_error(text)
            if err is not None:
                try:
                    rel = md.relative_to(root)
                except ValueError:
                    rel = md
                report.errors.append(
                    f"malformed frontmatter in library file '{rel}': {err}"
                )


# Literal placeholder tokens shipped in fleet.yaml.seed (three of them). Reaching
# validate() with one still in place means the template was copied but never
# filled in.
#
# This is an ERROR, never a warning. getting-started.md §4 tells the user that a
# run with "warnings only" is a success, so a warning here reads as "fine" — the
# user generates, spins the bot up, and claudfather tries to post to chat id
# REPLACE_ME. The failure then surfaces much later as a Telegram API error, far
# from its cause. Catching it pre-flight turns the most likely first-run mistake
# into an actionable message.
_PLACEHOLDER_TOKENS = {"replace_me", "change_me"}


def _is_placeholder(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in _PLACEHOLDER_TOKENS


def _validate_placeholders(fleet: FleetConfig, report: ValidationReport) -> None:
    """Hard-fail on unreplaced template placeholders."""
    if _is_placeholder(fleet.telegram_group_chat_id):
        report.errors.append(
            f"fleet.telegram_group_chat_id is still the template placeholder "
            f"'{fleet.telegram_group_chat_id}' — set your Telegram group id "
            "(add @RawDataBot to the group; the id starts with -100)"
        )
    if _is_placeholder(fleet.human_telegram_id):
        report.errors.append(
            f"fleet.human_telegram_id is still the template placeholder "
            f"'{fleet.human_telegram_id}' — set your Telegram user id "
            "(message @userinfobot to get it)"
        )

    for bot_name, bot in fleet.bots.items():
        if _is_placeholder(bot.telegram.handle):
            report.errors.append(
                f"bot '{bot_name}': telegram.handle is still the template placeholder "
                f"'{bot.telegram.handle}' — set the @handle BotFather gave the bot"
            )
        if _is_placeholder(bot.telegram.chat_id):
            report.errors.append(
                f"bot '{bot_name}': telegram.chat_id is still the template placeholder "
                f"'{bot.telegram.chat_id}' — set the bot's Telegram chat id"
            )


def validate(fleet: FleetConfig, paths: Paths) -> ValidationReport:
    """Validate a fleet against the library (env vars, MCP refs, scopes); returns a ValidationReport."""
    report = ValidationReport()

    if not fleet.bots:
        report.errors.append("fleet.bots is empty — nothing to compose")

    _validate_placeholders(fleet, report)

    fleet_env = dotenv.read(paths.env_file)
    _validate_bots(fleet, paths, fleet_env, report)
    _validate_teams(fleet, report)
    _validate_fleet(fleet, report)
    _validate_timers(fleet, report)
    _validate_mission(fleet, paths, report)
    _validate_workstreams(fleet, report)
    _validate_sweep(fleet, report)
    _validate_projects(fleet, paths, report)
    _validate_cross_fleet_collisions(fleet, paths, report)
    _validate_library_frontmatter(paths, report)

    # bench marker — multi-bot fleets should designate a bench bot
    if len(fleet.bots) > 1 and not any(b.bench for b in fleet.bots.values()):
        report.warnings.append(
            "fleet has multiple bots but none has bench: true — "
            "cold-start benchmarking will not know which bot to measure"
        )

    return report
