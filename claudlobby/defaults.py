"""Compositor-side defaults for every library entity type (#1168 Phase 1).

A hand-rolled fleet should get a working baseline without going to find it. The
merge pathway for that already exists and is universal — all 12 entity types
merge fleet defaults in ``_coerce_bot``. This module is where each type's
disposition gets decided.

TWO of the twelve are settled: ``guardrails`` (RESTRICT, declared at Phase 1)
and ``protocols`` (INSTRUCT, admitted at Phase 3). Both were ALREADY COMPOSING
before they were registered, and neither changed a composed byte on the default
path when it was — which is the only shape of entry that may land without an
A/B on real bots. See ``grandfathered`` on ``Disposition``: an entry that
predates its own registration is not evidence that the tier test is passable.

THIS REGISTRY IS THE SOURCE, NOT A DESCRIPTION (F4, binding).
``DEFAULT_GUARDRAILS`` is DERIVED from ``REGISTRY`` below and re-exported;
``config.py`` imports it rather than declaring its own. A registry that listed
dispositions while constants were declared separately would be two sources of
one fact — they drift, the test passes against the mirror, and the mirror is not
what composes. That is this codebase's most reliable failure mode: #1046
(``_fleet_manager_worker_counts`` hand-rolling manager detection that
``manager_bots()`` already did) and #892/#1143 (an inline ``parse_fleet_bots``
copy drifting from the shared one).

WHAT IS DELIBERATELY NOT HERE. ``DEFAULT_MARKETPLACES`` and ``DEFAULT_PLUGINS``
are real constants and stay in ``config.py``. They are not library entity types:
they do not live under ``library/``, they are not merged per-bot by
``_coerce_bot``, and they answer "what does the HOST install" rather than "what
does this BOT compose". Folding them in would conflate two axes and make the
completeness test below assert over a set that is not a set. (An earlier
baseline counted them toward "3 of 12 entity types have a constant". The true
figure was 1 of 12 at Phase 1 and is 2 of 12 now that ``protocols`` is admitted;
neither count includes these two.)

WHY NOT ``library/`` (F5, the part worth keeping): ``library/`` is content the
compositor consumes, not policy it enforces. Declaring defaults there would let
library content decide what library content ships.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Tier(Enum):
    """What a default DOES to a bot — the fault line, not the directory (F1).

    The bar for membership differs by tier because the cost of a wrong entry
    differs by tier. One bar was rejected as cheapest-and-wrong; twelve bespoke
    bars were rejected as a junk drawer arrived at from the opposite direction.
    """

    #: Removes freedom. A wrong entry costs a bot some capability, loudly and
    #: visibly — which is why `guardrails` could safely have a default years
    #: before anything else did.
    RESTRICT = "restrict"

    #: Changes what a bot DOES. A wrong entry changes behaviour on every bot on
    #: the estate, silently. This is the tier the naked-bot observation gate
    #: (Phase 3) exists for, and no INSTRUCT default may land before it.
    INSTRUCT = "instruct"

    #: Makes something reachable. Inert until referenced, so the failure mode is
    #: an unused declaration rather than changed behaviour.
    WIRE = "wire"


#: The membership test each tier's entries must be argued against. Held as data
#: so an entry cannot be added without a reviewer being able to name the bar it
#: had to clear.
TIER_TESTS: dict[Tier, str] = {
    Tier.RESTRICT: (
        "Its protective value depends on UNIVERSAL coverage, because the harm "
        "it prevents is estate-wide rather than scoped to the bot carrying it. "
        "A rule that only protects the fleet holding it does NOT qualify and "
        "stays opt-in. (Inherited verbatim from DEFAULT_GUARDRAILS, the sole "
        "worked example of this pattern.)"
    ),
    Tier.INSTRUCT: (
        "Every bot would be WORSE at its job without it, and no bot is made to "
        "do something surprising by having it. The burden is higher than "
        "RESTRICT because a wrong entry changes behaviour silently: 'useful to "
        "many bots' is NOT sufficient, because a bot that did not ask for an "
        "instruction cannot tell that it received one."
    ),
    Tier.WIRE: (
        "It is inert until referenced, AND its absence would be discovered only "
        "at the moment of use. Wiring that costs nothing when unused and "
        "everything when missing qualifies; wiring that implies a service the "
        "fleet may not have does not."
    ),
}


@dataclass(frozen=True)
class Disposition:
    """One entity type's default, its tier, and the argument for both.

    ``entries`` is the GLOBAL default. ``roles`` maps a role name to entries
    layered ON TOP of it — the resolved value for a bot holding that role is the
    union (F2).
    """

    tier: Tier
    reason: str
    entries: tuple[str, ...] = ()
    roles: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: False means "no default has been argued for yet" — Phase 2's worklist.
    #: An empty list is a first-class answer, but "empty because nobody looked"
    #: and "empty because nothing clears the bar" are different claims and the
    #: registry must not let them read the same.
    settled: bool = False
    #: Entry NAMES that were already composing before they were registered.
    #: Registering one makes an existing default visible and disableable; it is
    #: NOT a finding that it cleared the tier test, and a grandfathered entry is
    #: never precedent for a new one. The distinction has to be structural
    #: rather than prose in `reason`: an entry that ships despite failing half
    #: its bar reads, to the next author, as evidence that the bar is soft.
    #:
    #: NAMES, NOT A BOOL, and the difference is the whole safety property. A
    #: per-TYPE bool exempts every FUTURE entry of that type: `protocols` would
    #: be flagged once and `entries=("shared-documentation", "something-new")`
    #: would then add a genuinely new estate-wide instruction without tripping
    #: anything. That is the same granularity mistake `AVAILABILITY_GATES`
    #: is keyed-by-entry to avoid, in the opposite direction.
    #:
    #: Scope is the INSTRUCT admission bar specifically. `guardrails` predates
    #: its own registration too and carries nothing here, because RESTRICT has
    #: no such gate — the field answers "is this exempt from the Phase 3
    #: new-instruction gate", not "is this older than the registry".
    grandfathered: tuple[str, ...] = ()


_UNARGUED = (
    "No default argued yet. Phase 2 decides; empty until it clears the tier test above."
)

#: Every library entity type, with an explicit disposition. A thirteenth type
#: added to `library/` without an entry here fails `test_defaults_registry.py`.
REGISTRY: dict[str, Disposition] = {
    # --- RESTRICT ------------------------------------------------------------
    "guardrails": Disposition(
        tier=Tier.RESTRICT,
        entries=("claudlobby-dev-in-projects",),
        settled=True,
        reason=(
            "The shared install is CLAUDLOBBY_ROOT for every bot on the host, so "
            "one bot branching there swaps supervision and dispatch scripts for "
            "all of them — estate-wide harm, universal coverage required."
        ),
    ),
    "permissions": Disposition(tier=Tier.RESTRICT, reason=_UNARGUED),
    # --- INSTRUCT ------------------------------------------------------------
    # The Phase 3 naked-bot observation gate exists as of #1171, so entries are
    # now admissible here. Each must diff against the recorded baseline.
    "expertise": Disposition(tier=Tier.INSTRUCT, reason=_UNARGUED),
    "skills": Disposition(tier=Tier.INSTRUCT, reason=_UNARGUED),
    "protocols": Disposition(
        tier=Tier.INSTRUCT,
        entries=("shared-documentation",),
        settled=True,
        grandfathered=("shared-documentation",),
        reason=(
            "ALREADY SHIPPING, now admitted. `composer.py` appended this protocol "
            "directly, downstream of the merge this registry feeds, so every "
            "overlay-mode bot on every fleet has been carrying six undeclared "
            "`###` sections with no way to switch them off — not even the "
            "`system_defaults: false` kill switch (measured, all 16 baseline "
            "arms). Registering it changes no composed byte on the default path; "
            "it makes an existing INSTRUCT default visible and disableable.\n\n"
            "AGAINST THE TIER TEST IT IS HALF PASS, HALF FAIL, and is registered "
            "as grandfathered for exactly that reason.\n"
            "  PASSES 'every bot would be worse without it': a fleet whose bots "
            "write to a shared doc tree and do not share the INDEX/frontmatter/"
            "single-writer conventions accumulates duplicate docs, which is the "
            "failure the protocol names first.\n"
            "  FAILS 'no bot is made to do something surprising': a bot with "
            "`claudron_vault_path` set composes the template's vault section — "
            "'reached through the Claudron door, NOT by reading a raw doc tree' — "
            "and then this protocol telling it to scan `planning/active/INDEX.md` "
            "by hand. Both land in one file today; the append never checked for a "
            "vault. That contradiction PRE-DATES this entry and is deliberately "
            "not fixed here, because fixing it would change composed instructions "
            "on the default path, which is the silent estate-wide edit this "
            "registry exists to prevent. It is tracked as its own decision.\n\n"
            "KNOWN BOUND — the entry is NOT composed on every bot, and the "
            "registry alone cannot say so: it is gated by AVAILABILITY_GATES "
            "below on `Paths.shared_docs`, which is falsy in root mode. Measured: "
            "a root-mode naked bot composes no shared-documentation section at "
            "all, so an ungated default would newly instruct every root-mode bot."
        ),
    ),
    "principles": Disposition(tier=Tier.INSTRUCT, reason=_UNARGUED),
    "post_actions": Disposition(tier=Tier.INSTRUCT, reason=_UNARGUED),
    # --- WIRE ----------------------------------------------------------------
    "mcp": Disposition(tier=Tier.WIRE, reason=_UNARGUED),
    "tools": Disposition(tier=Tier.WIRE, reason=_UNARGUED),
    "integrations": Disposition(tier=Tier.WIRE, reason=_UNARGUED),
    "resources": Disposition(tier=Tier.WIRE, reason=_UNARGUED),
    "lessons": Disposition(tier=Tier.WIRE, reason=_UNARGUED),
}


# --- availability gates ------------------------------------------------------
# Some defaults are conditional on a fact this module cannot see. `guardrails`
# needs none: its merge happens in `load_fleet`, which knows nothing but the
# parsed YAML. `shared-documentation` does, and the gate cannot move down into
# that merge — `load_fleet` takes a bare path and never learns whether the fleet
# is overlay- or root-mode, and threading `Paths` into it would widen a config
# parser into a filesystem consumer.
#
# KEYED BY ENTRY NAME, DELIBERATELY NOT BY TYPE. Keying by type would make the
# gate a property of `protocols`, so the next protocol default added here would
# silently inherit a shared-docs condition nobody wrote for it — and would then
# vanish on root-mode fleets with nothing to say why.
#
# The value is a PREDICATE over `Paths`, not the NAME of a `Paths` attribute.
# A name resolved by `getattr(paths, name, None)` has exactly one failure mode
# and it is the worst available: a renamed or mistyped attribute reads falsy,
# which SUPPRESSES the default on every bot on the estate, silently, in the
# INSTRUCT tier. A predicate turns that same mistake into an `AttributeError` at
# compose time. Loud and instant beats quiet and universal.
#
# `paths` is duck-typed on purpose — importing `Paths` here would make the
# policy module depend on the path resolver it is meant to be independent of.
#
# WHEN A SECOND PER-ENTRY FACT APPEARS, do not add a third side table: that is
# the point at which `entries` should become `tuple[Entry, ...]` and both facts
# move onto the entry itself. Two tables is a pair; three is a pattern nobody
# chose.
AVAILABILITY_GATES: dict[str, Callable[[Any], bool]] = {
    "shared-documentation": lambda paths: paths.shared_docs is not None,
}


def available(entry: str, paths: Any) -> bool:
    """Whether ``entry``'s precondition holds for this fleet.

    An entry with no gate is unconditional — the common case, and stated HERE
    rather than in the composer so a reader of the registry does not have to
    open another module to learn what an absent gate means.
    """
    gate = AVAILABILITY_GATES.get(entry)
    return True if gate is None else gate(paths)


# --- roles -------------------------------------------------------------------
# KNOWN BOUND, stated so the next person finds a seam rather than a hardcode
# (F2, binding).
#
# The mechanism below is keyed on role GENERALLY. Today exactly one role is
# detectable: `manager`, via `FleetConfig.manager_bots()` — which is the only
# role predicate the composer has. There is no general `role` field on a bot, so
# "roles" is currently `{manager, not-manager}` however plural the type looks.
#
# TO ADD A SECOND ROLE you need a predicate that can DETECT it, not just a key
# here. A role named in `roles` that nothing can resolve is silently inert — it
# would never be unioned in, and nothing would say so. Extend detection first,
# then populate; `resolve()` takes the caller's already-resolved role names
# precisely so this module never has to guess.
#
# Role-scoping is NOT an exemption from the tier test (F1 x F2): a role overlay
# entry must clear its tier's bar exactly as a global entry does. And the
# overlay is available to ALL THREE tiers, not just INSTRUCT — #1161 is the
# counter-example, where a RESTRICT-tier guardrail (`merge-policy-auto-admin`)
# is legitimately manager-scoped.
ROLE_MANAGER = "manager"

#: Roles the composer can currently DETECT. Adding a name here without a
#: predicate that resolves it is the trap the note above describes.
DETECTABLE_ROLES: frozenset[str] = frozenset({ROLE_MANAGER})


def resolve(entity_type: str, roles: tuple[str, ...] = ()) -> list[str]:
    """The compositor-side default for ``entity_type``, for a bot in ``roles``.

    Global entries plus every matching role overlay, de-duplicated, order
    preserved. Unknown entity types return ``[]`` rather than raising: this
    resolves defaults, and a caller asking about a type that does not exist has
    no default by definition. The completeness test is what catches a MISSING
    disposition; making this raise would turn that into a compose-time crash.
    """
    d = REGISTRY.get(entity_type)
    if d is None:
        return []
    out: list[str] = list(d.entries)
    for role in roles:
        for entry in d.roles.get(role, ()):
            if entry not in out:
                out.append(entry)
    return out


# --- derived constants -------------------------------------------------------
# DERIVED, never re-declared. config.py imports this rather than holding its own
# copy, so the registry above is the single source and the completeness test
# asserts over the value the composer actually consumes (F4, binding).
DEFAULT_GUARDRAILS: list[str] = resolve("guardrails")
