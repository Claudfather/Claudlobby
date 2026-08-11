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

from pathlib import Path

import pytest

from claudlobby import config, defaults
from claudlobby.defaults import REGISTRY, TIER_TESTS, Disposition, Tier, resolve

REPO_ROOT = Path(__file__).resolve().parent.parent
LIBRARY = REPO_ROOT / "library"


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
        assert settled == ["guardrails"], (
            "Phase 1 ships EXISTING constants only — a newly settled type means a "
            f"default landed early: {settled}"
        )
        assert len(unsettled) == len(REGISTRY) - 1


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
        # `manager` is the only role the composer can resolve, via
        # manager_bots(); adding a second needs a predicate first.
        assert defaults.DETECTABLE_ROLES == frozenset({defaults.ROLE_MANAGER})
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

    def test_no_instruct_default_ships_before_the_phase_3_gate(self):
        # Phase 3 GATES Phase 2 rather than following it. An INSTRUCT entry
        # appearing here before the naked-bot observation exists is the silent
        # behaviour change the whole tier split was drawn to prevent.
        instruct = {
            t: d.entries
            for t, d in REGISTRY.items()
            if d.tier is Tier.INSTRUCT and d.entries
        }
        assert not instruct, (
            f"INSTRUCT defaults present before the Phase 3 observation gate: {instruct}"
        )
