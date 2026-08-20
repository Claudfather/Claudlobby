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

from claudlobby.config import (
    BotConfig,
    FleetConfig,
    PluginsConfig,
    TelegramConfig,
    ToolPermissionsConfig,
)
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
        tool_permissions=ToolPermissionsConfig(allow=["mcp__mystery__*"]),
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
            tool_permissions=ToolPermissionsConfig(allow=["mcp__mystery__*"]),
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
                tool_permissions=ToolPermissionsConfig(allow=["mcp__x__*"]),
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


def test_sourced_grants_mirrors_every_composer_grant_resolver():
    """Drift guard: every allow-contributing composer resolver must be referenced
    by `_sourced_grants`. Adding a new grant/permission resolver to the composer
    without mirroring it here would misclassify its grants as orphans (false
    fail); this converts that latent drift into a CI failure at add-time."""
    import inspect

    from claudlobby import composer, freshbox

    src = inspect.getsource(freshbox._sourced_grants)
    resolvers = [
        name
        for name in dir(composer)
        if name.startswith("_resolve_") and ("grant" in name or "permission" in name)
    ]
    assert resolvers, "expected composer to expose grant/permission resolvers"
    missing = [r for r in resolvers if r not in src]
    assert not missing, (
        "_sourced_grants must reference every composer grant/permission resolver; "
        f"missing: {missing} (mirror them or the audit false-flags their grants)"
    )


# ── Scope 4: path + orphan-unit assertions folded into the audit ──────────────


def _seed_bot_dir(paths: Paths, bot_id: str = "kev") -> Path:
    bot_dir = paths.bot_runtime(bot_id)
    bot_dir.mkdir(parents=True, exist_ok=True)
    return bot_dir


def test_orphan_short_form_plist_is_flagged_warn(tmp_path):
    root = tmp_path / "cl"
    _build_library(root)
    bot = BotConfig(bot_id="kev", name="kev", expertise=["eng"])
    fleet = _fleet({"kev": bot})
    paths = Paths(root=root, fleet_dir=root)
    bot_dir = _seed_bot_dir(paths)
    (bot_dir / "p.kev.plist").write_text("<plist/>")  # composed long-form — kept
    (bot_dir / "p.kev.service").write_text("[Unit]\n")  # composed long-form — kept
    (bot_dir / "kev.plist").write_text("<plist/>")  # pre-naming orphan — flagged

    orphans = [f for f in audit_bot(bot, fleet, paths) if f.kind == "orphan_unit"]
    assert [f.severity for f in orphans] == ["warn"]
    assert "kev.plist" in orphans[0].detail


def test_reap_removes_orphan_keeps_long_form(tmp_path):
    from claudlobby.freshbox import reap_orphan_units

    root = tmp_path / "cl"
    _build_library(root)
    bot = BotConfig(bot_id="kev", name="kev", expertise=["eng"])
    fleet = _fleet({"kev": bot})
    paths = Paths(root=root, fleet_dir=root)
    bot_dir = _seed_bot_dir(paths)
    long_form = bot_dir / "p.kev.plist"
    orphan = bot_dir / "kev.plist"
    long_form.write_text("<plist/>")
    orphan.write_text("<plist/>")

    removed = reap_orphan_units(fleet, paths)
    assert removed == [orphan]
    assert not orphan.exists()
    assert long_form.exists()
    # Recurrence closed: a re-audit now finds no orphan.
    assert [f for f in audit_bot(bot, fleet, paths) if f.kind == "orphan_unit"] == []


def test_flat_path_in_emitted_mcp_json_is_improper_path_fail(tmp_path):
    import json

    root = tmp_path / "cl"
    _build_library(root)  # root-level shared library
    fleet_dir = root / "local" / "home" / "tl"  # nested overlay
    (fleet_dir / "runtime" / "bots").mkdir(parents=True)
    bot = BotConfig(bot_id="kev", name="kev", expertise=["eng"])
    fleet = _fleet({"kev": bot})
    paths = Paths(root=root, fleet_dir=fleet_dir)
    bot_dir = _seed_bot_dir(paths)
    flat = f"{root}/local/tl/dist/index.js"  # flat husk: local/tl not local/home/tl
    (bot_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"x": {"args": [flat]}}})
    )

    findings = audit_bot(bot, fleet, paths)
    improper = [f for f in findings if f.kind == "improper_path"]
    assert [f.severity for f in improper] == ["fail"]
    assert ".mcp.json" in improper[0].detail and flat in improper[0].detail


