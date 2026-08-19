"""#644 P4 — fresh-box self-containment audit (static half of the F4(c) gate).

Proves each composed bot ships self-contained in its own config dir: every grant
in its ``settings.local.json`` allow-list traces to an equipped source's contract
(no orphan / over-grant), every grant its sources declare survives into the
allow-list (no under-grant / silent reliance on the retired global ``~/.claude``),
and the Tier-A settings surface (``enabledPlugins`` / skip-flags / ``sandbox``) is
composed per-bot rather than inherited from the hand-accumulated global. The
real-boot half of the gate lives in ``lib/freshbox-boot-gate.sh``.

#703 folds the deny-by-default path guard into the same audit: a source re-check
(the L1 guard, ``_value_findings``), an externals visibility report
(``_externals_report``), the fleet-tier ``.env`` rung generate cannot see
(``_env_file_findings``, F5), and rendered ``tools/`` scripts in the L2 emitted-path
scan (F6, in :mod:`path_audit`). All ride the existing severity/report machinery.

#792 adds a per-bot secret-leak rung (``_env_secret_leak_findings``): a per-bot
identity secret — a var whose env contract is bot-tier, or any bot's Telegram
``token_env`` — sitting in a host-shared ``.env`` tier (the global ``~/.env`` or the
deprecated ``$CLAUDLOBBY_ROOT/.env``) is a FAIL, since ``source_env_tiered`` exports
it to every bot on the host.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .composer import (
    BASE_TOOLS,
    _resolve_channel_permissions,
    _resolve_expertise_permissions,
    _resolve_guardrail_permissions,
    _resolve_integration_grants,
    _resolve_mcp_permissions,
    _resolve_skill_grants,
    _resolve_skill_permissions,
    compose_settings_local,
)
from .config import BotConfig, FleetConfig
from .paths import Paths

# Severities mirror doctor.Check: fail blocks the gate, warn is an advisory
# report line (drift signal, not a fresh-box breakage). info is visibility only
# (the externals report) — it never blocks, even under --strict.
FAIL = "fail"
WARN = "warn"
INFO = "info"


@dataclass
class Finding:
    bot_id: str
    kind: str
    severity: str
    detail: str


def _sourced_grants(bot: BotConfig, paths: Paths) -> set[str]:
    """Every allow pattern that traces to an equipped source's contract.

    The union of the same per-source resolvers :func:`compose_settings_local`
    feeds into the allow list, recomputed here independently so a divergence
    between the composed output and the declared contracts surfaces. Excludes
    the always-injected ``BASE_TOOLS`` floor and the ad-hoc fleet
    ``tools.allow`` override, which are classified separately.
    """
    expertise_allow, _ = _resolve_expertise_permissions(bot, paths)
    guardrail_allow, _ = _resolve_guardrail_permissions(bot, paths)
    sourced: set[str] = set(expertise_allow) | set(guardrail_allow)
    sourced |= set(_resolve_mcp_permissions(bot, paths))
    sourced |= set(_resolve_integration_grants(bot, paths))
    sourced |= set(_resolve_channel_permissions(bot))
    sourced |= set(_resolve_skill_permissions(bot))
    sourced |= set(_resolve_skill_grants(bot, paths))
    return sourced


# Grant finding vocabulary — kind → (severity, allow-list detail suffix). One
# home for both, so severity and detail can't drift apart. (missing_tier_a has a
# dynamic per-key detail and lives in _tier_a_findings.)
_GRANT_KINDS: dict[str, tuple[str, str]] = {
    "orphan_grant": (
        FAIL,
        "in composed allow but traces to no source, base floor, or fleet override",
    ),
    "unsourced_grant": (
        WARN,
        "granted via fleet tools.allow but no equipped source declares it",
    ),
    "under_grant": (
        FAIL,
        "declared by an equipped source but missing from the composed allow "
        "(not denied) — the bot would fall back to the global",
    ),
}


def classify_grants(
    allow: list[str],
    deny: list[str],
    sourced: set[str],
    base: set[str],
    override: set[str],
) -> list[tuple[str, str, str]]:
    """Classify each grant against its provenance — the pure, testable core.

    Returns ``(kind, severity, grant)`` triples:
      - ``orphan_grant`` (fail): an allow entry tracing to no source, base floor,
        or fleet override — composition emitted a grant nothing produced.
      - ``unsourced_grant`` (warn): an allow entry present only via the ad-hoc
        fleet ``tools.allow`` override (drift signal — no source contract).
      - ``under_grant`` (fail): a source-declared grant that is not denied yet
        never reached the allow list (the bot would rely on the retired global).
    """
    results: list[tuple[str, str, str]] = []
    allow_set = set(allow)
    deny_set = set(deny)

    for grant in allow:
        if grant in sourced or grant in base:
            continue
        kind = "unsourced_grant" if grant in override else "orphan_grant"
        results.append((kind, _GRANT_KINDS[kind][0], grant))

    for grant in sorted(sourced):
        if grant not in allow_set and grant not in deny_set:
            results.append(("under_grant", _GRANT_KINDS["under_grant"][0], grant))

    return results


# Tier-A settings that must travel with the bot in its own config dir, never
# inherited from the hand-accumulated global ~/.claude (#644 P2b / Fork F5).
_TIER_A_KEYS = (
    "skipAutoPermissionPrompt",
    "skipDangerousModePermissionPrompt",
    "sandbox",
    "enabledPlugins",
)


def _tier_a_findings(bot_id: str, settings: dict) -> list[Finding]:
    """Flag any Tier-A settings key not composed per-bot (would be global-inherited)."""
    findings: list[Finding] = []
    for key in _TIER_A_KEYS:
        value = settings.get(key)
        # enabledPlugins present-but-empty is as inert as absent on a fresh box.
        if value is None or (key == "enabledPlugins" and not value):
            findings.append(
                Finding(
                    bot_id,
                    "missing_tier_a",
                    FAIL,
                    f"{key} not composed into settings.local.json — a fresh box "
                    "would inherit it from the retired global ~/.claude",
                )
            )
    return findings


def _path_findings(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> list[Finding]:
    """Improper-path findings — the generate-time guard folded into the audit so
    the same 'no flat/dangling absolute fleet path' contract holds on the emitted
    wiring. Reading the emitted files (not a re-compose) also catches post-generate
    drift a re-compose would miss."""
    from .path_audit import audit_bot_paths

    return [
        Finding(
            bot.bot_id, "improper_path", FAIL, f"{pf.file}: {pf.path} — {pf.reason}"
        )
        for pf in audit_bot_paths(bot, fleet, paths)
    ]


def _value_findings(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> list[Finding]:
    """Denied-source-value findings — the L1 deny-by-default guard (#702) folded
    into the audit so a fresh box reports an unanchored, undeclared absolute in a
    bot source (config leaves + loaded MCP fragments), the source-side complement
    to the emitted-path check above."""
    from .composer import _load_bot_fragments
    from .path_audit import audit_bot_sources

    return [
        Finding(
            bot.bot_id, "denied_value", FAIL, f"{sf.source}: {sf.path} — {sf.reason}"
        )
        for sf in audit_bot_sources(bot, fleet, paths, _load_bot_fragments(bot, paths))
    ]


def _mask(value: str) -> str:
    """Mask a source value so a finding never echoes a secret (R4). A base64 secret
    can legitimately lead with ``/`` and trip the path grammar, so the finding shows
    only the head character and the length — never the body."""
    if not value:
        return "(empty)"
    return f"{value[:1]}…({len(value)} chars)"


def _host_env_paths(paths: Paths, *, home: Path | None = None) -> list[Path]:
    """The host-shared ``.env`` tiers ``source_env_tiered`` inherits into EVERY bot:
    the deprecated/install-shared ``$CLAUDLOBBY_ROOT/.env`` (always) and the
    operator's global ``~/.env`` (only when a caller injects ``home`` — the library
    never reaches into a personal home by default, so a unit audit never touches it).
    The single definition of "host tier", shared by the F5 rung and the #792 rung."""
    tiers = [paths.root / ".env"]
    if home is not None:
        tiers.append(home / ".env")
    return tiers


