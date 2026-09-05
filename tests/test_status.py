"""Tests for claudlobby.status — fleet health dashboard."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from claudlobby.status import (
    _SVC_UNDETERMINED,
    BotStatus,
    _check_launchd_service,
    _check_systemd_service,
    _health_indicator,
    _service_display,
    _heartbeat_display,
    _latest_heartbeats,
    _state_display,
    collect_fleet_status,
    format_bot_detail,
    format_json,
    format_table,
)
from claudlobby.utilization import load_fleet_state


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture
def mock_paths(tmp_path):
    """Create a minimal Paths-like object."""
    from claudlobby.paths import Paths

    root = tmp_path / "claudlobby"
    root.mkdir()
    (root / "library").mkdir()
    (root / "lib").mkdir()
    fleet_dir = root / "local" / "test-fleet"
    fleet_dir.mkdir(parents=True)
    runtime = fleet_dir / "runtime" / "bots"
    runtime.mkdir(parents=True)
    return Paths(root=root, fleet_dir=fleet_dir)


@pytest.fixture
def mock_fleet():
    """Minimal FleetConfig with two bots."""
    from claudlobby.config import BotConfig, FleetConfig

    return FleetConfig(
        name="test-fleet",
        service_prefix="com.test",
        bots={
            "alice": BotConfig(bot_id="alice", name="alice", expertise=["eng"]),
            "bob": BotConfig(bot_id="bob", name="bob", expertise=["eng"]),
        },
    )


class TestCheckTmuxSessions:
    def test_survives_misconfigured_bot_when_fleet_name_set(
        self, mock_fleet, mock_paths, monkeypatch
    ):
        """The SSOT resolver fail-fasts on a bot with no socket while FLEET_NAME
        is set; _check_tmux_sessions must catch that and not crash the dashboard
        (the bots simply read as not-alive)."""
        from claudlobby.status import _check_tmux_sessions

        monkeypatch.setenv("FLEET_NAME", "test-fleet")
        # alice/bob have no bot.conf → resolver raises; must be caught.
        alive = _check_tmux_sessions(mock_fleet, mock_paths)
        assert alive == set()


# -- load_fleet_state (now in utilization.py) --------------------------------


class TestLoadFleetState:
    @pytest.fixture(autouse=True)
    def _clear_state_env(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLEET_STATE_PATH", None)
            yield

    def test_missing_file(self, mock_paths):
        result = load_fleet_state(mock_paths)
        assert result == {}

    def test_valid_json(self, mock_paths):
        state_dir = mock_paths.root / "state"
        state_dir.mkdir()
        state_file = state_dir / "fleet-state.json"
        data = {
            "updated": "2026-01-01T00:00:00Z",
            "bots": {"alice": {"status": "idle"}},
        }
        state_file.write_text(json.dumps(data))
        result = load_fleet_state(mock_paths)
        assert result["bots"]["alice"]["status"] == "idle"

    def test_corrupt_json(self, mock_paths):
        state_dir = mock_paths.root / "state"
        state_dir.mkdir()
        (state_dir / "fleet-state.json").write_text("{bad json")
        result = load_fleet_state(mock_paths)
        assert result == {}

    def test_env_override(self, mock_paths, tmp_path):
        custom = tmp_path / "custom-state.json"
        data = {"bots": {"alice": {"status": "working"}}}
        custom.write_text(json.dumps(data))
        with patch.dict(os.environ, {"FLEET_STATE_PATH": str(custom)}):
            result = load_fleet_state(mock_paths)
        assert result["bots"]["alice"]["status"] == "working"


# -- _parse_keepalive_log ----------------------------------------------------


def _land_heartbeats(root, fleet: str, bot: str, states: list[str]) -> None:
    """Heartbeat samples on a plane under `root`, oldest first, as keepalive
    lands them (one a minute, ending a minute ago)."""
    from claudlobby.plane.emit_api import emit_batch

    (root / "state" / "plane").mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    n = len(states)
    out = emit_batch(root, [{"event_type": "metric_sample", "emitter": "keepalive", "fleet": fleet,
                             "occurred_at": (now - timedelta(minutes=n - i)).isoformat(),
                             "payload": {"subject_kind": "bot_instance", "subject": f"bot:{fleet}/{bot}",
                                         "metric": "bot.heartbeat", "value": {"state": st}}}
                            for i, st in enumerate(states)])
    assert all(o.status == "committed" for o in out), out


class TestLatestHeartbeats:
    """F18 closure R2b: the newest bot.heartbeat sample per bot replaces the
    keepalive.log tail (TestParseKeepaliveLog went with the file)."""

    def test_no_plane_rows(self, mock_paths):
        from tests.plane_fixtures import ro

        _land_heartbeats(mock_paths.root, "other-fleet", "zed", ["IDLE"])
        with ro(mock_paths.root) as conn:
            assert _latest_heartbeats(conn, "test-fleet") == {}          # another fleet's bot is not ours

    def test_newest_sample_wins(self, mock_paths):
        from tests.plane_fixtures import ro

        _land_heartbeats(mock_paths.root, "test-fleet", "alice", ["IDLE", "BUSY"])
        with ro(mock_paths.root) as conn:
            got = _latest_heartbeats(conn, "test-fleet")
        assert set(got) == {"alice"}
        ts, pane = got["alice"]
        assert pane == "BUSY" and ts.tzinfo is not None and (datetime.now(timezone.utc) - ts).total_seconds() < 120

    def test_case_variant_alias_is_the_same_bot(self, mock_paths):
        from tests.plane_fixtures import ro

        _land_heartbeats(mock_paths.root, "test-fleet", "ALICE", ["BUSY"])
        with ro(mock_paths.root) as conn:
            assert _latest_heartbeats(conn, "test-fleet")["alice"][1] == "BUSY"


# -- Health indicator --------------------------------------------------------


# Disable color for predictable assertions
@pytest.fixture(autouse=True)
def no_color():
    with patch("claudlobby.status._COLOR", False):
        yield


class TestHealthIndicator:
    def test_tmux_down(self):
        bs = BotStatus(name="x", tmux_alive=False, service_active=True)
        assert _health_indicator(bs) == "x"

    def test_service_down(self):
        # service_sub is explicit: the default now means "never asked", which is
        # a different state and renders differently.
        bs = BotStatus(
            name="x", tmux_alive=True, service_active=False, service_sub="dead"
        )
        assert _health_indicator(bs) == "x"

    def test_healthy(self):
        bs = BotStatus(name="x", tmux_alive=True, service_active=True, state="idle")
        assert _health_indicator(bs) == "o"

    def test_blocked(self):
        bs = BotStatus(name="x", tmux_alive=True, service_active=True, state="blocked")
        assert _health_indicator(bs) == "!"

    def test_service_undetermined_is_not_a_failure(self):
        """A supervisor that never answered must not render as a dead one."""
        bs = BotStatus(
            name="x",
            tmux_alive=True,
            service_active=False,
            service_sub=_SVC_UNDETERMINED,
            state="idle",
        )
        assert _health_indicator(bs) == "?"

    def test_tmux_down_outranks_undetermined_service(self):
        """A missing pane still reports x, even when the service did not answer.

        Note this is a scope statement, not a claim that tmux presence is always
        known: _check_tmux_sessions swallows the same timeout. Modelling tmux
        uncertainty is deliberately out of scope for #1044.
        """
        bs = BotStatus(
            name="x",
            tmux_alive=False,
            service_active=False,
            service_sub=_SVC_UNDETERMINED,
        )
        assert _health_indicator(bs) == "x"

    def test_stale_heartbeat(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=15)
        bs = BotStatus(
            name="x",
            tmux_alive=True,
            service_active=True,
            state="idle",
            last_heartbeat=old,
        )
        assert _health_indicator(bs) == "~"


# -- Service rendering (#1044) ------------------------------------------------


def _svc(sub: str, active: bool = False) -> BotStatus:
    return BotStatus(name="x", tmux_alive=True, service_active=active, service_sub=sub)


# The four things the SVC column can mean. Names are what an operator would say.
_SVC_CASES = {
    "up": _svc("running", active=True),
    "not-enrolled": _svc("not-found"),
    "undetermined": _svc(_SVC_UNDETERMINED),
    "down": _svc("dead"),
}


class TestServiceCheckSentinel:
    """The check functions must distinguish a real absence from no answer."""

    @pytest.mark.parametrize(
        "check,args",
        [
            (_check_systemd_service, ("bot", "svc")),
            (_check_launchd_service, ("bot", "svc")),
        ],
        ids=["systemd", "launchd"],
    )
    @pytest.mark.parametrize(
        "exc",
        [subprocess.TimeoutExpired(cmd="x", timeout=5), FileNotFoundError()],
        ids=["timeout", "no-binary"],
    )
    def test_no_answer_is_undetermined(self, check, args, exc):
        with patch("claudlobby.status.subprocess.run", side_effect=exc):
            assert check(*args) == (False, _SVC_UNDETERMINED)

    def test_systemd_nonzero_is_still_not_found(self):
        """A real absence keeps its own answer — this is the existing precedent."""
        with patch("claudlobby.status.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            assert _check_systemd_service("bot", "svc") == (False, "not-found")

    def test_undetermined_is_not_the_string_systemd_can_report(self):
        """systemd reports a literal SubState of 'unknown' on calls that SUCCEED.

        If the sentinel were that same string, a successful check would render
        as "we could not tell" — the inverse of this bug.
        """
        assert _SVC_UNDETERMINED != "unknown"
        with patch("claudlobby.status.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "ActiveState=failed\nSubState=unknown\n"
            active, sub = _check_systemd_service("bot", "svc")
        assert (active, sub) == (False, "unknown")
        assert _service_display(_svc(sub)) == "down"  # a real answer, rendered red

    def test_never_asked_defaults_to_undetermined(self):
        """collect_fleet_status runs no check on a non-Linux host with no label."""
        assert BotStatus(name="x").service_undetermined is True


class TestServiceDisplayStatesAreDistinct:
    """The point of the fix: undetermined must be unmistakable for up OR down."""

    def test_all_four_states_render_differently(self):
        # Explicit patch: a module-level autouse fixture forces colour OFF, so
        # without this this test would duplicate the no-colour one below.
        with patch("claudlobby.status._COLOR", True):
            rendered = {k: _service_display(bs) for k, bs in _SVC_CASES.items()}
        assert len(set(rendered.values())) == 4, rendered

    def test_all_four_states_render_differently_without_color(self):
        """Colour is not the carrier — a no-colour terminal must still separate them.

        This is the assertion that fails if "undetermined" is rendered as a
        yellow "down": identical glyphs, distinguished only by an SGR code that
        a pipe, a log file or a colour-blind reader never receives.
        """
        rendered = {k: _service_display(bs) for k, bs in _SVC_CASES.items()}
        assert len(set(rendered.values())) == 4, rendered
        assert rendered["undetermined"] == "?"


class TestUndeterminedInAggregates:
    """The summary line, table, detail view and JSON are reports too."""

    def test_summary_names_undetermined_rather_than_implying_down(self):
        statuses = [
            _svc("running", active=True),
            _svc(_SVC_UNDETERMINED),
            _svc(_SVC_UNDETERMINED),
        ]
        out = format_table(statuses, "fleet")
        # The shortfall in "1/3 up" is explained rather than left to read as down.
        assert "1/3 up" in out
        assert "2 undetermined" in out

    def test_summary_omits_the_word_when_nothing_is_undetermined(self):
        statuses = [_svc("running", active=True), _svc("dead")]
        out = format_table(statuses, "fleet")
        assert "undetermined" not in out

    def test_table_row_is_not_the_row_a_dead_unit_gets(self):
        """Sensitive to the rendering, not just to the sentinel's spelling."""
        undet = format_table([_svc(_SVC_UNDETERMINED)], "fleet")
        down = format_table([_svc("dead")], "fleet")
        assert undet != down
        assert "down" in down
        assert "down" not in undet

    def test_detail_view_distinguishes_it_from_down(self):
        undet = format_bot_detail(_svc(_SVC_UNDETERMINED))
        down = format_bot_detail(_svc("dead"))
        assert "down" in down
        assert "down" not in undet
        assert "undetermined" in undet

    def test_json_carries_an_explicit_flag(self):
        """A scripted `if not service_active` must not repeat this bug."""
        doc = json.loads(format_json([_svc(_SVC_UNDETERMINED), _svc("dead")], "fleet"))
        undet, down = doc["bots"]
        assert undet["service_undetermined"] is True
        assert down["service_undetermined"] is False
        # Both are service_active False — the flag is the only discriminator.
        assert undet["service_active"] == down["service_active"] is False


