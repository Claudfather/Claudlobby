"""creds-check keeps a state file per fleet and checks what the fleet declares (#1213).

Two defects let a missing or dead credential go unreported.

**One state file for the host.** Fleets tick minutes apart against the same
keys, so each tick overwrote the verdict another fleet had written. A run
reset at every flip: the skip escalation never reached day 3, and a dead token
alerted as a fresh FAIL at age 0 each day instead of climbing its 1, 3, 7-day
ladder. The selected CLI has passed each fleet its own file since #1989; a
direct run still wrote the one host file.

**A provider was checked on every fleet, declared or not.** A token with no
value was a `skip`, so a fleet whose bots declare the GitHub MCP and hold no
token, the very condition the check exists for, was never failed. And a fleet
that never wanted Railway kept a lasting `skip` that from day 3 re-surfaced as
"a permanent hole".

So a provider is now checked only where a bot of the fleet declares its
credential (the selected CLI passes the activated roster's declarations as
`--declares VAR=SURFACE`); declared with no value is a FAIL; one no bot
declares gets no probe, no row and no alert.

These drive the real script against a scratch data root and a curl stub. Each
fleet's alerts go to a file of its own, through a stub sender.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from tests.conftest import _write_exec, constructed_env
from tests.package_fixtures import git_package
from tests.test_creds_check_telegram import IS_VALID, _curl_stub
from tests.test_maintenance_jobs import _native_fixture

DAY = 86400
RAILWAY = ("RAILWAY_PERSONAL_PROJECT_TOKEN=integration/railway",
           "RAILWAY_PERSONAL_TOKEN=integration/railway")
GITHUB = ("GITHUB_PAT=mcp/github",)
GITHUB_APP = tuple(f"{var}=mcp/github-app" for var in (
    "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH"))
#: Not real credentials. The curl stub refuses DEAD with an HTTP 401, the way
#: GitHub and Railway refuse a revoked token, and accepts LIVE.
DEAD = "not-a-real-token-refused-with-401"
LIVE = "not-a-real-token" + IS_VALID
PROVIDERS = ("github_pat", "railway_personal_token", "railway_personal_project_token")


class Estate:
    """One data root holding several fleets, each with one bot and a fleet-tier .env.

    The bot is no channel bot, so the Telegram checks record nothing and every
    row and alert here is a GitHub or Railway one.
    """

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "root"
        (self.root / "state").mkdir(parents=True)
        (self.root / ".env").write_text("")
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.native = _native_fixture(tmp_path, "creds-check.sh")
        _write_exec(self.native / "tg-post.sh",
                    '#!/bin/bash\necho "$*" >> "$CREDS_TEST_POSTS"\n')
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        _curl_stub(self.bindir)
        self.installation = git_package(self.root).installation

    def fleet(self, name: str, **values: str) -> Path:
        fleet_root = self.root / "local" / name
        bot = fleet_root / "runtime" / "bots" / "worker"
        (bot / "data").mkdir(parents=True)
        (bot / "bot.conf").write_text(
            f'export BOT_ID="worker"\nexport BOT_SERVICE="com.t.{name}.worker"\n')
        (fleet_root / "fleet.yaml").write_text(
            f"fleet:\n  name: {name}\n  bots:\n    worker:\n      expertise: [x]\n")
        (fleet_root / ".env").write_text("".join(f'{k}="{v}"\n' for k, v in values.items()))
        return fleet_root

    def selected_state(self, fleet: str) -> Path:
        """The file the selected CLI passes each fleet (credential_check.py)."""
        return self.root / "state" / "creds-check" / f"{fleet}.json"

    def direct_state(self, fleet: str) -> Path:
        return self.root / "state" / f"creds-check-state.{fleet}.json"

    def posts(self, fleet: str) -> str:
        sent = self.tmp / f"posts-{fleet}.log"
        return sent.read_text() if sent.exists() else ""

    def log(self) -> str:
        return (self.root / "creds-check.log").read_text()

    def probed(self, host: str) -> bool:
        argv = self.bindir / "argv.log"
        return argv.exists() and host in argv.read_text()

    def _env(self, fleet: str, **extra) -> dict:
        # Built from nothing: no credential reaches the script from the host.
        return constructed_env(
            PATH=f"{self.bindir}:{os.environ['PATH']}", HOME=self.home,
            CLAUDLOBBY_ROOT=self.root, CLAUDLOBBY_CREDS_LOG=self.root / "creds-check.log",
            CREDS_TEST_POSTS=self.tmp / f"posts-{fleet}.log", **extra)

    def run_selected(self, fleet: str, *declares: str) -> subprocess.CompletedProcess:
        """One tick as the selected CLI runs it, with the fleet's declarations."""
        fleet_root = self.root / "local" / fleet
        env = self._env(fleet, CLAUDLOBBY_INSTALLATION=self.installation.directory,
                        CLAUDLOBBY_NATIVE_ADMITTED="1", CLAUDLOBBY_NATIVE_DIR=self.native,
                        CLAUDLOBBY_CLI=self.installation.cli, FLEET_ROOT=fleet_root,
                        CLAUDLOBBY_FLEET=fleet, CLAUDLOBBY_CREDS_STATE=self.selected_state(fleet))
        argv = ["bash", str(self.native / "creds-check.sh"), "--admitted-runtime",
                "--fleet", fleet, "--fleet-root", str(fleet_root),
                "--bots-dir", str(fleet_root / "runtime" / "bots"), "--bot", "worker"]
        for item in declares:
            argv += ["--declares", item]
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)

    def selected(self, fleet: str, *declares: str) -> dict:
        """A selected tick that completed, and the state file it left."""
        result = self.run_selected(fleet, *declares)
        assert result.returncode == 0 and result.stdout == "tick-complete\n", result.stderr
        return json.loads(self.selected_state(fleet).read_text())

    def direct(self, fleet: str | None) -> subprocess.CompletedProcess:
        """One direct tick: no CLI, so no declarations and no state path passed in."""
        env_file = self.root / "local" / fleet / ".env" if fleet else self.root / ".env"
        env = self._env(fleet or "root", CLAUDLOBBY_ENV=env_file)
        argv = ["bash", str(self.native / "creds-check.sh"), *([fleet] if fleet else [])]
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)


