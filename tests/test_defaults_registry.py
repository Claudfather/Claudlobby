"""#1168 Phase 1 — the junk-drawer guard, live before there is a junk drawer.

Twelve default lists become a junk drawer unless "empty, and here is why" is a
first-class answer. The registry makes that answer DATA, and this file is what
makes it enforced: a thirteenth entity type added to `library/` without a
disposition fails here rather than being silently absent.

The other half is F4's binding refinement. A registry that merely DESCRIBED the
constants would be two sources of one fact — they drift, and the test passes
against whichever copy is not the one that composes. So these assert over the
value `config` actually exposes, by IDENTITY, and derive the entity-type list
from the filesystem rather than restating it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from claudlobby import config, defaults
from claudlobby.composer import compose_bot, compose_claude_md
from claudlobby.config import load_fleet
from claudlobby.defaults import REGISTRY, TIER_TESTS, Disposition, Tier, resolve
from claudlobby.paths import Paths
from tests.conftest import install_real_template

REPO_ROOT = Path(__file__).resolve().parent.parent
LIBRARY = REPO_ROOT / "library"

#: INSTRUCT entries that are genuinely NEW — not already composing when they
#: were registered — together with where their observation-gate delta was
#: recorded. Every line here changes what some bot is told, so a line is the
#: deliberate act, never a side effect of adding an entry to the registry.
NEW_INSTRUCT_DEFAULTS_WITH_GATE_EVIDENCE = {
    "shared-documentation-vault": (
        "#1172 — replaces the hand-scan form on vault-wired bots only. "
        "Vault arm delta named in the PR body; non-vault arm measured "
        "byte-identical."
    ),
    "checkin": (
        "manager-checkin PR 4 chunk 4 — the leaf-manager role overlay on "
        "`protocols` (defaults.REGISTRY['protocols'].roles). The naked-bot "
        "gate's leaf-manager arm is Task 4 of PR 4; that task records the "
        "dated baseline under documentation/baselines naming this entry's "
        "delta. A single bot can never be a leaf manager, so the gate's "
        "existing (rootless) naked-probe shape is unaffected — the arm that "
        "actually exercises this entry is the new one Task 4 adds."
    ),
}


def _library_entity_types() -> set[str]:
    """Entity types as they actually exist on disk.

    Read rather than restated: a hardcoded list here would be a third source of
    the same fact, and would keep passing on the day someone adds a directory.
    """
    return {
        d.name for d in LIBRARY.iterdir() if d.is_dir() and not d.name.startswith(".")
    }


class TestCompleteness:
    """Every entity type has an explicit disposition — the guard itself."""

    def test_registry_covers_exactly_the_library_entity_types(self):
        on_disk = _library_entity_types()
        registered = set(REGISTRY)
        assert registered == on_disk, (
            "registry and library/ disagree — a new entity type needs an explicit "
            f"disposition before it composes.\n  only on disk: {sorted(on_disk - registered)}"
            f"\n  only in registry: {sorted(registered - on_disk)}"
        )

    def test_every_disposition_states_a_reason(self):
        # An empty default is a valid answer; an unexplained one is not. This is
        # the difference between "nothing clears the bar" and "nobody looked",
        # which is precisely what makes twelve lists a junk drawer.
        missing = [t for t, d in REGISTRY.items() if not d.reason.strip()]
        assert not missing, f"no reason given for: {missing}"

    def test_every_tier_has_a_membership_test(self):
        assert set(TIER_TESTS) == set(Tier), (
            "a tier without a written bar cannot be argued against"
        )
        assert all(v.strip() for v in TIER_TESTS.values())

    def test_unsettled_types_are_visible_as_the_phase_2_worklist(self):
        # Not a failure — an inventory. Phase 2 exists to work through these,
        # and they must be enumerable rather than discovered one at a time.
        unsettled = sorted(t for t, d in REGISTRY.items() if not d.settled)
        settled = sorted(t for t, d in REGISTRY.items() if d.settled)
        assert settled == ["guardrails", "protocols"], (
            "the settled set moved. Every entry here changes what a bot composes "
            "on the default path, so it lands by editing this list deliberately — "
            f"never by a default arriving unannounced: {settled}"
        )
        assert len(unsettled) == len(REGISTRY) - len(settled)


class TestRegistryIsTheSource:
    """F4, binding: the registry is where the constant lives, not a mirror."""

    def test_config_reexports_the_registry_value_by_identity(self):
        # IDENTITY, not equality. Equality would still pass if someone
        # re-declared `DEFAULT_GUARDRAILS = ["claudlobby-dev-in-projects"]` in
        # config.py — two sources holding the same value today and drifting
        # tomorrow, which is the exact failure this refinement exists to stop.
        assert config.DEFAULT_GUARDRAILS is defaults.DEFAULT_GUARDRAILS, (
            "config.DEFAULT_GUARDRAILS is no longer the registry's value — it has "
            "been re-declared. The registry must be the single source."
        )

    def test_the_derived_constant_matches_what_the_registry_resolves(self):
        assert defaults.DEFAULT_GUARDRAILS == resolve("guardrails")

    def test_the_composer_consumes_the_registry_value(self, tmp_path):
        # Through `load_fleet`, the real door — NOT `_coerce_bot`. The default is
        # injected into merged_defaults at the fleet level before bots are
        # coerced, so calling the inner function proves nothing about what a bot
        # actually receives. A naked fleet in miniature: declares nothing.
        import yaml

        from claudlobby.config import load_fleet

        f = tmp_path / "fleet.yaml"
        f.write_text(
            yaml.safe_dump(
                {"fleet": {"name": "probe", "bots": {"b": {"expertise": ["eng"]}}}}
            )
        )
        fleet, _ = load_fleet(f)
        got = fleet.bots["b"].guardrails
        for entry in resolve("guardrails"):
            assert entry in got, (
                f"{entry} is in the registry but did not reach a bot in a fleet "
                f"that declared nothing — got {got}"
            )


class TestRoleOverlay:
    """F2, generalised: keyed on role, shipping with one role populated."""

    def test_global_entries_apply_with_no_role(self):
        assert resolve("guardrails") == ["claudlobby-dev-in-projects"]

    def test_a_role_overlay_is_unioned_on_top_of_the_global(self):
        d = Disposition(
            tier=Tier.RESTRICT, reason="t", entries=("g",), roles={"manager": ("m",)}
        )
        REGISTRY["_probe"] = d
        try:
            assert resolve("_probe") == ["g"]
            assert resolve("_probe", ("manager",)) == ["g", "m"]
            assert resolve("_probe", ("nobody",)) == ["g"]
        finally:
            del REGISTRY["_probe"]

    def test_an_overlay_never_duplicates_a_global_entry(self):
        d = Disposition(
            tier=Tier.WIRE, reason="t", entries=("x",), roles={"manager": ("x",)}
        )
        REGISTRY["_probe"] = d
        try:
            assert resolve("_probe", ("manager",)) == ["x"]
        finally:
            del REGISTRY["_probe"]

    def test_only_detectable_roles_are_declared(self):
        # The stated bound. A role named here that nothing can DETECT would be
        # silently inert — never unioned in, and nothing would say so. Today
        # `manager` (manager_bots()) and `leaf-manager` (leaf_manager_bots())
        # are the only roles the composer can resolve; adding a third needs a
        # predicate first.
        assert defaults.DETECTABLE_ROLES == frozenset(
            {defaults.ROLE_MANAGER, defaults.ROLE_LEAF_MANAGER}
        )
        declared = {r for d in REGISTRY.values() for r in d.roles}
        assert declared <= defaults.DETECTABLE_ROLES, (
            f"role(s) declared that nothing can detect: {sorted(declared - defaults.DETECTABLE_ROLES)}"
        )


class TestScopeBoundary:
    """What is deliberately NOT in this registry."""

    @pytest.mark.parametrize("name", ["marketplaces", "plugins"])
    def test_host_level_constants_stay_out(self, name):
        # DEFAULT_MARKETPLACES / DEFAULT_PLUGINS are real and stay in config.py:
        # they are not library entity types, do not merge per-bot, and answer
        # "what does the host install" rather than "what does this bot compose".
        # Folding them in would make the completeness test assert over a set
        # that is not a set.
        assert name not in REGISTRY
        assert hasattr(config, f"DEFAULT_{name.upper()}")

    def test_every_instruct_default_was_already_composing_when_registered(self):
        # The Phase 3 naked-bot gate exists (#1171), so INSTRUCT entries are now
        # admissible — but only the one shape that cannot change a bot silently:
        # a default that was ALREADY composing, registered to make it visible and
        # disableable, with a byte-identical default path.
        #
        # A genuinely NEW instruction — one no bot receives today — changes what
        # every bot on the estate is told, and no unit test can evidence that. It
        # needs the observation gate plus a named delta in the PR body, so it
        # lands by editing this test with that evidence, never by inheriting the
        # permission this one earned.
        # PER ENTRY, never per type. A type-level exemption would let a second
        # entry ride in beside a grandfathered one — `entries=("shared-
        # documentation", "something-new")` — adding a genuinely new estate-wide
        # instruction while tripping nothing here.
        #
        # A NEW instruction lands by adding a line to
        # NEW_INSTRUCT_DEFAULTS_WITH_GATE_EVIDENCE, which is the deliberate act
        # this test exists to force. It is not an escape hatch: the entry still
        # has to carry a named observation-gate delta, and the line records
        # where. `shared-documentation-vault` is the worked example.
        #
        # WALKS `d.roles.values()` TOO, not just `d.entries` (PR4 chunk 4). A
        # role-scoped INSTRUCT default is not a global one, but it is still an
        # INSTRUCT default: a bot that holds the role receives the instruction
        # exactly as it would from `entries`, and `resolve()` unions the two
        # without distinction. Checking only `entries` is how a role-scoped
        # `checkin` could have landed here already passing, with nothing
        # short of `lib/naked-bot-observe.py` (which this file does not run)
        # ever having looked.
        allowed = set(NEW_INSTRUCT_DEFAULTS_WITH_GATE_EVIDENCE)
        unexplained = {
            t: tuple(
                e
                for e in (*d.entries, *(n for names in d.roles.values() for n in names))
                if e not in (set(d.grandfathered) | allowed)
            )
            for t, d in REGISTRY.items()
            if d.tier is Tier.INSTRUCT
        }
        ungrandfathered = {t: e for t, e in unexplained.items() if e}
        assert not ungrandfathered, (
            "a NEW INSTRUCT default is present (global or role-scoped). It "
            "cannot be justified by unit test: run lib/naked-bot-observe.py "
            f"--baseline and name the delta. {ungrandfathered}"
        )

    def test_every_new_instruct_allowance_names_a_live_entry_and_its_evidence(self):
        # The allowlist's hazard runs the OPPOSITE way to `grandfathered`'s. A
        # stale name there merely exempts nothing; a stale name HERE
        # pre-authorises a future entry that happens to reuse it — a new
        # estate-wide instruction landing green, through the door built to make
        # that impossible.
        #
        # "live" now means live in `entries` OR in some `roles` tuple (PR4
        # chunk 4): a role-scoped default is exactly as live as a global one,
        # just narrower in WHO receives it, and the allowlist's job is to name
        # a real entry, not to insist it be an unconditional one.
        entries = {
            e
            for d in REGISTRY.values()
            if d.tier is Tier.INSTRUCT
            for e in (*d.entries, *(n for names in d.roles.values() for n in names))
        }
        for name, evidence in NEW_INSTRUCT_DEFAULTS_WITH_GATE_EVIDENCE.items():
            assert name in entries, (
                f"{name} is allowed as a new INSTRUCT default but is not a live "
                "INSTRUCT entry (global or role-scoped) — remove it, or it "
                "silently pre-authorises the next entry to reuse the name"
            )
            assert evidence.strip(), f"{name}: allowed with no evidence recorded"

    def test_a_grandfathered_name_is_an_entry_that_is_settled_and_argued(self):
        # `grandfathered` is a claim about history, so it must not be reachable
        # as a way to skip the argument: the reason still has to be written.
        # And a name here that is NOT in `entries` would exempt nothing while
        # reading as though it did — the quiet direction.
        for t, d in REGISTRY.items():
            if not d.grandfathered:
                continue
            assert d.settled, f"{t}: grandfathered but not settled"
            assert d.reason.strip() != defaults._UNARGUED, (
                f"{t}: grandfathered but still carries the placeholder reason"
            )
            unknown = set(d.grandfathered) - set(d.entries)
            assert not unknown, (
                f"{t}: grandfathered names that are not entries: {sorted(unknown)}"
            )

    def test_every_availability_gate_names_a_real_entry_and_is_callable(self):
        # A gate keyed to an entry that no longer exists would silently stop
        # gating — the quiet direction, so it is asserted. The gate VALUE needs
        # no such guard: it is a predicate, so a bad `Paths` attribute raises at
        # compose time instead of reading falsy and suppressing the default on
        # every bot, which is why it is a predicate rather than an attribute name.
        registered = {e for d in REGISTRY.values() for e in d.entries}
        for entry, gate in defaults.AVAILABILITY_GATES.items():
            assert entry in registered, f"gate for unregistered entry: {entry}"
            assert callable(gate), f"gate for {entry} is not callable"

    def test_an_ungated_entry_is_available_and_a_gated_one_consults_its_gate(self):
        # The no-gate rule is the one most callers depend on and the one most
        # easily inverted, so it gets a positive AND a negative case.
        no_docs = defaults.Facts(shared_docs=False, vault_wired=False)
        raw_tree = defaults.Facts(shared_docs=True, vault_wired=False)

        assert defaults.available("not-gated-by-anything", no_docs) is True
        assert defaults.available("shared-documentation", no_docs) is False
        assert defaults.available("shared-documentation", raw_tree) is True

    @pytest.mark.parametrize("shared_docs", [True, False])
    @pytest.mark.parametrize("vault_wired", [True, False])
    def test_the_two_shared_doc_forms_are_mutually_exclusive(
        self, shared_docs, vault_wired
    ):
        # THE #1172 INVARIANT, over every combination of facts rather than the
        # two shapes that happen to exist on this estate.
        #
        # Both composing is the original defect re-created by its own fix: the
        # bot is told to hand-scan a tree and never to open it. Neither
        # composing where a shared-docs tree EXISTS silently drops the protocol
        # estate-wide — the quiet direction, and the one no bot would report.
        facts = defaults.Facts(shared_docs=shared_docs, vault_wired=vault_wired)
        on = [
            e
            for e in ("shared-documentation", "shared-documentation-vault")
            if defaults.available(e, facts)
        ]
        expected = 1 if shared_docs else 0
        assert len(on) == expected, (
            f"shared_docs={shared_docs} vault_wired={vault_wired} composes {on}; "
            f"expected exactly {expected}"
        )

    def test_every_registered_entry_resolves_to_a_library_file(self):
        # Asserted for ALL twelve types, not just the one this phase touched: a
        # registry entry naming a file that does not exist fails at compose time
        # on a real fleet, and only for the fleets that did not opt out.
        #
        # ALSO resolved with EVERY DETECTABLE ROLE passed at once (PR4 chunk
        # 4) — `resolve(etype)` alone never asks for a role overlay, so a
        # role-scoped entry naming a missing file would resolve clean here
        # while failing on the one real fleet shape that carries the role.
        # Passing every detectable role together is deliberate, not a
        # shortcut: `resolve()` unions whatever roles it is given, so the
        # per-entity-type union of "no role" and "every role" is exactly the
        # set of names any real bot could ever receive from this registry.
        all_roles = tuple(defaults.DETECTABLE_ROLES)
        missing = [
            f"{etype}/{entry}"
            for etype in REGISTRY
            for entry in set(resolve(etype)) | set(resolve(etype, all_roles))
            if not (LIBRARY / etype / f"{entry}.md").is_file()
        ]
        assert not missing, f"registered but absent from library/: {missing}"


# ---------------------------------------------------------------------------
# The `checkin` leaf-manager default, end to end (PR4 chunk 4, #1569 task 3).
# `fleet_dir` (conftest.py) ships lead/worker-1 with lead managing eng —
# lead is already a leaf manager by that shape alone (test_leaf_manager_role
# precedent). A `coord` bot is added, managing a team of one (lead), so ONE
# fleet carries all three roles at once: coord is a coordinator (its only
# report, lead, is itself a manager — not leaf), lead stays leaf (it still
# manages eng/worker-1), worker-1 is a plain worker (never a manager). None of
# the three DECLARES `protocols: [checkin]` — every assertion here is about
# what the DEFAULT does, not a hand-equipped bot (that is
# tests/test_checkin_library.py's job).
# ---------------------------------------------------------------------------


def _write_checkin_library(fleet_dir: Path) -> None:
    """Copy the REAL checkin protocol + skill into the fixture's library, so
    the default reaches through to genuine content rather than a stand-in —
    tests/test_checkin_library.py's precedent."""
    shutil.copy(
        LIBRARY / "protocols" / "checkin.md",
        fleet_dir / "library" / "protocols" / "checkin.md",
    )
    dst = fleet_dir / "library" / "skills" / "checkin"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(LIBRARY / "skills" / "checkin", dst)


