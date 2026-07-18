"""#644 P4 static gate — fresh-box self-containment audit.

Every grant a composed bot holds must trace to an equipped source's contract
(no orphan / over-grant), the composed allow must cover every grant its sources
declare (no under-grant / silent reliance on the global), and the Tier-A
settings surface (``enabledPlugins`` / skip-flags / ``sandbox``) must be composed
per-bot, not inherited from a hand-accumulated global ``~/.claude``. This sweep
proves the audit *mechanism* on synthetic bots; the real fleets are audited at
generate time via ``claudlobby freshbox`` and in the PR's empirical proof (they
live in gitignored ``local/`` overlays, unreferenceable from a committed test).
"""

from __future__ import annotations

from pathlib import Path

from claudlobby.config import BotConfig, FleetConfig, PluginsConfig, ToolsConfig
from claudlobby.freshbox import (
    Finding,
    audit_bot,
    audit_fleet,
    classify_grants,
    has_failures,
)
from claudlobby.paths import Paths


def _build_library(root: Path) -> None:
    """Minimal library: one expertise granting Write/Edit."""
    (root / "runtime" / "bots").mkdir(parents=True)
    exp = root / "library" / "expertise"
    exp.mkdir(parents=True)
    (exp / "eng.md").write_text(
        "---\ntitle: eng\npermissions:\n  allow: [Write, Edit]\n---\n\n# eng\n"
    )


def _fleet(bots: dict[str, BotConfig]) -> FleetConfig:
    return FleetConfig(
        name="t",
        service_prefix="p",
        plugins=PluginsConfig(
            required=["claudna@Claudfather", "superpowers@claude-plugins-official"]
        ),
        bots=bots,
    )


def test_fleet_override_grant_with_no_source_is_flagged_unsourced(tmp_path):
    """A ``tools.allow`` grant no equipped source declares is reported (drift signal)."""
    root = tmp_path / "claudlobby"
    _build_library(root)
    paths = Paths(root=root, fleet_dir=root)
    bot = BotConfig(
        bot_id="w",
        name="w",
        expertise=["eng"],
        tools=ToolsConfig(allow=["mcp__mystery__*"]),
    )
    fleet = _fleet({"w": bot})

    findings = audit_bot(bot, fleet, paths)

    unsourced = [f for f in findings if f.kind == "unsourced_grant"]
    assert any("mcp__mystery__*" in f.detail for f in unsourced), (
        f"expected mcp__mystery__* flagged as unsourced; got {findings}"
    )


def test_declared_grant_missing_from_allow_is_under_grant():
    """A source declares a grant that never reached the allow list (not denied) →
    under-grant: the bot would silently rely on the retired global."""
    results = classify_grants(
        allow=["Read", "Write"],
        deny=[],
        sourced={"mcp__github__*", "Write"},
        base={"Read", "Grep", "Glob"},
        override=set(),
    )
    flagged = {(kind, grant) for kind, _sev, grant in results}
    assert ("under_grant", "mcp__github__*") in flagged
    # Write is declared AND granted — not flagged.
    assert not any(grant == "Write" for _k, _s, grant in results)


def test_denied_declared_grant_is_not_under_grant():
    """deny-wins is legitimate: a sourced grant that is denied is absent from
    allow by design, not an under-grant."""
    results = classify_grants(
        allow=["Read"],
        deny=["Bash"],
        sourced={"Bash"},
        base={"Read", "Grep", "Glob"},
        override=set(),
    )
    assert not any(kind == "under_grant" for kind, _s, _g in results)


def test_allow_entry_tracing_to_nothing_is_orphan():
    """An allow entry matching no source, no base floor, and no fleet override is
    an orphan — composition emitted a grant nothing produced."""
    results = classify_grants(
        allow=["Read", "SomethingWeird"],
        deny=[],
        sourced={"Read"},
        base={"Read", "Grep", "Glob"},
        override=set(),
    )
    flagged = {(kind, grant) for kind, _sev, grant in results}
    assert ("orphan_grant", "SomethingWeird") in flagged


def test_enabledplugins_absent_is_flagged_missing_tier_a(tmp_path):
    """A fleet composing no ``enabledPlugins`` → the bot inherits plugin
    enablement from the global; a Tier-A self-containment failure."""
    root = tmp_path / "claudlobby"
    _build_library(root)
    paths = Paths(root=root, fleet_dir=root)
    bot = BotConfig(bot_id="w", name="w", expertise=["eng"])
    fleet = FleetConfig(
        name="t",
        service_prefix="p",
        plugins=PluginsConfig(required=[]),
        bots={"w": bot},
    )

    findings = audit_bot(bot, fleet, paths)

    tier_a = [
        f for f in findings if f.kind == "missing_tier_a" and f.severity == "fail"
    ]
    assert any("enabledPlugins" in f.detail for f in tier_a), (
        f"expected enabledPlugins flagged missing Tier-A; got {findings}"
    )


