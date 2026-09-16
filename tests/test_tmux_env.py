"""Tests for .tmux-env generation and per-bot env isolation.

Validates that start-bot.sh's .tmux-env mechanism correctly propagates
all bot.conf vars, isolates identity between bots, respects tier ordering,
and keeps secrets out of bot.conf.

Ref: https://github.com/Claudfather/Claudlobby/issues/367
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


from claudlobby.config import (
    BotConfig,
    FleetConfig,
    TeamConfig,
    TelegramConfig,
)
from claudlobby.composer import compose_bot_conf
from claudlobby.paths import Paths


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=root)


def _compose_bot(
    tmp_path: Path,
    *,
    bot_id: str = "worker",
    name: str = "worker",
    telegram_handle: str = "w_bot",
    token_env: str = "TELEGRAM_TOKEN_W",
    chat_id: str = "-100999",
    teams: dict[str, TeamConfig] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[Path, str]:
    """Generate bot.conf for a bot and return (bot_dir, conf_text)."""
    root = tmp_path / "claudlobby"
    root.mkdir(exist_ok=True)
    bot_dir = root / "runtime" / "bots" / bot_id
    bot_dir.mkdir(parents=True, exist_ok=True)
    (root / "lib").mkdir(exist_ok=True)

    bot = BotConfig(
        bot_id=bot_id,
        name=name,
        expertise=["eng"],
        telegram=TelegramConfig(
            handle=telegram_handle,
            token_env=token_env,
        ),
        env=env or {},
    )
    fleet = FleetConfig(
        name="test-fleet",
        service_prefix="com.test",
        telegram_group_chat_id=chat_id,
        teams=teams or {},
    )
    paths = _make_paths(root)
    conf = compose_bot_conf(bot, fleet, paths)
    (bot_dir / "bot.conf").write_text(conf)
    return bot_dir, conf


def _build_tmux_env(bot_dir: Path, env_files: list[Path] | None = None) -> Path:
    """Build a .tmux-env file mirroring start-bot.sh's logic.

    Writes source directives for .env tier files, then sources bot.conf
    with set -a (auto-export), matching the production code path.
    """
    tmux_env = bot_dir / ".tmux-env"
    lines: list[str] = []

    # Tier 1-3: .env source directives
    for env_file in env_files or []:
        if env_file.is_file():
            lines.append(f". '{env_file}'")

    # bot.conf — auto-export all assignments (matches start-bot.sh lines 111-113)
    lines.append("set -a")
    lines.append(f". '{bot_dir / 'bot.conf'}'")
    lines.append("set +a")

    # Token indirection (matches start-bot.sh line 117)
    lines.append(
        '[ -n "${TELEGRAM_TOKEN_ENV_NAME:-}" ] && '
        'export TELEGRAM_BOT_TOKEN="${!TELEGRAM_TOKEN_ENV_NAME:-}"'
    )

    tmux_env.write_text("\n".join(lines) + "\n")
    return tmux_env


def _source_env_and_dump(tmux_env: Path) -> dict[str, str]:
    """Source .tmux-env in a bash subprocess and return its exported env."""
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail; . '{tmux_env}' && env"],
        capture_output=True,
        text=True,
        timeout=5,
        env={"HOME": str(tmux_env.parent.parent), "PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode == 0, f"source failed: {result.stderr}"
    env = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env[k] = v
    return env


# ---------------------------------------------------------------------------
# 1. Completeness — every export in bot.conf reaches the tmux session
# ---------------------------------------------------------------------------


class TestTmuxEnvCompleteness:
    """After sourcing .tmux-env, every export from bot.conf must be present."""

    def test_all_bot_conf_exports_propagate(self, tmp_path):
        bot_dir, conf = _compose_bot(tmp_path)
        tmux_env = _build_tmux_env(bot_dir)

        # Parse expected exports from bot.conf
        expected = {}
        for line in conf.splitlines():
            stripped = line.strip()
            # Match both 'export VAR=...' and bare 'VAR=...' (set -a exports both)
            if stripped.startswith("#") or not stripped or "=" not in stripped:
                continue
            decl = stripped.removeprefix("export ")
            key = decl.split("=", 1)[0].strip()
            if key and key.isidentifier():
                expected[key] = True

        env = _source_env_and_dump(tmux_env)

        missing = [k for k in expected if k not in env]
        assert not missing, f"bot.conf vars missing from tmux session: {missing}"

    def test_identity_vars_present(self, tmp_path):
        """BOT_ID, BOT_NAME, FLEET_NAME — the critical identity triple."""
        bot_dir, _ = _compose_bot(tmp_path, bot_id="alpha", name="alpha")
        tmux_env = _build_tmux_env(bot_dir)
        env = _source_env_and_dump(tmux_env)

        assert env["BOT_ID"] == "alpha"
        assert env["BOT_NAME"] == "alpha"
        assert env["FLEET_NAME"] == "test-fleet"

    def test_telegram_vars_present(self, tmp_path):
        bot_dir, _ = _compose_bot(
            tmp_path,
            telegram_handle="my_bot",
            token_env="TG_TOK_MINE",
            chat_id="-100555",
        )
        tmux_env = _build_tmux_env(bot_dir)
        env = _source_env_and_dump(tmux_env)

        assert env["TELEGRAM_BOT_HANDLE"] == "my_bot"
        assert env["TELEGRAM_TOKEN_ENV_NAME"] == "TG_TOK_MINE"
        assert env["TELEGRAM_GROUP_CHAT_ID"] == "-100555"

    def test_custom_env_vars_propagate(self, tmp_path):
        bot_dir, _ = _compose_bot(
            tmp_path, env={"MY_CUSTOM_VAR": "hello", "ANOTHER": "world"}
        )
        tmux_env = _build_tmux_env(bot_dir)
        env = _source_env_and_dump(tmux_env)

        assert env["MY_CUSTOM_VAR"] == "hello"
        assert env["ANOTHER"] == "world"

    def test_observability_vars_propagate(self, tmp_path):
        """Observability vars appear when the bot has observability config set
        (as happens after system defaults merge)."""
        from claudlobby.config import ObservabilityConfig

        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)
        bot_dir = root / "runtime" / "bots" / "worker"
        bot_dir.mkdir(parents=True, exist_ok=True)
        (root / "lib").mkdir(exist_ok=True)

        bot = BotConfig(
            bot_id="worker",
            name="worker",
            expertise=["eng"],
            telegram=TelegramConfig(handle="w_bot", token_env="TG_TOKEN"),
            observability=ObservabilityConfig(
                pulse_interval=300,
                activity_stuck_threshold=1800,
                dispatch_deadline=1800,
            ),
        )
        fleet = FleetConfig(
            name="test-fleet",
            service_prefix="com.test",
            telegram_group_chat_id="-100999",
        )
        paths = _make_paths(root)
        conf = compose_bot_conf(bot, fleet, paths)
        (bot_dir / "bot.conf").write_text(conf)

        tmux_env = _build_tmux_env(bot_dir)
        env = _source_env_and_dump(tmux_env)

        for var in (
            "OBSERVABILITY_PULSE_INTERVAL",
            "OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD",
            "OBSERVABILITY_DISPATCH_DEADLINE",
        ):
            assert var in env, f"{var} missing from tmux session env"


# ---------------------------------------------------------------------------
# 2. Per-bot isolation — no env bleed between bots
# ---------------------------------------------------------------------------


class TestPerBotIsolation:
    """Two bots with different identities must not leak vars to each other."""

    def _make_two_bots(self, tmp_path):
        bot_a_dir, _ = _compose_bot(
            tmp_path,
            bot_id="alex",
            name="alex",
            telegram_handle="alex_bot",
            token_env="TG_TOKEN_ALEX",
            chat_id="-100111",
        )

        # Second bot needs its own subtree to avoid path collisions.
        bot_b_dir, _ = _compose_bot(
            tmp_path,
            bot_id="branden",
            name="branden",
            telegram_handle="branden_bot",
            token_env="TG_TOKEN_BRANDEN",
            chat_id="-100222",
        )

        env_a = _build_tmux_env(bot_a_dir)
        env_b = _build_tmux_env(bot_b_dir)
        return env_a, env_b

    def test_bot_ids_differ(self, tmp_path):
        env_a, env_b = self._make_two_bots(tmp_path)
        a = _source_env_and_dump(env_a)
        b = _source_env_and_dump(env_b)

        assert a["BOT_ID"] == "alex"
        assert b["BOT_ID"] == "branden"
        assert a["BOT_ID"] != b["BOT_ID"]

    def test_telegram_handles_differ(self, tmp_path):
        env_a, env_b = self._make_two_bots(tmp_path)
        a = _source_env_and_dump(env_a)
        b = _source_env_and_dump(env_b)

        assert a["TELEGRAM_BOT_HANDLE"] == "alex_bot"
        assert b["TELEGRAM_BOT_HANDLE"] == "branden_bot"

    def test_token_env_names_differ(self, tmp_path):
        env_a, env_b = self._make_two_bots(tmp_path)
        a = _source_env_and_dump(env_a)
        b = _source_env_and_dump(env_b)

        assert a["TELEGRAM_TOKEN_ENV_NAME"] == "TG_TOKEN_ALEX"
        assert b["TELEGRAM_TOKEN_ENV_NAME"] == "TG_TOKEN_BRANDEN"

    def test_chat_ids_differ(self, tmp_path):
        env_a, env_b = self._make_two_bots(tmp_path)
        a = _source_env_and_dump(env_a)
        b = _source_env_and_dump(env_b)

        assert a["TELEGRAM_GROUP_CHAT_ID"] == "-100111"
        assert b["TELEGRAM_GROUP_CHAT_ID"] == "-100222"

    def test_bot_dirs_differ(self, tmp_path):
        env_a, env_b = self._make_two_bots(tmp_path)
        a = _source_env_and_dump(env_a)
        b = _source_env_and_dump(env_b)

        assert "alex" in a["BOT_DIR"]
        assert "branden" in b["BOT_DIR"]
        assert a["BOT_DIR"] != b["BOT_DIR"]

    def test_no_cross_contamination(self, tmp_path):
        """No alex-specific value appears in branden's env and vice versa."""
        env_a, env_b = self._make_two_bots(tmp_path)
        a = _source_env_and_dump(env_a)
        b = _source_env_and_dump(env_b)

        # alex's identity strings must not appear in branden's env values
        alex_markers = {"alex", "alex_bot", "TG_TOKEN_ALEX", "-100111"}
        branden_markers = {"branden", "branden_bot", "TG_TOKEN_BRANDEN", "-100222"}

        for key, val in b.items():
            for marker in alex_markers:
                assert marker not in val, (
                    f"alex marker {marker!r} leaked into branden's {key}={val!r}"
                )

        for key, val in a.items():
            for marker in branden_markers:
                assert marker not in val, (
                    f"branden marker {marker!r} leaked into alex's {key}={val!r}"
                )


