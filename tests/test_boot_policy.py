"""Tests for BootPolicy — the boot policy truth (#1573, design doc §6.1).

Covers:
- resolve_boot_policy defaults against an empty host.boot block
- derive_slots' auto clamp across a spread of cpu counts
- an explicit admission_slots override winning over auto
- ready_timeout_s derived from mcp_timeout_ms (epic fork F3)
- priority from fleet.manager_bots() (manager 0, worker 1)
- invalid host.boot values raising ValueError naming the key (negative,
  non-integer string, bare float, stray bool)
- admission_slots refusing zero (spec §8: the composer never emits a cap of
  0) while admission_wait_max_s: 0 stays legal ("never wait")
- plugin_update_once_per_boot read from an exact spelling set, never by
  truthiness (a quoted "false" is off); unreadable values refused
- bot_conf_lines' fixed eight-line render, MCP_TIMEOUT the only export
- load_host_boot() reading the package system.yaml (the loader + the
  package defaults, wired together)
- PR B (#1573): admission_slots: auto rendered VERBATIM (F14) and the bash
  gate's own clamp agreeing with derive_slots on the same cpu counts;
  admission_wait_max_s DERIVED from the fleet's drain when host.boot names
  none, an explicit value winning, an explicit value SHORTER than the drain
  refused, an explicit 0 still legal (F13); hold_ceiling_s and boot_grace_s
  (F15)
"""

from __future__ import annotations

import pytest

import shutil
import subprocess
from pathlib import Path

from claudlobby.boot import (
    ADMISSION_WAIT_FLOOR_S,
    DEFAULTS,
    HOLD_CEILING_MARGIN_S,
    BootPolicy,
    bot_conf_lines,
    derive_slots,
    resolve_boot_policy,
)
from claudlobby.config import BotConfig, FleetConfig, TeamConfig, load_host_boot


def _lead_worker_fleet() -> FleetConfig:
    """A minimal fleet with one leaf manager and one worker."""
    return FleetConfig(
        name="test-fleet",
        service_prefix="com.test",
        bots={
            "lead": BotConfig(bot_id="lead", name="lead", expertise=["orchestration"]),
            "worker": BotConfig(bot_id="worker", name="worker", expertise=["eng"]),
        },
        teams={"eng": TeamConfig(name="eng", manager="lead", workers=["worker"])},
    )


# --- resolve_boot_policy: defaults ------------------------------------------------


def test_defaults_with_empty_host_block():
    fleet = _lead_worker_fleet()
    policy = resolve_boot_policy(fleet.bots["worker"], fleet, {}, cpu_count=8)
    assert policy == BootPolicy(
        admission_slots="auto",  # F14: rendered verbatim, resolved on the host
        admission_wait_max_s=1200,  # max(1200, ceil(2/2) x 320) -- the floor wins
        priority=1,
        mcp_timeout_ms=180_000,
        ready_timeout_s=200,
        plugin_update_once_per_boot=True,
        hold_ceiling_s=320,  # 200 + 120
        boot_grace_s=1520,  # 1200 + 320 (F15: the wait plus the HOLD ceiling)
    )


def test_defaults_dict_matches_the_documented_shape():
    assert DEFAULTS == {
        "admission_slots": "auto",
        "mcp_timeout_ms": 180_000,
        "plugin_update_once_per_boot": True,
    }


# --- derive_slots: the auto clamp -------------------------------------------------


@pytest.mark.parametrize(
    "cpu_count,expected",
    [(0, 1), (1, 1), (2, 1), (3, 1), (4, 1), (8, 2), (12, 3), (64, 4), (100, 4)],
)
def test_derive_slots_clamps_auto(cpu_count, expected):
    assert derive_slots(cpu_count) == expected


def test_derive_slots_falls_back_to_one_cpu_when_unknown():
    assert derive_slots(None) == 1


# --- an explicit override wins over auto ------------------------------------------


def test_explicit_admission_slots_wins_over_auto():
    fleet = _lead_worker_fleet()
    # cpu_count=2 would derive 1 via auto; the explicit value must win outright.
    policy = resolve_boot_policy(
        fleet.bots["worker"], fleet, {"admission_slots": 7}, cpu_count=2
    )
    assert policy.admission_slots == 7