# ── #702 L1 source guard folded into the audit (denied_value) ─────────


def test_denied_source_value_is_a_fail_finding(tmp_path):
    """A foreign absolute in a bot SOURCE (not just emitted wiring) surfaces as a
    denied_value FAIL — the L1 complement to the improper_path (L2) check."""
    root = tmp_path / "claudlobby"
    _build_library(root)
    paths = Paths(root=root, fleet_dir=root)
    bot = BotConfig(
        bot_id="w", name="w", expertise=["eng"], env={"GA4_KEY": "/Users/x/ga4.json"}
    )
    fleet = _fleet({"w": bot})

    findings = audit_bot(bot, fleet, paths)

    denied = [f for f in findings if f.kind == "denied_value"]
    assert [f.severity for f in denied] == ["fail"]
    assert "/Users/x/ga4.json" in denied[0].detail


def test_anchored_source_value_has_no_denied_finding(tmp_path):
    root = tmp_path / "claudlobby"
    _build_library(root)
    paths = Paths(root=root, fleet_dir=root)
    bot = BotConfig(
        bot_id="w", name="w", expertise=["eng"], env={"P": "${FLEET_ROOT}/mcp/x.py"}
    )
    fleet = _fleet({"w": bot})

    denied = [f for f in audit_bot(bot, fleet, paths) if f.kind == "denied_value"]
    assert denied == []


# ─────────────────────────────────────────────────────────────────────────────
# #703 Phase 2 — externals report, .env rung (F5), rendered tools/ (F6), INFO tier
#
# Three rungs fold into the same audit_bot flow beyond #644's grant/path checks:
# an externals visibility report (INFO + unused-declaration WARN), a fleet-tier
# .env path-value rung (F5 — FAIL on fleet-owned files, WARN on host-tier), and
# rendered tools/ folded into the L2 emitted-path scan (F6). INFO never blocks;
# .env findings mask their value (R4) so a report never echoes a secret.
# ─────────────────────────────────────────────────────────────────────────────


def _nested_paths(root: Path) -> Paths:
    """Overlay layout so the fleet-tier .env (fleet_config_dir/.env) and the
    install-tier .env (root/.env) are distinct files — needed to tell F5's FAIL
    tier from its WARN tier (they collapse to one file when fleet_dir == root)."""
    fleet_dir = root / "local" / "home" / "tl"
    (fleet_dir / "runtime" / "bots").mkdir(parents=True)
    return Paths(root=root, fleet_dir=fleet_dir)


def _kev(**kw) -> BotConfig:
    return BotConfig(bot_id="kev", name="kev", expertise=["eng"], **kw)


# ── F5: the .env rung — fleet-tier FAIL, host-tier WARN, value masked ──────────


def test_env_bot_tier_path_value_is_masked_fail(tmp_path):
    """A raw absolute in a bot's own .env (fleet-owned) is a FAIL, and R4 holds:
    the value is masked — its body never reaches the report (a base64 secret can
    legitimately lead with '/' and trip the grammar)."""
    from claudlobby.freshbox import _env_file_findings

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev()
    fleet = _fleet({"kev": bot})
    bot_dir = paths.bot_runtime("kev")
    bot_dir.mkdir(parents=True)
    secret = "/Users/x/very-secret-service-account.json"
    (bot_dir / ".env").write_text(f"GA4_SA_KEY_PATH={secret}\n")

    env = [
        f for f in _env_file_findings(bot, fleet, paths) if f.kind == "env_denied_value"
    ]
    assert [f.severity for f in env] == ["fail"]
    assert "GA4_SA_KEY_PATH" in env[0].detail  # the key names the offender
    assert secret not in env[0].detail  # R4: value masked
    assert "very-secret-service-account" not in env[0].detail


def test_env_fleet_overlay_path_value_is_fail(tmp_path):
    from claudlobby.freshbox import _env_file_findings

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev()
    fleet = _fleet({"kev": bot})
    (paths.fleet_config_dir / ".env").write_text("KEY=/opt/foreign/x.json\n")

    env = [
        f for f in _env_file_findings(bot, fleet, paths) if f.kind == "env_denied_value"
    ]
    assert [f.severity for f in env] == ["fail"]


