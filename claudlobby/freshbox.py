"""#644 P4 — fresh-box self-containment audit (static half of the F4(c) gate).

Proves each composed bot ships self-contained in its own config dir: every grant
in its ``settings.local.json`` allow-list traces to an equipped source's contract
(no orphan / over-grant), every grant its sources declare survives into the
allow-list (no under-grant / silent reliance on the retired global ``~/.claude``),
and the Tier-A settings surface (``enabledPlugins`` / skip-flags / ``sandbox``) is
composed per-bot rather than inherited from the hand-accumulated global. The
real-boot half of the gate lives in ``lib/freshbox-boot-gate.sh``.
"""

from __future__ import annotations

from dataclasses import dataclass

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
# report line (drift signal, not a fresh-box breakage).
FAIL = "fail"
WARN = "warn"


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


def _grant_detail(kind: str, grant: str) -> str:
    if kind == "unsourced_grant":
        return (
            f"{grant} granted via fleet tools.allow but no equipped source declares it"
        )
    if kind == "orphan_grant":
        return (
            f"{grant} in composed allow but traces to no source, base floor, or "
            "fleet override"
        )
    return (
        f"{grant} declared by an equipped source but missing from the composed "
        "allow (not denied) — the bot would fall back to the global"
    )


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
        sev = WARN if kind == "unsourced_grant" else FAIL
        results.append((kind, sev, grant))

    for grant in sorted(sourced):
        if grant not in allow_set and grant not in deny_set:
            results.append(("under_grant", FAIL, grant))

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


def audit_bot(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> list[Finding]:
    """Fresh-box self-containment findings for one composed bot."""
    settings = compose_settings_local(bot, fleet, paths)
    perms = settings.get("permissions", {})
    triples = classify_grants(
        allow=perms.get("allow", []),
        deny=perms.get("deny", []),
        sourced=_sourced_grants(bot, paths),
        base=set(BASE_TOOLS),
        override=set(bot.tools.allow),
    )
    findings = [
        Finding(bot.bot_id, kind, sev, _grant_detail(kind, grant))
        for kind, sev, grant in triples
    ]
    findings.extend(_tier_a_findings(bot.bot_id, settings))
    return findings


def audit_fleet(fleet: FleetConfig, paths: Paths) -> list[Finding]:
    """Fresh-box self-containment findings across every bot in the fleet."""
    findings: list[Finding] = []
    for bot in fleet.bots.values():
        findings.extend(audit_bot(bot, fleet, paths))
    return findings


def has_failures(findings: list[Finding]) -> bool:
    """True if any finding is fail-severity — the gate blocks. Warns are advisory."""
    return any(f.severity == FAIL for f in findings)


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
    for bot_id in sorted(by_bot):
        lines.append(f"  {bot_id}:")
        for f in by_bot[bot_id]:
            icon = "FAIL" if f.severity == FAIL else "warn"
            lines.append(f"    [{icon}] {f.kind}: {f.detail}")

    fails = sum(1 for f in findings if f.severity == FAIL)
    warns = sum(1 for f in findings if f.severity == WARN)
    lines.append(f"  {fails} fail, {warns} warn")
    return "\n".join(lines)
