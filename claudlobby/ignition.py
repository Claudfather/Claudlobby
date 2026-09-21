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