# -- Display helpers ---------------------------------------------------------


class TestStateDisplay:
    def test_idle(self):
        bs = BotStatus(name="x", state="idle")
        assert _state_display(bs) == "idle"

    def test_working(self):
        bs = BotStatus(name="x", state="working")
        assert _state_display(bs) == "working"


class TestHeartbeatDisplay:
    def test_no_heartbeat(self):
        bs = BotStatus(name="x")
        assert _heartbeat_display(bs) == "--"

    def test_recent(self):
        bs = BotStatus(
            name="x",
            last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=30),
        )
        result = _heartbeat_display(bs)
        assert "s ago" in result


# -- Format functions --------------------------------------------------------


class TestFormatTable:
    def test_empty_fleet(self):
        result = format_table([], "test")
        assert "No bots defined" in result

    def test_renders_bot_names(self):
        statuses = [
            BotStatus(name="alice", state="idle", tmux_alive=True, service_active=True),
            BotStatus(
                name="bob", state="working", tmux_alive=True, service_active=True
            ),
        ]
        result = format_table(statuses, "test-fleet")
        assert "alice" in result
        assert "bob" in result
        assert "test-fleet" in result

    def test_summary_line(self):
        statuses = [
            BotStatus(name="a", tmux_alive=True, service_active=True),
            BotStatus(name="b", tmux_alive=False, service_active=True),
        ]
        result = format_table(statuses, "t")
        assert "1/2 up" in result