def test_explicit_admission_slots_accepts_a_numeric_string():
    fleet = _lead_worker_fleet()
    policy = resolve_boot_policy(
        fleet.bots["worker"], fleet, {"admission_slots": "3"}, cpu_count=2
    )
    assert policy.admission_slots == 3


# --- ready_timeout_s: derived, never configured (F3) ------------------------------


@pytest.mark.parametrize(
    "mcp_timeout_ms,expected",
    [
        (180_000, 200),  # default: 180 + 20 = 200
        (1_000, 90),  # floor: 1 + 20 = 21, clamped up to the 90s floor
        (300_000, 320),  # 300 + 20 = 320
    ],
)
def test_ready_timeout_s_derived_from_mcp_timeout(mcp_timeout_ms, expected):
    fleet = _lead_worker_fleet()
    policy = resolve_boot_policy(
        fleet.bots["worker"], fleet, {"mcp_timeout_ms": mcp_timeout_ms}, cpu_count=4
    )
    assert policy.ready_timeout_s == expected
    assert policy.mcp_timeout_ms == mcp_timeout_ms


# --- priority: fleet.manager_bots() decides ---------------------------------------


def test_manager_gets_priority_zero_and_worker_one():
    fleet = _lead_worker_fleet()
    assert resolve_boot_policy(fleet.bots["lead"], fleet, {}).priority == 0
    assert resolve_boot_policy(fleet.bots["worker"], fleet, {}).priority == 1


def test_bot_absent_from_fleet_is_a_worker():
    fleet = _lead_worker_fleet()
    stray = BotConfig(bot_id="stray", name="stray", expertise=[])
    assert resolve_boot_policy(stray, fleet, {}).priority == 1


# --- invalid values raise ValueError naming the key -------------------------------


@pytest.mark.parametrize(
    "key", ["admission_slots", "admission_wait_max_s", "mcp_timeout_ms"]
)
@pytest.mark.parametrize("bad_value", [-1, "-1", "soon", "12.5", 12.5, True])
def test_invalid_numeric_values_raise_naming_the_key(key, bad_value):
    fleet = _lead_worker_fleet()
    with pytest.raises(ValueError, match=key):
        resolve_boot_policy(fleet.bots["worker"], fleet, {key: bad_value}, cpu_count=4)


# --- admission_slots refuses zero: a cap of 0 can never be taken (spec §8) --------


@pytest.mark.parametrize("zero", [0, "0"])
def test_admission_slots_refuses_zero(zero):
    """With a cap of 0 no slot can ever be created and every bot queues for
    the full wait cap; the spec says the composer never emits it."""
    fleet = _lead_worker_fleet()
    with pytest.raises(ValueError, match="admission_slots"):
        resolve_boot_policy(
            fleet.bots["worker"], fleet, {"admission_slots": zero}, cpu_count=4
        )


def test_admission_wait_max_s_accepts_zero():
    """A wait cap of 0 is legal -- "never wait" -- so the generic non-negative
    rule stays for the other numeric keys; only the slot count has a floor of 1."""
    fleet = _lead_worker_fleet()
    policy = resolve_boot_policy(
        fleet.bots["worker"], fleet, {"admission_wait_max_s": 0}, cpu_count=4
    )
    assert policy.admission_wait_max_s == 0


# --- plugin_update_once_per_boot: validated, never truthiness-coerced -------------


@pytest.mark.parametrize(
    "spelling,expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("1", True),
        ("0", False),
        ("yes", True),
        ("no", False),
        (" TRUE ", True),  # case-insensitive, whitespace-stripped
        ("No", False),
    ],
)
def test_plugin_update_once_per_boot_reads_each_accepted_spelling(spelling, expected):
    fleet = _lead_worker_fleet()
    policy = resolve_boot_policy(
        fleet.bots["worker"], fleet, {"plugin_update_once_per_boot": spelling}
    )
    assert policy.plugin_update_once_per_boot is expected


def test_quoted_false_is_off_not_truthy():
    """The review's bug: bool("false") is True in Python, so a quoted "false"
    read as ON. It must read as off and render as BOOT_PLUGIN_UPDATE_ONCE=0."""
    fleet = _lead_worker_fleet()
    policy = resolve_boot_policy(
        fleet.bots["worker"], fleet, {"plugin_update_once_per_boot": "false"}
    )
    assert policy.plugin_update_once_per_boot is False
    assert "BOOT_PLUGIN_UPDATE_ONCE=0" in bot_conf_lines(policy)