def test_env_install_tier_root_env_is_warn(tmp_path):
    """root/.env is install-shared host-tier — a path value there WARNs, never FAILs
    (fleet policy's writ ends at fleet-owned files, F5)."""
    from claudlobby.freshbox import _env_file_findings

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev()
    fleet = _fleet({"kev": bot})
    (paths.root / ".env").write_text("KEY=/opt/foreign/x.json\n")

    env = [
        f for f in _env_file_findings(bot, fleet, paths) if f.kind == "env_denied_value"
    ]
    assert [f.severity for f in env] == ["warn"]


def test_env_home_tier_scanned_only_when_home_injected(tmp_path):
    """The operator's personal ~/.env is out of fleet jurisdiction: the library
    never reaches into it by default (home=None), so a unit audit never reads the
    real home. When the CLI injects home, its path values WARN."""
    from claudlobby.freshbox import _env_file_findings

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev()
    fleet = _fleet({"kev": bot})
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".env").write_text("KEY=/opt/foreign/x.json\n")

    # default: home not injected → ~/.env untouched
    assert _env_file_findings(bot, fleet, paths) == []
    # injected: host-tier WARN
    env = [
        f
        for f in _env_file_findings(bot, fleet, paths, home=fake_home)
        if f.kind == "env_denied_value"
    ]
    assert [f.severity for f in env] == ["warn"]


def test_env_declared_value_is_not_flagged(tmp_path):
    """A .env path blessed by an external_paths declaration is legitimate — no finding."""
    from claudlobby.freshbox import _env_file_findings
    from claudlobby.path_audit import ExternalDecl

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev(
        external_paths=[ExternalDecl(path="/opt/printify/**", purpose="printify tree")]
    )
    fleet = _fleet({"kev": bot})
    bot_dir = paths.bot_runtime("kev")
    bot_dir.mkdir(parents=True)
    (bot_dir / ".env").write_text("PRINTIFY=/opt/printify/data/x\n")

    assert _env_file_findings(bot, fleet, paths) == []


def test_env_anchored_and_plain_var_values_are_not_flagged(tmp_path):
    from claudlobby.freshbox import _env_file_findings

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev()
    fleet = _fleet({"kev": bot})
    bot_dir = paths.bot_runtime("kev")
    bot_dir.mkdir(parents=True)
    (bot_dir / ".env").write_text(
        "ANCHORED=${FLEET_ROOT}/mcp/x.py\nTOKEN=${GITHUB_PAT}\nPLAIN=not-a-path\n"
    )
    assert _env_file_findings(bot, fleet, paths) == []


def test_env_missing_files_no_findings_no_crash(tmp_path):
    from claudlobby.freshbox import _env_file_findings

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev()
    fleet = _fleet({"kev": bot})
    assert _env_file_findings(bot, fleet, paths) == []


def test_env_root_mode_dedups_to_single_fail(tmp_path):
    """When fleet_dir == root the fleet-tier and install-tier .env are the same file
    — reported once, as the stricter FAIL, never twice."""
    from claudlobby.freshbox import _env_file_findings

    root = tmp_path / "cl"
    _build_library(root)
    paths = Paths(root=root, fleet_dir=root)  # fleet_config_dir == root
    bot = _kev()
    fleet = _fleet({"kev": bot})
    (root / ".env").write_text("KEY=/opt/foreign/x.json\n")

    env = [
        f for f in _env_file_findings(bot, fleet, paths) if f.kind == "env_denied_value"
    ]
    assert [f.severity for f in env] == ["fail"]


# ── Externals visibility report — INFO surface + unused-declaration WARN ───────


def test_external_declared_and_used_is_info_not_unused(tmp_path):
    from claudlobby.freshbox import INFO, _externals_report
    from claudlobby.path_audit import ExternalDecl

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev(
        external_paths=[ExternalDecl(path="/opt/printify/bin", purpose="printify cli")],
        env={"PRINTIFY_BIN": "/opt/printify/bin"},
    )
    fleet = _fleet({"kev": bot})

    findings = _externals_report(bot, fleet, paths)
    refs = [f for f in findings if f.kind == "external_ref"]
    assert not [f for f in findings if f.kind == "unused_declaration"]
    assert any("/opt/printify/bin" in f.detail and f.severity == INFO for f in refs)
    assert any("PRINTIFY_BIN" in f.detail for f in refs)  # usage provenance shown