def _env_tier_files(
    bot: BotConfig, fleet: FleetConfig, paths: Paths, *, home: Path | None = None
) -> list[tuple[Path, str]]:
    """The ``.env`` files freshbox audits, each with its severity, in start-bot's
    tier order. Fleet-owned tiers — a bot's own ``.env`` and the fleet overlay
    ``.env`` — FAIL: the fleet controls them, and the founding #602 dangle lived
    exactly there. The install-shared ``root/.env`` and the operator's personal
    ``~/.env`` are host-tier WARN — fleet policy's writ ends at fleet-owned files
    (F5). ``~/.env`` is scanned only when the caller injects ``home`` (the CLI opts
    in; the library never reaches into a personal home by default, so a unit audit
    never reads it). Deduped by path with FAIL winning, so a root-mode fleet
    (``fleet_config_dir == root``) reports its one ``.env`` once, as fleet-tier."""
    tiers: list[tuple[Path, str]] = [
        (paths.bot_runtime(bot.bot_id) / ".env", FAIL),
        (paths.fleet_config_dir / ".env", FAIL),
    ]
    tiers.extend((p, WARN) for p in _host_env_paths(paths, home=home))
    seen: set[str] = set()
    out: list[tuple[Path, str]] = []
    for path, severity in tiers:  # FAIL-first order → first-wins keeps the strictest
        if str(path) not in seen:
            seen.add(str(path))
            out.append((path, severity))
    return out