def age(state_file: Path, provider: str, days: int) -> None:
    """Back-date a run so it reads as `days` older on the next tick."""
    assert state_file.is_file(), f"{state_file.name} was never written"
    state = json.loads(state_file.read_text())
    state[provider]["since_epoch"] -= days * DAY
    state_file.write_text(json.dumps(state))


def todays_row(status: str, detail: str, days_old: int, alerts: int) -> dict:
    """A row in the shape the check writes before this change (`_write_state`)."""
    return {"status": status, "detail": detail, "ts": "2026-11-16T06:10:00-05:00",
            "since_epoch": int(time.time()) - days_old * DAY, "alerts": alerts}


class TestEachFleetKeepsItsOwnStateFile:
    """The selected CLI passes each fleet its own file (#1989). These pin the
    direct entry, whose default was one file for the whole host."""

    def test_two_fleets_direct_ticks_write_two_files(self, tmp_path):
        e = Estate(tmp_path)
        for name in ("a", "b"):
            e.fleet(name)
            assert e.direct(name).returncode == 0
        assert e.direct_state("a").is_file() and e.direct_state("b").is_file()
        assert not (e.root / "state" / "creds-check-state.json").exists(), (
            "a direct tick with a fleet wrote the host file every fleet shared")

    def test_a_dead_token_keeps_its_age_while_another_fleet_ticks(self, tmp_path):
        """The defect itself. Fleet b holds no Railway token, so its direct tick
        records skip; on one shared file that skip ended fleet a's failing run
        every day, and a's next tick alerted a fresh FAIL at age 0."""
        e = Estate(tmp_path)
        e.fleet("a", RAILWAY_PERSONAL_TOKEN=DEAD)
        e.fleet("b")
        e.direct("a")
        e.direct("b")
        for _day in (1, 2):
            age(e.direct_state("a"), "railway_personal_token", 1)
            e.direct("a")
            e.direct("b")
        posts = e.posts("a")
        assert posts.count("railway_personal_token FAIL") == 1, posts
        assert "railway_personal_token STILL FAILING after 1d" in posts, posts
        assert json.loads(e.direct_state("b").read_text())[
            "railway_personal_token"]["status"] == "skip"

    def test_the_host_file_from_before_is_left_as_it_was(self, tmp_path):
        e = Estate(tmp_path)
        e.fleet("a")
        host = e.root / "state" / "creds-check-state.json"
        host.write_text(json.dumps({"railway_personal_token": todays_row(
            "skip", "RAILWAY_PERSONAL_TOKEN not declared", 9, 2)}))
        before = host.read_bytes()
        assert e.direct("a").returncode == 0
        assert host.read_bytes() == before

    def test_root_mode_keeps_its_one_file(self, tmp_path):
        """No fleet, one file, as before: the control for the two tests above."""
        e = Estate(tmp_path)
        result = e.direct(None)
        assert result.returncode == 0, result.stderr
        assert (e.root / "state" / "creds-check-state.json").is_file()

    @pytest.mark.parametrize("name", ["../x", "a/b", "..", "."])
    def test_a_name_that_is_a_path_cannot_choose_the_file(self, tmp_path, name):
        e = Estate(tmp_path)
        result = e.direct(name)
        assert result.returncode == 2 and "cannot name a fleet" in result.stderr
        assert not list(e.root.rglob("creds-check-state*"))