class TestFormatBotDetail:
    def test_includes_name(self):
        bs = BotStatus(name="alice", state="idle", last_completed="did a thing")
        result = format_bot_detail(bs)
        assert "alice" in result
        assert "did a thing" in result


class TestFormatJson:
    def test_valid_json(self):
        statuses = [
            BotStatus(name="alice", state="idle", tmux_alive=True),
        ]
        result = format_json(statuses, "test")
        parsed = json.loads(result)
        assert parsed["fleet"] == "test"
        assert len(parsed["bots"]) == 1
        assert parsed["bots"][0]["name"] == "alice"
        assert parsed["bots"][0]["state"] == "idle"
        assert parsed["bots"][0]["tmux_alive"] is True

    def test_heartbeat_iso(self):
        ts = datetime(2026, 5, 16, 23, 0, 0, tzinfo=timezone.utc)
        statuses = [BotStatus(name="x", last_heartbeat=ts)]
        result = json.loads(format_json(statuses, "t"))
        assert result["bots"][0]["last_heartbeat"] == "2026-05-16T23:00:00+00:00"


# -- collect_fleet_status (integration-ish, mocked externals) ----------------


class TestCollectFleetStatus:
    def test_basic_collection(self, mock_fleet, mock_paths):
        """Smoke test: collection runs without error and returns all bots."""
        with (
            patch("claudlobby.status._check_tmux_sessions", return_value={"alice"}),
            patch(
                "claudlobby.status._check_systemd_service",
                return_value=(True, "exited"),
            ),
            patch("claudlobby.utilization.load_fleet_state", return_value={"bots": {}}),
        ):
            results = collect_fleet_status(mock_fleet, mock_paths)
        assert len(results) == 2
        names = {bs.name for bs in results}
        assert names == {"alice", "bob"}
        # alice has tmux
        alice = next(bs for bs in results if bs.name == "alice")
        assert alice.tmux_alive is True
        # bob does not
        bob = next(bs for bs in results if bs.name == "bob")
        assert bob.tmux_alive is False
        # no plane under this root: the recorded half is UNKNOWN on every bot,
        # said so — never rendered as a healthy blank
        assert alice.plane_unreachable and "plane" in alice.plane_unreachable
        assert _health_indicator(alice) == "?" and _heartbeat_display(alice) == "unknown"
        assert alice.busy_pct_24h is None                      # unknown, not 0%
        assert json.loads(format_json(results, "f"))["bots"][0]["plane_unreachable"]
        table = format_table(results, "test-fleet")
        assert "plane is unreachable" in table and "restore state/plane/plane.db" in table
        assert "plane unreachable" in format_bot_detail(alice)

    def test_the_plane_serves_heartbeat_pane_state_and_utilization(self, mock_fleet, mock_paths):
        """With a plane: alice's newest sample is BUSY (heartbeat + pane state),
        her series rolls up to 100% busy; bob, never recorded, is blank."""
        _land_heartbeats(mock_paths.root, "test-fleet", "alice", ["BUSY", "BUSY", "BUSY"])
        with (
            patch("claudlobby.status._check_tmux_sessions", return_value={"alice"}),
            patch("claudlobby.status._check_systemd_service", return_value=(True, "exited")),
            patch("claudlobby.utilization.load_fleet_state", return_value={"bots": {}}),
        ):
            results = collect_fleet_status(mock_fleet, mock_paths)
        alice = next(bs for bs in results if bs.name == "alice")
        bob = next(bs for bs in results if bs.name == "bob")
        assert not alice.plane_unreachable and alice.pane_state == "BUSY" and alice.last_heartbeat is not None
        assert alice.busy_pct_24h == 100.0 and alice.current_task_age_secs is not None
        assert bob.last_heartbeat is None and bob.pane_state == "" and bob.busy_pct_24h == 0.0
        assert "plane is unreachable" not in format_table(results, "test-fleet")

    def test_systemd_check_queries_bot_service_label(self, mock_fleet, mock_paths):
        """#657: on Linux the SVC check must query the BOT_SERVICE unit
        (com.<fleet>.<bot>.service) the installer names the unit after, not
        the bare bot id — otherwise every healthy bot renders SVC=down."""
        from types import SimpleNamespace

        alice_dir = mock_paths.bot_runtime("alice")
        alice_dir.mkdir(parents=True, exist_ok=True)
        (alice_dir / "bot.conf").write_text("BOT_SERVICE=com.test.alice\n")

        queried: list[list[str]] = []

        def fake_run(argv, **kwargs):
            queried.append(argv)
            return SimpleNamespace(
                returncode=0, stdout="ActiveState=active\nSubState=running\n"
            )

        with (
            patch("claudlobby.status.platform.system", return_value="Linux"),
            patch("claudlobby.status._check_tmux_sessions", return_value=set()),
            patch("claudlobby.status.subprocess.run", side_effect=fake_run),
            patch("claudlobby.utilization.load_fleet_state", return_value={"bots": {}}),
        ):
            results = collect_fleet_status(mock_fleet, mock_paths)

        units = [a for argv in queried for a in argv if a.endswith(".service")]
        assert "com.test.alice.service" in units
        assert "alice.service" not in units
        alice = next(bs for bs in results if bs.name == "alice")
        assert alice.service_active is True
