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
        ungrandfathered = {
            t: tuple(e for e in d.entries if e not in d.grandfathered)
            for t, d in REGISTRY.items()
            if d.tier is Tier.INSTRUCT
            and any(e not in d.grandfathered for e in d.entries)
        }
        assert not ungrandfathered, (
            "a NEW INSTRUCT default is present. It cannot be justified by unit "
            "test: run lib/naked-bot-observe.py --baseline and name the delta. "
            f"{ungrandfathered}"
        )

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
        class _NoDocs:
            shared_docs = None

        class _WithDocs:
            shared_docs = Path("/somewhere/shared")

        assert defaults.available("not-gated-by-anything", _NoDocs()) is True
        assert defaults.available("shared-documentation", _NoDocs()) is False
        assert defaults.available("shared-documentation", _WithDocs()) is True

    def test_every_registered_entry_resolves_to_a_library_file(self):
        # Asserted for ALL twelve types, not just the one this phase touched: a
        # registry entry naming a file that does not exist fails at compose time
        # on a real fleet, and only for the fleets that did not opt out.
        missing = [
            f"{etype}/{entry}"
            for etype in REGISTRY
            for entry in resolve(etype)
            if not (LIBRARY / etype / f"{entry}.md").is_file()
        ]
        assert not missing, f"registered but absent from library/: {missing}"
