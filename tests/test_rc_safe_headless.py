"""RC-safe headless trim (#533 fix, items 1-2).

The July 2026 outage class: CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC (and
DISABLE_TELEMETRY) disable Claude Code's feature-flag evaluation, which
silently disables --remote-control — channel replies drop while inbound
still arrives. The composer must never emit those on any bot; the validator
must reject operator env that reintroduces them on an RC bot.
"""

from claudlobby.composer import (
    _HEADLESS_TRIM_VARS,
    _RC_KILLING_ENV_VARS,
    compose_bot_conf,
)
from claudlobby.validator import validate
from tests.conftest import MINIMAL_FLEET_YAML, load_test_fleet, make_paths


def _bot_conf(fleet_dir, bot_id="worker-1"):
    fleet = load_test_fleet(fleet_dir)
    return compose_bot_conf(fleet.bots[bot_id], fleet, make_paths(fleet_dir))


def _append_bot_yaml(fleet_dir, *lines: str) -> None:
    """Append bot-level fields to worker-1 (the fixture's last bot) — each
    line is written verbatim at bot-field indent (6 spaces)."""
    (fleet_dir / "fleet.yaml").write_text(
        MINIMAL_FLEET_YAML + "".join(f"      {ln}\n" for ln in lines)
    )


def test_trim_default_emits_granular_never_umbrella(fleet_dir):
    conf = _bot_conf(fleet_dir)
    for var in _HEADLESS_TRIM_VARS:
        assert f"export {var}=1" in conf, var
    for var in _RC_KILLING_ENV_VARS:
        assert var not in conf, f"RC-killing {var} must never be composed"


def test_trim_off_omits_all(fleet_dir):
    _append_bot_yaml(fleet_dir, "disable_nonessential_traffic: false")
    conf = _bot_conf(fleet_dir)
    for var in _HEADLESS_TRIM_VARS + _RC_KILLING_ENV_VARS:
        assert var not in conf, var


def test_validator_rejects_rc_killing_env_on_rc_bot(fleet_dir):
    for var in _RC_KILLING_ENV_VARS:
        _append_bot_yaml(fleet_dir, "env:", f'  {var}: "1"')
        fleet = load_test_fleet(fleet_dir)
        report = validate(fleet, make_paths(fleet_dir))
        assert any(
            var in e and "remote-control" in e for e in report.errors
        ), f"expected error for {var}: {report.errors}"


def test_validator_allows_rc_killing_env_on_non_rc_bot(fleet_dir):
    var = _RC_KILLING_ENV_VARS[0]
    _append_bot_yaml(
        fleet_dir,
        "remote_control: false",
        "channels: []",
        "env:",
        f'  {var}: "1"',
    )
    fleet = load_test_fleet(fleet_dir)
    # Genuinely channel-less + RC-off bot: the guard must not fire — this is
    # the validator error message's documented escape hatch.
    assert fleet.bots["worker-1"].channels == []  # explicit [] wins (presence fix)
    report = validate(fleet, make_paths(fleet_dir))
    assert not any(
        var in e and "remote-control" in e for e in report.errors
    ), report.errors
