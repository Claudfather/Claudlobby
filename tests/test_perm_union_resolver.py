"""P2 (#644) — generalized permission union resolver.

The composed allow/deny list is a union over every equipped source, split by the
two grant semantics (F2):

- **additive ``tool_grants``** — integrations (already wired) *and now skills*
  (:func:`_resolve_skill_grants`) contribute allow-only patterns;
- **deny-capable ``permissions:{}``** — expertise (already wired) *and now
  guardrails* (:func:`_resolve_guardrail_permissions`) contribute allow + deny
  via the shared :class:`ExpertisePermissions` fold.

Both fold into the layered union in :func:`compose_settings_local`. Layer 0
sibling isolation denies Write/Edit as well as Read (R9). The legacy
``_resolve_mcp_permissions`` path is intentionally *not* touched here — its cut
is gated on #628 P7.
"""

from __future__ import annotations

from pathlib import Path

from claudlobby.composer import (
    _resolve_guardrail_permissions,
    _resolve_skill_grants,
    compose_settings_local,
)
from claudlobby.config import BotConfig, FleetConfig


def _setup(tmp_path: Path):
    root = tmp_path / "claudlobby"
    (root / "runtime" / "bots").mkdir(parents=True)
    from claudlobby.paths import Paths

    return root, Paths(root=root, fleet_dir=root)


def _write_skill(root: Path, name: str, fm: str = "") -> None:
    d = root / "library" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n{fm}---\n\n# {name}\n\nbody\n")


def _write_guardrail(root: Path, name: str, fm: str = "") -> None:
    path = root / "library" / "guardrails" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {name}\n{fm}---\n\n# {name}\n\nbody\n")


def _bot(bot_id: str = "w", **kw) -> BotConfig:
    kw.setdefault("expertise", [])
    return BotConfig(bot_id=bot_id, name=bot_id, **kw)


def _single_bot_fleet(bot: BotConfig) -> FleetConfig:
    return FleetConfig(name="t", service_prefix="p", bots={bot.bot_id: bot})


# ── _resolve_skill_grants — additive tool_grants from equipped skills ──


class TestResolveSkillGrants:
    def test_flattens_equipped_skill_grants(self, tmp_path):
        root, paths = _setup(tmp_path)
        _write_skill(
            root, "dispatch", 'tool_grants:\n  - "Bash(tmux *)"\n  - "mcp__github__*"\n'
        )
        bot = _bot(skills=["dispatch"])
        assert _resolve_skill_grants(bot, paths) == ["Bash(tmux *)", "mcp__github__*"]

    def test_unions_across_skills(self, tmp_path):
        root, paths = _setup(tmp_path)
        _write_skill(root, "a", 'tool_grants:\n  - "Bash(git *)"\n')
        _write_skill(root, "b", 'tool_grants:\n  - "Read"\n')
        bot = _bot(skills=["a", "b"])
        assert _resolve_skill_grants(bot, paths) == ["Bash(git *)", "Read"]

    def test_prose_skill_contributes_nothing(self, tmp_path):
        root, paths = _setup(tmp_path)
        _write_skill(root, "plain")
        bot = _bot(skills=["plain"])
        assert _resolve_skill_grants(bot, paths) == []

    def test_folder_expansion_resolves_members(self, tmp_path):
        # ``pack/`` expands to every skill dir beneath it — grants must not be skipped.
        root, paths = _setup(tmp_path)
        _write_skill(root, "pack/one", 'tool_grants:\n  - "Bash(a *)"\n')
        _write_skill(root, "pack/two", 'tool_grants:\n  - "Bash(b *)"\n')
        bot = _bot(skills=["pack/"])
        grants = _resolve_skill_grants(bot, paths)
        assert "Bash(a *)" in grants and "Bash(b *)" in grants

    def test_no_skills(self, tmp_path):
        _, paths = _setup(tmp_path)
        bot = _bot()
        assert _resolve_skill_grants(bot, paths) == []


