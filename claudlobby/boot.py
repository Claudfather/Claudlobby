"""BootPolicy -- boot policy defined once, from the package host section.

Design: documentation/plans/2026-09-20-boot-admission-and-supervision-consolidation-design.md
section 6.1 (#1573). After a cold boot of a many-bot host, every session
spawns several MCP servers at once and nothing bounds how many may do that
together, nor how long Claude Code itself gets to start them -- the boot
stagger that existed lived only in the systemd unit, so a launchd host had
none at all. This module is where that stops being two things: every value a
bot's boot sequence needs -- how many bots may run their MCP-startup phase at
once, how long a queued bot waits before giving up on a slot, whether it
goes first (manager) or waits its turn (worker), how long Claude Code gets
to start its MCP servers, how long the readiness poll waits on that, and
whether `claude plugin update` runs more than once per boot -- is computed
HERE, ONCE, per bot, at compose time. `bot_conf_lines` renders it into
`bot.conf`, the one artifact both supervisors (systemd and launchd) deliver
identically, so a value computed once reaches both with no second code path
to drift from the first.

`ready_timeout_s` is DERIVED, never configured (epic fork F3, locked): the
readiness ceiling can never be shorter than the MCP startup timeout it waits
on, so raising `mcp_timeout_ms` alone still leaves the poll enough room --
`host.boot` carries no separate key for it. Fork F4 keeps the rendered name
`RC_READY_TIMEOUT_S`, the existing env var, as the carrier: an
un-regenerated `bot.conf` still has a value to fall back on.

PR B adds the two DERIVED keys the admission gate needs (`hold_ceiling_s`,
`boot_grace_s`), turns `admission_wait_max_s` into a derivation of the fleet's
own drain when `host.boot` does not name one (fork F13), and stops resolving
`admission_slots: auto` at compose time (fork F14) -- the gate resolves it on
the host it runs on, with the same clamp, so a fleet composed on one machine
and delivered to another gets that host's answer rather than the composer's.
`lib/boot-admission.sh` is the reader.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import BotConfig, FleetConfig


@dataclass(frozen=True)
class BootPolicy:
    # `auto` stays the literal string all the way into bot.conf (fork F14): the
    # gate derives it on the HOST it runs on, with the same clamp
    # `derive_slots` applies, so a fleet composed on one machine and delivered
    # to another resolves to that host's own answer rather than carrying the
    # composer's. An explicit integer renders as itself.
    admission_slots: int | str
    admission_wait_max_s: int
    priority: int  # 0 = manager, 1 = worker
    mcp_timeout_ms: int
    ready_timeout_s: int  # derived: max(READY_TIMEOUT_FLOOR_S, mcp_timeout_ms // 1000 + 20)
    plugin_update_once_per_boot: bool
    hold_ceiling_s: int  # derived: ready_timeout_s + HOLD_CEILING_MARGIN_S
    boot_grace_s: int  # derived: admission_wait_max_s + hold_ceiling_s (F15)


# The readiness ceiling's floor (F3/F4). `resolve_boot_policy` never derives
# `ready_timeout_s` below this, and it is also the literal `lib/start-bot.sh`
# falls back to when `RC_READY_TIMEOUT_S` is absent or unparseable in an
# un-regenerated bot.conf that predates the key -- bash cannot import a
# Python constant, so that file names the same number twice by hand (the
# `${RC_READY_TIMEOUT_S:-90}` default and the `_rc_timeout_s=90` coercion
# fallback). `tests/test_boot_policy_conformance.py` reads the shipped script
# and pins both spellings back to this one name, so the two can never drift
# without a failing test naming which side moved.
READY_TIMEOUT_FLOOR_S = 90


# The margin between a bot's readiness ceiling and the admission gate's hold
# ceiling -- how long a slot may be held before the reaper reclaims it whatever
# its holder's pid says. NOT a new guess: it is the margin
# `lib/rolling-restart.sh:86-104` already derives and enumerates
# ("pre-stop-handoff, spin-up, the tmux session spawn, and the poller's own
# settle"), so the gate's hold ceiling and the restart drivers' per-bot budget
# are the same number by construction rather than two derivations of one
# boot-timing truth. `lib/boot-admission.sh` names READY_TIMEOUT_FLOOR_S + this
# (90 + 120 = 210) by hand as its un-regenerated-bot.conf fallback, the same way
# `lib/start-bot.sh` names the readiness floor twice -- bash cannot import a
# Python constant.
HOLD_CEILING_MARGIN_S = 120


# The floor under the DERIVED admission wait cap (F13). This is the flat value
# `host.boot.admission_wait_max_s` used to carry as a package default, kept as
# a FLOOR so the derivation can only ever RAISE the cap above what the estate
# already ships with -- a small fleet on a fast host must not end up waiting
# LESS than every fleet did before the derivation existed.
ADMISSION_WAIT_FLOOR_S = 1200


# host.boot keys and their package-tier defaults (claudlobby/system.yaml).
# `priority`, `ready_timeout_s`, `hold_ceiling_s` and `boot_grace_s` are
# deliberately absent: the first comes from fleet topology
# (fleet.manager_bots()) and the other three are always derived (F3, F15) --
# none is a knob a host.boot block can hold.
#
# `admission_wait_max_s` is absent for a DIFFERENT reason (F13): it has no
# package-tier constant at all any more. Left at a flat 1200 it silently
# under-budgeted every host whose drain exceeds it -- a four-core host running
# 21 bots at one slot drains in 4200s -- so when `host.boot` does not name it,
# `resolve_boot_policy` derives it from the fleet's own drain. An explicit
# integer still wins outright, and an explicit 0 still means "never wait".
DEFAULTS = {
    "admission_slots": "auto",
    "mcp_timeout_ms": 180_000,
    "plugin_update_once_per_boot": True,
}


def derive_slots(cpu_count: int | None) -> int:
    """The `auto` admission-slot count: `clamp((cpu or 1) // 4, 1, 4)`.

    A host that cannot report its own CPU count (``cpu_count=None``) is
    treated as a single-core host -- the conservative floor -- rather than
    raising or silently admitting an unbounded number of bots at once.
    """
    quarter = (cpu_count or 1) // 4
    return max(1, min(4, quarter))


def _coerce_int(
    key: str, value: object, *, minimum: int, or_literal: str | None = None
) -> int:
    """Parse `value` as an integer no smaller than `minimum`, or raise naming `key`.

    Accepts a real ``int`` or a string that parses cleanly as one (a
    ``host.boot`` value may arrive from YAML as either). A bare float is
    refused rather than truncated, and ``bool`` is refused even though
    Python's ``bool`` is an ``int`` subclass -- a stray ``true``/``false``
    or ``12.5`` on a numeric key is a config mistake, not a 1, a 0 or a 12.
    `or_literal` names a non-numeric spelling the key also accepts (the
    slot count's ``'auto'``), so the refusal states the whole accepted set.
    """
    accepted = f"an integer >= {minimum}"
    if or_literal is not None:
        accepted = f"{or_literal} or {accepted}"
    refusal = ValueError(f"host.boot.{key} must be {accepted}, got {value!r}")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise refusal
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise refusal from None
    if parsed < minimum:
        raise refusal
    return parsed


# The spellings `plugin_update_once_per_boot` reads -- the same set the
# runtime side reads as 1/0 -- compared case-insensitively after stripping.
_BOOL_SPELLINGS = {
    "true": True,
    "1": True,
    "yes": True,
    "false": False,
    "0": False,
    "no": False,
}


def _coerce_bool(key: str, value: object) -> bool:
    """Parse `value` as a boolean, or raise naming `key`.

    A real ``bool`` passes through; a string is read from `_BOOL_SPELLINGS`
    and nothing else is coerced. Truthiness is exactly the wrong tool here:
    ``bool("false")`` is True in Python, which is how a quoted ``"false"``
    read as ON before this existed. An int, ``None`` or an unreadable
    string is a config mistake, not a value.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        spelled = value.strip().lower()
        if spelled in _BOOL_SPELLINGS:
            return _BOOL_SPELLINGS[spelled]
    raise ValueError(
        f"host.boot.{key} must be a boolean (true/false, 1/0, yes/no), got {value!r}"
    )


def resolve_boot_policy(
    bot: BotConfig,
    fleet: FleetConfig,
    host_boot: dict,
    *,
    cpu_count: int | None = None,
) -> BootPolicy:
    """Compute one bot's BootPolicy from the package `host.boot` block.

    `host_boot` is `{}` when the host declares no overrides, in which case
    every configurable field takes its DEFAULTS value and every derived one
    (`priority`, `ready_timeout_s`, `hold_ceiling_s`, `boot_grace_s`, and --
    since F13 -- `admission_wait_max_s`) is computed. `cpu_count` is injected
    rather than read here (`os.cpu_count()` is the production caller's job) so
    resolution stays a pure function of its arguments and tests never depend
    on the machine they run on.

    Raises `ValueError` when an EXPLICIT `admission_wait_max_s` is shorter
    than the fleet's own drain -- a cap that cannot outlast the queue it caps
    is a misconfiguration the composer can see and the operator cannot.
    """
    # The slot count's floor is 1, not 0, and that is its own rule (spec §8):
    # with a cap of 0 no slot can ever be created, so every bot queues for
    # the full wait cap -- the composer never emits it. `auto` is already
    # floored at 1 by derive_slots; an explicit value is floored here.
    slots_raw = host_boot.get("admission_slots", DEFAULTS["admission_slots"])
    if slots_raw == "auto":
        # F14: `auto` is rendered VERBATIM and resolved by the gate on the host
        # it runs on. `slots_effective` is only the composer's own estimate,
        # used for the drain arithmetic below -- never rendered.
        admission_slots: int | str = "auto"
        slots_effective = derive_slots(cpu_count)
    else:
        admission_slots = _coerce_int(
            "admission_slots", slots_raw, minimum=1, or_literal="'auto'"
        )
        slots_effective = admission_slots

    # A timeout of 0 is legal here (it is Claude Code's own MCP_TIMEOUT), so
    # this keeps the generic non-negative rule rather than the slot count's
    # floor of 1.
    mcp_timeout_ms = _coerce_int(
        "mcp_timeout_ms",
        host_boot.get("mcp_timeout_ms", DEFAULTS["mcp_timeout_ms"]),
        minimum=0,
    )
    plugin_update_once_per_boot = _coerce_bool(
        "plugin_update_once_per_boot",
        host_boot.get(
            "plugin_update_once_per_boot", DEFAULTS["plugin_update_once_per_boot"]
        ),
    )

    priority = 0 if bot.bot_id in fleet.manager_bots() else 1
    ready_timeout_s = max(READY_TIMEOUT_FLOOR_S, mcp_timeout_ms // 1000 + 20)
    hold_ceiling_s = ready_timeout_s + HOLD_CEILING_MARGIN_S

    # F13. The drain is what the cap has to survive: `ceil(bots / slots)` rounds
    # of a whole hold, where the hold is the WHOLE bring-up -- seeding, the
    # plugin block, the session spawn and the readiness poll -- i.e. the hold
    # ceiling, not the readiness ceiling.
    #
    # `bots_in_fleet` is a FLOOR on the host's real bot count, not that count:
    # the composer sees ONE fleet at a time and several fleets share a host, so
    # a two-fleet host drains slower than this arithmetic says. The per-ticket
    # dispersion in the gate (`effective_cap = cap + arrival_rank x
    # (ready_timeout_s / slots)`) is what covers the other fleets' share --
    # ranks keep climbing across fleets because the queue is host-wide.
    bots_in_fleet = max(1, len(fleet.bots))
    fleet_drain_s = -(-bots_in_fleet // max(1, slots_effective)) * hold_ceiling_s

    if "admission_wait_max_s" in host_boot:
        admission_wait_max_s = _coerce_int(
            "admission_wait_max_s", host_boot["admission_wait_max_s"], minimum=0
        )
        # 0 is the one legal value BELOW the drain: it means "never wait", an
        # explicit choice to proceed ungated rather than an under-budget.
        if 0 < admission_wait_max_s < fleet_drain_s:
            raise ValueError(
                "host.boot.admission_wait_max_s "
                f"({admission_wait_max_s}s) is shorter than this fleet's own "
                f"drain ({fleet_drain_s}s = ceil({bots_in_fleet} bots / "
                f"{slots_effective} slot(s)) x {hold_ceiling_s}s hold ceiling), "
                "so every queued bot would time out before its turn came. Raise "
                "it, raise admission_slots, or set it to 0 to never wait."
            )
    else:
        admission_wait_max_s = max(ADMISSION_WAIT_FLOOR_S, fleet_drain_s)

    # F15. The keepalive/fleet-pulse boot grace has to outlast the phase it
    # brackets, and PR B moved the host-wide wait INSIDE that phase: a queued
    # bot is legitimately mid-ExecStart for its whole admission wait and then
    # its whole bring-up. Derived from the two values it must agree with rather
    # than set beside them.
    #
    # The second term is the HOLD CEILING, not the readiness ceiling, and that
    # is this commit's own arithmetic rather than a new guess: the granted phase
    # is budgeted at `ready_timeout_s + 120` everywhere else here -- that is
    # what `hold_ceiling_s` IS, and `lib/rolling-restart.sh`'s enumeration of
    # what the 120s covers (pre-stop-handoff, spin-up, the tmux spawn, the
    # poller's settle) is exactly the non-readiness part of ExecStart. Summing
    # the wait with only the readiness ceiling left the grace 120s short of the
    # phase it brackets: noise at cap 1200, but at `admission_wait_max_s: 0` it
    # made BOOT_GRACE_S 200 against a bring-up this estate already budgets at
    # 320, so service_is_starting would stop suppressing ~120s before a
    # legitimately booting bot finished. (Provisional -- controller, on the
    # Task 1 review's arithmetic; reversible before merge, under F15's lock.)
    boot_grace_s = admission_wait_max_s + hold_ceiling_s

    return BootPolicy(
        admission_slots=admission_slots,
        admission_wait_max_s=admission_wait_max_s,
        priority=priority,
        mcp_timeout_ms=mcp_timeout_ms,
        ready_timeout_s=ready_timeout_s,
        plugin_update_once_per_boot=plugin_update_once_per_boot,
        hold_ceiling_s=hold_ceiling_s,
        boot_grace_s=boot_grace_s,
    )


def bot_conf_lines(policy: BootPolicy) -> list[str]:
    """Render `policy` into `bot.conf` lines, fixed order.

    Only `MCP_TIMEOUT` is exported: it is the one key Claude Code itself
    reads out of the environment. The rest are the launcher's own
    (`start-bot.sh`'s admission gate, readiness poll, and plugin-update-once
    stamp) and stay plain shell assignments, sourced under `set -a` same as
    everything else in `bot.conf` but with no export of their own to shadow.

    `BOOT_ADMISSION_SLOTS` renders `auto` verbatim when that is what the host
    declared (F14) -- `lib/boot-admission.sh` resolves it with the same clamp
    `derive_slots` applies and writes the resolved count once per boot, so
    every waiter on the host agrees.

    `BOOT_HOLD_CEILING_S` and `BOOT_GRACE_S` are the two derived keys PR B
    adds: the first is the gate's reaper ceiling, the second is the
    keepalive/fleet-pulse boot grace, whose phase bound moved when the
    host-wide wait moved inside `ExecStart` (F15).
    """
    return [
        f"BOOT_ADMISSION_SLOTS={policy.admission_slots}",
        f"BOOT_ADMISSION_WAIT_MAX_S={policy.admission_wait_max_s}",
        f"BOOT_PRIORITY={policy.priority}",
        f"export MCP_TIMEOUT={policy.mcp_timeout_ms}",
        f"RC_READY_TIMEOUT_S={policy.ready_timeout_s}",
        f"BOOT_PLUGIN_UPDATE_ONCE={1 if policy.plugin_update_once_per_boot else 0}",
        f"BOOT_HOLD_CEILING_S={policy.hold_ceiling_s}",
        f"BOOT_GRACE_S={policy.boot_grace_s}",
    ]
