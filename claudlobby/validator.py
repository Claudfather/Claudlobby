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
from .config import (
    _PROJECT_VALIDATION_KEYS,
    GITHUB_APP_ENV_VARS,
    FleetConfig,
    is_pos_int,
)
from .known_values import (
    AUTO_ELIGIBLE_SKILLS,
    BYPASS_ACTIONS,
    DEPRECATED_ENV_TIERS,
    ENV_TIERS,
    EXPERTISE_CORE_TOOLS,
    KNOWN_CREDENTIAL_SOURCES,
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
    token_env, git_credentials, github_app — so a new site cannot reintroduce
    the fork.
    """
    return bool(effective_env.get(var))


def _git_config_probe(operator: Path, *query: str):
    """One ``git config --file <operator> --includes <query...>`` run, or None
    when the file or git is absent. ``--includes`` is NON-OPTIONAL and lives
    here so no probe can drop it: it is off by default for ``--file`` reads
    but ON when git reads the same file as global config — which is exactly
    how the bot will read it — so omitting it silently under-detects anything
    living one [include] deeper."""
    if not operator.is_file() or not shutil.which("git"):
        return None
    return subprocess.run(
        ["git", "config", "--file", str(operator), "--includes", *query],
        capture_output=True,
        text=True,
    )


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
    probe = _git_config_probe(operator, "--get", "user.email")
    if probe is None:
        return None  # cannot check; not the validator's business to guess
    if probe.returncode != 0 or not probe.stdout.strip():
        return f"{operator} sets no user.email"
    return None


def _operator_reverse_insteadof() -> str | None:
    """An ssh-forcing GitHub rewrite in the operator gitconfig, or None.

    The composed App routing works over https; a
    ``url."git@github.com:".insteadOf = https://github.com/`` (or
    ``pushInsteadOf``) in the INCLUDED operator config rewrites https remotes
    to ssh BEFORE the credential layer runs, bypassing it entirely (D6).
    Single-pass rewriting means nothing composable can undo it — the only
    honest handling is naming it at validate time. Same delegation posture as
    ``_operator_git_identity_problem``: ask git, never hand-parse the file.
    """
    from .composer import _operator_gitconfig  # local: composer imports config, not us

    operator = _operator_gitconfig()
    probe = _git_config_probe(operator, "--get-regexp", r"url\..*\.(push)?insteadof")
    if probe is None or probe.returncode != 0:
        return None  # unprobeable, or no insteadOf config at all
    for line in probe.stdout.splitlines():
        key, _, value = line.partition(" ")
        if "git@github.com" in key.lower() and "https://github.com" in value.lower():
            return f"{operator} carries '{key} = {value}'"
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


#: A permission rule carrying a path argument, e.g. ``Read(/a/b/**)``.
_PATH_RULE = re.compile(r"^(Read|Edit|Write|MultiEdit|Glob|Grep)\((/[^/].*)\)$")


def _inert_path_errors(
    bot_name: str, source_kind: str, source_name: str, grants: list[str]
) -> list[str]:
    """Hard errors for path rules that can never match (#1312).

    A permission-rule path with a SINGLE leading slash anchors at the SETTINGS
    SOURCE, not the filesystem root, so an absolute path names a directory that
    never exists and the rule silently permits exactly what it names. Measured in
    a scratch project, with a no-rule control and the mechanism shown directly:
    ``Read(/target/**)`` BLOCKS ``<project>/target/inside.txt``, while
    ``Read(/abs/target/**)`` does not block ``/abs/target/secret.txt`` and
    ``Read(//abs/target/**)`` does.

    This is an ERROR rather than a warning, and both halves of that are deliberate.

    **Error, because the class is always a no-op.** There is no bare-absolute path
    rule that works, so there is nothing to weigh — unlike an over-broad grant,
    which is a judgement call. A rule that looks like a constraint and enforces
    nothing is worse than no rule, because it is counted as coverage.

    **And because a warning would not be seen.** ``validate`` now emits a warning
    per bare-``Bash`` expertise grant (#1315), 19 of them on this host. A new
    warning class would arrive inside that wall. The failure this guards is
    silent by construction; its report must not be.

    Blast radius measured before choosing ERROR: **zero** declared bare-absolute
    path rules exist in ``library/``, in any fleet overlay, or in any
    ``fleet.yaml`` on this host. The composer was the only producer, and it no
    longer is. So this cannot fail an existing fleet — it exists to stop the class
    being reintroduced by hand.
    """
    out: list[str] = []
    for grant in grants:
        if not isinstance(grant, str):
            continue
        match = _PATH_RULE.match(grant.strip())
        if match:
            out.append(
                f"bot '{bot_name}': {source_kind} '{source_name}' rule '{grant}' can "
                "never match — a single leading slash anchors at the settings "
                "source, not the filesystem root. Use "
                f"'{match.group(1)}(/{match.group(2)})' for an absolute path."
            )
    return out


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


def _env_contract_errors(contract: object, where: str) -> list[str]:
    """Shape errors in one env contract, from EITHER declaration surface.

    Two rules, both hard errors:

    - every entry declares ``secret`` (bool). Required rather than inferred
      because a name heuristic re-derives a fact the contract author already
      knows, and its failures are silent in BOTH directions — a mislabelled
      secret never fires the credential alert, and a mislabelled config fires
      forever until someone suppresses the rung and loses the real alerts with
      it (fork F4).
    - ``source``, when present, is a whole identifier in the closed registry.
      Rejecting an unregistered value is what keeps fork F5's injection
      guarantee true: the resolver dispatches on the entire string through a
      fixed per-entry ``case`` arm, so an unrecognised value must never reach
      it rather than being passed through as command text.

    Surface-agnostic by design: MCP fragments and integration-doc frontmatter
    declare the same facts, so one rule serves both and neither can drift.
    *where* is the caller's label for the declaring file.

    Takes the PARSED contract: this is a property of the file, and the caller
    scans the library once rather than re-reading per equipping bot.
    """
    if not isinstance(contract, dict):
        return []

    errors: list[str] = []
    for var_name, meta in contract.items():
        if not isinstance(meta, dict):
            continue
        # A per-var label, NEVER rebinding the loop-invariant `where`. Rebinding
        # it made each error append the previous var's name, so the eighth error
        # on a file read "var 'A' var 'B' ... var 'H'" and pointed a reader at
        # seven vars that were fine. A message that names the wrong location is
        # worse than a terse one — it sends someone to edit the wrong line.
        at = f"{where} var '{var_name}'"
        if "secret" not in meta:
            errors.append(
                f"{at}: env contract entry is missing required 'secret' "
                f"(bool). The test: can the integration AUTHENTICATE without "
                f"this value? No -> true. Yes -> false (e.g. a shop id is "
                f"false — you still authenticate, you just cannot target a "
                f"shop). Note 'source' is OPTIONAL and must be omitted unless "
                f"the value is machine-resolvable; never invent a source."
            )
        elif not isinstance(meta["secret"], bool):
            errors.append(
                f"{at}: 'secret' must be a JSON boolean, got "
                f"{type(meta['secret']).__name__}."
            )
        if "tier" in meta:
            errors.append(
                f"{at}: 'tier' was renamed to 'default_tier' (#1226). Left as-is "
                f"it is silently ignored and the var falls back to the 'fleet' "
                f"default — so this must fail here rather than at runtime, where "
                f"a mis-tiered var still resolves from wherever a value happens "
                f"to sit and nothing looks wrong. Rename the key; the meaning "
                f"also changed, from THE location to a placement DEFAULT"
            )
        declared_tier = meta.get("default_tier")
        if declared_tier is not None and declared_tier not in ENV_TIERS:
            errors.append(
                f"{at}: unknown default_tier {declared_tier!r} — one of: "
                f"{', '.join(ENV_TIERS)}. This is a PLACEMENT DEFAULT, not the "
                f"resolution location: the runtime cascades all four tiers and "
                f"the most specific one that sets the var wins"
                f"{hint(str(declared_tier), ENV_TIERS)}"
            )
        elif declared_tier in DEPRECATED_ENV_TIERS:
            # Warned, not rejected: the tier is real and the register must be
            # able to report a value found there. Discouraging a NEW declaration
            # is a different act from refusing to describe the estate as it is.
            errors.append(
                f"{at}: default_tier '{declared_tier}' is the host-shared "
                f"repo-root .env, which is being wound down — declare 'fleet' "
                f"(or 'host' for a genuinely machine-wide identity) instead"
            )
        source = meta.get("source")
        if source is not None and source not in KNOWN_CREDENTIAL_SOURCES:
            known = ", ".join(sorted(KNOWN_CREDENTIAL_SOURCES))
            errors.append(
                f"{at}: unregistered source {source!r} — 'source' is a closed "
                f"framework-owned registry, one of: {known}. Omit it entirely to "
                f"mean 'a human supplies this value'"
                f"{hint(source, KNOWN_CREDENTIAL_SOURCES)}"
            )
    return errors


def _validate_env_contracts(paths: Paths, report: ValidationReport) -> None:
    """Gate BOTH env-contract surfaces — library altitude, not per-bot.

    The two surfaces are MCP fragments (``_env_contract``) and integration-doc
    frontmatter (``env_contract:``). They declare the same facts and are held to
    the same rule, because **gating only one is how the detector ends up not
    covering the case it was built for**: 10 of the 21 vars on the integration
    surface have no paired MCP fragment at all (`type: cli` integrations —
    neon, railway, snowflake), so an MCP-only gate is not merely deferred for
    them, it is structurally unreachable forever. Among those are
    ``RAILWAY_API_TOKEN``, ``RAILWAY_PERSONAL_TOKEN``, ``NEON_API_KEY`` and the
    two Snowflake key vars — the exact credentials whose silent blanking
    started this workstream.

    A var declared on BOTH surfaces must agree, or ``required_vars`` yields two
    records disagreeing about whether it is a credential and the fail-loud rung
    reads whichever it happens to see first.

    Deliberately NOT wired into the per-bot equipment loop, and the placement is
    load-bearing in three ways an earlier draft got wrong:

    - **An unequipped fragment must still be checked.** The closed registry's
      whole purpose is that an unregistered ``source`` never reaches the
      resolver; a gate that only fires when some bot happens to equip the
      fragment leaves a malformed one sitting in the library, clean, until the
      day someone equips it.
    - **The defect is a property of the FILE, so the message must name the
      file.** Prefixing it with a bot name sends the reader to `fleet.yaml`
      when the fix is a one-line library edit.
    - **One defect, one error.** Per-bot, a single missing ``secret`` on a
      fragment every bot equips produced one identical error per bot — 21 lines
      for one typo on a 21-bot fleet.

    Scans base and overlay libraries exactly as
    :func:`_validate_library_frontmatter` does, so a fleet overlay that shadows
    a library file is held to the same rule — which is what makes the registry
    closed "at any tier" rather than only in this repo.
    """
    from .loader import parse_frontmatter

    seen: set[Path] = set()
    labels: dict[str, list[tuple[str, bool]]] = {}

    def _record(contract: dict, where: str) -> None:
        for var, meta in contract.items():
            if isinstance(meta, dict) and isinstance(meta.get("secret"), bool):
                labels.setdefault(var, []).append((where, meta["secret"]))

    for root in (paths.base_library, paths.overlay_library):
        if root is None:
            continue

        for frag_path in sorted((root / "mcp").glob("*.json")):
            resolved = frag_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                frag = json.loads(frag_path.read_text())
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                # Malformed JSON is reported here rather than deferred: compose
                # raises on it, but validate exists to say so first.
                report.errors.append(f"invalid JSON in MCP fragment {frag_path}: {exc}")
                continue
            rel = f"mcp fragment '{frag_path.name}'"
            report.errors.extend(_env_contract_errors(frag.get("_env_contract"), rel))
            _record(frag.get("_env_contract") or {}, rel)

        for doc in sorted((root / "integrations").glob("*.md")):
            resolved = doc.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                fm, _ = parse_frontmatter(doc.read_text())
            except (OSError, ValueError, KeyError):
                # Malformed frontmatter is _validate_library_frontmatter's to
                # report; saying it twice would read as two unrelated defects.
                continue
            if not isinstance(fm, dict):
                continue
            rel = f"integration '{doc.name}'"
            report.errors.extend(_env_contract_errors(fm.get("env_contract"), rel))
            contract = fm.get("env_contract")
            _record(contract if isinstance(contract, dict) else {}, rel)

    for var, decls in sorted(labels.items()):
        values = {secret for _, secret in decls}
        if len(values) > 1:
            detail = ", ".join(f"{where} says {str(s).lower()}" for where, s in decls)
            report.errors.append(
                f"env var '{var}' is declared on more than one surface with "
                f"DIFFERENT 'secret' values ({detail}) — required_vars yields "
                f"both records, so the fail-loud rung would read whichever it "
                f"saw first. Make them agree."
            )


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
        if any(
            b.git_credentials or b.github_app for b in fleet.bots.values()
        )
        else None
    )
    # D6 (App-auth P3): an operator ~/.gitconfig carrying an ssh-FORCING
    # rewrite (url."git@github.com:".insteadOf/pushInsteadOf = https://...)
    # defeats the whole composed credential layer — URL rewriting is
    # single-pass, so the composed git@→https rule cannot chain to undo it.
    # Nothing composable fixes it; a warning at validate time is the only
    # place the failure points back to its cause. Probed once per run.
    reverse_insteadof_problem = (
        _operator_reverse_insteadof()
        if any(b.github_app for b in fleet.bots.values())
        else None
    )
    # Vault resolution is a full walk-up + scan per call, and `claudron_vault_path`
    # falls back to a fleet-wide default (config.py) — so every bot in a fleet
    # typically resolves the *same* path. Memo per run, same reason as above.
    vault_resolutions: dict[str, bool] = {}

    # Grant-contract readers (folder-aware; shared with the P2 composer resolvers).
    from .loader import (
        integration_tool_grants,
        iter_expertise_permissions,
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
        for req in _mcp_required_vars(bot, paths):
            if _env_has_value(effective_env, req.name):
                continue
            inst_note = f" (instance: {req.instance})" if req.instance else ""
            # The consequence is stated per what was MEASURED, not per what the
            # name suggests (#1214 F8). An MCP server with an unresolved var does
            # NOT fail: the client expands the unset placeholder to the empty
            # string and the server starts and serves ANONYMOUSLY — verified
            # across the running estate, 11 live servers holding an empty token
            # and zero holding the literal placeholder. The old wording promised
            # a loud failure that never arrives, which is worse than silence
            # because it talks the reader out of investigating.
            consequence = (
                "MCP server will start and serve ANONYMOUSLY (unauthenticated), "
                "not fail"
                if req.secret
                else "MCP server will start with this unset"
            )
            # Names the CONVENTIONAL tier, and says it is one of four. The old
            # wording — "add to <tier>-tier .env" — read the declared default as
            # THE location, which is the #1226 defect in one sentence: it sends
            # an operator to the fleet file for a var that would resolve just as
            # well from ~/.env, and it is why a value already placed at the host
            # or bot tier still reads as missing here.
            # SET-BUT-EMPTY is a different defect from ABSENT and needs the
            # opposite remedy. `_env_has_value` correctly treats both as "no
            # value", but telling an operator to ADD a var that is already
            # assigned-empty sends them to write it at the very tier whose empty
            # assignment is blanking it. That is not hypothetical: two fleets
            # carry a pristine `export GITHUB_PAT=` scaffold stub, and under
            # shell assignment semantics that stub WINS over anything upstream.
            if req.name in effective_env:
                remedy = (
                    f"it is SET BUT EMPTY — some tier assigns it the empty "
                    f"string, which under shell sourcing WINS over any value at "
                    f"a less specific tier. Fill it in or delete the assignment; "
                    f"adding it again at the same tier changes nothing"
                )
            else:
                remedy = (
                    f"no .env tier sets it — add it at any tier "
                    f"({', '.join(ENV_TIERS)}); conventionally {req.default_tier}. "
                    f"The most specific tier that sets it wins"
                )
            report.warnings.append(
                f"bot '{bot_name}': {req.origin}{inst_note} requires {req.name} but "
                f"{remedy} ({consequence})"
            )

        # Per-scope credential source overrides (#1214 F6c). Held to the SAME
        # closed registry as a contract's own `source`, and that is the point:
        # fork F5's injection guarantee is that the resolver dispatches on a
        # whole registered identifier through a fixed `case` arm, so a value
        # arriving from fleet.yaml must be no more admissible than one arriving
        # from a library fragment. A second, laxer door into the same resolver
        # would void the guarantee for both.
        for var_name, source in sorted(bot.credential_sources.items()):
            if source not in KNOWN_CREDENTIAL_SOURCES:
                known = ", ".join(sorted(KNOWN_CREDENTIAL_SOURCES))
                report.errors.append(
                    f"bot '{bot_name}': credential_sources['{var_name}'] = "
                    f"{source!r} is not in the closed source registry, one of: "
                    f"{known}{hint(source, KNOWN_CREDENTIAL_SOURCES)}"
                )
            elif source == "mint:github-app":
                # Registered but deliberately unresolvable: no boot-time
                # resolver reads it, and that is a design decision, not a gap
                # (a resolver would put ~1h tokens at rest in the launch env).
                # Fleet-scope App minting ships as the USE-TIME helper instead.
                # Declaring it is legal and records intent; saying so here is
                # what stops someone waiting for a value that is never coming.
                report.warnings.append(
                    f"bot '{bot_name}': credential_sources['{var_name}'] = "
                    f"'mint:github-app' is RESERVED — no boot-time resolver "
                    f"reads it (deliberate; App-auth mints at use time via "
                    f"lib/git-credential-github-app, see mcp: [github-app] "
                    f"and lib/mint-github-token.sh). Supply {var_name} in a "
                    f".env tier or adopt App mode; the resolver arm belongs "
                    f"to #252's per-bot sidecar"
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

        # Grant contracts on equipped sources — expertise (deny-capable
        # permissions:), integrations (additive tool_grants), skills (additive
        # tool_grants), guardrails (deny-capable permissions:) — all validated
        # against the single F3(a) grammar via _grant_shape_warnings.
        # iter_integration_grants folder-expands dir/ equips so a contract nested in
        # an expanded folder is not silently skipped (same guarantee as skills).
        #
        # Expertise is checked here because it was the ONE grant-declaring source
        # the shape check never ran on, and it is the source the shipped library
        # actually uses to grant bare 'Bash' (#913). Three doors were policed and
        # the fourth, unpoliced one is where the library does the thing the other
        # three forbid. allow_all gets its own warning rather than riding the
        # bare-'Bash' one: the parse keeps it as a separate flag and never expands
        # it into .allow (loader._parse_expertise_permissions), so the expansion to
        # ALL_TOOLS — bare 'Bash' included — happens later in the composer and is
        # invisible to a check that only reads .allow.
        from .composer import resolve_effective_integrations

        for area, xperms in iter_expertise_permissions(paths, bot.expertise):
            if xperms is None:
                continue
            report.warnings.extend(
                _grant_shape_warnings(
                    bot_name, "expertise", area, xperms.allow, allow_side=True
                )
            )
            report.errors.extend(
                _inert_path_errors(bot_name, "expertise", area, xperms.allow)
                + _inert_path_errors(bot_name, "expertise", area, xperms.deny)
            )
            report.warnings.extend(
                _grant_shape_warnings(
                    bot_name, "expertise", area, xperms.deny, allow_side=False
                )
            )
            if xperms.allow_all:
                report.warnings.append(
                    f"bot '{bot_name}': expertise '{area}' declares allow_all — expands "
                    "to ALL_TOOLS including bare 'Bash', which subsumes every "
                    "Bash(<cmd> *) grant composed beside it"
                )

        for name, grants in iter_integration_grants(
            paths, resolve_effective_integrations(bot, paths)
        ):
            report.warnings.extend(
                _grant_shape_warnings(
                    bot_name, "integration", name, grants, allow_side=True
                )
            )
            report.errors.extend(
                _inert_path_errors(bot_name, "integration", name, grants)
            )
        for name, grants in iter_skill_grants(paths, bot.skills):
            report.warnings.extend(
                _grant_shape_warnings(bot_name, "skill", name, grants, allow_side=True)
            )
            report.errors.extend(_inert_path_errors(bot_name, "skill", name, grants))
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
            report.errors.extend(
                _inert_path_errors(bot_name, "guardrail", name, gperms.allow)
                + _inert_path_errors(bot_name, "guardrail", name, gperms.deny)
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
            # Mirror the generate-time gh-shim collision as a validate error so
            # `validate` ≡ `generate` (the contract this function stands on): an
            # App bot composes a tools/gh shim, so a declared tool also
            # rendering 'gh' would only fail at generate otherwise.
            if target == "gh" and bot.github_app:
                report.errors.append(
                    f"bot '{bot_name}': tool '{tool_entry.name}' renders 'gh', "
                    "but github_app composes a tools/gh shim — rename the tool "
                    "or disable github_app for this bot"
                )
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
                    f"not set in any tier of .env — the org helper answers with an "
                    f"EMPTY password, which git presents and GitHub 401s; later "
                    f"helpers (App or host default) are NOT consulted (D2: a "
                    f"declared org wins by declaration, not by having a value)"
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

        # GitHub App routing (App-auth P3 #1273) — all warn, never fail.
        app = bot.github_app
        if app:
            for var_name in GITHUB_APP_ENV_VARS:
                if not _env_has_value(effective_env, var_name):
                    report.warnings.append(
                        f"bot '{bot_name}': github_app routing requires {var_name}, "
                        f"not set in any tier of .env — the composed helper will "
                        f"fail loudly (quit=1) at the first git auth; set it, or "
                        f"run lib/setup-github-app.sh (its config-file fallback "
                        f"covers operator/cron shells only, never bot sessions)"
                    )
                if var_name in bot.env:
                    report.warnings.append(
                        f"bot '{bot_name}': bot-tier env overrides {var_name} — "
                        f"all bots on this host share ONE git credential-cache "
                        f"daemon keyed only by URL, so a per-bot installation "
                        f"override can cross-serve another installation's cached "
                        f"token with zero symptoms (D4); App credentials are "
                        f"fleet-tier in v1"
                    )
            if bool(app.slug) != bool(app.bot_user_id):
                have, need = (
                    ("slug", "bot_user_id") if app.slug else ("bot_user_id", "slug")
                )
                report.warnings.append(
                    f"bot '{bot_name}': github_app declares {have} without {need} — "
                    f"the App commit identity composes only when BOTH are set, so "
                    f"commits will carry the operator identity (get both from "
                    f"lib/setup-github-app.sh output)"
                )
            if git_identity_problem and not app.composes_identity:
                report.warnings.append(
                    f"bot '{bot_name}': github_app without a composed App identity "
                    f"relies on the operator include for user.email, but "
                    f"{git_identity_problem} — commits will fail 'Author identity "
                    f"unknown'"
                )
            for shadow in ("GH_TOKEN", "GITHUB_TOKEN"):
                if _env_has_value(effective_env, shadow):
                    report.warnings.append(
                        f"bot '{bot_name}': {shadow} is set in a .env tier while "
                        f"github_app is declared — the composed tools/gh shim "
                        f"mints only when neither GH_TOKEN nor GITHUB_TOKEN is "
                        f"set, so an ambient value silently makes `gh` run as "
                        f"THAT identity, not the App (the silent operator-"
                        f"identity substitution the shim exists to stop)"
                    )
            if reverse_insteadof_problem:
                report.warnings.append(
                    f"bot '{bot_name}': github_app routing is defeated by the "
                    f"operator gitconfig: {reverse_insteadof_problem} — an "
                    f"ssh-forcing rewrite bypasses the credential layer entirely "
                    f"(URL rewriting is single-pass; the composed git@->https "
                    f"rule cannot undo it); remove it or scope it away from "
                    f"github.com"
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
            # The one source a human hand-writes, so the one most likely to
            # acquire a bare-absolute path rule now the composer cannot emit one.
            report.errors.extend(
                _inert_path_errors(
                    bot_name, "fleet.yaml", "tools.allow", bot.tool_permissions.allow
                )
                + _inert_path_errors(
                    bot_name, "fleet.yaml", "tools.deny", bot.tool_permissions.deny
                )
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
            "(add @RawDataBot to the group; it is a negative number — "
            "-100... for a supergroup, plain negative for a basic group)"
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
    _validate_env_contracts(paths, report)

    # bench marker — multi-bot fleets should designate a bench bot
    if len(fleet.bots) > 1 and not any(b.bench for b in fleet.bots.values()):
        report.warnings.append(
            "fleet has multiple bots but none has bench: true — "
            "cold-start benchmarking will not know which bot to measure"
        )

    return report
