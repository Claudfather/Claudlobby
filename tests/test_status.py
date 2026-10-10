"""Tests for claudlobby.status — fleet health dashboard."""

from __future__ import annotations

from tests.plane_setup import initialize_plane

from pathlib import Path

import json
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
    _tmux_display,
    collect_fleet_status,
    format_bot_detail,
    format_json,
    format_table,
)

REPO = Path(__file__).resolve().parent.parent


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture
def mock_paths(tmp_path):
    """Create a minimal Paths-like object."""
    from tests.package_fixtures import source_package
    from claudlobby.paths import Paths

    root = tmp_path / "claudlobby"
    root.mkdir()
    (root / "library").mkdir()
    (root / "lib").symlink_to(REPO / "claudlobby/_runtime_scripts")   # the install's claudlobby/_runtime_scripts/: every reader rides the matcher's session
    fleet_dir = root / "local" / "test-fleet"
    fleet_dir.mkdir(parents=True)
    runtime = fleet_dir / "runtime" / "bots"
    runtime.mkdir(parents=True)
    return Paths(root=root, fleet_dir=fleet_dir, package=source_package())


@pytest.fixture
def mock_fleet():
    """Minimal FleetConfig with two bots."""
    from claudlobby.config import BotConfig, FleetConfig

    return FleetConfig(
        name="test-fleet",
        manager="bob",
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


# -- _parse_keepalive_log ----------------------------------------------------


def _land_heartbeats(root, fleet: str, bot: str, states: list[str]) -> None:
    """Heartbeat samples on a plane under `root`, oldest first, as keepalive
    lands them (one a minute, ending a minute ago)."""
    from claudlobby.plane.emit_api import emit_batch

    (root / "state" / "plane").mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    n = len(states)
    initialize_plane(root)
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

    def test_newest_wins_across_case_variant_aliases(self, mock_paths):
        """`bot:F/ALEX` after `bot:F/alex` mints a second instance; the loop
        once let the query's LAST row win, so an older sample overwrote a newer
        one (the R2b-1 adversarial lens). The query yields the two instances in
        uid order — a hash, unknowable in advance — so the pin first learns
        which alias the query yields LAST and then lands the OLDER sample under
        it: last-row-wins must pick the stale state, newest-by-occurred the
        live one."""
        from claudlobby.plane.emit_api import emit_batch
        from claudlobby.plane.queries import LATEST_HEARTBEAT_SQL
        from tests.plane_fixtures import ro

        (mock_paths.root / "state" / "plane").mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)

        def _sample(bot, state, minutes_ago):
            return {"event_type": "metric_sample", "emitter": "keepalive", "fleet": "test-fleet",
                    "occurred_at": (now - timedelta(minutes=minutes_ago)).isoformat(),
                    "payload": {"subject_kind": "bot_instance", "subject": f"bot:test-fleet/{bot}",
                                "metric": "bot.heartbeat", "value": {"state": state}}}

        initialize_plane(mock_paths.root)
        out = emit_batch(mock_paths.root, [_sample("alex", "UNKNOWN", 30), _sample("ALEX", "UNKNOWN", 30)])
        assert all(o.status == "committed" for o in out), out
        with ro(mock_paths.root) as conn:
            order = [r["alias"].split("/")[-1] for r in conn.execute(LATEST_HEARTBEAT_SQL)
                     if r["alias"].startswith("bot:test-fleet/")]
        assert sorted(order) == ["ALEX", "alex"], order
        first, last = order
        out = emit_batch(mock_paths.root, [_sample(first, "BUSY", 1), _sample(last, "IDLE", 5)])
        assert all(o.status == "committed" for o in out), out
        with ro(mock_paths.root) as conn:
            got = _latest_heartbeats(conn, "test-fleet")
        assert set(got) == {"alex"} and got["alex"][1] == "BUSY", (order, got)

    def test_state_and_tmux_cannot_diverge_on_a_case_variant_collision(self, mock_paths):
        """#1615 review (vera): STATE and TMUX both read heartbeats now, so the
        PR's whole claim is that they cannot disagree. They could. TMUX picked
        the newest sample across case-variant aliases (the R2b-1 fix above);
        the presence path collapsed `{alias.lower(): ...}` and kept whichever
        `derive_presence`'s `sorted()` visited LAST -- an artifact of ASCII
        case, not recency. Measured divergence: TMUX=idle (correct) while
        STATE=working off a 5-minute-stale BUSY sample.

        Same construction as the sibling pin: learn which alias the query
        yields last, land the OLDER sample under it, so an order-dependent
        collapse must pick the stale one and a recency-based collapse the live
        one. Both readers are asserted, because the bug was that they differed.

        SCOPE, because a commit message once claimed more than this test does:
        it calls `derive_presence` directly and hands it a live list that is
        ALREADY lower-cased, so it pins the RECORDED half only. The live half
        is built inside `collect_fleet_status`, and reverting that lowering
        leaves this test green. `TestCollectFleetStatus::test_a_manifest_case_
        variant_cannot_diverge_state_from_tmux` is the pin for it.
        """
        from claudlobby.plane.emit_api import emit_batch
        from claudlobby.plane.queries import LATEST_HEARTBEAT_SQL
        from claudlobby.plane.presence import derive_presence
        from claudlobby.status import _presence_rows
        from tests.plane_fixtures import ro

        (mock_paths.root / "state" / "plane").mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)

        def _sample(bot, state, minutes_ago):
            return {"event_type": "metric_sample", "emitter": "keepalive", "fleet": "test-fleet",
                    "occurred_at": (now - timedelta(minutes=minutes_ago)).isoformat(),
                    "payload": {"subject_kind": "bot_instance", "subject": f"bot:test-fleet/{bot}",
                                "metric": "bot.heartbeat", "value": {"state": state}}}

        initialize_plane(mock_paths.root)
        emit_batch(mock_paths.root, [_sample("alex", "UNKNOWN", 30), _sample("ALEX", "UNKNOWN", 30)])
        with ro(mock_paths.root) as conn:
            order = [r["alias"].split("/")[-1] for r in conn.execute(LATEST_HEARTBEAT_SQL)
                     if r["alias"].startswith("bot:test-fleet/")]
        assert sorted(order) == ["ALEX", "alex"], order
        first, last = order
        emit_batch(mock_paths.root, [_sample(first, "BUSY", 1), _sample(last, "IDLE", 5)])

        with ro(mock_paths.root) as conn:
            tmux = _latest_heartbeats(conn, "test-fleet")
            rows = _presence_rows(conn, "test-fleet")

        # ONE row per bot: derive_presence can no longer emit two Presence
        # entries that a later case-folding collapse has to choose between.
        assert len(rows) == 1, rows
        live = [{"fleet": "test-fleet", "bot": "alex", "status": "up"}]
        pres = {pz.alias.lower(): pz for pz in derive_presence(rows, live, now=now)}
        verdict = pres["bot:test-fleet/alex"].presence

        assert tmux["alex"][1] == "BUSY", tmux          # TMUX: newest wins
        assert verdict == "working", (verdict, rows)    # STATE: the SAME sample
        assert (verdict == "working") == (tmux["alex"][1] == "BUSY")   # and they agree

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