def _env_file_findings(
    bot: BotConfig, fleet: FleetConfig, paths: Paths, *, home: Path | None = None
) -> list[Finding]:
    """The F5 rung: a path-classified value in a ``.env`` file — the runtime-sourced
    surface generate physically cannot see — is denied unless anchored or declared.
    Fleet-owned tiers FAIL, host tiers WARN. The value is masked (R4): the finding
    names the key and file but never prints the value, which may be a secret."""
    from . import dotenv
    from .path_audit import denied_source_paths

    decls = list(bot.external_paths)
    findings: list[Finding] = []
    for path, severity in _env_tier_files(bot, fleet, paths, home=home):
        for key, value in dotenv.read(path).items():
            if denied_source_paths(value, decls):
                findings.append(
                    Finding(
                        bot.bot_id,
                        "env_denied_value",
                        severity,
                        f"{path}: {key}={_mask(value)} — undeclared, unanchored "
                        "absolute path in a runtime-sourced .env (invisible at "
                        "generate); anchor it on ${FLEET_ROOT} or declare it via "
                        "external_paths / secret_files",
                    )
                )
    return findings


def _env_secret_leak_findings(
    fleet: FleetConfig, paths: Paths, *, home: Path | None = None
) -> list[Finding]:
    """#792: a per-bot identity secret sitting in a host-shared env tier.

    ``source_env_tiered`` (lib/lib-common.sh) sources the global ``~/.env`` and the
    deprecated/install-shared ``$CLAUDLOBBY_ROOT/.env`` into EVERY bot's process
    env, so a per-bot secret placed in either leaks host-wide — the A1
    config-review incident, where one bot's token became readable by another. A
    var is per-bot when its own env contract is bot-tier (``collect_env_contracts``
    tier == "bot": Telegram ``token_env``, Slack ``SLACK_TOKEN``, instance-scoped
    MCP vars). The contract tier is the SSOT — more robust than a key-name pattern,
    since Slack's ``SLACK_TOKEN`` carries no per-bot affix. Fleet/host-scoped: the
    host tiers are shared, so this runs once over the fleet (via ``audit_fleet``),
    not per bot. FAIL — not the F5 rung's host-tier WARN for the same files —
    because it is a higher-precision signal: the var's own contract says bot-tier,
    so host-tier placement contradicts it, where F5 flags only a heuristic path
    grammar. Scoped to the host-shared tiers the issue names; the correct home (the
    owning bot's ``.env``) is not flagged, and a per-bot secret in the fleet-overlay
    ``.env`` (a narrower within-fleet leak) is a tracked follow-up. Value masked (R4)."""
    from . import dotenv
    from .composer import collect_env_contracts

    bot_tier_vars = {
        ev.name for ev in collect_env_contracts(fleet, paths) if ev.default_tier == "bot"
    }
    # collect_env_contracts drops a Telegram token_env that is self-referential
    # (== the plugin's own read var TELEGRAM_BOT_TOKEN) so generate never scaffolds
    # an authoritative-looking empty stub — but that var is still a per-bot identity
    # secret (each bot resolves its own), and TELEGRAM_BOT_TOKEN in a shared tier is
    # the canonical A1 leak. Union every bot's token_env in so it is not missed.
    for bot in fleet.bots.values():
        if bot.telegram.token_env:
            bot_tier_vars.add(bot.telegram.token_env)
    if not bot_tier_vars:
        return []
    findings: list[Finding] = []
    for path in _host_env_paths(paths, home=home):
        for key, value in dotenv.read(path).items():
            if key in bot_tier_vars:
                findings.append(
                    Finding(
                        "(host)",
                        "env_bot_secret_leaked",
                        FAIL,
                        f"{path}: {key}={_mask(value)} — a bot-tier secret in a "
                        "host-shared .env tier; source_env_tiered exports it to "
                        "every bot on the host. Move it to the owning bot's .env.",
                    )
                )
    return findings