class TestOnlyWhatTheFleetDeclaresIsChecked:

    def test_a_provider_no_bot_declares_gets_no_probe_and_no_row(self, tmp_path):
        e = Estate(tmp_path)
        # A value is not a declaration: these sit at the fleet tier unasked for.
        e.fleet("a", RAILWAY_PERSONAL_TOKEN=LIVE, GITHUB_PAT=LIVE)
        state = e.selected("a")
        assert not set(PROVIDERS) & set(state), state
        assert not e.probed("backboard.railway.app") and not e.probed("api.github.com")
        assert e.posts("a") == ""
        assert ("railway_personal_token not checked: no bot of this fleet declares "
                "RAILWAY_PERSONAL_TOKEN") in e.log()

    def test_a_declared_token_with_no_value_fails_and_names_its_surface(self, tmp_path):
        e = Estate(tmp_path)
        e.fleet("a")
        row = e.selected("a", *RAILWAY)["railway_personal_token"]
        assert row["status"] == "fail", row
        assert "RAILWAY_PERSONAL_TOKEN" in row["detail"] and "integration/railway" in row["detail"]
        assert "railway_personal_token FAIL" in e.posts("a")
        assert not e.probed("backboard.railway.app"), "a token with no value has nothing to probe"

    def test_a_declared_github_token_with_no_value_fails(self, tmp_path):
        """Defect 1: the GitHub MCP starts with an empty token and runs without
        one, so this fleet's skip was the one case the check existed for."""
        e = Estate(tmp_path)
        e.fleet("a")
        row = e.selected("a", *GITHUB)["github_pat"]
        assert row["status"] == "fail" and "declared by mcp/github," in row["detail"], row
        assert "github_pat FAIL" in e.posts("a")

    def test_a_declared_app_with_incomplete_config_fails_naming_the_app(self, tmp_path):
        e = Estate(tmp_path)
        e.fleet("a", GITHUB_APP_ID="12345")
        row = e.selected("a", *GITHUB_APP)["github_pat"]
        assert row["status"] == "fail" and "declared by mcp/github-app," in row["detail"], row

    @pytest.mark.parametrize("token,status", [(LIVE, "ok"), (DEAD, "fail")])
    def test_a_declared_token_with_a_value_is_probed_as_before(self, tmp_path, token, status):
        e = Estate(tmp_path)
        e.fleet("a", RAILWAY_PERSONAL_TOKEN=token, GITHUB_PAT=token)
        state = e.selected("a", *RAILWAY, *GITHUB)
        assert state["railway_personal_token"]["status"] == status, state
        assert state["github_pat"]["status"] == status, state
        assert e.probed("backboard.railway.app") and e.probed("api.github.com")

    def test_each_token_is_gated_on_its_own_declaration(self, tmp_path):
        e = Estate(tmp_path)
        e.fleet("a")
        state = e.selected("a", "RAILWAY_PERSONAL_TOKEN=integration/railway")
        assert state["railway_personal_token"]["status"] == "fail"
        assert "railway_personal_project_token" not in state

    @pytest.mark.parametrize("entry", ["direct", "selected"])
    @pytest.mark.parametrize("line", ["_cc_declared_known=1", "_cc_declares=GITHUB_PAT=mcp/github"])
    def test_a_tier_file_cannot_change_what_the_fleet_declares(self, tmp_path, line, entry):
        """The declarations are fixed before any .env file is parsed, on both
        entries. A tier that assigns one stops the tick, loudly, before a probe
        or a state write: read as "declares nothing", it would end the checks.
        bash 3.2 exits 0 from that stop, so the script makes it exit 2."""
        e = Estate(tmp_path)
        fleet_root = e.fleet("a", RAILWAY_PERSONAL_TOKEN=LIVE)
        with (fleet_root / ".env").open("a") as env:
            env.write(line + "\n")
        result = e.direct("a") if entry == "direct" else e.run_selected("a", *RAILWAY)
        assert result.returncode == 2 and "readonly variable" in result.stderr, result
        assert "creds-check: the tick stopped while reading the .env tiers" in result.stderr
        assert not e.direct_state("a").exists() and not e.selected_state("a").exists()
        assert not e.probed("backboard.railway.app")