def test_wellformed_scoped_bot_has_no_fail_findings(tmp_path):
    """A normal scoped bot — Tier-A keys composed, every grant sourced — passes
    the gate clean (no fail-severity findings)."""
    root = tmp_path / "claudlobby"
    _build_library(root)
    paths = Paths(root=root, fleet_dir=root)
    bot = BotConfig(bot_id="w", name="w", expertise=["eng"])
    fleet = _fleet({"w": bot})

    findings = audit_bot(bot, fleet, paths)

    fails = [f for f in findings if f.severity == "fail"]
    assert fails == [], f"expected a clean bot to have no fail findings; got {fails}"


def test_audit_fleet_sweeps_every_bot(tmp_path):
    """The sweep visits all bots; a drifty one surfaces, a clean one stays silent."""
    root = tmp_path / "claudlobby"
    _build_library(root)
    paths = Paths(root=root, fleet_dir=root)
    bots = {
        "clean": BotConfig(bot_id="clean", name="clean", expertise=["eng"]),
        "drifty": BotConfig(
            bot_id="drifty",
            name="drifty",
            expertise=["eng"],
            tools=ToolsConfig(allow=["mcp__mystery__*"]),
        ),
    }
    fleet = _fleet(bots)

    findings = audit_fleet(fleet, paths)

    assert any(f.bot_id == "drifty" for f in findings)
    assert not any(f.bot_id == "clean" for f in findings)


def test_has_failures_true_only_for_fail_severity():
    """The gate blocks on fail-severity findings; warns are advisory."""
    assert has_failures([Finding("b", "missing_tier_a", "fail", "x")])
    assert not has_failures([Finding("b", "unsourced_grant", "warn", "x")])
    assert not has_failures([])


def test_format_report_renders_clean_summary_and_findings(tmp_path):
    """The report reads as self-contained when clean, and names the drift otherwise."""
    from claudlobby.freshbox import format_report

    root = tmp_path / "claudlobby"
    _build_library(root)
    paths = Paths(root=root, fleet_dir=root)

    clean = _fleet({"w": BotConfig(bot_id="w", name="w", expertise=["eng"])})
    clean_out = format_report(clean, audit_fleet(clean, paths))
    assert "self-contained" in clean_out.lower()

    drifty = _fleet(
        {
            "d": BotConfig(
                bot_id="d",
                name="d",
                expertise=["eng"],
                tools=ToolsConfig(allow=["mcp__x__*"]),
            )
        }
    )
    drifty_out = format_report(drifty, audit_fleet(drifty, paths))
    assert "mcp__x__*" in drifty_out
    assert "d" in drifty_out


def test_rich_multisource_bot_audits_clean(tmp_path):
    """A bot drawing grants from expertise + integration + guardrail + channel
    audits clean — the gate must not false-alarm on a well-formed bot."""
    from claudlobby.config import TelegramConfig

    root = tmp_path / "claudlobby"
    _build_library(root)  # expertise/eng.md
    integ = root / "library" / "integrations"
    integ.mkdir(parents=True)
    (integ / "github.md").write_text(
        '---\ntitle: github\ntool_grants:\n  - "mcp__github__*"\n---\n\n# github\n'
    )
    guard = root / "library" / "guardrails"
    guard.mkdir(parents=True)
    (guard / "safe.md").write_text(
        '---\ntitle: safe\npermissions:\n  deny: ["Bash(rm *)"]\n---\n\n# safe\n'
    )
    paths = Paths(root=root, fleet_dir=root)
    bot = BotConfig(
        bot_id="rich",
        name="rich",
        expertise=["eng"],
        integrations=["github"],
        guardrails=["safe"],
        telegram=TelegramConfig(handle="rich_bot"),
    )
    fleet = _fleet({"rich": bot})

    findings = audit_bot(bot, fleet, paths)

    fails = [f for f in findings if f.severity == "fail"]
    assert fails == [], f"rich well-formed bot should audit clean; got {fails}"
    # the integration grant is recognized as sourced, not flagged as drift.
    assert not any("mcp__github__*" in f.detail for f in findings)