# ---------------------------------------------------------------------------
# 3. Tier ordering — bot.conf (via set -a) overrides .env tier values
# ---------------------------------------------------------------------------


class TestTierOrdering:
    """If a var appears in .env and bot.conf, bot.conf wins because it's
    sourced after .env files in .tmux-env (with set -a)."""

    def test_bot_conf_overrides_env_file(self, tmp_path):
        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)

        # Fleet .env sets FLEET_NAME to a wrong value
        fleet_env = root / ".env"
        fleet_env.write_text('export FLEET_NAME="wrong-fleet"\n')

        # bot.conf (generated by compositor) sets FLEET_NAME correctly
        bot_dir, _ = _compose_bot(tmp_path)

        # .tmux-env sources .env first, then bot.conf — bot.conf wins
        tmux_env = _build_tmux_env(bot_dir, env_files=[fleet_env])
        env = _source_env_and_dump(tmux_env)

        assert env["FLEET_NAME"] == "test-fleet", (
            "bot.conf must override .env tier — got FLEET_NAME="
            + env.get("FLEET_NAME", "<missing>")
        )

    def test_bot_env_overrides_fleet_env(self, tmp_path):
        """Bot-tier .env overrides fleet-tier .env for the same key."""
        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)

        fleet_env = root / "fleet.env"
        fleet_env.write_text('export SHARED_SECRET="fleet_value"\n')

        bot_dir, _ = _compose_bot(tmp_path)
        bot_env = bot_dir / ".env"
        bot_env.write_text('export SHARED_SECRET="bot_value"\n')

        # Fleet env first, then bot env, then bot.conf
        tmux_env = _build_tmux_env(bot_dir, env_files=[fleet_env, bot_env])
        env = _source_env_and_dump(tmux_env)

        assert env["SHARED_SECRET"] == "bot_value"

    def test_env_files_source_before_bot_conf(self, tmp_path):
        """Vars from .env that are NOT in bot.conf still appear in the session."""
        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)

        fleet_env = root / "fleet.env"
        fleet_env.write_text('export EXTRA_FROM_ENV="present"\n')

        bot_dir, _ = _compose_bot(tmp_path)
        tmux_env = _build_tmux_env(bot_dir, env_files=[fleet_env])
        env = _source_env_and_dump(tmux_env)

        assert env.get("EXTRA_FROM_ENV") == "present"