class TestTheMixedEstate:
    """One fleet declares Railway, another declares none, and their ticks
    interleave the way two fleets' timers do."""

    def test_a_fleet_that_declares_no_provider_never_hears_about_one(self, tmp_path):
        """Per-fleet files alone are not enough: on its own file a fleet that
        declares no Railway kept a lasting skip, which re-surfaced from day 3 as
        "SKIPPED ... a permanent hole" for a provider it never wanted."""
        e = Estate(tmp_path)
        e.fleet("b")
        for day in range(9):
            if day:
                state = json.loads(e.selected_state("b").read_text())
                for row in state.values():
                    row["since_epoch"] -= DAY
                e.selected_state("b").write_text(json.dumps(state))
            e.selected("b")
        assert e.posts("b") == ""
        assert not set(PROVIDERS) & set(json.loads(e.selected_state("b").read_text()))

    def test_a_dead_declared_token_climbs_its_ladder_while_another_fleet_ticks(self, tmp_path):
        e = Estate(tmp_path)
        e.fleet("a", RAILWAY_PERSONAL_TOKEN=DEAD, RAILWAY_PERSONAL_PROJECT_TOKEN=DEAD)
        e.fleet("b")
        fired = []
        for day in range(9):
            if day:
                age(e.selected_state("a"), "railway_personal_token", 1)
            before = e.posts("a")
            e.selected("a", *RAILWAY)
            e.selected("b")
            if "railway_personal_token STILL FAILING" in e.posts("a")[len(before):]:
                fired.append(day)
        assert e.posts("a").count("railway_personal_token FAIL") == 1, e.posts("a")
        assert fired == [1, 3, 7], fired
        assert e.posts("b") == ""


class TestTodaysStateFileOnTheFirstTick:
    """The first selected tick after the update reads the per-fleet file the
    check already keeps, rows and all."""

    def test_a_failing_run_keeps_its_age(self, tmp_path):
        """A dead token two days into its run, re-surfaced once at day 1. The
        first tick continues that run: no fresh FAIL, and day 3 fires on time."""
        e = Estate(tmp_path)
        e.fleet("a", RAILWAY_PERSONAL_TOKEN=DEAD)
        seeded = todays_row("fail", "RAILWAY_PERSONAL_TOKEN: HTTP 401 on /graphql/v2", 2, 1)
        e.selected_state("a").parent.mkdir(parents=True)
        e.selected_state("a").write_text(json.dumps({"railway_personal_token": seeded}))
        row = e.selected("a", "RAILWAY_PERSONAL_TOKEN=integration/railway")["railway_personal_token"]
        assert (row["status"], row["since_epoch"], row["alerts"]) == (
            "fail", seeded["since_epoch"], 1), row
        assert e.posts("a") == "", "the update is not a new failure"
        age(e.selected_state("a"), "railway_personal_token", 1)
        e.selected("a", "RAILWAY_PERSONAL_TOKEN=integration/railway")
        assert "railway_personal_token STILL FAILING after 3d" in e.posts("a")

    def test_rows_for_what_the_fleet_does_not_declare_go_without_an_alert(self, tmp_path):
        e = Estate(tmp_path)
        e.fleet("a")
        e.selected_state("a").parent.mkdir(parents=True)
        e.selected_state("a").write_text(json.dumps({
            "github_pat": todays_row("skip", "no GITHUB_PERSONAL_ACCESS_TOKEN", 5, 1),
            "railway_personal_token": todays_row("fail", "HTTP 401 on /graphql/v2", 2, 1),
            "railway_personal_project_token": todays_row("ok", "projects answered", 9, 0)}))
        state = e.selected("a")
        assert not set(PROVIDERS) & set(state), state
        assert e.posts("a") == ""
        assert ("github_pat not checked: no bot of this fleet declares GITHUB_PAT or "
                "GITHUB_APP_*; its earlier row is removed") in e.log()

    def test_a_skip_for_a_declared_token_becomes_one_fail(self, tmp_path):
        """A declared token with no value read skip before the update. It reads
        fail from the first tick: one transition alert, then the fail ladder."""
        e = Estate(tmp_path)
        e.fleet("a")
        seeded = todays_row("skip", "no GITHUB_PERSONAL_ACCESS_TOKEN and no GITHUB_APP_* config", 5, 1)
        e.selected_state("a").parent.mkdir(parents=True)
        e.selected_state("a").write_text(json.dumps({"github_pat": seeded}))
        row = e.selected("a", *GITHUB)["github_pat"]
        assert row["status"] == "fail" and row["alerts"] == 0, row
        assert row["since_epoch"] > seeded["since_epoch"], "a fail is a new run"
        assert e.posts("a").count("github_pat FAIL") == 1