def test_external_declared_unused_is_warn(tmp_path):
    from claudlobby.freshbox import _externals_report
    from claudlobby.path_audit import ExternalDecl

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev(
        external_paths=[ExternalDecl(path="/opt/ghost/bin", purpose="nothing uses me")]
    )
    fleet = _fleet({"kev": bot})

    unused = [
        f
        for f in _externals_report(bot, fleet, paths)
        if f.kind == "unused_declaration"
    ]
    assert [f.severity for f in unused] == ["warn"]
    assert "/opt/ghost/bin" in unused[0].detail


def test_external_mount_and_vault_are_info(tmp_path):
    from claudlobby.freshbox import INFO, _externals_report

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev(mounts={"data": "/mnt/host/data"}, claudron_vault_path="/mnt/vault")
    fleet = _fleet({"kev": bot})

    refs = [f for f in _externals_report(bot, fleet, paths) if f.kind == "external_ref"]
    details = " ".join(f.detail for f in refs)
    assert "/mnt/host/data" in details
    assert "/mnt/vault" in details
    assert all(f.severity == INFO for f in refs)


def test_external_default_account_emits_nothing(tmp_path):
    """A bot with no external surface and the default account produces no external
    findings — the report never noises up a clean bot (preserves audit_fleet's
    clean-bot invariant)."""
    from claudlobby.freshbox import _externals_report

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev()
    fleet = _fleet({"kev": bot})  # accounts defaults to {"default": "~/.claude"}
    assert _externals_report(bot, fleet, paths) == []


def test_external_overbroad_declaration_shows_both_use_sites(tmp_path):
    """Adversarial: an over-broad /**-declaration silently blessing a second use
    site is invisible to the unused-WARN (it always matches something) — the INFO
    surfaces every provenance so a reviewer can see the extra coupling."""
    from claudlobby.freshbox import _externals_report
    from claudlobby.path_audit import ExternalDecl

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev(
        external_paths=[ExternalDecl(path="/opt/vendor/**", purpose="vendor tree")],
        env={"A": "/opt/vendor/tool-a", "B": "/opt/vendor/deep/tool-b"},
    )
    fleet = _fleet({"kev": bot})

    refs = [f for f in _externals_report(bot, fleet, paths) if f.kind == "external_ref"]
    ref = next(f for f in refs if "/opt/vendor/**" in f.detail)
    assert "env.A" in ref.detail and "env.B" in ref.detail  # both use sites visible


# ── INFO tier + exit contract (the --strict trap) ─────────────────────────────


def test_info_findings_never_block():
    from claudlobby.freshbox import INFO, Finding, exits_nonzero, has_failures

    info = [Finding("b", "external_ref", INFO, "just visibility")]
    assert not has_failures(info)
    assert not exits_nonzero(info, strict=False)
    assert not exits_nonzero(info, strict=True)  # --strict must not trip on INFO


def test_strict_blocks_warn_but_not_info():
    from claudlobby.freshbox import FAIL, WARN, Finding, exits_nonzero

    warn = [Finding("b", "unused_declaration", WARN, "rot")]
    assert not exits_nonzero(warn, strict=False)  # WARN advisory by default
    assert exits_nonzero(warn, strict=True)  # ...but blocks under --strict
    fail = [Finding("b", "denied_value", FAIL, "x")]
    assert exits_nonzero(fail, strict=False)  # FAIL always blocks


def test_format_report_renders_info_lines():
    from claudlobby.freshbox import INFO, Finding, format_report

    fleet = _fleet({"kev": _kev()})
    out = format_report(
        fleet, [Finding("kev", "external_ref", INFO, "mount data → /mnt/x")]
    )
    assert "[info]" in out
    assert "/mnt/x" in out
    assert "0 fail" in out and "1 fail" not in out


def test_clean_bot_with_used_external_has_no_fail(tmp_path):
    """A bot whose externals are all declared + used audits with zero FAIL — the INFO
    report doesn't turn a healthy bot red, and --strict stays green on it."""
    from claudlobby.freshbox import exits_nonzero
    from claudlobby.path_audit import ExternalDecl

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev(
        external_paths=[ExternalDecl(path="/opt/printify/bin", purpose="cli")],
        env={"PRINTIFY_BIN": "/opt/printify/bin"},
    )
    fleet = _fleet({"kev": bot})
    paths.bot_runtime("kev").mkdir(parents=True)

    findings = audit_bot(bot, fleet, paths)
    assert [f for f in findings if f.severity == "fail"] == []
    assert not exits_nonzero(findings, strict=False)


