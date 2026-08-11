"""creds-check persistence alerting (#1095) and the deleted MCP probe (#1096).

Transition-only alerting could not express steady state: a credential that
failed and STAYED failed alerted once and then went silent forever, so failure
duration and alarm volume ran in opposite directions. Our own estate had the
live case — a dead Railway token and, separately, a `skip` that had never once
been surfaced.

These drive the REAL script end to end through the existing fixture, then
back-date ``since_epoch`` in the state file to simulate elapsed days. Nothing
here mocks the alerting decision itself.
"""

from __future__ import annotations

import json

from tests.test_creds_check_telegram import _fleet, _run

DAY = 86400


def _age(fleet: dict, provider: str, days: int) -> None:
    """Back-date a provider's run so it reads as `days` old on the next tick."""
    state = json.loads(fleet["state"].read_text())
    assert provider in state, f"{provider} not in state: {sorted(state)}"
    state[provider]["since_epoch"] = state[provider]["since_epoch"] - days * DAY
    fleet["state"].write_text(json.dumps(state))


def _posts(fleet: dict) -> str:
    log = fleet["tg_log"]
    return log.read_text() if log.exists() else ""


def _a_skipping_provider(state: dict) -> str:
    """A provider this fixture leaves in `skip` (its .env is empty)."""
    for name, row in state.items():
        if row["status"] == "skip":
            return name
    raise AssertionError(f"fixture produced no skip row: {sorted(state)}")


class TestStateCarriesAge:
    def test_new_fields_are_written(self, tmp_path):
        state = _run(_fleet(tmp_path))
        row = next(iter(state.values()))
        assert "since_epoch" in row and "alerts" in row, (
            "#1095 needs the age of a run, not just its status — the state file "
            "is where that age lives"
        )
        assert row["since_epoch"] > 0

    def test_a_pre_1095_state_file_upgrades_without_crashing(self, tmp_path):
        """The live state file predates these fields. It must not need a wipe."""
        f = _fleet(tmp_path)
        _run(f)
        legacy = {
            k: {"status": v["status"], "detail": v["detail"], "ts": v["ts"]}
            for k, v in json.loads(f["state"].read_text()).items()
        }
        f["state"].write_text(json.dumps(legacy))
        state = _run(f)  # must not raise
        row = next(iter(state.values()))
        assert row["since_epoch"] > 0, "a legacy row should start a fresh run"


class TestPersistentSkipResurfaces:
    """The case our own fleet was blind to.

    ``github_pat`` has been `skip` forever because the check reads a variable
    the fleet does not set. It is a SKIP, not a fail — so a fail-only fix would
    have left the very example that motivated this silent.
    """

    def test_a_fresh_skip_is_silent(self, tmp_path):
        f = _fleet(tmp_path)
        state = _run(f)
        _a_skipping_provider(state)  # assert the fixture really produces one
        assert "SKIPPED" not in _posts(f), (
            "a skip on the first tick is usually a provider this fleet has not "
            "configured — alerting on it would be noise, and that is why the "
            "original design stayed quiet"
        )

    def test_a_skip_still_there_after_three_days_surfaces(self, tmp_path):
        f = _fleet(tmp_path)
        provider = _a_skipping_provider(_run(f))
        _age(f, provider, 3)
        _run(f)
        posts = _posts(f)
        assert "SKIPPED" in posts, (
            "a skip that persists is a check that can NEVER run — a permanent "
            "hole wearing a skip's clothing. It must surface once."
        )
        assert provider in posts, "the alert must name which check is dead"
        assert "3d" in posts, "the age is the actionable part, not the state"

    def test_it_does_not_re_alert_the_next_day(self, tmp_path):
        """Decaying, not daily — the noise-control intent survives."""
        f = _fleet(tmp_path)
        provider = _a_skipping_provider(_run(f))
        _age(f, provider, 3)
        _run(f)
        first = _posts(f).count("SKIPPED")
        _age(f, provider, 1)  # day 4 — next mark is day 7
        _run(f)
        assert _posts(f).count("SKIPPED") == first, (
            "re-surfacing must decay (3, 7, then weekly). Alerting every tick "
            "is how a check trains people to filter it out."
        )