def _externals_report(
    bot: BotConfig, fleet: FleetConfig, paths: Paths
) -> list[Finding]:
    """Make the fleet's external coupling visible in one place (INFO), flag a
    declaration that blesses nothing (WARN — declaration rot), and fail on a
    by-construction external that is not there (FAIL). Surfaces every
    ``external_paths`` entry with the source values it actually blesses (so an
    over-broad ``/**`` silently covering a second use site is readable), plus the
    declared-by-construction externals: mount targets, the claudron vault path, a
    non-default account dir, and the two host files per-org git credential routing
    leans on. The default ``~/.claude`` account is the norm, not coupling, so it is
    not surfaced — a clean bot stays silent here."""
    from . import dotenv
    from .composer import _load_bot_fragments
    from .path_audit import (
        classified_source_paths,
        classify_source_value,
        match_external,
    )

    findings: list[Finding] = []

    # Which declaration blesses which live source value. Source paths come from the
    # config walk + MCP fragments, plus the fleet-owned .env values a declaration may
    # exist solely to bless (host-tier ~/.env is out of scope, so home is not
    # injected here).
    classified = classified_source_paths(bot, _load_bot_fragments(bot, paths))
    for env_path, _severity in _env_tier_files(bot, fleet, paths):
        for key, value in dotenv.read(env_path).items():
            for path in classify_source_value(value):
                classified.append((f"{env_path.name}:{key}", path))

    for decl in bot.external_paths:
        users = sorted(
            {prov for prov, path in classified if match_external(path, [decl])}
        )
        if users:
            findings.append(
                Finding(
                    bot.bot_id,
                    "external_ref",
                    INFO,
                    f"external_paths[{decl.path}] — {decl.purpose}; blesses "
                    f"{len(users)} source value(s): {', '.join(users)}",
                )
            )
        else:
            findings.append(
                Finding(
                    bot.bot_id,
                    "unused_declaration",
                    WARN,
                    f"external_paths[{decl.path}] — {decl.purpose}; matches no source "
                    "value (declaration rot — the dependency it guarded is gone, or "
                    "the reference was anchored/removed; drop it)",
                )
            )

    for name, target in sorted(bot.mounts.items()):
        findings.append(
            Finding(bot.bot_id, "external_ref", INFO, f"mount {name} → {target}")
        )
    if bot.claudron_vault_path:
        findings.append(
            Finding(
                bot.bot_id,
                "external_ref",
                INFO,
                f"claudron_vault_path → {bot.claudron_vault_path}",
            )
        )
    account_dir = fleet.accounts.get(bot.account)
    if account_dir and account_dir != "~/.claude":
        findings.append(
            Finding(
                bot.bot_id,
                "external_ref",
                INFO,
                f"account {bot.account} → {account_dir} (non-default config dir)",
            )
        )

    # Per-org git credential routing leans on two host files: the operator's own
    # ~/.gitconfig (included for identity — the git-identity-no-overrides guardrail
    # forbids composing a per-bot one) and gh's credential helper for undeclared
    # orgs. The include is the one that breaks quietly: git ignores a missing
    # include path without a word, so the bot goes on routing credentials
    # correctly while user.name/user.email vanish, surfacing only as
    # 'Author identity unknown' at the first commit with nothing pointing back at
    # the composed file. An absent host prerequisite belongs in the fresh-box gate.
    if bot.git_credentials or bot.github_app:
        from .composer import _operator_gitconfig, _resolve_gh_executable

        # An App bot with a COMPOSED identity ([user] after the include) does
        # not depend on the include for user.email, so 'Author identity
        # unknown' cannot occur for it — a FAIL promising that symptom would
        # outlive its truth (D7 split). It still WARNs: the include also
        # carries aliases and the operator's non-identity config.
        app = bot.github_app
        app_identity = bool(app and app.slug and app.bot_user_id)
        operator = _operator_gitconfig()
        if operator.is_file():
            findings.append(
                Finding(
                    bot.bot_id,
                    "external_ref",
                    INFO,
                    f"git identity include → {operator}",
                )
            )
        elif app_identity:
            findings.append(
                Finding(
                    bot.bot_id,
                    "missing_external",
                    WARN,
                    f"composed .gitconfig includes {operator}, which does not "
                    "exist — commit identity is safe (the composed App "
                    "identity supplies user.name/user.email), but any operator "
                    "aliases/config the include would carry are silently absent",
                )
            )
        else:
            findings.append(
                Finding(
                    bot.bot_id,
                    "missing_external",
                    FAIL,
                    f"composed .gitconfig includes {operator}, which does not "
                    "exist — git ignores a missing include silently, so "
                    "credential routing will work while user.name/user.email are "
                    "unset and every commit fails 'Author identity unknown'. Create "
                    "it (git config --global user.email …) or drop the declaration",
                )
            )
        if app:
            # The private key is the App's crown jewel and the one file the
            # composed routing hard-requires at mint time. Missing → FAIL
            # (every git auth will quit=1); readable by group/other → FAIL
            # (a shared-host secret at rest, the pii-protection class).
            # Same tier rule as credentials.py: fleet .env if present, else
            # root — one or the other, never merged; ambient env as fallback.
            env_vals = dotenv.read(paths.env_file) if paths.env_file.is_file() else {}
            key_path = env_vals.get("GITHUB_APP_PRIVATE_KEY_PATH") or os.environ.get(
                "GITHUB_APP_PRIVATE_KEY_PATH", ""
            )
            if not key_path:
                pass  # absent VALUE is the validator's warn, not a host audit
            else:
                kp = Path(key_path).expanduser()
                if not kp.is_file():
                    findings.append(
                        Finding(
                            bot.bot_id,
                            "missing_external",
                            FAIL,
                            f"GITHUB_APP_PRIVATE_KEY_PATH → {kp}, which does not "
                            "exist — every git auth on this bot will fail loudly "
                            "(helper quit=1) at first use",
                        )
                    )
                else:
                    mode = kp.stat().st_mode & 0o777
                    if mode & 0o077:
                        findings.append(
                            Finding(
                                bot.bot_id,
                                "denied_value",
                                FAIL,
                                f"App private key {kp} is mode {mode:o} — group/"
                                "other-readable key material on a shared host; "
                                "chmod 600 it",
                            )
                        )
                    else:
                        findings.append(
                            Finding(
                                bot.bot_id,
                                "external_ref",
                                INFO,
                                f"App private key → {kp} (0{mode:o})",
                            )
                        )
        # gh fallback: emitted for git_credentials bots and org-scoped App
        # bots; a host-generic App suppresses it in the composed file, so
        # reporting it as an external would claim a dependency that is not
        # wired (coverage honesty).
        if (gh_bin := _resolve_gh_executable()) and not (app and not app.orgs):
            findings.append(
                Finding(
                    bot.bot_id,
                    "external_ref",
                    INFO,
                    f"git credential fallback for undeclared orgs → {gh_bin}",
                )
            )
    return findings


