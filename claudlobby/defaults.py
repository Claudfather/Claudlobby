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
        entries=("shared-documentation", "shared-documentation-vault"),
        settled=True,
        grandfathered=("shared-documentation",),
        reason=(
            "ONE SLOT, TWO MUTUALLY EXCLUSIVE ENTRIES — how a bot reaches fleet "
            "knowledge, which differs by whether a Claudron vault is wired. "
            "AVAILABILITY_GATES below picks exactly one; neither is composed in "
            "root mode, where there is no shared-docs tree to describe.\n\n"
            "`shared-documentation` (GRANDFATHERED) is the hand-scan form and is "
            "unchanged since it was admitted: `composer.py` had appended it "
            "directly, downstream of the merge this registry feeds, so every "
            "overlay-mode bot carried six undeclared `###` sections that nothing "
            "could switch off — not even the `system_defaults: false` kill "
            "switch. It PASSES 'every bot would be worse without it' for a fleet "
            "whose bots share a raw doc tree: without the INDEX/frontmatter/"
            "single-writer conventions they accumulate duplicate docs, the "
            "failure the protocol names first.\n\n"
            "`shared-documentation-vault` is NOT grandfathered — it is a new "
            "instruction and it changes what vault-wired bots are told, which is "
            "the point. It exists because the hand-scan form FAILED the tier "
            "test's second half on exactly those bots: they compose the "
            "template's vault section ('reached through the Claudron door, NOT by "
            "reading a raw doc tree') and then an instruction to hand-scan that "
            "same tree. Measured on this estate, the contradiction is literal "
            "rather than stylistic — `CLAUDRON_VAULT_PATH` is `.../local` and the "
            "fleet's `shared/` tree is `.../local/<system>/<fleet>/shared`, so the "
            "INDEX files the protocol named ARE vault files. Splitting the entry "
            "is what lets the grandfathered half stay defensible (#1172).\n\n"
            "WHY TWO FILES RATHER THAN ONE THAT STATES BOTH CASES: library "
            "bodies get plain `{{KEY}}` substitution, not Jinja, so a library "
            "file cannot branch on its own — the choice is composed branch or "
            "prose that names both worlds.\n\n"
            "THE SELECTOR IS DIVERGENCE SIZE, and it is a rule, not a "
            "preference. Here MOST of the six sections differ under a vault, so "
            "prose naming both worlds would be two protocols interleaved and a "
            "bot picking its way through. `library/protocols/dispatch.md` takes "
            "the OPPOSITE treatment for the opposite reason: ~15 lines of a "
            "large protocol diverge, so it states the rule and defers the "
            "mechanism to the bot's composed Shared Documentation section, "
            "because a near-duplicate twin of a large file drifts the first "
            "time either copy is edited. Dereferencing a section of your own "
            "composed file is not adjudicating a contradiction; being handed "
            "two conflicting instructions a thousand lines apart is, and that "
            "is the harm #1172 recorded."
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


@dataclass(frozen=True)
class Facts:
    """The composition facts an availability gate may consult.

    A NAMED, CLOSED SET — deliberately not ``paths`` and ``bot`` themselves. A
    gate handed the real objects can reach any attribute on either, so what a
    default's availability depends on becomes unbounded and undiscoverable: you
    would have to read every lambda to learn what makes a default appear. Here
    the answer is this class, and widening it is a visible edit.

    It also keeps this module free of a `Paths`/`BotConfig` import, so policy
    does not depend on the path resolver or the config parser it exists above.

    RESOLVED BY THE COMPOSER, never derived here — `vault_wired` in particular
    must be the same predicate `claude.md.j2` branches on, or a bot composes the
    vault section beside the hand-scan protocol, which is #1172 exactly.
    """

    #: The fleet has a shared-docs tree at all. False in root mode.
    shared_docs: bool
    #: The bot is wired to a Claudron vault (`claudron_vault_path`).
    vault_wired: bool


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
# WHEN A SECOND PER-ENTRY FACT APPEARS, do not add a third side table: that is
# the point at which `entries` should become `tuple[Entry, ...]` and both facts
# move onto the entry itself. Two tables is a pair; three is a pattern nobody
# chose.
AVAILABILITY_GATES: dict[str, Callable[[Facts], bool]] = {
    # MUTUALLY EXCLUSIVE, and `test_defaults_registry.py` asserts it over every
    # combination of facts. These two carry the same six-section slot with
    # opposite navigation instructions, so a fleet shape that satisfied both
    # would compose a bot that is told to hand-scan a tree AND never to open it
    # — which is #1172, re-created by the mechanism built to fix it. A shape
    # that satisfied neither would silently drop the protocol estate-wide.
    "shared-documentation": lambda f: f.shared_docs and not f.vault_wired,
    "shared-documentation-vault": lambda f: f.shared_docs and f.vault_wired,
}


#: Entries that occupy ONE slot: at most one may compose, whatever its source.
#:
#: AVAILABILITY IS NOT ENOUGH, and assuming it was is how the first version of
#: this fix re-created #1172 in a worse form. A gate filters the DEFAULT
#: injection; it never sees `bot.protocols`. So a vault-wired fleet that
#: DECLARED `shared-documentation` composed the hand-scan form (declared, hence
#: unfiltered) AND the vault form (gated in) AND the template's vault section —
#: three `Shared Documentation` headings with opposite instructions. Measured on
#: a real compose, not reasoned about.
#:
#: `local/home/tl-enterprises/fleet.yaml` declares `shared-documentation`
#: explicitly today. It is vault-less, so the defect is latent there rather than
#: live — which is exactly the kind of thing that ships and then wakes up.
#:
#: The DECLARATION SELECTS THE SLOT, NOT THE FORM. "Give this bot the
#: shared-documentation protocol" is a fleet's statement of intent; which form
#: implements it is a compositor concern, decided by how the fleet is wired. So
#: the unavailable member is dropped even when named explicitly — the operator
#: gets the protocol they asked for, in the form their fleet can actually
#: follow.
EXCLUSIVE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"shared-documentation", "shared-documentation-vault"}),
)


def resolve_exclusions(names: list[str], facts: Facts) -> list[str]:
    """Drop members of an exclusive group that are not available for *facts*.

    Applied to the FINAL list — declared plus defaulted — because the declared
    half is the one the availability gate cannot reach. Order is preserved; a
    group with no available member drops all of them, which is correct: no form
    of the slot fits this fleet.
    """
    unavailable = {e for g in EXCLUSIVE_GROUPS for e in g if not available(e, facts)}
    return [n for n in names if n not in unavailable]


def available(entry: str, facts: Facts) -> bool:
    """Whether ``entry``'s precondition holds for this fleet.

    An entry with no gate is unconditional — the common case, and stated HERE
    rather than in the composer so a reader of the registry does not have to
    open another module to learn what an absent gate means.
    """
    gate = AVAILABILITY_GATES.get(entry)
    return True if gate is None else gate(facts)


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