# ── F6: rendered tools/ folded into the L2 emitted-path scan ──────────────────


def test_flat_path_in_rendered_tool_is_improper_path_fail(tmp_path):
    """A fleet-shaped absolute baked into a rendered tool script dangles on a fleet
    move exactly like one in .mcp.json — freshbox flags it (L2 shape), FAIL."""
    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev()
    fleet = _fleet({"kev": bot})
    bot_dir = paths.bot_runtime("kev")
    (bot_dir / "tools").mkdir(parents=True)
    flat = f"{root}/local/tl/dist/deploy.sh"  # flat husk: local/tl not local/home/tl
    (bot_dir / "tools" / "deploy.sh").write_text(f"#!/bin/sh\nexec {flat}\n")

    improper = [f for f in audit_bot(bot, fleet, paths) if f.kind == "improper_path"]
    assert [f.severity for f in improper] == ["fail"]
    assert "tools/deploy.sh" in improper[0].detail and flat in improper[0].detail


def test_legit_rendered_tool_paths_pass(tmp_path):
    """A tool script's system paths (/usr/bin/env, /dev/null) are not fleet-owned —
    F6 is L2 shape only, never L1, so they never false-positive."""
    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    bot = _kev()
    fleet = _fleet({"kev": bot})
    bot_dir = paths.bot_runtime("kev")
    (bot_dir / "tools").mkdir(parents=True)
    (bot_dir / "tools" / "ok.sh").write_text(
        "#!/usr/bin/env bash\ncat /dev/null > /tmp/x\n"
    )

    improper = [f for f in audit_bot(bot, fleet, paths) if f.kind == "improper_path"]
    assert improper == []


# ── #792: per-bot identity secret leaking via a host-shared .env tier ──────────
# source_env_tiered sources the global ~/.env and the deprecated/install-shared
# $CLAUDLOBBY_ROOT/.env into EVERY bot, so a per-bot secret placed there leaks
# host-wide (the A1 config-review incident). A var is per-bot when its own env
# contract is bot-tier — the robust signal, since Slack's SLACK_TOKEN carries no
# per-bot affix a key-name regex could catch.


def _token_bot(bot_id: str, token_env: str) -> BotConfig:
    return BotConfig(
        bot_id=bot_id,
        name=bot_id,
        expertise=["eng"],
        telegram=TelegramConfig(token_env=token_env),
    )


def test_bot_secret_in_deprecated_root_env_is_fail(tmp_path):
    """A per-bot token in the install-shared/deprecated root/.env — host-wide via
    source_env_tiered — is a FAIL (value masked, R4); a co-located non-bot-tier var
    in the same file is left alone (selectivity)."""
    from claudlobby.freshbox import _env_secret_leak_findings

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    fleet = _fleet({"kev": _token_bot("kev", "TELEGRAM_TOKEN_KEV")})
    secret = "8888888:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    (root / ".env").write_text(
        f"TELEGRAM_TOKEN_KEV={secret}\nSOME_SHARED_SETTING=value\n"
    )

    leaks = [
        f
        for f in _env_secret_leak_findings(fleet, paths, home=None)
        if f.kind == "env_bot_secret_leaked"
    ]
    assert [f.severity for f in leaks] == ["fail"]
    assert "TELEGRAM_TOKEN_KEV" in leaks[0].detail
    assert "SOME_SHARED_SETTING" not in leaks[0].detail  # only the bot-tier key
    assert secret not in leaks[0].detail  # R4: value masked


def test_bot_secret_in_global_home_env_flagged_only_when_home_injected(tmp_path):
    """The operator's global ~/.env is scanned only when the CLI injects home; the
    library default (home=None) never reaches into a personal home."""
    from claudlobby.freshbox import _env_secret_leak_findings

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    fleet = _fleet({"kev": _token_bot("kev", "TELEGRAM_TOKEN_KEV")})
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".env").write_text("TELEGRAM_TOKEN_KEV=8888888:AAAA\n")

    assert _env_secret_leak_findings(fleet, paths, home=None) == []
    leaks = [
        f
        for f in _env_secret_leak_findings(fleet, paths, home=fake_home)
        if f.kind == "env_bot_secret_leaked"
    ]
    assert [f.severity for f in leaks] == ["fail"]
    assert "TELEGRAM_TOKEN_KEV" in leaks[0].detail


