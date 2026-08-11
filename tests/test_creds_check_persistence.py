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

# Any value at all makes check_railway_token RUN rather than skip; the fixture's
# curl stub then answers 404, so the outcome is `fail` deterministically. The
# value is never a real credential and never leaves the stub.
FORCE_RAILWAY = "not-a-real-token-forces-the-check-to-run"

# Credential vars that decide whether a provider runs or skips. They must be set
# EXPLICITLY per test, never inherited.
#
# This is the defect that shipped a red CI: `_fleet` builds its child env from
# `_scrubbed_env`, which is an inherit-and-subtract DENYLIST — it strips
# TELEGRAM*/CLAUDLOBBY*/FLEET*/BOT_* and passes everything else through. A
# developer machine with a (dead) RAILWAY_API_TOKEN exported made railway_token
# resolve to `fail`; CI, with no such variable, resolved it to `skip`. The tests
# passed on both machines a human used and failed only in CI, because they were
# reading host state instead of their own fixture.
#
# `conftest._scrubbed_env`'s own docstring names this: "only as complete as this
# prefix list (#846) ... new tests should not add call sites here."
CREDENTIAL_VARS = (
    "RAILWAY_API_TOKEN",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_PAT",
)


def _controlled(tmp_path, *, railway_fails: bool):
    """A fleet whose credential inputs are set by the TEST, not by the host.

    railway_fails=True  -> railway_token resolves `fail` (token present, stub 404)
    railway_fails=False -> railway_token resolves `skip` (no token at all)
    """
    f = _fleet(tmp_path)
    for var in CREDENTIAL_VARS:
        f["env"].pop(var, None)
    if railway_fails:
        f["env"]["RAILWAY_API_TOKEN"] = FORCE_RAILWAY
    return f


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
        state = _run(_controlled(tmp_path, railway_fails=False))
        row = next(iter(state.values()))
        assert "since_epoch" in row and "alerts" in row, (
            "#1095 needs the age of a run, not just its status — the state file "
            "is where that age lives"
        )
        assert row["since_epoch"] > 0

    def test_a_pre_1095_state_file_upgrades_without_crashing(self, tmp_path):
        """The live state file predates these fields. It must not need a wipe."""
        f = _controlled(tmp_path, railway_fails=False)
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

    ``github_pat`` has been `skip` on this estate for as long as anyone looked.
    NOT because of a name mismatch — ``check_github_pat`` falls back through
    ``GITHUB_PERSONAL_ACCESS_TOKEN`` then ``GITHUB_TOKEN`` then ``GITHUB_PAT``,
    so it does read the name the fleet uses. The value is simply EMPTY, and an
    empty value takes the same ``[ -z ]`` branch as an absent one.

    The point that survives either diagnosis: it is a SKIP, not a fail, and
    ``skip`` was documented as "recorded but never alerted". A fail-only fix
    would have left the very example that motivated this change silent.
    """

    def test_a_fresh_skip_is_silent(self, tmp_path):
        f = _controlled(tmp_path, railway_fails=False)
        state = _run(f)
        _a_skipping_provider(state)  # assert the fixture really produces one
        assert "SKIPPED" not in _posts(f), (
            "a skip on the first tick is usually a provider this fleet has not "
            "configured — alerting on it would be noise, and that is why the "
            "original design stayed quiet"
        )

    def test_a_skip_still_there_after_three_days_surfaces(self, tmp_path):
        f = _controlled(tmp_path, railway_fails=False)
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
        f = _controlled(tmp_path, railway_fails=False)
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
    """The fail ladder: 1, 3, 7 days, then weekly.

    ``railway_token`` is used as the vehicle, and the test SETS the token so the
    check runs and the stub answers 404. It does not rely on the host happening
    to export one — that dependency is what turned this file red in CI while
    passing on two developer machines.
    """

    def test_the_first_fail_alerts_as_it_always_did(self, tmp_path):
        f = _controlled(tmp_path, railway_fails=True)
        state = _run(f)
        assert state["railway_token"]["status"] == "fail"
        assert "railway_token FAIL" in _posts(f), (
            "the transition alert must not regress"
        )

    def test_a_fail_that_stays_failed_re_alerts_with_its_age(self, tmp_path):
        f = _controlled(tmp_path, railway_fails=True)
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
        f = _controlled(tmp_path, railway_fails=True)
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
        f = _controlled(tmp_path, railway_fails=True)
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
        f = _controlled(tmp_path, railway_fails=True)
        _run(f)
        state = json.loads(f["state"].read_text())
        state["telegram_f_bot1"]["status"] = "fail"
        f["state"].write_text(json.dumps(state))
        _run(f)
        assert "telegram_f_bot1 RECOVERED" in _posts(f)

    def test_recovery_clears_the_run_so_a_later_fail_alerts_again(self, tmp_path):
        f = _controlled(tmp_path, railway_fails=True)
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
        state = _run(_controlled(tmp_path, railway_fails=False))
        assert "mcp_probe" not in state, (
            "the MCP probe is deleted, not disabled. It gated on MCP_PROBE_URL "
            "— set in no fleet config, no bot.conf and no .env — so it recorded "
            "`skip` forever. Re-adding it means re-adding a permanent skip line, "
            "and permanent skip lines are what teach operators to ignore skips."
        )

    def test_the_script_still_runs_its_remaining_checks(self, tmp_path):
        state = _run(_controlled(tmp_path, railway_fails=False))
        assert state, "deleting one check must not empty the tick"
        assert any(k.startswith("telegram_") for k in state), (
            "the telegram per-bot checks are the ones that actually run here"
        )


class TestTheseTestsAreHermetic:
    """Guard against the defect that shipped a red CI.

    These tests passed on two developer machines and failed in CI, because
    `_fleet` inherits its child env through `_scrubbed_env` — a prefix DENYLIST
    that strips TELEGRAM*/CLAUDLOBBY*/FLEET*/BOT_* and passes everything else.
    A machine with a dead RAILWAY_API_TOKEN exported resolved railway_token to
    `fail`; CI, with none, resolved it to `skip`.

    A test whose outcome depends on which credentials happen to exist on the
    host is not testing the ladder, and it is green wherever it is written.
    """

    def test_an_ambient_token_cannot_reach_the_script(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "ambient-must-not-leak")
        monkeypatch.setenv("GITHUB_PAT", "ambient-must-not-leak")
        f = _controlled(tmp_path, railway_fails=False)
        for var in CREDENTIAL_VARS:
            assert var not in f["env"], (
                f"{var} leaked from the host into the fixture env. Credential "
                "inputs must be set by the test; inheriting them is what made "
                "this file pass locally and fail in CI."
            )

    def test_an_ambient_token_does_not_change_the_verdict(self, tmp_path, monkeypatch):
        """The exact CI divergence, pinned.

        With a token exported this used to resolve `fail`. It must resolve
        `skip` regardless, because the TEST said no token.
        """
        monkeypatch.setenv("RAILWAY_API_TOKEN", "ambient-must-not-leak")
        state = _run(_controlled(tmp_path, railway_fails=False))
        assert state["railway_token"]["status"] == "skip", (
            "railway_token resolved from the HOST's environment rather than the "
            "fixture's. This is precisely the CI failure: green on a machine "
            "with a stale Railway token, red on one without."
        )

    def test_the_test_can_still_force_a_fail(self, tmp_path, monkeypatch):
        """The inverse: no ambient token, and the fixture still produces a fail."""
        monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
        state = _run(_controlled(tmp_path, railway_fails=True))
        assert state["railway_token"]["status"] == "fail", (
            "the fail ladder's vehicle must be constructed by the test, not "
            "borrowed from the host"
        )


def _strip_to_legacy(fleet: dict, provider: str) -> None:
    """Reduce a row to its pre-#1095 shape: status/detail/ts, no history.

    This is not a synthetic edge case — it is the literal on-disk state of every
    already-failing credential on the day this ships.
    """
    state = json.loads(fleet["state"].read_text())
    state[provider] = {k: state[provider][k] for k in ("status", "detail", "ts")}
    fleet["state"].write_text(json.dumps(state))
    fleet["tg_log"].write_text("")  # drop alerts from the seeding tick


def _walk(fleet: dict, provider: str, days: int, marker: str) -> list[int]:
    """Tick day by day, each day building on the PREVIOUS day's real state."""
    fired: list[int] = []
    for day in range(days):
        if day:
            state = json.loads(fleet["state"].read_text())
            state[provider]["since_epoch"] -= DAY
            fleet["state"].write_text(json.dumps(state))
        before = _posts(fleet)
        _run(fleet)
        if marker in _posts(fleet)[len(before):]:
            fired.append(day)
    return fired


