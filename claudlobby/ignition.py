"""Whether ANYTHING gives an idle bot on this fleet a turn (#1633).

Five doors can start a bot's own turn — ``manager-checkin``, ``task-recheck``,
``worker-unassigned``, per-bot ``briefing.slots``, and per-bot
``brief.on_start`` — and each is individually visible somewhere (a switch
row, a validator warning, a composed hook). Nothing before this module
answered the one composite question an operator actually asks: does THIS
fleet have any of them armed at all? A reviewed 21-bot host had four of the
five unarmed and no surface said so.

Declared state only. Whether an armed door is actually ENROLLED (a unit
installed and running) is composed-vs-enrolled drift — #839/#1040's
territory, not this one's; ``doctor``/``validate`` say so in their own text
rather than silently answering a narrower question than the one asked.

It also holds the GOAL-BINDING half of the cross-reference the two findings
carry (#1680) — the clause a goal-binding warning appends, beside the one an
ignition warning appends. Not because the clause is about ignition, but
because ``doctor`` imports ``validator`` at module scope, so the validator
cannot import the doctor, and this module is the only non-circular floor both
emitters already stand on. The pair belongs adjacent whatever else is true.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .config import FleetConfig
    from .paths import Paths

#: The doors this module knows how to check, in the order the #1633 evidence
#: names them.
_SWITCH_DOORS: tuple[str, ...] = (
    "manager-checkin",
    "task-recheck",
    "worker-unassigned",
)


@dataclass(frozen=True)
class Door:
    """One ignition door: its name, whether it's armed HERE, and the one
    line that arms it (meaningful whether or not it's armed — a PASS still
    names what's working, per the doctor rung's convention elsewhere)."""

    name: str
    armed: bool
    arm_line: str = ""


def ignition_doors(fleet: "FleetConfig", paths: "Paths") -> list["Door"]:
    """Every door that can give an idle bot a turn, and whether it's armed.

    The three job-scoped doors (``manager-checkin``, ``task-recheck``,
    ``worker-unassigned``) come straight off :func:`switches.resolve` — a
    fleet-wide on/off with a derived arm line, exactly what a job switch
    already answers. The two per-bot doors (``briefing.slots``,
    ``brief.on_start``) have no single fleet-wide switch state — each bot
    declares its own — so ``armed`` is computed directly off
    :class:`FleetConfig`; ``brief.on_start``'s arm line is still borrowed
    from the ``boot-brief`` switch registration (one rendering, not a
    hand-typed second copy) even though that switch's own ``.on`` cannot see
    a per-bot field and would otherwise read a fleet with every bot armed as
    unarmed.
    """
    from . import switches as _sw

    states = {s.switch.key: s for s in _sw.resolve(paths, fleet)}
    doors: list[Door] = []

    for key in _SWITCH_DOORS:
        state = states.get(key)
        if state is not None:
            doors.append(Door(key, bool(state.on), state.arm))

    briefing_armed = any(
        bot.briefing is not None and bot.briefing.slots for bot in fleet.bots.values()
    )
    doors.append(
        Door(
            "briefing.slots",
            briefing_armed,
            "bots.<bot>.briefing.slots in fleet.yaml, then generate + lib/setup-fleet",
        )
    )

    boot_brief_armed = any(bot.brief_on_start for bot in fleet.bots.values())
    boot_brief_switch = states.get("boot-brief")
    doors.append(
        Door(
            "brief.on_start",
            boot_brief_armed,
            boot_brief_switch.arm
            if boot_brief_switch is not None
            else "bots.<bot>.brief.on_start: true in fleet.yaml, then generate",
        )
    )

    return doors


#: Appended to a "nothing to dispatch against" warning when the ignition
#: warning is ALSO firing, and vice versa (#1680). Two warnings is the correct
#: output there — one says there is nothing to dispatch AT, the other that
#: there is nothing to dispatch WITH, and arming a door on a fleet with no
#: project registry still cannot dispatch. What was missing is that neither
#: said so: a reader who fixes one and still sees the other may reasonably
#: conclude their first fix did not work, and the cheapest wrong response to
#: that is to undo it.
#:
#: Each clause NAMES the other warning and never restates its finding.
#: Restating it is not merely redundant — the phrase that identifies a warning
#: is also the phrase its readers select on, so a copy of it inside the other
#: warning makes the two indistinguishable to anything scanning the list.
#: (Measured: the first draft restated "no ignition door is armed" and
#: immediately collided with an existing test's own discriminator.)
#:
#: One definition each because each is emitted from TWO surfaces (``doctor``
#: and ``validate``), and a clause that drifts from the rung it names is worse
#: than no clause at all.
NO_DOOR_CO_REQUISITE = (
    " — and the ignition warning beside this one is firing too: it is a "
    "co-requisite, not a duplicate, so this fix alone still produces no work"
)

NO_PROJECTS_CO_REQUISITE = (
    " The goal-binding warning beside this one is firing too: it is a "
    "co-requisite, not a duplicate, so this fix alone still produces no work."
)


def ignition_gap(
    fleet: "FleetConfig", paths: "Paths", doors: list["Door"] | None = None
) -> bool:
    """Whether NOTHING gives an idle bot a turn on a fleet that has one to give.

    The exact condition both ignition warnings fire on, so the goal-binding
    warnings can ask *this module* whether its counterpart is speaking instead
    of re-deriving the answer.

    THE LEAF-MANAGER CONJUNCT, stated here once because three rungs turn on
    it: a fleet with no leaf manager has no dispatcher and no idle-manager
    beat, so neither "nothing gives an idle bot a turn" nor "nothing to
    dispatch against" is a finding an operator can act on there. Both rungs
    report it as not-applicable instead; #1680 is what happened while only
    one of them did.

    The cheap conjunct is tested FIRST and short-circuits: resolving the doors
    shells out to ``lib/env-tiers.sh`` through the switch cascade, and a
    manager-less fleet can never have a gap however its doors read. ``doors``
    is the seam for a caller that has already paid for that resolve — both
    ``validate`` and ``check_goal_binding`` pass it, so one run resolves once.
    """
    if not fleet.leaf_manager_bots():
        return False
    doors = ignition_doors(fleet, paths) if doors is None else doors
    return not any(d.armed for d in doors)


def ignition_warning_tail(arm_line: str, *, goal_binding_warns: bool) -> str:
    """The shared tail of BOTH ignition warnings: the co-requisite clause, if
    the goal-binding warning is firing, then the line that arms the cheapest
    door.

    The ORDER is the invariant, and it is the reason this is a function rather
    than two hand-built strings: the clause goes BEFORE ``Cheapest to arm:``,
    never after, because a sentence trailing a copy-pasteable config line is
    the one place it gets read as part of the line.

    ``goal_binding_warns`` is the CALLER's, deliberately: `doctor` and
    `validate` emit different sets of goal-binding warnings — doctor has a
    plain no-projects line for a leaf manager the check-in is not composed
    onto, and validate does not — so the condition differs per surface even
    though the placement does not.
    """
    clause = NO_PROJECTS_CO_REQUISITE if goal_binding_warns else ""
    return clause + " Cheapest to arm: " + arm_line