@pytest.mark.parametrize("bad_value", ["maybe", 2, None])
def test_unreadable_boolean_raises_naming_the_key(bad_value):
    fleet = _lead_worker_fleet()
    with pytest.raises(ValueError, match="plugin_update_once_per_boot"):
        resolve_boot_policy(
            fleet.bots["worker"], fleet, {"plugin_update_once_per_boot": bad_value}
        )


# --- bot_conf_lines: the eight lines, fixed order ---------------------------------


def test_bot_conf_lines_fixed_order_and_export():
    policy = BootPolicy(
        admission_slots=2,
        admission_wait_max_s=1200,
        priority=0,
        mcp_timeout_ms=180_000,
        ready_timeout_s=200,
        plugin_update_once_per_boot=True,
        hold_ceiling_s=320,
        boot_grace_s=1520,
    )
    assert bot_conf_lines(policy) == [
        "BOOT_ADMISSION_SLOTS=2",
        "BOOT_ADMISSION_WAIT_MAX_S=1200",
        "BOOT_PRIORITY=0",
        "export MCP_TIMEOUT=180000",
        "RC_READY_TIMEOUT_S=200",
        "BOOT_PLUGIN_UPDATE_ONCE=1",
        "BOOT_HOLD_CEILING_S=320",
        "BOOT_GRACE_S=1520",
    ]


def test_bot_conf_lines_renders_auto_verbatim():
    """F14: `auto` reaches bot.conf as the word, because the gate resolves it
    on the host it runs on -- not as whatever the COMPOSER host derived."""
    policy = BootPolicy(
        admission_slots="auto",
        admission_wait_max_s=1200,
        priority=0,
        mcp_timeout_ms=180_000,
        ready_timeout_s=200,
        plugin_update_once_per_boot=True,
        hold_ceiling_s=320,
        boot_grace_s=1520,
    )
    assert bot_conf_lines(policy)[0] == "BOOT_ADMISSION_SLOTS=auto"


def test_bot_conf_lines_renders_false_flag_as_zero():
    policy = BootPolicy(
        admission_slots=1,
        admission_wait_max_s=1200,
        priority=1,
        mcp_timeout_ms=180_000,
        ready_timeout_s=200,
        plugin_update_once_per_boot=False,
        hold_ceiling_s=320,
        boot_grace_s=1520,
    )
    assert "BOOT_PLUGIN_UPDATE_ONCE=0" in bot_conf_lines(policy)


def test_only_mcp_timeout_line_is_exported():
    fleet = _lead_worker_fleet()
    policy = resolve_boot_policy(fleet.bots["worker"], fleet, {})
    exported = [line for line in bot_conf_lines(policy) if line.startswith("export ")]
    assert exported == ["export MCP_TIMEOUT=180000"]


# --- load_host_boot: the loader, wired to the package defaults -------------------


def test_load_host_boot_reads_the_package_defaults():
    host_boot = load_host_boot()
    assert host_boot["admission_slots"] == "auto"
    assert host_boot["mcp_timeout_ms"] == 180_000
    assert host_boot["plugin_update_once_per_boot"] is True


def test_package_system_yaml_declares_no_flat_wait_cap():
    """F13: the package must NOT name admission_wait_max_s at all, or the
    derivation below is dead code that no host ever reaches. A key present
    here and a derivation in boot.py would both look correct in review."""
    assert "admission_wait_max_s" not in load_host_boot()


def test_resolve_boot_policy_against_the_real_package_defaults():
    fleet = _lead_worker_fleet()
    policy = resolve_boot_policy(
        fleet.bots["worker"], fleet, load_host_boot(), cpu_count=4
    )
    assert policy == BootPolicy(
        admission_slots="auto",
        admission_wait_max_s=1200,  # max(1200, ceil(2/1) x 320) -- the floor wins
        priority=1,
        mcp_timeout_ms=180_000,
        ready_timeout_s=200,
        plugin_update_once_per_boot=True,
        hold_ceiling_s=320,
        boot_grace_s=1520,
    )


# --- PR B: the two derived keys (F15) ---------------------------------------------