class TestPersistentFailResurfaces:
    """``railway_token`` fails on every tick in this fixture (404 from the curl
    stub), which is the same shape as the real estate's dead Railway token —
    the case #1095 was opened on."""

    def test_the_first_fail_alerts_as_it_always_did(self, tmp_path):
        f = _fleet(tmp_path)
        state = _run(f)
        assert state["railway_token"]["status"] == "fail"
        assert "railway_token FAIL" in _posts(f), "the transition alert must not regress"

    def test_a_fail_that_stays_failed_re_alerts_with_its_age(self, tmp_path):
        f = _fleet(tmp_path)
        _run(f)
        _age(f, "railway_token", 1)
        _run(f)
        posts = _posts(f)
        assert "STILL FAILING" in posts, (
            "under transition-only alerting this second tick was SILENT, and "
            "stayed silent forever. That is the whole defect: failure duration "
            "and alarm volume ran in opposite directions."
        )
        assert "1d" in posts, (
            "the age is the actionable half — 'failing since yesterday' and "
            "'failing since last month' are different operational facts"
        )

    def test_it_decays_rather_than_alerting_every_tick(self, tmp_path):
        f = _fleet(tmp_path)
        _run(f)
        _age(f, "railway_token", 1)
        _run(f)
        first = _posts(f).count("STILL FAILING")
        _age(f, "railway_token", 1)  # day 2 — the next mark is day 3
        _run(f)
        assert _posts(f).count("STILL FAILING") == first, (
            "ladder is 1, 3, 7 then weekly. Re-alerting daily would recreate "
            "the noise problem transition-only alerting existed to solve."
        )

    def test_the_seven_day_mark_fires(self, tmp_path):
        f = _fleet(tmp_path)
        _run(f)
        for day in (1, 2, 4):  # reaches day 1, then 3, then 7
            _age(f, "railway_token", day)
            _run(f)
        assert _posts(f).count("STILL FAILING") == 3, (
            "three re-surfaces expected at days 1, 3 and 7"
        )

    def test_recovery_still_alerts(self, tmp_path):
        """The half that already worked must not regress.

        ``telegram_f_bot1`` genuinely evaluates to ok, so seeding it as fail
        and re-running exercises the real fail to ok edge.
        """
        f = _fleet(tmp_path)
        _run(f)
        state = json.loads(f["state"].read_text())
        state["telegram_f_bot1"]["status"] = "fail"
        f["state"].write_text(json.dumps(state))
        _run(f)
        assert "telegram_f_bot1 RECOVERED" in _posts(f)

    def test_recovery_clears_the_run_so_a_later_fail_alerts_again(self, tmp_path):
        f = _fleet(tmp_path)
        _run(f)
        state = json.loads(f["state"].read_text())
        state["telegram_f_bot1"]["status"] = "fail"
        f["state"].write_text(json.dumps(state))
        state = _run(f)
        assert state["telegram_f_bot1"]["alerts"] == 0, (
            "a recovered provider must start a clean run, or its next outage "
            "inherits a spent ladder and re-surfaces late"
        )


class TestMcpProbeIsGone:
    """#1096 — the probe gated on MCP_PROBE_URL, set nowhere, so it never ran."""

    def test_no_mcp_probe_row_is_recorded(self, tmp_path):
        state = _run(_fleet(tmp_path))
        assert "mcp_probe" not in state, (
            "the MCP probe is deleted, not disabled. It gated on MCP_PROBE_URL "
            "— set in no fleet config, no bot.conf and no .env — so it recorded "
            "`skip` forever. Re-adding it means re-adding a permanent skip line, "
            "and permanent skip lines are what teach operators to ignore skips."
        )

    def test_the_script_still_runs_its_remaining_checks(self, tmp_path):
        state = _run(_fleet(tmp_path))
        assert state, "deleting one check must not empty the tick"
        assert any(k.startswith("telegram_") for k in state), (
            "the telegram per-bot checks are the ones that actually run here"
        )