class TestTheLadderFromAnInheritedFailingState:
    """The gap that let a real double-fire ship green (#1169).

    Every other fail-ladder test starts from `_controlled` then a fresh `_run`,
    which is always a genuine ok->fail TRANSITION. The transition path never
    evaluates `sent=0`, so a bug living in that bucket was unreachable from the
    whole suite.

    A credential inherited ALREADY failing with no history is a different entry
    path, and it is the one that matters on deploy day: it evaluates `sent=0`
    for real, earns `sent=1` by firing, and then walks the same ladder. These
    tests walk it forward day by day rather than re-deriving each day fresh.
    """

    def test_it_fires_on_exactly_the_documented_days(self, tmp_path):
        f = _controlled(tmp_path, railway_fails=True)
        _run(f)
        _strip_to_legacy(f, "railway_token")

        fired = _walk(f, "railway_token", days=9, marker="STILL FAILING")

        assert fired == [1, 3, 7], (
            f"documented ladder is 1, 3, 7 — this fired on {fired}.\n"
            "\n"
            "If day 2 is in that list, the fail branch of next_alert_day has "
            "re-merged its sent=0 and sent=1 buckets. That is invisible from a "
            "fresh transition (which never evaluates sent=0) and fires a "
            "duplicate alert on the second day for every credential that was "
            "already failing when this deployed — which is the exact condition "
            "the motivating credential is in."
        )

    def test_it_does_not_double_fire_on_day_two(self, tmp_path):
        """Stated separately so the failure names the symptom, not just a list."""
        f = _controlled(tmp_path, railway_fails=True)
        _run(f)
        _strip_to_legacy(f, "railway_token")

        fired = _walk(f, "railway_token", days=4, marker="STILL FAILING")

        assert 2 not in fired, (
            "day 2 fired. The ladder is 1, 3, 7: after the day-1 alert the next "
            "rung is day 3. A day-2 alert means sent=1 was read as though it "
            "were still the pre-transition sent=0."
        )

    def test_the_transition_path_is_undisturbed(self, tmp_path):
        """The contrast, so the two entry paths are legible as a pair.

        A genuine ok->fail alerts immediately AND then walks the same ladder
        from its own start. The transition alert does not consume a rung.
        """
        f = _controlled(tmp_path, railway_fails=True)
        _run(f)
        assert "railway_token FAIL" in _posts(f), "transition alert must still fire"

        fired = _walk(f, "railway_token", days=9, marker="STILL FAILING")

        assert fired == [1, 3, 7], (
            f"fresh-transition ladder should also be 1, 3, 7 — got {fired}. "
            "Both entry paths share one accounting; if they diverge, one of "
            "them is counting the transition alert as a ladder rung."
        )