def _fleet_of(n: int, managers: int = 1) -> FleetConfig:
    """A generic n-bot fleet -- no real fleet, bot, host or person identifier
    anywhere (public repo). Bot ids are b0..b<n-1>; the first `managers` of
    them are the team managers, which is what BootPolicy.priority reads."""
    bots = {
        f"b{i}": BotConfig(bot_id=f"b{i}", name=f"b{i}", expertise=["eng"])
        for i in range(n)
    }
    teams = {
        f"t{i}": TeamConfig(
            name=f"t{i}",
            manager=f"b{i}",
            workers=[k for k in bots if k != f"b{i}"],
        )
        for i in range(managers)
    }
    return FleetConfig(
        name="fixture-fleet", service_prefix="com.fixture", bots=bots, teams=teams
    )


@pytest.mark.parametrize(
    "mcp_timeout_ms,expected_ready,expected_ceiling",
    [(180_000, 200, 320), (0, 90, 210), (600_000, 620, 740)],
)
def test_hold_ceiling_is_ready_timeout_plus_the_restart_margin(
    mcp_timeout_ms, expected_ready, expected_ceiling
):
    """The margin is lib/rolling-restart.sh's, not a new guess: the gate's hold
    ceiling and the restart drivers' per-bot budget are the same number by
    construction."""
    fleet = _lead_worker_fleet()
    policy = resolve_boot_policy(
        fleet.bots["worker"], fleet, {"mcp_timeout_ms": mcp_timeout_ms}, cpu_count=8
    )
    assert policy.ready_timeout_s == expected_ready
    assert policy.hold_ceiling_s == expected_ceiling
    assert policy.hold_ceiling_s == policy.ready_timeout_s + HOLD_CEILING_MARGIN_S


def test_boot_grace_is_the_sum_of_the_two_phases_it_brackets():
    """F15. PR B moved the host-wide wait INSIDE ExecStart, so the phase the
    grace brackets is the admission wait PLUS the whole granted bring-up.

    The second term is the HOLD ceiling, not the readiness ceiling: the granted
    phase is budgeted at `ready_timeout_s + 120` everywhere else in PR B, so
    summing with the readiness ceiling alone left the grace 120s short of the
    phase it brackets. Asserted against `hold_ceiling_s` AND against the
    readiness ceiling it must no longer equal, because the two differ by a
    constant and an implementation that used the wrong one would still satisfy
    a single equality written loosely."""
    fleet = _fleet_of(21)
    policy = resolve_boot_policy(fleet.bots["b3"], fleet, {}, cpu_count=4)
    assert policy.boot_grace_s == policy.admission_wait_max_s + policy.hold_ceiling_s
    assert policy.boot_grace_s != policy.admission_wait_max_s + policy.ready_timeout_s


# --- PR B: the derived wait cap (F13) ---------------------------------------------


def test_wait_cap_is_derived_from_the_fleet_drain_when_the_host_names_none():
    """21 bots, cpu 4 -> derive_slots(4) == 1 slot, hold ceiling 320s:
    ceil(21/1) x 320 = 6720s, which is what the flat 1200 under-budgeted."""
    fleet = _fleet_of(21)
    policy = resolve_boot_policy(fleet.bots["b3"], fleet, {}, cpu_count=4)
    assert policy.admission_wait_max_s == 6720


def test_wait_cap_never_falls_below_the_floor():
    fleet = _fleet_of(2)
    policy = resolve_boot_policy(fleet.bots["b1"], fleet, {}, cpu_count=64)
    assert policy.admission_wait_max_s == ADMISSION_WAIT_FLOOR_S


def test_the_derived_cap_follows_the_compose_time_cpu_seam():
    """`auto` is rendered verbatim, but the composer still needs a slot count
    for the DRAIN arithmetic -- so a different cpu count must still move the
    derived cap. Without this the cpu seam would be untested once F14 stopped
    rendering it (cpu 4 -> 1 slot -> 21 rounds; cpu 64 -> 4 slots -> 6)."""
    fleet = _fleet_of(21)
    slow = resolve_boot_policy(fleet.bots["b3"], fleet, {}, cpu_count=4)
    fast = resolve_boot_policy(fleet.bots["b3"], fleet, {}, cpu_count=64)
    assert slow.admission_wait_max_s == 21 * 320
    assert fast.admission_wait_max_s == 6 * 320
    assert slow.admission_slots == fast.admission_slots == "auto"