def _add_coordinator(fleet_dir: Path) -> None:
    """Add `coord`, managing a team of one (lead) — the same text-surgery
    convention as test_leaf_manager_role.py's non-leaf validator fixture,
    minus the explicit `protocols: [checkin]` declaration that test exists to
    warn about: here nothing is declared, only defaulted."""
    text = (fleet_dir / "fleet.yaml").read_text()
    text = text.replace(
        "  teams:\n    eng:\n      manager: lead\n      workers: [worker-1]\n",
        "  teams:\n    eng:\n      manager: lead\n      workers: [worker-1]\n"
        "    top:\n      manager: coord\n      workers: [lead]\n",
    )
    text = text.replace(
        "  bots:\n    lead:\n",
        "  bots:\n    coord:\n      expertise: [orchestration]\n    lead:\n",
    )
    (fleet_dir / "fleet.yaml").write_text(text)


def _paths(fleet_dir: Path) -> Paths:
    return Paths(root=fleet_dir, fleet_dir=fleet_dir)


class TestLeafManagerCheckinDefault:
    def test_the_leaf_manager_overlay_composes_the_checkin_protocol(
        self, fleet_dir, monkeypatch
    ):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        install_real_template(fleet_dir)
        _write_checkin_library(fleet_dir)
        _add_coordinator(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        assert fleet.leaf_manager_bots() == {"lead"}  # coord is a coordinator
        paths = _paths(fleet_dir)

        lead_md = compose_claude_md(fleet.bots["lead"], fleet, paths)
        coord_md = compose_claude_md(fleet.bots["coord"], fleet, paths)
        worker_md = compose_claude_md(fleet.bots["worker-1"], fleet, paths)

        # None of the three DECLARED protocols: [checkin] — this is the
        # default reaching lead alone.
        for bot_id in ("lead", "coord", "worker-1"):
            assert "checkin" not in fleet.bots[bot_id].protocols

        assert "Silence is the default" in lead_md
        assert "Silence is the default" not in coord_md
        assert "Silence is the default" not in worker_md

    def test_the_overlay_brings_the_skill_and_its_grants(
        self, fleet_dir, monkeypatch
    ):
        # Task 1's union, reached end to end THROUGH the registry (Task 1's
        # own tests prove the union mechanism with a hand-declared protocol;
        # this proves the same union fires when the protocol itself arrives
        # from a role default rather than a fleet.yaml declaration).
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        install_real_template(fleet_dir)
        _write_checkin_library(fleet_dir)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = _paths(fleet_dir)
        bot = fleet.bots["lead"]
        assert "checkin" not in bot.skills  # never declared, only defaulted

        compose_bot(bot, fleet, paths, log=lambda m: None)

        link = paths.bot_runtime("lead") / ".claude" / "skills" / "checkin"
        assert link.is_symlink()
        settings = json.loads(
            (paths.bot_runtime("lead") / ".claude" / "settings.local.json").read_text()
        )
        allow = settings["permissions"]["allow"]
        assert "Skill(checkin)" in allow
        assert "Skill(checkin:*)" in allow

    def test_the_checkin_entry_is_not_grandfathered(self):
        d = REGISTRY["protocols"]
        assert "checkin" not in d.grandfathered
        assert "checkin" in NEW_INSTRUCT_DEFAULTS_WITH_GATE_EVIDENCE
        assert NEW_INSTRUCT_DEFAULTS_WITH_GATE_EVIDENCE["checkin"].strip()
        # both TestScopeBoundary gates pass on this reason — exercised for
        # real by the class itself; this pins the two facts they depend on.
        assert "checkin" in d.roles.get(defaults.ROLE_LEAF_MANAGER, ())

    def test_the_entry_resolves_to_a_library_file(self):
        # test_every_registered_entry_resolves_to_a_library_file (extended,
        # above) already walks every role for every registered entry and
        # would fail if this file went missing — this pins the SPECIFIC fact
        # so a reader does not have to re-derive it from the sweep, and so a
        # future change that narrows the sweep's role coverage still has one
        # targeted witness for this entry.
        assert "checkin" in resolve("protocols", (defaults.ROLE_LEAF_MANAGER,))
        assert (LIBRARY / "protocols" / "checkin.md").is_file()