# ── _resolve_guardrail_permissions — deny-capable permissions:{} ──────


class TestResolveGuardrailPermissions:
    def test_deny_capable_block(self, tmp_path):
        root, paths = _setup(tmp_path)
        _write_guardrail(root, "no-write", "permissions:\n  deny: [Write, Edit]\n")
        bot = _bot(guardrails=["no-write"])
        allow, deny = _resolve_guardrail_permissions(bot, paths)
        assert deny == ["Write", "Edit"]
        assert allow == []

    def test_allow_and_deny(self, tmp_path):
        root, paths = _setup(tmp_path)
        _write_guardrail(root, "g", "permissions:\n  allow: [Read]\n  deny: [Bash]\n")
        bot = _bot(guardrails=["g"])
        allow, deny = _resolve_guardrail_permissions(bot, paths)
        assert allow == ["Read"]
        assert deny == ["Bash"]

    def test_prose_guardrail_contributes_nothing(self, tmp_path):
        # Snowflake SELECT-only stays prose (grammar can't express it).
        root, paths = _setup(tmp_path)
        _write_guardrail(root, "snowflake-read-only")
        bot = _bot(guardrails=["snowflake-read-only"])
        assert _resolve_guardrail_permissions(bot, paths) == ([], [])

    def test_unions_and_deny_wins_within_merge(self, tmp_path):
        # One guardrail allows Bash, another denies it — deny wins in the merged set.
        root, paths = _setup(tmp_path)
        _write_guardrail(root, "loose", "permissions:\n  allow: [Bash, Read]\n")
        _write_guardrail(root, "strict", "permissions:\n  deny: [Bash]\n")
        bot = _bot(guardrails=["loose", "strict"])
        allow, deny = _resolve_guardrail_permissions(bot, paths)
        assert "Bash" in deny
        assert "Bash" not in allow
        assert "Read" in allow

    def test_bash_allow_expands_to_pattern(self, tmp_path):
        root, paths = _setup(tmp_path)
        _write_guardrail(root, "g", 'permissions:\n  bash_allow: ["git status"]\n')
        bot = _bot(guardrails=["g"])
        allow, _ = _resolve_guardrail_permissions(bot, paths)
        assert "Bash(git status *)" in allow

    def test_folder_expansion_resolves_members(self, tmp_path):
        root, paths = _setup(tmp_path)
        _write_guardrail(root, "git/no-force", 'permissions:\n  deny: ["Bash(x *)"]\n')
        _write_guardrail(root, "git/no-clean", 'permissions:\n  deny: ["Bash(y *)"]\n')
        bot = _bot(guardrails=["git/"])
        _, deny = _resolve_guardrail_permissions(bot, paths)
        assert "Bash(x *)" in deny and "Bash(y *)" in deny

    def test_no_guardrails(self, tmp_path):
        _, paths = _setup(tmp_path)
        bot = _bot()
        assert _resolve_guardrail_permissions(bot, paths) == ([], [])


# ── compose_settings_local wiring (the union in situ) ─────────────────


