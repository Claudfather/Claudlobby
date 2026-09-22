"""Conformance test: BootPolicy lands in bot.conf exactly once; the units
carry none of it (#1573, design doc §6.1,
`.superpowers/sdd/2026-09-20-boot-admission-pr-a-truth-and-adapter/`
task 2).

`compose_bot_conf` renders each of the eight `claudlobby.boot.bot_conf_lines`
keys exactly once with its resolved value, `MCP_TIMEOUT` in the `export`
form (the one Claude Code itself reads out of the environment); neither
`compose_systemd_unit` nor `compose_launchd_plist` carries any of it. The
systemd unit still renders its own `ExecStartPre=/bin/sleep` stagger in this
PR — retired in a later PR — so that line is deliberately not asserted
against here, only the `BOOT_`/`MCP_TIMEOUT`/`RC_READY_TIMEOUT_S` shapes.

PR B (#1573) adds the last two, `BOOT_HOLD_CEILING_S` and `BOOT_GRACE_S`,
and changes `BOOT_ADMISSION_SLOTS` to carry `auto` VERBATIM (fork F14) --
the gate resolves it on the host it runs on, so a fleet composed on one
machine and delivered to another no longer carries the composer's answer.

The eight key names are pinned as a literal list here, independent of
`claudlobby.boot`'s own vocabulary, so a rename over there shows up as a
conformance failure rather than the test quietly following it.

The value-asserting tests below monkeypatch `claudlobby.composer.load_host_boot`
(the name the composer binds -- `from .config import load_host_boot` --
not `claudlobby.config.load_host_boot`) to a FIXED dict rather than reading
the real package `system.yaml` through it (final wave item 1): this file
used to call `compose_bot_conf` with no mock at all, so its expected values
were secretly the package's `host.boot` defaults, and a legitimate change to
those defaults would fail a composer test that has nothing to do with them.
`tests/test_boot_policy.py::test_load_host_boot_reads_the_package_defaults`
stays the one place those real package defaults are asserted. The mocked
dict also carries values that DIFFER from `claudlobby.boot.DEFAULTS`
(`admission_wait_max_s`, `mcp_timeout_ms`) rather than merely matching them
under a different name: an assertion that happens to match what `{}` (no
host block at all) would ALSO produce cannot tell "the composer consulted
load_host_boot()" from "the composer silently defaulted" -- final wave item
5's mutant, `_bot_boot_policy` passing `{}` instead of `load_host_boot()`,
survives every version of this test that does not carry that distinction.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from claudlobby.boot import READY_TIMEOUT_FLOOR_S
from claudlobby.composer import (
    compose_bot_conf,
    compose_launchd_plist,
    compose_systemd_unit,
)
from claudlobby.config import BotConfig, FleetConfig, TeamConfig
from claudlobby.paths import Paths

# design doc §6.1's table, in claudlobby.boot.bot_conf_lines' render order.
BOOT_KEYS = [
    "BOOT_ADMISSION_SLOTS",
    "BOOT_ADMISSION_WAIT_MAX_S",
    "BOOT_PRIORITY",
    "MCP_TIMEOUT",
    "RC_READY_TIMEOUT_S",
    "BOOT_PLUGIN_UPDATE_ONCE",
    "BOOT_HOLD_CEILING_S",
    "BOOT_GRACE_S",
]

# Only MCP_TIMEOUT carries an `export` prefix IN THE FILE (spec §6.1: it is
# the one key Claude Code itself reads out of the environment; the rest are
# read by the launcher). That is a fact about bot.conf's text, NOT about what
# reaches the session: lib/start-bot.sh sources bot.conf under `set -a`, so
# all six land in `exec claude`'s environment as exported variables whatever
# their prefix. The prefix is what makes MCP_TIMEOUT independent of that
# sourcing convention.
EXPORTED_BOOT_KEYS = {"MCP_TIMEOUT"}

FORBIDDEN_UNIT_SUBSTRINGS = ["BOOT_", "MCP_TIMEOUT", "RC_READY_TIMEOUT_S"]

# Generic placeholder fleet — no real fleet/bot/host/person identifier
# (public repo). One manager ("lead"), two workers ("w1", "w2").
_BOT_IDS_AND_PRIORITY = [("lead", 0), ("w1", 1), ("w2", 1)]


def _fixture_fleet() -> FleetConfig:
    return FleetConfig(
        name="fixture-fleet",
        service_prefix="com.fixture",
        bots={
            "lead": BotConfig(bot_id="lead", name="lead", expertise=["orchestration"]),
            "w1": BotConfig(bot_id="w1", name="w1", expertise=["eng"]),
            "w2": BotConfig(bot_id="w2", name="w2", expertise=["eng"]),
        },
        teams={"eng": TeamConfig(name="eng", manager="lead", workers=["w1", "w2"])},
    )


def _fixture_paths(tmp_path) -> Paths:
    root = tmp_path / "claudlobby"
    for bot_id in ("lead", "w1", "w2"):
        (root / "runtime" / "bots" / bot_id).mkdir(parents=True)
    (root / "lib").mkdir()
    return Paths(root=root, fleet_dir=root)


def _pin_cpu_count(monkeypatch, count: int) -> None:
    """Pins the compose-time CPU-count seam (`claudlobby.composer._host_cpu_count`)
    so `admission_slots`' `auto` derivation is deterministic across hosts,
    rather than monkeypatching the stdlib `os.cpu_count` for every consumer
    of it. `derive_slots(8) == 2` (claudlobby/boot.py; same fixture value
    tests/test_boot_policy.py uses)."""
    import claudlobby.composer as comp

    monkeypatch.setattr(comp, "_host_cpu_count", lambda: count)


# A host.boot block that DIFFERS from claudlobby.boot.DEFAULTS on the two
# numeric keys (admission_wait_max_s, mcp_timeout_ms) -- deliberately, so a
# test asserting against it cannot be satisfied by a composer that silently
# defaulted instead of consulting load_host_boot() (final wave item 5's
# mutant: `_bot_boot_policy` passing `{}`). `admission_slots` stays `"auto"`
# so the CPU-count pin above still governs its derivation.
_MOCK_HOST_BOOT = {
    "admission_slots": "auto",
    # Above this 3-bot fixture's own drain at one slot (3 x 263 = 789s), or
    # F13 refuses it at compose time -- and distinct from the 1200 the
    # derivation would produce, so the mutant this mock exists to catch (a
    # composer passing {} instead of load_host_boot()) still shows up.
    "admission_wait_max_s": 999,
    "mcp_timeout_ms": 123_000,
    "plugin_update_once_per_boot": True,
}


def _mock_host_boot(monkeypatch) -> dict:
    """Monkeypatches `claudlobby.composer.load_host_boot` -- the name the
    composer binds, not `claudlobby.config.load_host_boot` -- to
    `_MOCK_HOST_BOOT`, decoupling this file from the real package
    `system.yaml` (final wave item 1). Returns the dict so a caller can
    compute its own expected values from the exact object the composer will
    see."""
    import claudlobby.composer as comp

    monkeypatch.setattr(comp, "load_host_boot", lambda: dict(_MOCK_HOST_BOOT))
    return _MOCK_HOST_BOOT


def _key_line(conf: str, key: str) -> list[str]:
    """Every bot.conf line whose key is `key`, matching the exact grep
    `bot_conf_get` (lib/lib-common.sh) uses at runtime: optional `export `,
    then `KEY=`."""
    pattern = re.compile(rf"^(export )?{re.escape(key)}=")
    return [line for line in conf.splitlines() if pattern.match(line)]


class TestBootPolicyInBotConf:
    def _expected_line(self, key: str, value: str) -> str:
        prefix = "export " if key in EXPORTED_BOOT_KEYS else ""
        return f"{prefix}{key}={value}"

    def _expected_values(self, priority: int) -> dict[str, str]:
        return {
            # F14: the word, not a number -- whatever this host derives.
            "BOOT_ADMISSION_SLOTS": "auto",
            "BOOT_ADMISSION_WAIT_MAX_S": "999",  # _MOCK_HOST_BOOT, not derived
            "BOOT_PRIORITY": str(priority),
            "MCP_TIMEOUT": "123000",  # _MOCK_HOST_BOOT, not DEFAULTS
            "RC_READY_TIMEOUT_S": "143",  # max(90, 123000 // 1000 + 20)
            "BOOT_PLUGIN_UPDATE_ONCE": "1",
            "BOOT_HOLD_CEILING_S": "263",  # 143 + HOLD_CEILING_MARGIN_S
            "BOOT_GRACE_S": "1262",  # 999 + 263 (F15: cap + HOLD ceiling)
        }

    @pytest.mark.parametrize("bot_id,priority", _BOT_IDS_AND_PRIORITY)
    def test_each_key_appears_exactly_once_with_resolved_value(
        self, tmp_path, monkeypatch, bot_id, priority
    ):
        _pin_cpu_count(monkeypatch, 8)
        _mock_host_boot(monkeypatch)
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)

        conf = compose_bot_conf(fleet.bots[bot_id], fleet, paths)

        expected_values = self._expected_values(priority)
        for key in BOOT_KEYS:
            lines = _key_line(conf, key)
            assert len(lines) == 1, (
                f"{key} appeared {len(lines)} time(s) in {bot_id}'s bot.conf: {lines}"
            )
            assert lines[0] == self._expected_line(key, expected_values[key])

    @pytest.mark.parametrize("bot_id,_priority", _BOT_IDS_AND_PRIORITY)
    def test_mcp_timeout_is_the_exported_form(
        self, tmp_path, monkeypatch, bot_id, _priority
    ):
        _pin_cpu_count(monkeypatch, 8)
        _mock_host_boot(monkeypatch)
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)

        conf = compose_bot_conf(fleet.bots[bot_id], fleet, paths)

        assert "export MCP_TIMEOUT=123000" in conf
        assert "\nMCP_TIMEOUT=123000\n" not in conf  # never the bare form

    @pytest.mark.parametrize("cpu", [4, 64])
    def test_admission_slots_reaches_bot_conf_as_auto_on_any_host(
        self, tmp_path, monkeypatch, cpu
    ):
        """F14 INVERTED the old contract here, so this says so rather than
        quietly following it: the composer used to bake derive_slots(cpu) into
        bot.conf, which meant a fleet composed on one machine and delivered to
        another carried the composer host's answer. `auto` now reaches the file
        verbatim and the gate resolves it where it runs -- pinned at two cpu
        counts, because a value that happened to match on one would not show
        the difference. tests/test_boot_policy.py keeps the cpu seam itself
        under test through the DERIVED wait cap, which still consumes it."""
        _pin_cpu_count(monkeypatch, cpu)
        _mock_host_boot(monkeypatch)
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)

        conf = compose_bot_conf(fleet.bots["lead"], fleet, paths)

        assert _key_line(conf, "BOOT_ADMISSION_SLOTS") == ["BOOT_ADMISSION_SLOTS=auto"]

    def test_an_explicit_slot_count_still_renders_the_integer(
        self, tmp_path, monkeypatch
    ):
        """The other half of F14: only `auto` is deferred to the host. A host
        that names a number means that number, everywhere."""
        import claudlobby.composer as comp

        _pin_cpu_count(monkeypatch, 64)
        monkeypatch.setattr(
            comp, "load_host_boot", lambda: dict(_MOCK_HOST_BOOT, admission_slots=3)
        )
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)

        conf = compose_bot_conf(fleet.bots["lead"], fleet, paths)

        assert _key_line(conf, "BOOT_ADMISSION_SLOTS") == ["BOOT_ADMISSION_SLOTS=3"]


class TestUnitsCarryNoBootPolicy:
    """Neither supervisor unit renders any boot-policy value — bot.conf is
    the one carrier both read. The systemd unit's own `ExecStartPre` stagger
    is untouched by this PR and deliberately not asserted against here."""

    @pytest.mark.parametrize("bot_id,_priority", _BOT_IDS_AND_PRIORITY)
    def test_systemd_unit_carries_none_of_it(
        self, tmp_path, monkeypatch, bot_id, _priority
    ):
        _pin_cpu_count(monkeypatch, 8)
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)

        unit = compose_systemd_unit(fleet.bots[bot_id], fleet, paths, boot_delay_s=0)

        for needle in FORBIDDEN_UNIT_SUBSTRINGS:
            assert needle not in unit, f"{needle!r} leaked into the systemd unit"

    @pytest.mark.parametrize("bot_id,_priority", _BOT_IDS_AND_PRIORITY)
    def test_launchd_plist_carries_none_of_it(
        self, tmp_path, monkeypatch, bot_id, _priority
    ):
        _pin_cpu_count(monkeypatch, 8)
        fleet = _fixture_fleet()
        paths = _fixture_paths(tmp_path)

        plist = compose_launchd_plist(fleet.bots[bot_id], fleet, paths)

        for needle in FORBIDDEN_UNIT_SUBSTRINGS:
            assert needle not in plist, f"{needle!r} leaked into the launchd plist"


# lib/start-bot.sh cannot import claudlobby.boot.READY_TIMEOUT_FLOOR_S -- bash
# has no such door -- so it names the floor literally in two places (final
# wave item 7): the `${RC_READY_TIMEOUT_S:-N}` default read at startup, and
# the `_rc_timeout_s=N` fallback a non-numeric/empty override coerces to
# under `set -u` (F4: an un-regenerated bot.conf that predates the BOOT_*
# keys still has a value to fall back on). Nothing pinned either literal to
# the Python constant it must equal -- this reads the real shipped script.
_START_BOT_SH = Path(__file__).resolve().parent.parent / "lib" / "start-bot.sh"


class TestStartBotShReadyTimeoutFloor:
    def _start_bot_sh_text(self) -> str:
        assert _START_BOT_SH.is_file(), f"missing script: {_START_BOT_SH}"
        return _START_BOT_SH.read_text()

    def test_env_var_default_matches_the_floor(self):
        text = self._start_bot_sh_text()
        match = re.search(r"RC_READY_TIMEOUT_S:-(\d+)\}", text)
        assert match, (
            "could not find ${RC_READY_TIMEOUT_S:-N} in lib/start-bot.sh"
        )
        assert int(match.group(1)) == READY_TIMEOUT_FLOOR_S

    def test_coercion_fallback_matches_the_floor(self):
        text = self._start_bot_sh_text()
        match = re.search(r"_rc_timeout_s=(\d+)\s*;;", text)
        assert match, (
            "could not find the _rc_timeout_s=N coercion fallback in lib/start-bot.sh"
        )
        assert int(match.group(1)) == READY_TIMEOUT_FLOOR_S
