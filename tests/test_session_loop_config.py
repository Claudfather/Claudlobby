"""The session-loop arming flag is strict while null still means inherit."""

import pytest
import yaml

from claudlobby.composer import compose_settings_local
from claudlobby.config import load_fleet
from claudlobby.paths import Paths


def _load(tmp_path, bot_value, default_value, *, vault=False):
    bot = {"expertise": ["eng"]}
    defaults = {}
    for target, value in ((bot, bot_value), (defaults, default_value)):
        if value is not None:
            target["claudron_session_loop"] = yaml.safe_load(value)
    if vault:
        bot["claudron_vault_path"] = str(tmp_path / "vault")
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({"fleet": {
        "name": "test", "system_defaults": False,
        "defaults": defaults, "bots": {"worker": bot},
    }}))
    return load_fleet(path)[0]


@pytest.mark.parametrize("invalid", ["'false'", "'true'", "typo", "''", "0", "1", "[]", "{}"])
@pytest.mark.parametrize("tier", ["bot", "defaults"])
def test_non_boolean_session_loop_is_rejected(tmp_path, invalid, tier):
    values = (invalid, None) if tier == "bot" else (None, invalid)
    with pytest.raises(ValueError, match="claudron_session_loop.*YAML boolean"):
        _load(tmp_path, *values)


def test_valid_bot_override_does_not_hide_invalid_default(tmp_path):
    with pytest.raises(ValueError, match="fleet defaults.*claudron_session_loop.*YAML boolean"):
        _load(tmp_path, "false", "'false'")


@pytest.mark.parametrize("bot_value,default_value,expected", [
    (None, None, None),
    ("null", "null", None),
    ("true", "false", True),
    ("false", "true", False),
    ("null", "true", True),
    ("null", "false", False),
    (None, "false", False),
])
@pytest.mark.parametrize("vault", [False, True])
def test_valid_inheritance_preserves_composed_hooks(tmp_path, bot_value, default_value, expected, vault):
    fleet = _load(tmp_path, bot_value, default_value, vault=vault)
    bot = fleet.bots["worker"]
    assert bot.claudron_session_loop is expected
    # Pure composition into a dictionary: no CLI, vault or hook is executed.
    settings = compose_settings_local(bot, fleet, Paths(root=tmp_path, fleet_dir=tmp_path))
    commands = [h["command"] for groups in settings.get("hooks", {}).values()
                for group in groups for h in group["hooks"]]
    loop_commands = [c for c in commands if " hook " in c]
    enabled = vault if expected is None else expected
    assert len(loop_commands) == (3 if enabled else 0)
    for event in ("session-start", "pre-compact", "session-end"):
        assert any(c.endswith(f"hook {event}") for c in loop_commands) is enabled
