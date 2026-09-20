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

This module defines the truth and nothing else. Wiring `resolve_boot_policy`
into the composer, and the admission gate that reads `bot_conf_lines`'
output back out in `start-bot.sh`, are later tasks.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import BotConfig, FleetConfig


@dataclass(frozen=True)
class BootPolicy:
    admission_slots: int
    admission_wait_max_s: int
    priority: int  # 0 = manager, 1 = worker
    mcp_timeout_ms: int
    ready_timeout_s: int  # derived: max(90, mcp_timeout_ms // 1000 + 20)
    plugin_update_once_per_boot: bool


# host.boot keys and their package-tier defaults (claudlobby/system.yaml).
# `priority` and `ready_timeout_s` are deliberately absent: the first comes
# from fleet topology (fleet.manager_bots()), the second is always derived
# (F3) -- neither is a knob a host.boot block can hold.
DEFAULTS = {
    "admission_slots": "auto",
    "admission_wait_max_s": 1200,
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
    every field but `priority` and `ready_timeout_s` takes its DEFAULTS
    value. `cpu_count` is injected rather than read here (`os.cpu_count()`
    is the production caller's job) so resolution stays a pure function of
    its arguments and tests never depend on the machine they run on.
    """
    # The slot count's floor is 1, not 0, and that is its own rule (spec §8):
    # with a cap of 0 no slot can ever be created, so every bot queues for
    # the full wait cap -- the composer never emits it. `auto` is already
    # floored at 1 by derive_slots; an explicit value is floored here.
    slots_raw = host_boot.get("admission_slots", DEFAULTS["admission_slots"])
    if slots_raw == "auto":
        admission_slots = derive_slots(cpu_count)
    else:
        admission_slots = _coerce_int(
            "admission_slots", slots_raw, minimum=1, or_literal="'auto'"
        )

    # A wait cap of 0 is legal ("never wait"), so these two keep the generic
    # non-negative rule.
    admission_wait_max_s = _coerce_int(
        "admission_wait_max_s",
        host_boot.get("admission_wait_max_s", DEFAULTS["admission_wait_max_s"]),
        minimum=0,
    )
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
    ready_timeout_s = max(90, mcp_timeout_ms // 1000 + 20)

    return BootPolicy(
        admission_slots=admission_slots,
        admission_wait_max_s=admission_wait_max_s,
        priority=priority,
        mcp_timeout_ms=mcp_timeout_ms,
        ready_timeout_s=ready_timeout_s,
        plugin_update_once_per_boot=plugin_update_once_per_boot,
    )


def bot_conf_lines(policy: BootPolicy) -> list[str]:
    """Render `policy` into `bot.conf` lines, fixed order.

    Only `MCP_TIMEOUT` is exported: it is the one key Claude Code itself
    reads out of the environment. The rest are the launcher's own
    (`start-bot.sh`'s admission gate, readiness poll, and plugin-update-once
    stamp) and stay plain shell assignments, sourced under `set -a` same as
    everything else in `bot.conf` but with no export of their own to shadow.
    """
    return [
        f"BOOT_ADMISSION_SLOTS={policy.admission_slots}",
        f"BOOT_ADMISSION_WAIT_MAX_S={policy.admission_wait_max_s}",
        f"BOOT_PRIORITY={policy.priority}",
        f"export MCP_TIMEOUT={policy.mcp_timeout_ms}",
        f"RC_READY_TIMEOUT_S={policy.ready_timeout_s}",
        f"BOOT_PLUGIN_UPDATE_ONCE={1 if policy.plugin_update_once_per_boot else 0}",
    ]