def _fleet_config(**bots):
    from claudlobby.config import BotConfig, FleetConfig

    return FleetConfig(name="f", manager="manager", service_prefix="test", bots={
        name: BotConfig(bot_id=name, name=name, expertise=["x"], **equipment)
        for name, equipment in bots.items()})


@pytest.fixture
def library_paths(tmp_path):
    from claudlobby.paths import Paths

    root = tmp_path / "root"
    fleet_root = root / "local" / "f"
    fleet_root.mkdir(parents=True)
    package = replace(git_package(root), native=git_package(root).runtime_native)
    return Paths(root=root, fleet_dir=fleet_root, package=package)


class TestTheSelectedCliPassesTheDeclarations:

    def test_they_come_from_each_bots_equipment(self, library_paths):
        from claudlobby.config import McpEntry
        from claudlobby.credential_check import fleet_declarations

        fleet = _fleet_config(manager={"mcp": [McpEntry("github")]},
                              worker={"integrations": ["railway"]}, idle={})
        assert fleet_declarations(fleet, library_paths) == [
            "GITHUB_PAT=mcp/github",
            "RAILWAY_PERSONAL_PROJECT_TOKEN=integration/railway",
            "RAILWAY_PERSONAL_TOKEN=integration/railway"]
        assert fleet_declarations(_fleet_config(manager={}), library_paths) == []

    def test_a_bot_with_github_app_declares_the_app_variables(self, library_paths):
        """`github_app:` routes a bot's git through the GitHub App, and the composer
        registers the App's three variables for such a bot whether or not it is
        equipped with the App's MCP fragment. They are declared under that
        fragment's name, which holds their contract, so they merge with its own."""
        from claudlobby.config import GithubAppConfig, McpEntry
        from claudlobby.credential_check import fleet_declarations

        app = GithubAppConfig()
        assert fleet_declarations(_fleet_config(manager={"github_app": app}), library_paths) == [
            *GITHUB_APP]
        assert fleet_declarations(_fleet_config(manager={
            "github_app": app, "mcp": [McpEntry("github-app")]}), library_paths) == [*GITHUB_APP]
        assert fleet_declarations(_fleet_config(manager={
            "github_app": app, "mcp": [McpEntry("github")]}), library_paths) == [
            *GITHUB_APP, *GITHUB]

    def test_a_bot_whose_declarations_cannot_be_read_refuses_the_check(
            self, library_paths, monkeypatch):
        """Read as declaring nothing, that bot's credentials would go unprobed."""
        from claudlobby import credential_check as check

        def unreadable(bot, paths):
            raise ValueError("a fragment that cannot be read")

        monkeypatch.setattr(check, "required_vars", unreadable)
        with pytest.raises(check.CredentialCheckError,
                           match="the credentials bot manager declares cannot be read"):
            check.fleet_declarations(_fleet_config(manager={}), library_paths)

    def test_a_declaration_the_probe_would_refuse_is_named_first(
            self, library_paths, monkeypatch):
        from claudlobby import credential_check as check

        monkeypatch.setattr(check, "required_vars", lambda bot, paths: [
            SimpleNamespace(name="ACME TOKEN", origin="mcp/acme")])
        with pytest.raises(check.CredentialCheckError,
                           match="a declaration the probe cannot take"):
            check.fleet_declarations(_fleet_config(manager={}), library_paths)

    def test_the_probe_is_handed_them_after_the_roster(self, library_paths, monkeypatch):
        from claudlobby import credential_check as check
        from claudlobby.config import McpEntry
        from claudlobby.runtime_admission import GitCurrentRuntime

        paths = library_paths
        manifest = paths.fleet_config_dir / "fleet.yaml"
        manifest.write_text("fleet: {name: f}\n")
        fleet = _fleet_config(manager={"mcp": [McpEntry("github")]},
                              worker={"integrations": ["railway"]})
        release = GitCurrentRuntime(paths.package.installation, paths.package.source_revision,
                                    "c" * 64, {"fleets": {"f": {"manifest": str(manifest),
                                                                "fleet": manifest.read_text()}}})

        @contextmanager
        def admitted(root, expected_release=None):
            yield release

        calls = []
        monkeypatch.delenv("CLAUDLOBBY_RELEASE_ID", raising=False)
        monkeypatch.delenv("CLAUDLOBBY_ARTIFACT_ID", raising=False)
        monkeypatch.setattr(check, "mutation_admission", admitted)
        monkeypatch.setattr(check, "native_environment", lambda paths, current: {})
        monkeypatch.setattr(check, "resolve_operation_scope", lambda **kwargs: (
            SimpleNamespace(paths=paths, fleet=fleet), SimpleNamespace(bot_id="manager")))
        monkeypatch.setattr(check.subprocess, "run", lambda argv, **kwargs: (
            calls.append(argv) or subprocess.CompletedProcess(argv, 0, "tick-complete\n", "")))
        check.check_credentials(root=paths.root, fleet="f")
        argv = calls[0]
        assert argv[argv.index("--bot"):] == [
            "--bot", "manager", "--bot", "worker",
            "--declares", "GITHUB_PAT=mcp/github",
            "--declares", "RAILWAY_PERSONAL_PROJECT_TOKEN=integration/railway",
            "--declares", "RAILWAY_PERSONAL_TOKEN=integration/railway"]