def test_an_explicit_cap_wins_over_the_derivation():
    fleet = _fleet_of(21)
    policy = resolve_boot_policy(
        fleet.bots["b3"], fleet, {"admission_wait_max_s": 9000}, cpu_count=4
    )
    assert policy.admission_wait_max_s == 9000


def test_an_explicit_cap_shorter_than_the_drain_is_refused_naming_the_numbers():
    """A cap that cannot outlast the queue it caps expires every waiter before
    its turn -- visible to the composer, invisible to the operator."""
    fleet = _fleet_of(21)
    with pytest.raises(ValueError) as exc:
        resolve_boot_policy(
            fleet.bots["b3"], fleet, {"admission_wait_max_s": 600}, cpu_count=4
        )
    message = str(exc.value)
    assert "admission_wait_max_s" in message
    assert "600" in message and "6720" in message


def test_an_explicit_zero_cap_stays_legal_below_the_drain():
    """0 is the one legal value below the drain: it means NEVER WAIT, an
    explicit choice to proceed ungated rather than an under-budget.

    `cap: 0` is also where F15's second term is most visible, which is why the
    grace is the HOLD ceiling here: with no wait at all the grace is exactly the
    bring-up budget (320s on this fixture), where the readiness ceiling alone
    would have made it 200s -- 120s short of a boot the estate already budgets
    at 320, i.e. the watchdog un-suppressing while the bot is still legitimately
    coming up."""
    fleet = _fleet_of(21)
    policy = resolve_boot_policy(
        fleet.bots["b3"], fleet, {"admission_wait_max_s": 0}, cpu_count=4
    )
    assert policy.admission_wait_max_s == 0
    assert policy.boot_grace_s == policy.hold_ceiling_s
    assert policy.boot_grace_s == 320


def test_an_explicit_slot_count_is_what_the_drain_is_computed_against():
    """cpu_count says 1 slot; the explicit 2 must be what the drain divides
    by, or the derived cap would be budgeted against a queue the host never
    forms. ceil(21/2) x 320 = 3520, comfortably past the floor, so the
    assertion can only be satisfied by the explicit value."""
    fleet = _fleet_of(21)
    policy = resolve_boot_policy(
        fleet.bots["b3"], fleet, {"admission_slots": 2}, cpu_count=4
    )
    assert policy.admission_slots == 2
    assert policy.admission_wait_max_s == 11 * 320  # ceil(21/2)


# --- F14: the two derivations of `auto` are ONE formula ----------------------------

_BOOT_ADMISSION_SH = (
    Path(__file__).resolve().parent.parent / "lib" / "boot-admission.sh"
)

# The same spread tests/test_boot_admission.sh pins the bash side against.
_CPU_COUNTS = [1, 3, 4, 8, 12, 16, 64]


def _bash_derive_slots(cpu: int) -> str:
    """Run lib/boot-admission.sh's own clamp, in bash, on the shipped file."""
    bash = shutil.which("bash") or "/bin/bash"
    proc = subprocess.run(
        [
            bash,
            "-c",
            f'. "{_BOOT_ADMISSION_SH}"; _boot_admission_derive_slots "$1"',
            "_",
            str(cpu),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"bash derivation failed: {proc.stderr}"
    return proc.stdout.strip()


@pytest.mark.parametrize("cpu", _CPU_COUNTS)
def test_the_bash_gate_and_derive_slots_agree_on_auto(cpu):
    """F14 hands `auto` to the RUNTIME, so two implementations of one clamp now
    exist -- `claudlobby.boot.derive_slots` for the drain arithmetic and the
    refusal, and `_boot_admission_derive_slots` for the host that actually
    resolves it. A pin that only read one of them would not notice the fork;
    this one runs BOTH."""
    assert _bash_derive_slots(cpu) == str(derive_slots(cpu))


def test_the_bash_gate_floors_an_unreadable_cpu_count_the_same_way():
    """derive_slots(None) is the conservative single-core floor; the bash side
    reaches the same case through an empty or non-numeric `getconf` answer."""
    assert derive_slots(None) == 1
    assert _bash_derive_slots("") == "1"
    assert _bash_derive_slots("abc") == "1"
    assert _bash_derive_slots(0) == "1"
