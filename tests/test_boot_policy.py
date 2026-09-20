"""Tests for BootPolicy — the boot policy truth (#1573, design doc §6.1).

Covers:
- resolve_boot_policy defaults against an empty host.boot block
- derive_slots' auto clamp across a spread of cpu counts
- an explicit admission_slots override winning over auto
- ready_timeout_s derived from mcp_timeout_ms (epic fork F3)
- priority from fleet.manager_bots() (manager 0, worker 1)
- invalid host.boot values raising ValueError naming the key
- bot_conf_lines' fixed six-line render, MCP_TIMEOUT the only export
- load_host_boot() reading the package system.yaml (the loader + the
  package defaults, wired together)
"""

from __future__ import annotations

import pytest

from claudlobby.boot import (
    DEFAULTS,
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
        admission_slots=2,  # derive_slots(8)
        admission_wait_max_s=1200,
        priority=1,
        mcp_timeout_ms=180_000,
        ready_timeout_s=200,
        plugin_update_once_per_boot=True,
    )


def test_defaults_dict_matches_the_documented_shape():
    assert DEFAULTS == {
        "admission_slots": "auto",
        "admission_wait_max_s": 1200,
        "mcp_timeout_ms": 180_000,
        "plugin_update_once_per_boot": True,
    }


# --- derive_slots: the auto clamp -------------------------------------------------


@pytest.mark.parametrize(
    "cpu_count,expected",
    [(2, 1), (4, 1), (8, 2), (12, 3), (64, 4)],
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
@pytest.mark.parametrize("bad_value", [-1, "-1", "soon", "12.5", True])
def test_invalid_numeric_values_raise_naming_the_key(key, bad_value):
    fleet = _lead_worker_fleet()
    with pytest.raises(ValueError, match=key):
        resolve_boot_policy(fleet.bots["worker"], fleet, {key: bad_value}, cpu_count=4)


# --- bot_conf_lines: the six lines, fixed order -----------------------------------


def test_bot_conf_lines_fixed_order_and_export():
    policy = BootPolicy(
        admission_slots=2,
        admission_wait_max_s=1200,
        priority=0,
        mcp_timeout_ms=180_000,
        ready_timeout_s=200,
        plugin_update_once_per_boot=True,
    )
    assert bot_conf_lines(policy) == [
        "BOOT_ADMISSION_SLOTS=2",
        "BOOT_ADMISSION_WAIT_MAX_S=1200",
        "BOOT_PRIORITY=0",
        "export MCP_TIMEOUT=180000",
        "RC_READY_TIMEOUT_S=200",
        "BOOT_PLUGIN_UPDATE_ONCE=1",
    ]


def test_bot_conf_lines_renders_false_flag_as_zero():
    policy = BootPolicy(
        admission_slots=1,
        admission_wait_max_s=1200,
        priority=1,
        mcp_timeout_ms=180_000,
        ready_timeout_s=200,
        plugin_update_once_per_boot=False,
    )
    assert bot_conf_lines(policy)[-1] == "BOOT_PLUGIN_UPDATE_ONCE=0"


def test_only_mcp_timeout_line_is_exported():
    fleet = _lead_worker_fleet()
    policy = resolve_boot_policy(fleet.bots["worker"], fleet, {})
    exported = [line for line in bot_conf_lines(policy) if line.startswith("export ")]
    assert exported == ["export MCP_TIMEOUT=180000"]


# --- load_host_boot: the loader, wired to the package defaults -------------------


def test_load_host_boot_reads_the_package_defaults():
    host_boot = load_host_boot()
    assert host_boot["admission_slots"] == "auto"
    assert host_boot["admission_wait_max_s"] == 1200
    assert host_boot["mcp_timeout_ms"] == 180_000
    assert host_boot["plugin_update_once_per_boot"] is True


def test_resolve_boot_policy_against_the_real_package_defaults():
    fleet = _lead_worker_fleet()
    policy = resolve_boot_policy(
        fleet.bots["worker"], fleet, load_host_boot(), cpu_count=4
    )
    assert policy == BootPolicy(
        admission_slots=1,  # derive_slots(4)
        admission_wait_max_s=1200,
        priority=1,
        mcp_timeout_ms=180_000,
        ready_timeout_s=200,
        plugin_update_once_per_boot=True,
    )