class TestComposeWiresNewLayers:
    def _sibling_deny(self, tmp_path):
        root, paths = _setup(tmp_path)
        bots = {b: _bot(b) for b in ("bot-a", "bot-b")}
        fleet = FleetConfig(name="t", service_prefix="p", bots=bots)
        deny = compose_settings_local(bots["bot-a"], fleet, paths)["permissions"][
            "deny"
        ]
        return [d for d in deny if "bot-b" in d]

    def test_sibling_isolation_covers_read_and_edit(self, tmp_path):
        """R9 cross-bot isolation, now as two rules rather than three.

        Edit covers every file-editing tool, Write included — which is what the
        boot warning this used to provoke says in so many words.
        """
        sibling = self._sibling_deny(tmp_path)
        assert any(d.startswith("Read(") for d in sibling)
        assert any(d.startswith("Edit(") for d in sibling)

    def test_sibling_isolation_emits_no_write_rule(self, tmp_path):
        """#873: Claude Code ACCEPTS a Write(path) rule and never consults it.

        It warns about it once per sibling on every boot, so the rule bought
        nothing and cost N-1 warnings per bot per boot. Asserting its ABSENCE
        rather than deleting the old assertion, so a well-meaning re-add fails
        here instead of quietly restoring the noise.
        """
        assert not any(d.startswith("Write(") for d in self._sibling_deny(tmp_path))

    def test_sibling_rules_are_double_slash_absolute(self, tmp_path):
        """#1312: the whole control was inert without this.

        A single leading slash anchors at the SETTINGS SOURCE, not the filesystem
        root, so `Read(/abs/**)` names a directory that never exists and permits
        exactly what it claims to deny. Measured in a scratch project, with a
        no-rule control and the mechanism shown directly: `Read(/target/**)`
        BLOCKS `<project>/target/inside.txt`.

        This asserts the SHAPE of every emitted rule, not just that some rule
        exists — a rule with the wrong prefix is indistinguishable from a correct
        one at a glance, and that is exactly how 372 inert rules shipped.
        """
        sibling = self._sibling_deny(tmp_path)
        assert sibling, "fixture produced no sibling rules"
        for rule in sibling:
            body = rule[rule.index("(") + 1 : rule.rindex(")")]
            assert body.startswith("//"), f"{rule} is not absolute-anchored"
            assert not body.startswith("///"), rule

    def test_skill_grants_land_in_allow(self, tmp_path):
        root, paths = _setup(tmp_path)
        _write_skill(root, "dispatch", 'tool_grants:\n  - "Bash(tmux *)"\n')
        bot = _bot("solo", skills=["dispatch"])
        result = compose_settings_local(bot, _single_bot_fleet(bot), paths)
        assert "Bash(tmux *)" in result["permissions"]["allow"]

    def test_guardrail_deny_lands_in_deny(self, tmp_path):
        root, paths = _setup(tmp_path)
        _write_guardrail(
            root,
            "no-force-push",
            'permissions:\n  deny: ["Bash(git push --force *)"]\n',
        )
        bot = _bot("solo", guardrails=["no-force-push"])
        result = compose_settings_local(bot, _single_bot_fleet(bot), paths)
        assert "Bash(git push --force *)" in result["permissions"]["deny"]

    def test_guardrail_allow_lands_in_allow(self, tmp_path):
        root, paths = _setup(tmp_path)
        _write_guardrail(root, "reader", "permissions:\n  allow: [WebFetch]\n")
        bot = _bot("solo", guardrails=["reader"])
        result = compose_settings_local(bot, _single_bot_fleet(bot), paths)
        assert "WebFetch" in result["permissions"]["allow"]

    def test_guardrail_deny_and_skill_grant_both_emitted(self, tmp_path):
        # A skill grants Bash(git *); a guardrail denies force-push. Both are emitted;
        # CC resolves deny-wins at runtime. Compose keeps the allow and the deny.
        root, paths = _setup(tmp_path)
        _write_skill(root, "gitter", 'tool_grants:\n  - "Bash(git *)"\n')
        _write_guardrail(
            root, "no-force", 'permissions:\n  deny: ["Bash(git push --force *)"]\n'
        )
        bot = _bot("solo", skills=["gitter"], guardrails=["no-force"])
        result = compose_settings_local(bot, _single_bot_fleet(bot), paths)
        assert "Bash(git *)" in result["permissions"]["allow"]
        assert "Bash(git push --force *)" in result["permissions"]["deny"]

    def test_legacy_mcp_path_untouched(self, tmp_path):
        # #628 P7 gate: the new resolvers must not disturb the legacy MCP grant path.
        import claudlobby.composer as composer

        assert hasattr(composer, "_resolve_mcp_permissions")