class TestTmuxDisplay:
    def test_held(self):
        # #2070: a box holding unsubmitted text is named, not shown as plain "up".
        bs = BotStatus(name="x", tmux_alive=True, pane_state="HELD")
        assert "held" in _tmux_display(bs)

    def test_busy_and_idle_unchanged(self):
        assert "busy" in _tmux_display(BotStatus(name="x", tmux_alive=True, pane_state="BUSY"))
        assert _tmux_display(BotStatus(name="x", tmux_alive=True, pane_state="IDLE")) == "idle"


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

    def test_one_bot_probes_and_reads_only_that_bot(self, mock_fleet, mock_paths):
        """`bot status B` once probed every bot's tmux/service and read the
        heartbeats twice; now B's probes alone, one heartbeat read, B's row."""
        import subprocess
        import claudlobby.status as status_mod

        _land_heartbeats(mock_paths.root, "test-fleet", "alice", ["BUSY"])
        _land_heartbeats(mock_paths.root, "test-fleet", "bob", ["IDLE", "BUSY"])
        reads, probes = [], []
        real_rows, real_run = status_mod._newest_heartbeat_rows, subprocess.run

        def _spy(conn, fleet_name, names=None):
            got = real_rows(conn, fleet_name, names)
            reads.append(set(got))
            return got

        def _run(argv, *a, **kw):              # the native probes, recorded at the process boundary
            if argv and argv[0] in ("tmux", "systemctl", "launchctl"):
                probes.append(argv)
                return subprocess.CompletedProcess(
                    argv, 0, stdout="ActiveState=active\nSubState=running\n", stderr="")
            return real_run(argv, *a, **kw)
        with (
            patch("claudlobby.status.subprocess.run", _run),
            patch("claudlobby.status.platform.system", return_value="Linux"),
            patch("claudlobby.status._newest_heartbeat_rows", _spy),
        ):
            results = collect_fleet_status(mock_fleet, mock_paths, only="bob")
        assert [bs.name for bs in results] == ["bob"]
        assert {p[0] for p in probes} == {"tmux", "systemctl"}, probes
        assert all("bob" in " ".join(p) and "alice" not in " ".join(p) for p in probes), probes
        assert reads == [{"bob"}]                       # ONE read; only bob's row kept
        assert results[0].tmux_alive and results[0].service_active
        assert results[0].pane_state == "BUSY" and results[0].state == "working"
        # the scoped series is exactly the fleet read's bob, matched case-insensitively
        from claudlobby.utilization import fleet_heartbeat_series
        from tests.plane_fixtures import ro
        now = datetime.now(timezone.utc)
        with ro(mock_paths.root) as conn:
            full = fleet_heartbeat_series(conn, "test-fleet", now)
            one = fleet_heartbeat_series(conn, "test-fleet", now, "BOB")
        assert set(full) == {"alice", "bob"} and one == {"bob": full["bob"]}

    def test_the_plane_serves_heartbeat_pane_state_and_utilization(self, mock_fleet, mock_paths):
        """With a plane: alice's newest sample is BUSY (heartbeat + pane state),
        her series rolls up to 100% busy; bob, never recorded, is blank."""
        _land_heartbeats(mock_paths.root, "test-fleet", "alice", ["BUSY", "BUSY", "BUSY"])
        with (
            patch("claudlobby.status._check_tmux_sessions", return_value={"alice"}),
            patch("claudlobby.status._check_systemd_service", return_value=(True, "exited")),
        ):
            results = collect_fleet_status(mock_fleet, mock_paths)
        alice = next(bs for bs in results if bs.name == "alice")
        bob = next(bs for bs in results if bs.name == "bob")
        assert not alice.plane_unreachable and alice.pane_state == "BUSY" and alice.last_heartbeat is not None
        assert alice.busy_pct_24h == 100.0 and alice.busy_age_secs is not None
        assert "no actor identity" in alice.work_unavailable
        assert bob.last_heartbeat is None and bob.pane_state == "" and bob.busy_pct_24h == 0.0
        assert "plane is unreachable" not in format_table(results, "test-fleet")

    def test_state_comes_from_the_plane_not_the_file(self, mock_fleet, mock_paths):
        """#1615: STATE and TMUX must answer the SAME question. The file's
        status is the last REPORT's word, frozen until the next report; the
        pane verdict is live. Measured on the estate before this fix: 3 of 4
        rows carried a STATE the pane contradicted, every one of them the
        direction that gets a working bot injected into."""
        _land_heartbeats(mock_paths.root, "test-fleet", "alice", ["BUSY", "BUSY"])
        (mock_paths.root / "state" / "fleet-state.json").write_text(
            '{"bots":{"alice":{"status":"idle","current_task":"stale task"}}}')
        with (
            patch("claudlobby.status._check_tmux_sessions", return_value={"alice"}),
            patch("claudlobby.status._check_systemd_service", return_value=(True, "exited")),
        ):
            results = collect_fleet_status(mock_fleet, mock_paths)
        alice = next(bs for bs in results if bs.name == "alice")
        # the file says idle; the pane says BUSY. The pane wins.
        assert alice.state == "working", alice.state
        assert alice.pane_state == "BUSY"
        assert alice.current_task is None

    def test_a_stale_working_in_the_file_does_not_survive_an_idle_pane(self, mock_fleet, mock_paths):
        """The inverse direction, and the one that makes a finished bot look
        busy: `ravi` rendered STATE=working off a report while his pane had
        already gone idle."""
        _land_heartbeats(mock_paths.root, "test-fleet", "alice", ["IDLE", "IDLE"])
        (mock_paths.root / "state" / "fleet-state.json").write_text(
            '{"bots":{"alice":{"status":"working","current_task":"stale task"}}}')
        with (
            patch("claudlobby.status._check_tmux_sessions", return_value={"alice"}),
            patch("claudlobby.status._check_systemd_service", return_value=(True, "exited")),
        ):
            results = collect_fleet_status(mock_fleet, mock_paths)
        alice = next(bs for bs in results if bs.name == "alice")
        assert alice.state == "idle", alice.state
        assert alice.current_task is None

    def test_canonical_assignment_block_and_return_are_distinct_from_presence(self, mock_fleet, mock_paths):
        """A queued task is fleet intake; only its current assignment is bot work."""
        from claudlobby.plane.db import connect, db_file
        from claudlobby.plane.identity import resolve_party
        from tests.test_task_state import _insert

        _land_heartbeats(mock_paths.root, "test-fleet", "alice", ["BUSY", "BUSY"])
        (mock_paths.root / "state" / "fleet-state.json").write_text(
            '{"bots":{"alice":{"status":"blocked","current_task":"stale task"}}}')
        conn = connect(db_file(mock_paths.root))
        fleet_uid = conn.execute("SELECT uid FROM identity_registry WHERE kind='fleet'"
                                 " AND alias='test-fleet'").fetchone()[0]
        # The legacy ingest party resolver did not set parent_uid on actors.
        alice_uid = resolve_party(conn, "bot:test-fleet/alice", now="2026-09-28T00:00:00Z")
        _insert(conn, "work_items", fleet_uid=fleet_uid, work_item_id="wi_canonical",
                title="Canonical work", created_by_uid=alice_uid)
        with (
            patch("claudlobby.status._check_tmux_sessions", return_value={"alice"}),
            patch("claudlobby.status._check_systemd_service", return_value=(True, "exited")),
        ):
            queued = next(bs for bs in collect_fleet_status(mock_fleet, mock_paths)
                          if bs.name == "alice")
        assert queued.state == "working" and queued.current_task is None
        assert queued.work_assignments == ()
        assert not queued.work_unavailable

        _insert(conn, "assignments", fleet_uid=fleet_uid, assignment_id="asg_canonical",
                work_item_id="wi_canonical", assignee_uid=alice_uid, assigned_by_uid=alice_uid)
        _insert(conn, "events", fleet_uid=fleet_uid, kind="task", work_item_id="wi_canonical",
                assignment_id="asg_canonical", event="blocked_waiting")
        with (
            patch("claudlobby.status._check_tmux_sessions", return_value={"alice"}),
            patch("claudlobby.status._check_systemd_service", return_value=(True, "exited")),
        ):
            busy = next(bs for bs in collect_fleet_status(mock_fleet, mock_paths)
                        if bs.name == "alice")
        assert busy.state == "working" and busy.current_task == "Canonical work"
        assert [(a.task_id, a.assignment_id, a.state) for a in busy.work_assignments] == [
            ("wi_canonical", "asg_canonical", "blocked")]

        _land_heartbeats(mock_paths.root, "test-fleet", "alice", ["IDLE", "IDLE"])
        with (
            patch("claudlobby.status._check_tmux_sessions", return_value={"alice"}),
            patch("claudlobby.status._check_systemd_service", return_value=(True, "exited")),
        ):
            idle = next(bs for bs in collect_fleet_status(mock_fleet, mock_paths)
                        if bs.name == "alice")
        assert idle.state == "blocked" and idle.work_assignments[0].state == "blocked"

        _insert(conn, "events", fleet_uid=fleet_uid, kind="task", work_item_id="wi_canonical",
                assignment_id="asg_canonical", event="returned_blocked")
        with (
            patch("claudlobby.status._check_tmux_sessions", return_value={"alice"}),
            patch("claudlobby.status._check_systemd_service", return_value=(True, "exited")),
        ):
            returned = next(bs for bs in collect_fleet_status(mock_fleet, mock_paths)
                            if bs.name == "alice")
        assert returned.state == "idle" and returned.current_task is None
        assert returned.work_assignments == ()

        _insert(conn, "work_items", fleet_uid=fleet_uid, work_item_id="wi_done",
                title="Completed work", created_by_uid=alice_uid)
        _insert(conn, "assignments", fleet_uid=fleet_uid, assignment_id="asg_done",
                work_item_id="wi_done", assignee_uid=alice_uid, assigned_by_uid=alice_uid)
        _insert(conn, "events", fleet_uid=fleet_uid, kind="task", work_item_id="wi_done",
                assignment_id="asg_done", event="completed")
        with (
            patch("claudlobby.status._check_tmux_sessions", return_value={"alice"}),
            patch("claudlobby.status._check_systemd_service", return_value=(True, "exited")),
        ):
            done = next(bs for bs in collect_fleet_status(mock_fleet, mock_paths)
                        if bs.name == "alice")
        assert done.current_task is None and done.last_completed == "Completed work"

        _insert(conn, "events", fleet_uid=fleet_uid, kind="task", work_item_id="wi_canonical",
                assignment_id="asg_missing", event="progress")
        conn.close()
        with (
            patch("claudlobby.status._check_tmux_sessions", return_value={"alice"}),
            patch("claudlobby.status._check_systemd_service", return_value=(True, "exited")),
        ):
            unresolved = next(bs for bs in collect_fleet_status(mock_fleet, mock_paths)
                              if bs.name == "alice")
        assert any(issue["code"] == "dangling_task_event" for issue in unresolved.work_issues)
        assert unresolved.work_unavailable and unresolved.work_unresolved
        assert "work unresolved" in format_table([unresolved], "test-fleet")

    def test_plane_unavailable_never_resurrects_stale_file_work(self, mock_fleet, mock_paths):
        """An unavailable canonical read is unknown even if a stale file exists."""
        state = mock_paths.root / "state"
        state.mkdir()
        (state / "fleet-state.json").write_text(
            '{"bots":{"alice":{"status":"working","current_task":"stale task"}}}')
        with (
            patch("claudlobby.status._check_tmux_sessions", return_value={"alice"}),
            patch("claudlobby.status._check_systemd_service", return_value=(True, "exited")),
        ):
            results = collect_fleet_status(mock_fleet, mock_paths)
        alice = next(bs for bs in results if bs.name == "alice")
        assert alice.plane_unreachable
        assert alice.state == "unknown" and alice.current_task is None
        assert alice.work_unavailable == alice.plane_unreachable
        assert json.loads(format_json(results, "test-fleet"))["bots"][0]["work_unavailable"]

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
        ):
            results = collect_fleet_status(mock_fleet, mock_paths)

        units = [a for argv in queried for a in argv if a.endswith(".service")]
        assert "com.test.alice.service" in units
        assert "alice.service" not in units
        alice = next(bs for bs in results if bs.name == "alice")
        assert alice.service_active is True

    def test_a_manifest_case_variant_cannot_diverge_state_from_tmux(self, mock_paths):
        """The LIVE half of the presence join, pinned through the door that
        builds it (#1615 review, PR #1695 follow-up).

        `TestLatestHeartbeats::test_state_and_tmux_cannot_diverge_on_a_case_
        variant_collision` asserts the same property but hand-builds
        `live = [{"bot": "alex", ...}]` already lower-cased and calls
        `derive_presence` directly — so it never reaches the `_live`
        comprehension in `collect_fleet_status` that does the lowering.
        Reverting that `b.lower()` leaves it green. The claim was covered on
        the recorded half only; this is the live half.

        The mechanism it pins: the live aliases are built from the MANIFEST's
        spelling, while `_presence_rows` lower-cases the recorded ones. Un-
        lowered, a bot declared `Alex` and recorded `alex` mints TWO verdicts,
        and the `{alias.lower(): ...}` collapse keeps whichever `sorted()`
        visited LAST. A mixed-case alias always sorts BEFORE its lower-case
        twin (ASCII 'A' < 'a'), so the record-only verdict wins and the live
        half's own `down` is thrown away — STATE=idle beside TMUX=down, the
        exact divergence #1615 exists to make impossible.
        """
        from claudlobby.config import BotConfig, FleetConfig

        fleet = FleetConfig(
            name="test-fleet",
            manager="Alex",
            service_prefix="com.test",
            bots={"Alex": BotConfig(bot_id="Alex", name="Alex", expertise=["eng"])},
        )
        _land_heartbeats(mock_paths.root, "test-fleet", "alex", ["IDLE", "IDLE"])

        with (
            patch("claudlobby.status._check_tmux_sessions", return_value=set()),
            patch("claudlobby.status._check_systemd_service", return_value=(False, "dead")),
        ):
            alex = next(bs for bs in collect_fleet_status(fleet, mock_paths)
                        if bs.name == "Alex")

        # Preconditions, or the assertion below passes on a plane that never
        # answered: STATE would keep its default and never reach the join.
        assert not alex.plane_unreachable, alex.plane_unreachable
        assert alex.last_heartbeat is not None and alex.pane_state == "IDLE", alex

        assert alex.tmux_alive is False
        assert alex.state == "down", alex.state      # the live half's verdict, kept
        assert (alex.state == "down") == (not alex.tmux_alive)

    def test_a_uniformly_cased_fleet_name_cannot_diverge_state_from_tmux(self, mock_paths):
        """The same line's other half: `plane.fleet`, pinned through the same door.

        Construction by vera on review, who falsified my first attempt at this.
        I built the fleet name MISMATCHED — directory `Test-Fleet`, rows recorded
        under `test-fleet` — watched `plane_session` refuse before `_live` ran,
        and concluded the half was unreachable. Two things were wrong with that.
        The mismatch cannot arise: `composer.py:81,954` exports
        `FLEET_NAME=fleet.name` VERBATIM, so every bot records under whatever
        casing `fleet.yaml` carries and the two sides cannot independently
        disagree. And "unreachable" generalised past what I had shown, which was
        only that ONE construction refuses.

        What does arise: an operator names the overlay directory and `name:`
        consistently in some non-lowercase style. Nothing in `config.py`
        constrains fleet-name casing. Then the plane session SUCCEEDS —
        both sides agree on `Test-Fleet` — and `_presence_rows` lower-cases the
        recorded prefix anyway, so it is the LOWERING ITSELF that must bring the
        live half to meet it. Without it, `bot:Test-Fleet/alex` and
        `bot:test-fleet/alex` are two verdicts and the collapse keeps the
        record-only one: STATE=idle beside TMUX=down.
        """
        from claudlobby.config import BotConfig, FleetConfig
        from tests.package_fixtures import source_package
        from claudlobby.paths import Paths

        fleet_dir = mock_paths.root / "local" / "Test-Fleet"
        (fleet_dir / "runtime" / "bots").mkdir(parents=True, exist_ok=True)
        paths = Paths(root=mock_paths.root, fleet_dir=fleet_dir, package=source_package())
        fleet = FleetConfig(
            name="Test-Fleet",
            manager="alex",
            service_prefix="com.test",
            bots={"alex": BotConfig(bot_id="alex", name="alex", expertise=["eng"])},
        )
        # SAME casing as the fleet name -- what the composer actually produces.
        # The bot id is held lower-case so only `plane.fleet`'s case varies:
        # one conjunct per test, or a single fixture varying both would pass
        # with either lowering removed.
        _land_heartbeats(mock_paths.root, "Test-Fleet", "alex", ["IDLE", "IDLE"])

        with (
            patch("claudlobby.status._check_tmux_sessions", return_value=set()),
            patch("claudlobby.status._check_systemd_service", return_value=(False, "dead")),
        ):
            alex = next(bs for bs in collect_fleet_status(fleet, paths)
                        if bs.name == "alex")

        # Preconditions. The first one is the whole difference from the attempt
        # this replaces: there, the session REFUSED and the join never ran.
        assert not alex.plane_unreachable, alex.plane_unreachable
        assert alex.last_heartbeat is not None and alex.pane_state == "IDLE", alex

        assert alex.tmux_alive is False
        assert alex.state == "down", alex.state
        assert (alex.state == "down") == (not alex.tmux_alive)


