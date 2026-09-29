"""tests/test_requires_linking.py — `requires:` frontmatter linking with the
GRANT UNION (PR4 task 1, spec §10). A protocol can bring a skill with it, and a
skill brought that way must be symlinked, GRANTED (its `settings.local.json`
allow entries) AND recorded as equipment — never linked-but-ungranted (the
cycle-1 review B8 regression this task exists to close)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from textwrap import dedent

from claudlobby import defaults
from claudlobby.composer import (
    _resolve_skill_grants,
    _resolve_skill_permissions,
    compose_bot,
    link_skills,
    resolve_effective_skills,
)
from claudlobby.config import load_fleet
from claudlobby.freshbox import audit_bot
from claudlobby.loader import iter_library_requires, library_requires
from tests.package_fixtures import source_package
from claudlobby.paths import Paths
from claudlobby.plane.registry_emit import bot_payload
from claudlobby.validator import validate

# ---------------------------------------------------------------------------
# Fixture helpers — a fake protocol requiring a fake skill. Names are
# obviously fake (the repo is public): "gadget" / "widget" / "sprocket", never
# a real fleet or bot name.
# ---------------------------------------------------------------------------


def _write_protocol(
    root: Path, name: str, requires_skills: list[str] | None = None
) -> Path:
    """A minimal protocol file under library/protocols/, optionally declaring
    ``requires: {skills: [...]}``."""
    fm = f"title: {name}\n"
    if requires_skills is not None:
        fm += "requires:\n  skills: [" + ", ".join(requires_skills) + "]\n"
    p = root / "library" / "protocols" / f"{name}.md"
    p.write_text(f"---\n{fm}---\n\n# {name}\n\nProtocol body.\n")
    return p


def _write_skill(root: Path, name: str, tool_grants: list[str] | None = None) -> Path:
    """A minimal skill dir under library/skills/<name>/SKILL.md, optionally
    declaring additive ``tool_grants``."""
    d = root / "library" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    fm = f"name: {name}\n"
    if tool_grants:
        fm += "tool_grants:\n" + "".join(f'  - "{g}"\n' for g in tool_grants)
    p = d / "SKILL.md"
    p.write_text(f"---\n{fm}---\n\n# {name}\n\nSkill body.\n")
    return p


def _equip(fleet_dir: Path, bot_id: str, **fields: list[str]) -> None:
    """Insert declared list fields (``protocols=[...]``, ``skills=[...]``)
    into a bot's stanza in the shared ``fleet_dir`` fixture's fleet.yaml —
    text surgery, the same convention ``tests/test_checkin_library.py`` uses."""
    text = (fleet_dir / "fleet.yaml").read_text()
    marker = f"    {bot_id}:\n"
    assert marker in text, f"bot {bot_id!r} not found in fleet.yaml"
    insert = "".join(f"      {k}: [{', '.join(v)}]\n" for k, v in fields.items())
    text = text.replace(marker, marker + insert, 1)
    (fleet_dir / "fleet.yaml").write_text(text)


def _paths(fleet_dir: Path) -> Paths:
    # These tests deliberately replace package.library with their tiny
    # synthetic library. Carry the real universal skill into that isolated
    # fixture so the effective-skill resolver can link and grant it.
    source = source_package()
    skill = fleet_dir / "library" / "skills" / "fleet-ops" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_bytes((source.library / "skills" / "fleet-ops" / "SKILL.md").read_bytes())
    return Paths(root=fleet_dir, fleet_dir=fleet_dir,
                 package=replace(source, library=fleet_dir / "library"))


# ---------------------------------------------------------------------------
# loader.library_requires / iter_library_requires — the generic reader
# ---------------------------------------------------------------------------


class TestLibraryRequires:
    def _write(self, root: Path, rel: str, fm_extra: str = "") -> Path:
        p = root / f"{rel}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntitle: {rel}\n{fm_extra}---\n\n# {rel}\n")
        return p

    def test_requires_block_parses_generically(self, tmp_path):
        # a generic requires block, not skills-only — protocols -> guardrails
        # or skills -> mcp must parse the same way without a format change.
        p = self._write(
            tmp_path,
            "sprocket",
            "requires:\n  skills: [gadget-a, gadget-b]\n  guardrails: [widget-guard]\n",
        )
        assert library_requires(p) == {
            "skills": ["gadget-a", "gadget-b"],
            "guardrails": ["widget-guard"],
        }

    def test_absent_requires_block_returns_empty_dict(self, tmp_path):
        p = self._write(tmp_path, "plain")
        assert library_requires(p) == {}

    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert library_requires(tmp_path / "ghost.md") == {}

    def test_non_dict_requires_returns_empty_dict_not_a_raise(self, tmp_path):
        p = self._write(tmp_path, "bad", "requires: not-a-mapping\n")
        assert library_requires(p) == {}

    def test_non_list_value_is_dropped_not_raised(self, tmp_path):
        # one malformed key among several must not sink the well-formed ones
        # (the `_read_tool_grants` posture, one door over).
        p = self._write(
            tmp_path,
            "half-bad",
            "requires:\n  skills: [gadget]\n  guardrails: not-a-list\n",
        )
        assert library_requires(p) == {"skills": ["gadget"]}

    def test_non_string_elements_are_dropped_not_raised(self, tmp_path):
        # review round 1, finding 3: an int/None/nested-list element inside an
        # otherwise well-formed list must not crash a downstream `.endswith`
        # (link_skills, _validate_library_requires) — dropped, never raised,
        # well-formed elements survive.
        p = self._write(
            tmp_path,
            "mixed",
            "requires:\n  skills: [alpha, 3, null, [nested, list], beta]\n",
        )
        assert library_requires(p) == {"skills": ["alpha", "beta"]}


class TestIterLibraryRequires:
    def test_resolves_single_entry(self, tmp_path):
        (tmp_path / "library" / "protocols").mkdir(parents=True)
        (tmp_path / "library" / "protocols" / "sprocket.md").write_text(
            "---\ntitle: Sprocket\nrequires:\n  skills: [gadget]\n---\n\n# Sprocket\n"
        )
        paths = Paths(root=tmp_path, fleet_dir=None, package=source_package())
        pairs = iter_library_requires(paths, "protocols", ["sprocket"])
        assert pairs == [("sprocket", {"skills": ["gadget"]})]

    def test_missing_entry_yields_empty_dict_not_skipped(self, tmp_path):
        paths = Paths(root=tmp_path, fleet_dir=None, package=source_package())
        assert iter_library_requires(paths, "protocols", ["ghost"]) == [("ghost", {})]

    def test_folder_expansion_resolves_members(self, tmp_path):
        d = tmp_path / "library" / "protocols" / "pack"
        d.mkdir(parents=True)
        (d / "one.md").write_text(
            "---\ntitle: One\nrequires:\n  skills: [a]\n---\n\n# One\n"
        )
        (d / "two.md").write_text("---\ntitle: Two\n---\n\n# Two\n")
        paths = Paths(root=tmp_path, fleet_dir=None, package=source_package())
        pairs = dict(iter_library_requires(paths, "protocols", ["pack/"]))
        assert pairs == {"pack/one": {"skills": ["a"]}, "pack/two": {}}


# ---------------------------------------------------------------------------
# composer.resolve_effective_skills — the effective-skill resolver
# ---------------------------------------------------------------------------


class TestResolveEffectiveSkills:
    def test_a_required_skill_joins_the_effective_set(self, fleet_dir):
        _write_protocol(fleet_dir, "needs-gadget", requires_skills=["gadget"])
        _write_skill(fleet_dir, "gadget", tool_grants=["Bash(gadget-tool *)"])
        _equip(fleet_dir, "lead", protocols=["needs-gadget"])
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        assert "gadget" not in bot.skills  # never declared, only required
        is_manager = bot.bot_id in fleet.manager_bots()
        result = resolve_effective_skills(
            bot, fleet, _paths(fleet_dir), is_manager=is_manager
        )
        assert "gadget" in result

    def test_a_declared_skill_is_not_duplicated(self, fleet_dir):
        _write_protocol(fleet_dir, "needs-gadget", requires_skills=["gadget"])
        _write_skill(fleet_dir, "gadget", tool_grants=["Bash(gadget-tool *)"])
        _write_skill(fleet_dir, "widget")
        _equip(fleet_dir, "lead", protocols=["needs-gadget"], skills=["widget", "gadget"])
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        bot = fleet.bots["lead"]
        is_manager = bot.bot_id in fleet.manager_bots()
        result = resolve_effective_skills(
            bot, fleet, _paths(fleet_dir), is_manager=is_manager
        )
        # declared entries keep their order and come first; the requirement
        # (already declared) is not appended a second time.
        assert result == ["widget", "gadget", "fleet-ops"]

    def test_opting_out_of_the_protocol_drops_its_requirement(self, tmp_path, monkeypatch):
        root = _make_minimal_root(tmp_path)
        _write_protocol(root, "needs-gadget", requires_skills=["gadget"])
        _write_skill(root, "gadget", tool_grants=["Bash(gadget-tool *)"])
        # Task 1 does not wire a real role-default protocol (that is later
        # work per spec §10's "why this matters here"); patch the registry
        # for this test only, to prove the GENERIC opt-out mechanism.
        monkeypatch.setitem(
            defaults.REGISTRY,
            "protocols",
            replace(defaults.REGISTRY["protocols"], entries=("needs-gadget",)),
        )
        paths = Paths(root=root, fleet_dir=root, package=source_package())

        fleet_on, _ = load_fleet(_write_fleet_yaml(root, protocols_default=True))
        bot_on = fleet_on.bots["worker"]
        on = resolve_effective_skills(bot_on, fleet_on, paths, is_manager=False)
        assert "gadget" in on  # baseline: the default protocol brings it

        fleet_off, _ = load_fleet(_write_fleet_yaml(root, protocols_default=False))
        bot_off = fleet_off.bots["worker"]
        off = resolve_effective_skills(bot_off, fleet_off, paths, is_manager=False)
        assert "gadget" not in off  # opted out: neither the protocol nor its requirement

    def test_declaring_the_skill_directly_survives_the_protocol_opt_out(
        self, tmp_path, monkeypatch
    ):
        root = _make_minimal_root(tmp_path)
        _write_protocol(root, "needs-gadget", requires_skills=["gadget"])
        _write_skill(root, "gadget", tool_grants=["Bash(gadget-tool *)"])
        monkeypatch.setitem(
            defaults.REGISTRY,
            "protocols",
            replace(defaults.REGISTRY["protocols"], entries=("needs-gadget",)),
        )
        paths = Paths(root=root, fleet_dir=root, package=source_package())
        fleet_path = _write_fleet_yaml(root, protocols_default=False, worker_skills=["gadget"])
        fleet, _ = load_fleet(fleet_path)
        bot = fleet.bots["worker"]
        assert "needs-gadget" not in bot.protocols  # opted out, never declared either
        result = resolve_effective_skills(bot, fleet, paths, is_manager=False)
        assert "gadget" in result  # declared directly — survives the opt-out


def _make_minimal_root(tmp_path: Path) -> Path:
    root = tmp_path / "claudlobby"
    root.mkdir()
    (root / "library" / "expertise").mkdir(parents=True)
    (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
    (root / "library" / "protocols").mkdir(parents=True)
    (root / "library" / "skills").mkdir(parents=True)
    (root / "library" / "guardrails").mkdir(parents=True)
    (root / "lib").mkdir()
    return root


def _write_fleet_yaml(
    root: Path,
    *,
    protocols_default: bool,
    worker_skills: list[str] | None = None,
) -> Path:
    skills_line = f"      skills: [{', '.join(worker_skills)}]\n" if worker_skills else ""
    fleet_path = root / "fleet.yaml"
    fleet_path.write_text(
        dedent(f"""\
        fleet:
          name: test-fleet
          manager: worker
          service_prefix: com.test
          system_defaults:
            protocols: {str(protocols_default).lower()}
          bots:
            worker:
              expertise: [eng]
        {skills_line}""")
    )
    return fleet_path


# ---------------------------------------------------------------------------
# The grant union — the cycle-1 B8 regression: linked but not granted
# ---------------------------------------------------------------------------


class TestGrantUnion:
    def test_default_fleet_ops_is_usable_without_worker_admin_grants(self, fleet_dir):
        text = (fleet_dir / "fleet.yaml").read_text().replace(
            "  accounts:\n", "  system_defaults: false\n\n  accounts:\n", 1
        )
        (fleet_dir / "fleet.yaml").write_text(text)
        (fleet_dir / "library" / "guardrails" / "no-push-main.md").write_text(
            '---\ntitle: No push to main\npermissions:\n  deny: ["Bash(git push *)"]\n---\n'
        )
        fleet, _ = load_fleet(fleet_dir / "fleet.yaml")
        paths = _paths(fleet_dir)
        for bot_id in ("lead", "worker-1"):
            bot = fleet.bots[bot_id]
            assert "fleet-ops" not in bot.skills  # no manual equipment
            compose_bot(bot, fleet, paths, log=lambda m: None)
            assert (paths.bot_runtime(bot_id) / ".claude" / "skills" / "fleet-ops").is_symlink()
            settings = json.loads(
                (paths.bot_runtime(bot_id) / ".claude" / "settings.local.json").read_text()
            )
            allow = settings["permissions"]["allow"]
            deny = settings["permissions"]["deny"]
            assert {
                "Skill(fleet-ops)",
                "Skill(fleet-ops:*)",
                "Bash(claudlobby --json brief)",
                "Bash(claudlobby --json brief *)",
                "Bash(claudlobby --json fleet inbox)",
                "Bash(claudlobby --json fleet inbox *)",
                "Bash(claudlobby --json task list)",
                "Bash(claudlobby --json task list *)",
                "Bash(claudlobby --json task admit *)",
                "Bash(claudlobby --json assignment accept *)",
                "Bash(claudlobby --json assignment progress *)",
                "Bash(claudlobby --json assignment block *)",
                "Bash(claudlobby --json assignment return *)",
                "Bash(claudlobby --json assignment complete *)",
                "Bash(claudlobby --json assignment fail *)",
                "Bash(claudlobby --json bot usage *)",
                "Bash(claudlobby --json bot automation status *)",
                "Bash(claudlobby --json bot automation pause *)",
                "Bash(claudlobby --json bot automation resume *)",
                "Bash(claudlobby --json bot automation record *)",
                "Bash(claudlobby --json fleet usage)",
                "Bash(claudlobby --json fleet usage *)",
                "Bash(claudlobby --json workstream list)",
                "Bash(claudlobby --json workstream show *)",
            } <= set(allow)
            assert "Bash(git push *)" in deny
            assert "Bash" not in allow
            assert not any("systemctl" in grant or "launchctl" in grant for grant in allow)
            lifecycle = {grant for grant in allow
                         if grant.startswith("Bash(claudlobby --json bot ")
                         and any(f" bot {verb} " in grant for verb in ("start", "stop", "restart"))}
            assert {f"Bash(claudlobby bot {verb} --help)"
                    for verb in ("start", "stop", "restart")} <= set(allow)
            if bot_id == "lead":
                assert lifecycle == {
                    "Bash(claudlobby --json bot restart lead)",
                    "Bash(claudlobby --json bot start worker-1)",
                    "Bash(claudlobby --json bot stop worker-1)",
                    "Bash(claudlobby --json bot restart worker-1)",
                    "Bash(claudlobby --json bot restart worker-1 --ceiling *)",
                }
            else:
                assert lifecycle == {"Bash(claudlobby --json bot restart worker-1)"}
            assert not [
                f for f in audit_bot(bot, fleet, paths)
                if f.kind in {"orphan_grant", "under_grant"}
            ], bot_id
            manager_grants = {
                "Bash(claudlobby --json task assign *)",
                "Bash(claudlobby --json assignment deliver *)",
                "Bash(claudlobby --json task withdraw *)",
                "Bash(claudlobby --json task reassign *)",
                "Bash(claudlobby --json task escalate *)",
                "Bash(claudlobby --json task nudge *)",
                "Bash(claudlobby --json workstream open *)",
                "Bash(claudlobby --json workstream progress *)",
                "Bash(claudlobby --json workstream renew *)",
                "Bash(claudlobby --json workstream block *)",
                "Bash(claudlobby --json workstream unblock *)",
                "Bash(claudlobby --json workstream close *)",
                "Bash(claudlobby --json workstream prune *)",
            }
            if bot_id == "lead":
                assert manager_grants <= set(allow)
            else:
                assert manager_grants.isdisjoint(allow)

    def test_a_required_skill_is_symlinked(self, fleet_dir):
        _write_protocol(fleet_dir, "needs-gadget", requires_skills=["gadget"])
        _write_skill(fleet_dir, "gadget", tool_grants=["Bash(gadget-tool *)"])
        _equip(fleet_dir, "lead", protocols=["needs-gadget"])
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _paths(fleet_dir)
        bot = fleet.bots["lead"]
        compose_bot(bot, fleet, paths, log=lambda m: None)
        link = paths.bot_runtime("lead") / ".claude" / "skills" / "gadget"
        assert link.is_symlink()

    def test_a_required_skill_is_GRANTED_not_only_linked(self, fleet_dir):
        # cycle-1 B8: settings compose BEFORE link_skills, so linking alone
        # passes the symlink test above and fails this one if the two
        # resolvers (_resolve_skill_permissions / _resolve_skill_grants)
        # still iterate bot.skills instead of the effective set.
        _write_protocol(fleet_dir, "needs-gadget", requires_skills=["gadget"])
        _write_skill(fleet_dir, "gadget", tool_grants=["Bash(gadget-tool *)"])
        _equip(fleet_dir, "lead", protocols=["needs-gadget"])
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _paths(fleet_dir)
        bot = fleet.bots["lead"]
        compose_bot(bot, fleet, paths, log=lambda m: None)
        settings = json.loads(
            (paths.bot_runtime("lead") / ".claude" / "settings.local.json").read_text()
        )
        allow = settings["permissions"]["allow"]
        assert "Skill(gadget)" in allow
        assert "Skill(gadget:*)" in allow
        assert "Bash(gadget-tool *)" in allow  # the skill's own tool_grants entry

    def test_an_opted_out_protocols_skill_is_neither_linked_nor_granted(
        self, fleet_dir, monkeypatch
    ):
        # review round 1, finding 2: the opt-out's POSITIVE direction (the
        # skill is absent from resolve_effective_skills) is pinned at the
        # resolver by TestResolveEffectiveSkills.test_opting_out_of_the_
        # protocol_drops_its_requirement; the OVER-GRANT direction — that an
        # opted-out requirement never reaches the composed artifact — was not.
        # This proves it at compose_bot's actual output: neither symlinked nor
        # granted.
        _write_protocol(fleet_dir, "needs-gadget", requires_skills=["gadget"])
        _write_skill(fleet_dir, "gadget", tool_grants=["Bash(gadget-tool *)"])
        # Task 1 does not wire a real role-default protocol (later work, spec
        # §10); patch the registry for this test only, to prove the GENERIC
        # opt-out mechanism reaches the composed artifact, not just the
        # resolver's return value.
        monkeypatch.setitem(
            defaults.REGISTRY,
            "protocols",
            replace(defaults.REGISTRY["protocols"], entries=("needs-gadget",)),
        )
        text = (fleet_dir / "fleet.yaml").read_text().replace(
            "  accounts:\n",
            "  system_defaults:\n    protocols: false\n\n  accounts:\n",
            1,
        )
        (fleet_dir / "fleet.yaml").write_text(text)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _paths(fleet_dir)
        bot = fleet.bots["lead"]
        assert "needs-gadget" not in bot.protocols  # opted out, never declared either

        compose_bot(bot, fleet, paths, log=lambda m: None)
        settings = json.loads(
            (paths.bot_runtime("lead") / ".claude" / "settings.local.json").read_text()
        )
        allow = settings["permissions"]["allow"]
        assert "Skill(gadget)" not in allow
        assert "Skill(gadget:*)" not in allow
        assert "Bash(gadget-tool *)" not in allow
        assert not (
            paths.bot_runtime("lead") / ".claude" / "skills" / "gadget"
        ).exists()


# ---------------------------------------------------------------------------
# The unresolvable-requirement error — one defect, one error, library-wide
# ---------------------------------------------------------------------------


def test_an_unresolvable_requirement_is_one_library_wide_error(fleet_dir):
    _write_protocol(fleet_dir, "broken-protocol", requires_skills=["nonexistent-skill"])
    # three bots equip the broken protocol
    _equip(fleet_dir, "lead", protocols=["broken-protocol"])
    _equip(fleet_dir, "worker-1", protocols=["broken-protocol"])
    text = (fleet_dir / "fleet.yaml").read_text()
    text += (
        "    worker-2:\n"
        "      expertise: [software-engineering]\n"
        "      protocols: [broken-protocol]\n"
        "      telegram:\n"
        "        handle: worker2_bot\n"
        "        token_env: TELEGRAM_TOKEN_WORKER2\n"
    )
    (fleet_dir / "fleet.yaml").write_text(text)
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    paths = _paths(fleet_dir)

    report = validate(fleet, paths)

    matches = [e for e in report.errors if "broken-protocol" in e]
    assert len(matches) == 1, matches  # one defect (the file), one error
    assert "nonexistent-skill" in matches[0]


def test_an_unresolvable_requirement_errors_even_when_no_bot_equips_it(fleet_dir):
    # review round 1, finding 4a: the rule is library-wide, so the defect must
    # still be caught when nothing equips the protocol — the exact case a
    # per-bot check would silently pass.
    _write_protocol(fleet_dir, "unequipped-broken", requires_skills=["nonexistent-skill"])
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    paths = _paths(fleet_dir)

    report = validate(fleet, paths)

    matches = [e for e in report.errors if "unequipped-broken" in e]
    assert len(matches) == 1, matches
    assert "nonexistent-skill" in matches[0]


def test_a_non_string_requires_entry_is_reported_once_per_file(fleet_dir):
    # review round 1, finding 3: a dropped non-string element must not be
    # silent — one error per protocol FILE, same "one defect, one error" rule.
    (fleet_dir / "library" / "protocols" / "mixed-protocol.md").write_text(
        "---\ntitle: mixed-protocol\nrequires:\n  skills: [3]\n---\n\n"
        "# mixed-protocol\n\nBody.\n"
    )
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    paths = _paths(fleet_dir)

    report = validate(fleet, paths)

    matches = [e for e in report.errors if "mixed-protocol" in e]
    assert len(matches) == 1, matches
    assert "non-string" in matches[0]


def test_two_protocols_requiring_one_skill_yield_it_once(fleet_dir):
    # review round 1, finding 4b: cross-protocol dedup, not just within one
    # protocol's own list or against a declared skill.
    _write_protocol(fleet_dir, "needs-gadget-a", requires_skills=["gadget"])
    _write_protocol(fleet_dir, "needs-gadget-b", requires_skills=["gadget"])
    _write_skill(fleet_dir, "gadget", tool_grants=["Bash(gadget-tool *)"])
    _equip(fleet_dir, "lead", protocols=["needs-gadget-a", "needs-gadget-b"])
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    paths = _paths(fleet_dir)
    bot = fleet.bots["lead"]
    is_manager = bot.bot_id in fleet.manager_bots()

    result = resolve_effective_skills(bot, fleet, paths, is_manager=is_manager)

    assert result.count("gadget") == 1


# ---------------------------------------------------------------------------
# The plane registry keyframe — #1405: effective, not declared
# ---------------------------------------------------------------------------


def test_the_registry_keyframe_records_the_effective_skills(fleet_dir):
    _write_protocol(fleet_dir, "needs-gadget", requires_skills=["gadget"])
    _write_skill(fleet_dir, "gadget", tool_grants=["Bash(gadget-tool *)"])
    _equip(fleet_dir, "lead", protocols=["needs-gadget"])
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    paths = _paths(fleet_dir)
    bot = fleet.bots["lead"]
    assert "gadget" not in bot.skills  # DECLARED list carries nothing

    eq = bot_payload(paths, fleet, bot, None)["equipment"]
    assert "gadget" in eq["skills"]  # EFFECTIVE list carries the requirement


# ---------------------------------------------------------------------------
# freshbox — a required skill's grants must trace, never read as orphaned
# ---------------------------------------------------------------------------


def test_freshbox_traces_a_required_skills_grants(fleet_dir):
    _write_protocol(fleet_dir, "needs-gadget", requires_skills=["gadget"])
    _write_skill(fleet_dir, "gadget", tool_grants=["Bash(gadget-tool *)"])
    _equip(fleet_dir, "lead", protocols=["needs-gadget"])
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    paths = _paths(fleet_dir)
    bot = fleet.bots["lead"]

    findings = audit_bot(bot, fleet, paths)
    orphans = [f for f in findings if f.kind == "orphan_grant"]
    assert not any("gadget-tool" in f.detail for f in orphans), orphans
    assert not any("Skill(gadget" in f.detail for f in orphans), orphans


# ---------------------------------------------------------------------------
# Existing declared skills keep their order beside the universal default
# ---------------------------------------------------------------------------


def test_a_fleet_with_no_requires_composes_exactly_the_declared_grants(fleet_dir):
    """Direct counter to a self-cancelling comparison (review round 1, finding
    1): a prior version of this test composed the SAME (post-change) code path
    twice, differing only in whether resolve_effective_skills was the real one
    or a stand-in returning the identical list on a fixture with no
    `requires:` — the two arms could never disagree, so the test could not
    catch a regression anywhere else in compose_settings_local's layering.
    This pins the FULL composed permissions.allow LITERALLY instead: no
    protocol in scope declares `requires:` (report-back, the only one this
    fleet composes by default, does not), so every entry below must trace to
    the two declared skills or the universal fleet-ops skill and its role
    grants. `channels: []` suppresses the default Telegram plugin grants."""
    _write_skill(fleet_dir, "gadget", tool_grants=["Bash(gadget-tool *)"])
    _write_skill(fleet_dir, "widget", tool_grants=["Bash(widget-tool *)"])
    _equip(fleet_dir, "lead", skills=["gadget", "widget"], channels=[])
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    paths = _paths(fleet_dir)
    bot = fleet.bots["lead"]

    compose_bot(bot, fleet, paths, log=lambda m: None)
    settings = json.loads(
        (paths.bot_runtime("lead") / ".claude" / "settings.local.json").read_text()
    )
    assert settings["permissions"]["allow"] == [
        "Glob",
        "Grep",
        "Read",
        "Skill(gadget)",
        "Skill(gadget:*)",
        "Skill(widget)",
        "Skill(widget:*)",
        "Skill(fleet-ops)",
        "Skill(fleet-ops:*)",
        "Bash(gadget-tool *)",
        "Bash(widget-tool *)",
        "Bash(claudlobby --help)",
        "Bash(claudlobby brief --help)",
        "Bash(claudlobby task list --help)",
        "Bash(claudlobby task show --help)",
        "Bash(claudlobby task reviews --help)",
        "Bash(claudlobby task admit --help)",
        "Bash(claudlobby task assign --help)",
        "Bash(claudlobby task withdraw --help)",
        "Bash(claudlobby task reassign --help)",
        "Bash(claudlobby task escalate --help)",
        "Bash(claudlobby task nudge --help)",
        "Bash(claudlobby workstream --help)",
        "Bash(claudlobby bot usage --help)",
        "Bash(claudlobby bot start --help)",
        "Bash(claudlobby bot stop --help)",
        "Bash(claudlobby bot restart --help)",
        "Bash(claudlobby bot automation --help)",
        "Bash(claudlobby fleet usage --help)",
        "Bash(claudlobby assignment show --help)",
        "Bash(claudlobby assignment accept --help)",
        "Bash(claudlobby assignment deliver --help)",
        "Bash(claudlobby assignment progress --help)",
        "Bash(claudlobby assignment block --help)",
        "Bash(claudlobby assignment return --help)",
        "Bash(claudlobby assignment complete --help)",
        "Bash(claudlobby assignment fail --help)",
        "Bash(claudlobby message show --help)",
        "Bash(claudlobby message receipt --help)",
        "Bash(claudlobby message wait --help)",
        "Bash(claudlobby message send --help)",
        "Bash(claudlobby message reply --help)",
        "Bash(claudlobby fleet reports submit --help)",
        "Bash(claudlobby fleet reports list --help)",
        "Bash(claudlobby fleet reports ack --help)",
        "Bash(claudlobby fleet inbox --help)",
        "Bash(claudlobby request show --help)",
        "Bash(claudlobby --json context show)",
        "Bash(claudlobby --json brief)",
        "Bash(claudlobby --json brief *)",
        "Bash(claudlobby --json task list)",
        "Bash(claudlobby --json task list *)",
        "Bash(claudlobby --json task show *)",
        "Bash(claudlobby --json task reviews *)",
        "Bash(claudlobby --json assignment show *)",
        "Bash(claudlobby --json message show *)",
        "Bash(claudlobby --json message receipt *)",
        "Bash(claudlobby --json message wait *)",
        "Bash(claudlobby --json message send *)",
        "Bash(claudlobby --json message reply *)",
        "Bash(claudlobby --json fleet reports submit *)",
        "Bash(claudlobby --json fleet reports list)",
        "Bash(claudlobby --json fleet reports list *)",
        "Bash(claudlobby --json fleet reports ack *)",
        "Bash(claudlobby --json fleet inbox)",
        "Bash(claudlobby --json fleet inbox *)",
        "Bash(claudlobby --json request show *)",
        "Bash(claudlobby --json workstream list)",
        "Bash(claudlobby --json workstream show *)",
        "Bash(claudlobby --json bot usage *)",
        "Bash(claudlobby --json bot automation status *)",
        "Bash(claudlobby --json bot automation pause *)",
        "Bash(claudlobby --json bot automation resume *)",
        "Bash(claudlobby --json bot automation record *)",
        "Bash(claudlobby --json fleet usage)",
        "Bash(claudlobby --json fleet usage *)",
        "Bash(claudlobby --json task admit *)",
        "Bash(claudlobby --json assignment accept *)",
        "Bash(claudlobby --json assignment progress *)",
        "Bash(claudlobby --json assignment block *)",
        "Bash(claudlobby --json assignment return *)",
        "Bash(claudlobby --json assignment complete *)",
        "Bash(claudlobby --json assignment fail *)",
        "Bash(claudlobby --json task assign *)",
        "Bash(claudlobby --json assignment deliver *)",
        "Bash(claudlobby --json task withdraw *)",
        "Bash(claudlobby --json task reassign *)",
        "Bash(claudlobby --json task escalate *)",
        "Bash(claudlobby --json task nudge *)",
        "Bash(claudlobby --json workstream open *)",
        "Bash(claudlobby --json workstream progress *)",
        "Bash(claudlobby --json workstream renew *)",
        "Bash(claudlobby --json workstream block *)",
        "Bash(claudlobby --json workstream unblock *)",
        "Bash(claudlobby --json workstream close *)",
        "Bash(claudlobby --json workstream prune *)",
        "Bash(claudlobby --json bot start worker-1)",
        "Bash(claudlobby --json bot stop worker-1)",
        "Bash(claudlobby --json bot restart worker-1)",
        "Bash(claudlobby --json bot restart worker-1 --ceiling *)",
        "Bash(claudlobby --json bot restart lead)",
    ]
    linked = sorted(
        p.name for p in (paths.bot_runtime("lead") / ".claude" / "skills").iterdir()
    )
    assert linked == ["fleet-ops", "gadget", "widget"]


def test_effective_skills_adds_only_the_universal_default_without_protocol_requires(
    fleet_dir,
):
    """Without protocol requirements, only fleet-ops joins declared skills."""
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    paths = _paths(fleet_dir)
    for bot_id in ("lead", "worker-1"):
        bot = fleet.bots[bot_id]
        is_manager = bot.bot_id in fleet.manager_bots()
        assert resolve_effective_skills(bot, fleet, paths, is_manager=is_manager) == [
            *bot.skills, "fleet-ops"
        ]


def test_a_briefing_stanza_equips_the_skill_its_timers_fire(fleet_dir):
    """#1819: the briefing: stanza composes timers that send /briefing into the
    bot's own session, and presence of the stanza equips the bot -- so the
    skill those timers invoke comes with it. A stanza alone used to compose the
    timers and no skill, and Claude Code rejected every /briefing locally while
    the send read as delivered."""
    _write_skill(fleet_dir, "briefing")
    text = (fleet_dir / "fleet.yaml").read_text()
    marker = "    lead:\n"
    assert text.count(marker) == 1
    stanza = "      briefing:\n        slots:\n          morning: '*-*-* 08:30:00'\n"
    (fleet_dir / "fleet.yaml").write_text(text.replace(marker, marker + stanza))
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    paths = _paths(fleet_dir)

    compose_bot(fleet.bots["lead"], fleet, paths, log=lambda m: None)
    assert (paths.bot_runtime("lead") / ".claude" / "skills" / "briefing").is_dir()


# ---------------------------------------------------------------------------
# link_skills — keyword-only, required `skills`
# ---------------------------------------------------------------------------


def test_link_skills_requires_the_skills_kwarg(fleet_dir):
    """`skills` is keyword-only and REQUIRED — never defaulted to bot.skills,
    which is exactly how the two lists would silently diverge again."""
    import inspect

    sig = inspect.signature(link_skills)
    param = sig.parameters["skills"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


def test_link_skills_links_the_list_it_is_given_not_bot_skills(fleet_dir):
    _write_skill(fleet_dir, "gadget")
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    paths = _paths(fleet_dir)
    bot = fleet.bots["lead"]
    assert not bot.skills  # nothing declared
    link_skills(bot, paths, lambda m: None, skills=["gadget"])
    link = paths.bot_runtime("lead") / ".claude" / "skills" / "gadget"
    assert link.is_symlink()


# ---------------------------------------------------------------------------
# _resolve_skill_permissions / _resolve_skill_grants — now list[str]-shaped
# ---------------------------------------------------------------------------


def test_resolve_skill_permissions_takes_a_plain_list(fleet_dir):
    assert _resolve_skill_permissions(["gadget"]) == ["Skill(gadget)", "Skill(gadget:*)"]


def test_resolve_skill_grants_takes_a_plain_list(fleet_dir):
    _write_skill(fleet_dir, "gadget", tool_grants=["Bash(gadget-tool *)"])
    paths = _paths(fleet_dir)
    assert _resolve_skill_grants(["gadget"], paths) == ["Bash(gadget-tool *)"]