def test_bot_secret_in_bots_own_env_is_not_a_leak(tmp_path):
    """The bot's own .env is the correct home for a per-bot secret — never flagged."""
    from claudlobby.freshbox import _env_secret_leak_findings

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    fleet = _fleet({"kev": _token_bot("kev", "TELEGRAM_TOKEN_KEV")})
    bot_dir = paths.bot_runtime("kev")
    bot_dir.mkdir(parents=True)
    (bot_dir / ".env").write_text("TELEGRAM_TOKEN_KEV=8888888:AAAA\n")

    assert _env_secret_leak_findings(fleet, paths, home=None) == []


def test_leak_surfaces_once_through_audit_fleet(tmp_path):
    """Fleet-scoped: audit_fleet reports a leaked var once, not once per bot."""
    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    fleet = _fleet(
        {
            "kev": _token_bot("kev", "TELEGRAM_TOKEN_KEV"),
            "moe": _token_bot("moe", "TELEGRAM_TOKEN_MOE"),
        }
    )
    (root / ".env").write_text("TELEGRAM_TOKEN_KEV=8888888:AAAA\n")

    leaks = [
        f
        for f in audit_fleet(fleet, paths, home=None)
        if f.kind == "env_bot_secret_leaked"
    ]
    assert len(leaks) == 1
    assert "TELEGRAM_TOKEN_KEV" in leaks[0].detail


def test_self_referential_telegram_token_in_shared_tier_is_flagged(tmp_path):
    """The real-fleet pattern: token_env is self-referential (== TELEGRAM_BOT_TOKEN,
    the plugin's own read var), which the env contract deliberately omits — yet it
    is still a per-bot secret, so TELEGRAM_BOT_TOKEN in a host-shared tier must
    still be flagged (the A1 incident vector, missed by contract-only detection)."""
    from claudlobby.freshbox import _env_secret_leak_findings

    root = tmp_path / "cl"
    _build_library(root)
    paths = _nested_paths(root)
    fleet = _fleet({"kev": _token_bot("kev", "TELEGRAM_BOT_TOKEN")})
    (root / ".env").write_text("TELEGRAM_BOT_TOKEN=8888888:AAAA\n")

    leaks = [
        f
        for f in _env_secret_leak_findings(fleet, paths, home=None)
        if f.kind == "env_bot_secret_leaked"
    ]
    assert [f.severity for f in leaks] == ["fail"]
    assert "TELEGRAM_BOT_TOKEN" in leaks[0].detail


# ── per-org git credential routing: the externals it depends on ──────


def _git_cred_fleet(tmp_path, monkeypatch, *, operator_exists=True, gh="/usr/bin/gh"):
    """A composed bot declaring git_credentials, with both host seams stubbed."""
    import claudlobby.composer as comp

    root = tmp_path / "cl"
    _build_library(root)
    operator = tmp_path / "operator.gitconfig"
    if operator_exists:
        operator.write_text("[user]\n\temail = operator@example.com\n")
    monkeypatch.setattr(comp, "_operator_gitconfig", lambda: operator)
    monkeypatch.setattr(comp, "_resolve_gh_executable", lambda: gh)
    bot = BotConfig(
        bot_id="kev",
        name="kev",
        expertise=["eng"],
        git_credentials={"OrgA": "ORG_A_PAT"},
    )
    fleet = _fleet({"kev": bot})
    paths = Paths(root=root, fleet_dir=root)
    _seed_bot_dir(paths)
    return bot, fleet, paths, operator


def test_missing_operator_gitconfig_is_a_fail(tmp_path, monkeypatch):
    """git ignores a missing [include] SILENTLY, so routing keeps working while
    user.email vanishes and every commit dies 'Author identity unknown' — a
    failure with nothing pointing back at the composed file. The whole point of
    a fresh-box gate is to catch an absent host prerequisite here instead."""
    bot, fleet, paths, operator = _git_cred_fleet(
        tmp_path, monkeypatch, operator_exists=False
    )
    findings = [f for f in audit_bot(bot, fleet, paths) if f.kind == "missing_external"]
    assert [f.severity for f in findings] == ["fail"]
    assert str(operator) in findings[0].detail
    assert has_failures(audit_bot(bot, fleet, paths))