def _orphan_unit_files(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> list[Path]:
    """Stale supervision units in the bot dir — any ``*.service`` / ``*.plist``
    that is not the composed long-form ``<service_prefix>.<bot>`` name (e.g. a
    pre-naming short-form ``<bot>.plist`` orphan). The composer emits only the
    long-form, so anything else is dead cruft an older layout left behind."""
    bot_dir = paths.bot_runtime(bot.bot_id)
    valid = {
        f"{fleet.service_prefix}.{bot.bot_id}.service",
        f"{fleet.service_prefix}.{bot.bot_id}.plist",
    }
    orphans: list[Path] = []
    for pattern in ("*.service", "*.plist"):
        orphans.extend(f for f in sorted(bot_dir.glob(pattern)) if f.name not in valid)
    return orphans


def _orphan_unit_findings(
    bot: BotConfig, fleet: FleetConfig, paths: Paths
) -> list[Finding]:
    return [
        Finding(
            bot.bot_id,
            "orphan_unit",
            WARN,
            f"{f.name} — stale supervision unit, not the composed "
            f"{fleet.service_prefix}.{bot.bot_id} long-form; reap with "
            "`claudlobby freshbox --reap`",
        )
        for f in _orphan_unit_files(bot, fleet, paths)
    ]


def reap_orphan_units(
    fleet: FleetConfig, paths: Paths, bots: list[BotConfig] | None = None
) -> list[Path]:
    """Remove stale short-form supervision units; return the files removed.

    Freshbox owns recurrence: the composer emits only the long-form unit, so any
    short-form ``<bot>.plist`` left by an older layout is dead cruft, safe to reap.
    """
    removed: list[Path] = []
    for bot in bots if bots is not None else fleet.bots.values():
        for f in _orphan_unit_files(bot, fleet, paths):
            try:
                f.unlink()
                removed.append(f)
            except OSError:
                pass
    return removed


def audit_bot(
    bot: BotConfig, fleet: FleetConfig, paths: Paths, *, home: Path | None = None
) -> list[Finding]:
    """Fresh-box self-containment findings for one composed bot. ``home`` (the CLI
    injects ``Path.home()``) opts the host-tier ``~/.env`` into the F5 rung; the
    default leaves a personal home untouched."""
    settings = compose_settings_local(bot, fleet, paths)
    perms = settings.get("permissions", {})
    triples = classify_grants(
        allow=perms.get("allow", []),
        deny=perms.get("deny", []),
        sourced=_sourced_grants(bot, paths),
        base=set(BASE_TOOLS),
        override=set(bot.tool_permissions.allow),
    )
    findings = [
        Finding(bot.bot_id, kind, sev, f"{grant} {_GRANT_KINDS[kind][1]}")
        for kind, sev, grant in triples
    ]
    findings.extend(_tier_a_findings(bot.bot_id, settings))
    findings.extend(_path_findings(bot, fleet, paths))
    findings.extend(_value_findings(bot, fleet, paths))
    findings.extend(_env_file_findings(bot, fleet, paths, home=home))
    findings.extend(_externals_report(bot, fleet, paths))
    findings.extend(_orphan_unit_findings(bot, fleet, paths))
    return findings


def _fleet_pulse_env_findings(
    fleet: FleetConfig, paths: Paths, *, home: Path | None = None
) -> list[Finding]:
    """#1120: a fleet-pulse escalation knob sitting in ANY ``.env`` tier.

    ``lib/fleet-pulse.sh`` runs from a composed timer unit that sources no
    ``.env`` at all — not the bot tier, not the fleet tier, not a host tier — so
    a ``FLEET_PULSE_*`` key in any of them reaches nothing and the script keeps
    its own default. The operator sees no change and cannot tell "ignored" from
    "applied, and the condition is genuinely still firing". That is the shape
    this rung exists to make audible: without it the only feedback is an audit
    weeks later.

    FAIL at every tier, deliberately, and not the F5 rung's tier-derived
    severity — for the same reason ``_env_secret_leak_findings`` departs from
    it: this is a high-precision signal rather than a heuristic. The key name is
    unambiguous and the placement is inert *wherever* it sits, so grading it by
    which file it is in would imply some tier works. None does.

    Keys come from :data:`FLEET_PULSE_ENV_KEYS`, the same mapping the composer
    emits from, so the guard cannot come to disagree with the emitter about
    which knobs exist. Values are masked (R4) — a chat id is an identifier.
    """
    from . import dotenv
    from .config import FLEET_PULSE_ENV_KEYS

    watched = set(FLEET_PULSE_ENV_KEYS.values())
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for bot in fleet.bots.values():
        for path, _severity in _env_tier_files(bot, fleet, paths, home=home):
            for key in dotenv.read(path):
                if key in watched and (str(path), key) not in seen:
                    seen.add((str(path), key))
                    findings.append(
                        Finding(
                            bot.bot_id,
                            "fleet_pulse_env_inert",
                            FAIL,
                            f"{path}: {key} is set but reaches nothing — the "
                            "composed fleet-pulse timer unit sources no .env, so "
                            "this is silently ignored and the script keeps its "
                            "default. Move it to the fleet.yaml `fleet_pulse:` "
                            "block, which composes into the unit's Environment=.",
                        )
                    )
    return findings


def audit_fleet(
    fleet: FleetConfig, paths: Paths, *, home: Path | None = None
) -> list[Finding]:
    """Fresh-box self-containment findings across every bot in the fleet."""
    findings: list[Finding] = []
    for bot in fleet.bots.values():
        findings.extend(audit_bot(bot, fleet, paths, home=home))
    # Fleet/host-scoped rung — the host-shared .env tiers are the same for every
    # bot, so scan them once (#792), not once per bot.
    findings.extend(_env_secret_leak_findings(fleet, paths, home=home))
    findings.extend(_fleet_pulse_env_findings(fleet, paths, home=home))
    return findings


def has_failures(findings: list[Finding]) -> bool:
    """True if any finding is fail-severity — the gate blocks. Warns are advisory."""
    return any(f.severity == FAIL for f in findings)


def exits_nonzero(findings: list[Finding], *, strict: bool) -> bool:
    """Freshbox's exit contract: a FAIL always blocks; under ``--strict`` a WARN
    blocks too; INFO (the externals visibility report) never blocks — so
    ``freshbox --strict`` stays green on a clean fleet that merely lists its
    external surface, instead of exiting non-zero on every INFO line."""
    if has_failures(findings):
        return True
    return strict and any(f.severity == WARN for f in findings)


def format_report(fleet: FleetConfig, findings: list[Finding]) -> str:
    """Human-readable fresh-box self-containment report (the over-grant report)."""
    lines = [
        f"Fresh-box self-containment audit — {fleet.name} ({len(fleet.bots)} bots)"
    ]
    if not findings:
        lines.append(
            "  OK — every grant traces to an equipped source; Tier-A settings "
            "composed per-bot. Self-contained."
        )
        return "\n".join(lines)

    by_bot: dict[str, list[Finding]] = {}
    for f in findings:
        by_bot.setdefault(f.bot_id, []).append(f)
    icons = {FAIL: "FAIL", WARN: "warn", INFO: "info"}
    for bot_id in sorted(by_bot):
        lines.append(f"  {bot_id}:")
        for f in by_bot[bot_id]:
            lines.append(
                f"    [{icons.get(f.severity, f.severity)}] {f.kind}: {f.detail}"
            )

    fails = sum(1 for f in findings if f.severity == FAIL)
    warns = sum(1 for f in findings if f.severity == WARN)
    infos = sum(1 for f in findings if f.severity == INFO)
    lines.append(f"  {fails} fail, {warns} warn, {infos} info")
    return "\n".join(lines)