# ---------------------------------------------------------------------------
# 4. MANAGER_TMUX regression — must be present for workers
# ---------------------------------------------------------------------------


class TestManagerTmux:
    """MANAGER_TMUX absence broke report-back for a month. Regression gate."""

    def test_worker_has_manager_tmux(self, tmp_path):
        teams = {
            "eng": TeamConfig(name="eng", manager="ari", workers=["worker"]),
        }
        bot_dir, conf = _compose_bot(tmp_path, teams=teams)

        # Verify it's in the raw bot.conf text
        assert "MANAGER_TMUX" in conf, "MANAGER_TMUX missing from composed bot.conf"

        # Verify it survives sourcing
        tmux_env = _build_tmux_env(bot_dir)
        env = _source_env_and_dump(tmux_env)
        assert env.get("MANAGER_TMUX") == "ari", (
            f"MANAGER_TMUX should be 'ari', got {env.get('MANAGER_TMUX')!r}"
        )

    def test_manager_has_self_as_manager_tmux(self, tmp_path):
        """Managers set MANAGER_TMUX to their own bot_id."""
        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)
        bot_dir = root / "runtime" / "bots" / "ari"
        bot_dir.mkdir(parents=True)
        (root / "lib").mkdir(exist_ok=True)

        teams = {
            "eng": TeamConfig(name="eng", manager="ari", workers=["worker"]),
        }
        bot = BotConfig(
            bot_id="ari",
            name="ari",
            expertise=["orchestration"],
            telegram=TelegramConfig(handle="ari_bot", token_env="TG_TOK_ARI"),
        )
        fleet = FleetConfig(
            name="test-fleet",
            service_prefix="com.test",
            telegram_group_chat_id="-100999",
            teams=teams,
        )
        paths = _make_paths(root)
        conf = compose_bot_conf(bot, fleet, paths)
        (bot_dir / "bot.conf").write_text(conf)

        tmux_env = _build_tmux_env(bot_dir)
        env = _source_env_and_dump(tmux_env)
        assert env.get("MANAGER_TMUX") == "ari"

    def test_bot_without_team_has_no_manager_tmux(self, tmp_path):
        """A bot not in any team should not have MANAGER_TMUX set."""
        bot_dir, conf = _compose_bot(tmp_path, teams={})

        assert "MANAGER_TMUX" not in conf