def test_present_operator_gitconfig_is_reported_as_external_not_failure(
    tmp_path, monkeypatch
):
    """Both host files the composed .gitconfig leans on are external coupling and
    belong in the visibility report — but INFO, so a correctly-composed bot does
    not fail the gate."""
    bot, fleet, paths, operator = _git_cred_fleet(tmp_path, monkeypatch)
    findings = audit_bot(bot, fleet, paths)
    assert not has_failures(findings)
    details = [f.detail for f in findings if f.kind == "external_ref"]
    assert any(str(operator) in d for d in details), details
    assert any("/usr/bin/gh" in d for d in details), details


def test_no_git_credentials_reports_no_git_externals(tmp_path, monkeypatch):
    """Inertness: a fleet declaring none must not grow report lines about a file
    it never includes."""
    bot, fleet, paths, _ = _git_cred_fleet(tmp_path, monkeypatch)
    bot.git_credentials = {}
    findings = audit_bot(bot, fleet, paths)
    assert not [f for f in findings if f.kind == "missing_external"]
    assert not [f for f in findings if "git " in f.detail]


class TestGithubAppFreshbox:
    """App-mode host audit (App-auth P3 #1273): the D7 include-FAIL/WARN split
    and the _app_key_findings branches. Each is a ratified deliverable; a
    WARN->FAIL flip must not merge green."""

    def _bot(self, tmp_path, **app_kwargs):
        from claudlobby.config import GithubAppConfig

        return BotConfig(
            bot_id="ga",
            name="ga",
            expertise=["eng"],
            github_app=GithubAppConfig(**app_kwargs),
        )

    def _audit(self, tmp_path, bot, monkeypatch, key_mode=None, key_exists=True, operator=True):
        import claudlobby.composer as comp

        root = tmp_path / "claudlobby"
        _build_library(root)
        paths = Paths(root=root, fleet_dir=root)
        op = tmp_path / "op.gitconfig"
        if operator:
            op.write_text("[user]\n\temail = o@example.com\n")
        monkeypatch.setattr(comp, "_operator_gitconfig", lambda: op)
        # env_resolved is the read door — stub it to a fixed key path.
        key = tmp_path / "app-key.pem"
        if key_exists:
            key.write_text("KEY")
            if key_mode is not None:
                key.chmod(key_mode)

        from claudlobby.env_tiers import Resolution

        def _resolved(bot_name=None):
            return {
                "GITHUB_APP_PRIVATE_KEY_PATH": Resolution(
                    "GITHUB_APP_PRIVATE_KEY_PATH", str(key), "fleet", None, ()
                )
            }

        monkeypatch.setattr(type(paths), "env_resolved", lambda self, bot_name=None: _resolved(bot_name))
        return audit_bot(bot, _fleet({"ga": bot}), paths)

    def test_key_0600_is_info(self, tmp_path, monkeypatch):
        finds = self._audit(tmp_path, self._bot(tmp_path), monkeypatch, key_mode=0o600)
        assert any(f.kind == "external_ref" and "App private key" in f.detail for f in finds)
        assert not any(f.severity == "fail" and "private key" in f.detail for f in finds)

    def test_key_group_readable_is_fail(self, tmp_path, monkeypatch):
        finds = self._audit(tmp_path, self._bot(tmp_path), monkeypatch, key_mode=0o640)
        assert any(f.severity == "fail" and "group/other-readable" in f.detail for f in finds)

    def test_missing_key_is_fail(self, tmp_path, monkeypatch):
        finds = self._audit(tmp_path, self._bot(tmp_path), monkeypatch, key_exists=False)
        assert any(f.severity == "fail" and "does not exist" in f.detail for f in finds)

    def test_missing_include_with_app_identity_softens_to_warn(self, tmp_path, monkeypatch):
        # D7: a composed App identity supplies user.email, so the missing
        # include cannot cause 'Author identity unknown' — WARN, not FAIL.
        bot = self._bot(tmp_path, slug="my-app", bot_user_id=7)
        finds = self._audit(tmp_path, bot, monkeypatch, key_mode=0o600, operator=False)
        include = [f for f in finds if "does not exist" in f.detail and "include" in f.detail]
        assert include and all(f.severity == "warn" for f in include), finds

    def test_missing_include_without_identity_is_fail(self, tmp_path, monkeypatch):
        bot = self._bot(tmp_path, key_exists=True) if False else self._bot(tmp_path)
        finds = self._audit(tmp_path, bot, monkeypatch, key_mode=0o600, operator=False)
        include = [f for f in finds if "does not exist" in f.detail and "include" in f.detail]
        assert include and all(f.severity == "fail" for f in include), finds