class TestRecordedStop:
    """#2243 F8: a bot the stop door stopped reads `stopped`, with who and since, in place of
    `down`. The facts are fleet-pulse's: no installed unit file, and the stop door's record. A
    missing unit with no record is never `stopped`."""

    def _stage(self, tmp_path, mock_paths, monkeypatch, *, record, unit):
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        alice = mock_paths.bot_runtime("alice")
        (alice / "data").mkdir(parents=True, exist_ok=True)
        (alice / "bot.conf").write_text("BOT_SERVICE=com.test.alice\n")
        if unit:
            path = home / ".config/systemd/user/com.test.alice.service"
            path.parent.mkdir(parents=True)
            path.write_text("[Service]\n")
        if record:
            (alice / "data" / ".stopped").write_text(json.dumps({
                "by": "bot:test-fleet/bob", "reason": "parked", "request_id": "r-1",
                "stopped_at": "2026-10-09T20:40:00Z", "stopped_epoch": 1791578400},
                sort_keys=True) + "\n")

    def _alice(self, mock_fleet, mock_paths):
        with (
            patch("claudlobby.status.platform.system", return_value="Linux"),
            patch("claudlobby.status._check_tmux_sessions", return_value=set()),
            patch("claudlobby.status._check_systemd_service", return_value=(False, "dead")),
        ):
            results = collect_fleet_status(mock_fleet, mock_paths)
        rows = json.loads(format_json(results, "test-fleet"))["bots"]
        return next(bs for bs in results if bs.name == "alice"), next(r for r in rows if r["name"] == "alice")

    def test_a_recorded_stop_reads_stopped_with_who_and_since(self, tmp_path, mock_fleet, mock_paths, monkeypatch):
        self._stage(tmp_path, mock_paths, monkeypatch, record=True, unit=False)
        alice, row = self._alice(mock_fleet, mock_paths)
        assert alice.state == "stopped"
        assert (row["state"], row["stopped_by"], row["stopped_since"], row["stop_reason"]) == (
            "stopped", "bot:test-fleet/bob", "2026-10-09T20:40:00Z", "parked")
        assert "stopped" in format_table([alice], "test-fleet")

    def test_a_missing_unit_with_no_record_is_not_stopped(self, tmp_path, mock_fleet, mock_paths, monkeypatch):
        self._stage(tmp_path, mock_paths, monkeypatch, record=False, unit=False)
        alice, row = self._alice(mock_fleet, mock_paths)
        assert alice.state != "stopped" and row["stopped_by"] is None

    def test_a_record_beside_an_installed_unit_is_stale_not_a_stop(self, tmp_path, mock_fleet, mock_paths, monkeypatch):
        self._stage(tmp_path, mock_paths, monkeypatch, record=True, unit=True)
        alice, row = self._alice(mock_fleet, mock_paths)
        assert alice.state != "stopped" and row["stopped_by"] is None