# ---------------------------------------------------------------------------
# 5. No secrets in bot.conf — tokens come from .env tier, not compositor
# ---------------------------------------------------------------------------


class TestNoSecretsInBotConf:
    """bot.conf must contain the indirection var name, never the actual token."""

    def test_token_env_name_present(self, tmp_path):
        _, conf = _compose_bot(tmp_path, token_env="MY_SECRET_TOKEN_VAR")
        assert "TELEGRAM_TOKEN_ENV_NAME" in conf
        assert "'MY_SECRET_TOKEN_VAR'" in conf or "MY_SECRET_TOKEN_VAR" in conf

    def test_no_raw_telegram_bot_token(self, tmp_path):
        """TELEGRAM_BOT_TOKEN must not appear as an export in bot.conf."""
        _, conf = _compose_bot(tmp_path)
        for line in conf.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not stripped.startswith("export TELEGRAM_BOT_TOKEN="), (
                "bot.conf must not export TELEGRAM_BOT_TOKEN directly — "
                "tokens belong in .env, resolved via TELEGRAM_TOKEN_ENV_NAME"
            )
            assert not stripped.startswith("TELEGRAM_BOT_TOKEN="), (
                "bot.conf must not set TELEGRAM_BOT_TOKEN — "
                "tokens belong in .env, resolved via TELEGRAM_TOKEN_ENV_NAME"
            )

    def test_token_indirection_resolves_at_source_time(self, tmp_path):
        """When .env provides the token and bot.conf names the indirection
        var, sourcing .tmux-env resolves TELEGRAM_BOT_TOKEN correctly."""
        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)

        # Simulate fleet .env with the actual token
        fleet_env = root / "fleet.env"
        fleet_env.write_text('export TG_SECRET="tok_abc123"\n')

        bot_dir, _ = _compose_bot(tmp_path, token_env="TG_SECRET")
        tmux_env = _build_tmux_env(bot_dir, env_files=[fleet_env])
        env = _source_env_and_dump(tmux_env)

        assert env.get("TELEGRAM_BOT_TOKEN") == "tok_abc123", (
            "Token indirection failed — TELEGRAM_BOT_TOKEN should resolve "
            f"to 'tok_abc123', got {env.get('TELEGRAM_BOT_TOKEN')!r}"
        )
        # The indirection var name should also be present
        assert env.get("TELEGRAM_TOKEN_ENV_NAME") == "TG_SECRET"

    def test_token_indirection_per_bot(self, tmp_path):
        """Two bots with different token_env names resolve different tokens."""
        root = tmp_path / "claudlobby"
        root.mkdir(exist_ok=True)

        fleet_env = root / "fleet.env"
        fleet_env.write_text(
            'export TG_TOK_A="token_for_alex"\nexport TG_TOK_B="token_for_branden"\n'
        )

        bot_a_dir, _ = _compose_bot(
            tmp_path,
            bot_id="alex",
            name="alex",
            telegram_handle="alex_bot",
            token_env="TG_TOK_A",
        )
        bot_b_dir, _ = _compose_bot(
            tmp_path,
            bot_id="branden",
            name="branden",
            telegram_handle="branden_bot",
            token_env="TG_TOK_B",
        )

        env_a = _source_env_and_dump(_build_tmux_env(bot_a_dir, env_files=[fleet_env]))
        env_b = _source_env_and_dump(_build_tmux_env(bot_b_dir, env_files=[fleet_env]))

        assert env_a["TELEGRAM_BOT_TOKEN"] == "token_for_alex"
        assert env_b["TELEGRAM_BOT_TOKEN"] == "token_for_branden"
        assert env_a["TELEGRAM_BOT_TOKEN"] != env_b["TELEGRAM_BOT_TOKEN"]