#: A complete App config. Not real: no key is at that path, and the mint the
#: ticks below stand in for never reads one.
APP_CONFIG = {"GITHUB_APP_ID": "999001", "GITHUB_APP_INSTALLATION_ID": "555002",
              "GITHUB_APP_PRIVATE_KEY_PATH": "/nonexistent/app-key.pem"}


@pytest.fixture
def app_only_declarations(library_paths):
    """What the selected CLI passes for a roster whose only GitHub equipment is
    one bot's `github_app:`."""
    from claudlobby.config import GithubAppConfig
    from claudlobby.credential_check import fleet_declarations

    return fleet_declarations(_fleet_config(manager={"github_app": GithubAppConfig()}),
                              library_paths)


class TestAnAppRoutedRosterIsChecked:
    """Ticks with the declarations of a roster whose only GitHub equipment is
    `github_app:`. The estate has a directory of its own beside the library
    fixture's root."""

    @staticmethod
    def _mint_fails(e: Estate) -> Path:
        """A stand-in for the App's mint helper that notes each call and fails,
        as the mint does for a revoked key or a wrong installation."""
        calls = e.tmp / "mint-calls.log"
        _write_exec(e.native / "mint-github-token.sh",
                    f'#!/bin/bash\necho minted >> "{calls}"\nexit 1\n')
        return calls

    def test_a_complete_app_config_reaches_the_app_probe(self, tmp_path, app_only_declarations):
        """A dead App key on such a fleet is a fail with its alert. With no
        declarations the tick logged "not checked" and raised none."""
        e = Estate(tmp_path / "estate")
        e.fleet("a", **APP_CONFIG)
        calls = self._mint_fails(e)
        state = e.selected("a", *app_only_declarations)
        assert calls.is_file(), "the tick never asked the App for a token: " + e.log()
        row = state["github_pat"]
        assert row["status"] == "fail" and "installation-token mint failed" in row["detail"], row
        assert "github_pat FAIL" in e.posts("a")

    def test_an_incomplete_app_config_fails_naming_the_app(self, tmp_path, app_only_declarations):
        e = Estate(tmp_path / "estate")
        e.fleet("a", GITHUB_APP_ID=APP_CONFIG["GITHUB_APP_ID"])
        calls = self._mint_fails(e)
        state = e.selected("a", *app_only_declarations)
        assert "github_pat" in state, "no GitHub row: " + e.log()
        row = state["github_pat"]
        assert row["status"] == "fail" and "declared by mcp/github-app," in row["detail"], row
        assert "github_pat FAIL" in e.posts("a")
        assert not calls.exists(), "an incomplete config has no App to ask"
