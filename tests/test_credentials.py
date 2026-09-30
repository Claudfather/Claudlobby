"""Unit tests for claudlobby/credentials.py — the #1104 credential reconciler.

The properties under test are the ones the issue turns on: shape 3 is never
silently omitted, a shadowed value is never reported as merely missing, and no
credential VALUE ever reaches the output.
"""

from __future__ import annotations

import os
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
import subprocess

import pytest

from claudlobby import credentials as creds

FLEET_YAML = dedent("""\
    fleet:
      manager: worker
      name: t
      service_prefix: com.t
      accounts:
        default: ~/.claude
      bots:
        worker:
          expertise: [software-engineering]
          integrations: [acme]
""")


def test_generated_credentials_reconcile_selects_own_fleet(tmp_path, monkeypatch):
    from claudlobby import context
    from claudlobby.commands import host_credentials
    from tests.package_fixtures import source_package

    root = tmp_path / "data"
    own = root / "local" / "own"
    own.mkdir(parents=True)
    (root / "fleet.yaml").write_text(FLEET_YAML)
    (own / "fleet.yaml").write_text(FLEET_YAML.replace("name: t", "name: own"))
    monkeypatch.setattr(context, "get_resources", source_package)
    monkeypatch.setenv("FLEET_NAME", "own")
    checked = []
    monkeypatch.setattr(creds, "reconcile", lambda paths, fleet:
                        (checked.append(fleet.name) or [], {}))
    monkeypatch.setattr(creds, "format_report", lambda findings, scope: "checked")
    args = SimpleNamespace(root=root, fleet=None, seed=False,
                           public_command="host.credentials.reconcile")
    result = host_credentials.dispatch(args)
    assert checked == ["own"]
    assert result.data["fleet"] == "own"

INTEGRATION = dedent("""\
    ---
    title: Acme
    type: cli
    env_contract:
      ACME_TOKEN:
        description: Acme API token
        default_tier: fleet
    ---

    # Acme
""")


@pytest.fixture
def estate(tmp_path: Path, monkeypatch):
    """A root with a SEPARATE fleet dir, so tier shadowing is expressible.

    The shared `make_paths` helper sets root == fleet_dir, which collapses the
    two tiers into one file and makes the shadowing case — the one that bit the
    estate — impossible to write.
    """
    from claudlobby.config import load_fleet
    from tests.package_fixtures import source_package
    from claudlobby.paths import Paths

    root = tmp_path / "claudlobby"
    (root / "library" / "integrations").mkdir(parents=True)
    (root / "library" / "mcp").mkdir(parents=True)
    (root / "library" / "integrations" / "acme.md").write_text(INTEGRATION)

    # The REAL runtime resolver, because this module now answers through it.
    # A stub would let this suite certify a cascade the runtime does not have,
    # which is the class of defect the whole change is about.
    repo = Path(__file__).resolve().parent.parent
    (root / "lib").mkdir(parents=True, exist_ok=True)
    # supervisor.sh is a third required sibling: lib-common.sh unconditionally
    # sources it from its own directory (#1573 task 6).
    for f in ("lib-common.sh", "env-tiers.sh", "supervisor.sh"):
        (root / "lib" / f).write_bytes((repo / "claudlobby/_runtime_scripts" / f).read_bytes())
    # An isolated HOST tier. Without this the tests would read the developer's
    # own ~/.env — non-hermetic, and on a machine that happens to define one of
    # these vars the suite would go green for the wrong reason.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    fleet_dir = root / "local" / "t"
    fleet_dir.mkdir(parents=True)
    (fleet_dir / "fleet.yaml").write_text(FLEET_YAML)

    fleet, _ = load_fleet(fleet_dir / "fleet.yaml")
    package = replace(source_package(), library=root / "library", native=root / "lib")
    paths = Paths(root=root, fleet_dir=fleet_dir, package=package)
    return root, fleet_dir, fleet, paths


def _write_env(path: Path, **vars_):
    path.write_text("".join(f"export {k}={v}\n" for k, v in vars_.items()))


def _shape(findings, n):
    return [f for f in findings if f.shape == n]


class TestFrontmatter:
    def test_parses_env_contract(self):
        got = creds.parse_frontmatter_block(INTEGRATION, "env_contract")
        assert "ACME_TOKEN" in got
        assert got["ACME_TOKEN"]["default_tier"] == "fleet"

    def test_absent_key_is_empty_not_error(self):
        assert creds.parse_frontmatter_block(INTEGRATION, "consumer_contract") == {}

    def test_malformed_frontmatter_is_empty_not_raise(self):
        assert (
            creds.parse_frontmatter_block("no frontmatter here", "env_contract") == {}
        )
        assert creds.parse_frontmatter_block("---\nunterminated", "env_contract") == {}


class TestShapeOne:
    def test_value_present_is_ok(self, estate):
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="sekrit")
        findings, _ = creds.reconcile(paths, fleet)
        row = next(f for f in _shape(findings, 1) if f.subject == "ACME_TOKEN")
        assert row.verdict == "OK"

    def test_absent_everywhere_is_fail(self, estate):
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", OTHER="x")
        findings, _ = creds.reconcile(paths, fleet)
        row = next(f for f in _shape(findings, 1) if f.subject == "ACME_TOKEN")
        assert row.verdict == "FAIL"
        assert "absent from every tier" in row.detail

    def test_present_but_empty_is_distinct_from_absent(self, estate):
        """Different diagnosis, different remedy — someone provisioned the slot
        and left it blank, rather than nobody knowing it was needed."""
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="")
        findings, _ = creds.reconcile(paths, fleet)
        row = next(f for f in _shape(findings, 1) if f.subject == "ACME_TOKEN")
        assert row.verdict == "FAIL"
        assert "EMPTY" in row.detail
        assert "absent from every tier" not in row.detail

    def test_a_value_at_a_less_specific_tier_simply_resolves(self, estate):
        """INVERTS `test_shadowed_by_tier_says_so`.

        WHY THE OLD ASSERTION WAS WRONG: it asserted FAIL / "shadowed, not
        missing" for a value at root while the fleet has its own `.env`. That
        was the best answer available to a reader that consulted ONE tier — it
        at least stopped someone re-adding a credential that existed. But the
        runtime sources root AND fleet, and the fleet file here does not assign
        ACME_TOKEN, so nothing shadows anything: the bot gets the value. The old
        test therefore certified a FAILURE THAT DOES NOT EXIST, and #1226's
        deliverable 5 says so in as many words — "shadowed, not missing" stops
        being a reported state and becomes a resolved one.

        Not a relaxation. The case it used to cover is now covered by
        `test_an_empty_assignment_that_blanks_a_real_value_fails`, which is the
        state that genuinely breaks a bot and that the old reader could not
        express at all.
        """
        root, fleet_dir, fleet, paths = estate
        _write_env(root / ".env", ACME_TOKEN="the-good-one")
        _write_env(fleet_dir / ".env", UNRELATED="x")
        findings, _ = creds.reconcile(paths, fleet)
        row = next(f for f in _shape(findings, 1) if f.subject == "ACME_TOKEN")
        assert row.verdict == "OK"
        assert "root" in row.detail

    def test_an_empty_assignment_that_blanks_a_real_value_fails(self, estate):
        """What replaces "shadowed, not missing", and it is strictly worse.

        An EMPTY assignment at a more specific tier beats a real value upstream,
        because sourcing is assignment. The key is set, so nothing calls it
        missing; a value exists, so nothing calls it unconfigured; and the
        integration gets "". Live on this estate: two fleets carry a pristine
        `export GITHUB_PAT=` scaffold stub.
        """
        root, fleet_dir, fleet, paths = estate
        _write_env(root / ".env", ACME_TOKEN="the-good-one")
        (fleet_dir / ".env").write_text("export ACME_TOKEN=\n")
        findings, _ = creds.reconcile(paths, fleet)
        row = next(f for f in _shape(findings, 1) if f.subject == "ACME_TOKEN")
        assert row.verdict == "FAIL"
        assert "BLANKS" in row.detail
        assert "root" in row.detail

    def test_a_host_tier_credential_is_not_called_missing(self, estate):
        """The defect alex found, as a unit test.

        A value in `~/.env` resolves at boot. The old reader enumerated root and
        fleet only and reported "absent from every tier" — false, and it sends
        an operator to provision a credential that is already there.
        """
        _root, _fleet_dir, fleet, paths = estate
        _write_env(Path(os.environ["HOME"]) / ".env", ACME_TOKEN="from-host")
        findings, _ = creds.reconcile(paths, fleet)
        row = next(f for f in _shape(findings, 1) if f.subject == "ACME_TOKEN")
        assert row.verdict == "OK", row.detail
        assert "host" in row.detail


class TestShapeTwo:
    def test_value_with_no_equipped_consumer_fails(self, estate):
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="x", ORPHAN_TOKEN="y")
        findings, _ = creds.reconcile(paths, fleet)
        subjects = {f.subject for f in _shape(findings, 2)}
        assert "ORPHAN_TOKEN" in subjects
        assert "ACME_TOKEN" not in subjects

    def test_empty_orphan_is_not_reported(self, estate):
        """An empty key is not a stored credential — reporting it as dead weight
        would bury the real ones."""
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="x", ORPHAN_TOKEN="")
        findings, _ = creds.reconcile(paths, fleet)
        assert "ORPHAN_TOKEN" not in {f.subject for f in _shape(findings, 2)}


class TestShapeThreeIsNeverSilent:
    """The property the whole module turns on."""

    def test_unknown_is_emitted_for_every_equipped_integration(self, estate):
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="x")
        findings, _ = creds.reconcile(paths, fleet)
        rows = _shape(findings, 3)
        assert [r.verdict for r in rows] == ["UNKNOWN"]
        assert "no consumer contract published for acme" in rows[0].detail

    def test_unknown_is_counted_in_the_report(self, estate):
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="x")
        findings, scope = creds.reconcile(paths, fleet)
        text = creds.format_report(findings, scope)
        assert "unknown=1" in text
        assert "This is a gap, not a pass." in text

    def test_unknown_alone_does_not_fail_the_run(self, estate):
        """A disclosed gap is not a defect. If UNKNOWN exited nonzero, operators
        would suppress the check and lose shapes 1 and 2 with it."""
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", ACME_TOKEN="x")
        findings, _ = creds.reconcile(paths, fleet)
        assert not creds.exits_nonzero(findings)

    def test_published_contract_resolves_to_ok(self, estate):
        root, fleet_dir, fleet, paths = estate
        (root / "library" / "integrations" / "acme.md").write_text(
            INTEGRATION.replace(
                "---\n\n# Acme",
                "consumer_contract:\n  ACME_TOKEN:\n    read_by: acme-cli\n---\n\n# Acme",
            )
        )
        _write_env(fleet_dir / ".env", ACME_TOKEN="x")
        findings, _ = creds.reconcile(paths, fleet)
        row = _shape(findings, 3)[0]
        assert row.verdict == "OK"
        assert "ACME_TOKEN" in row.detail

    def test_a_failing_run_still_exits_nonzero(self, estate):
        _root, fleet_dir, fleet, paths = estate
        _write_env(fleet_dir / ".env", NOTHING="x")
        findings, _ = creds.reconcile(paths, fleet)
        assert creds.exits_nonzero(findings)


class TestRedaction:
    def test_no_credential_value_reaches_the_report(self, estate):
        """This output goes into PR bodies and chat."""
        root, fleet_dir, fleet, paths = estate
        _write_env(root / ".env", ACME_TOKEN="SUPERSECRETVALUE")
        _write_env(fleet_dir / ".env", ORPHAN="ANOTHERSECRET")
        findings, scope = creds.reconcile(paths, fleet)
        text = creds.format_report(findings, scope)
        assert "SUPERSECRETVALUE" not in text
        assert "ANOTHERSECRET" not in text
        # ...while still naming the KEY, which is the actionable half.
        assert "ACME_TOKEN" in text
        assert "ORPHAN" in text

    def test_public_reconcile_keeps_failures_and_unknown_without_values(
        self, estate, monkeypatch, capsys
    ):
        from claudlobby import context
        from claudlobby.__main__ import main

        root, fleet_dir, _fleet, paths = estate
        monkeypatch.setattr(context, "get_resources", lambda: paths.package)
        _write_env(root / ".env", ACME_TOKEN="SUPERSECRETVALUE")
        _write_env(fleet_dir / ".env", ORPHAN="ANOTHERSECRET")
        assert main(["--root", str(root), "--fleet", "t", "host", "credentials",
                     "reconcile", "--json"]) == 4
        output = capsys.readouterr().out
        result = json.loads(output)
        assert result["command"] == "host.credentials.reconcile"
        assert result["data"]["failed"] == 1
        assert result["data"]["unknown"] == 1
        assert {row["subject"] for row in result["data"]["findings"]} == {
            "ACME_TOKEN", "ORPHAN", "acme"}
        assert "SUPERSECRETVALUE" not in output and "ANOTHERSECRET" not in output
        with pytest.raises(SystemExit):
            main(["--root", str(root), "--fleet", "t", "creds-reconcile"])


def test_selected_credential_check_passes_exact_fleet_and_refuses_foreign_bot(
    tmp_path, monkeypatch
):
    from claudlobby import credential_check as check
    from claudlobby.config import BotConfig, FleetConfig
    from claudlobby.config_plan import path_state
    from claudlobby.paths import Paths
    from tests.package_fixtures import source_package

    root = tmp_path / "root"
    fleet_root = root / "local" / "f"
    fleet_root.mkdir(parents=True)
    manifest = fleet_root / "fleet.yaml"
    manifest.write_text("fleet: {name: f}\n")
    other_root = root / "local" / "g"
    other_root.mkdir(parents=True)
    other_manifest = other_root / "fleet.yaml"
    other_manifest.write_text("fleet: {name: g}\n")
    paths = Paths(root=root, fleet_dir=fleet_root, package=source_package())
    other_paths = Paths(root=root, fleet_dir=other_root, package=source_package())
    fleet = FleetConfig(name="f", manager="manager", service_prefix="test",
                        bots={name: BotConfig(bot_id=name, name=name, expertise=["x"])
                              for name in ("manager", "worker")})
    destination = SimpleNamespace(paths=paths, fleet=fleet)
    other_fleet = FleetConfig(name="g", manager="manager", service_prefix="test",
                              bots={"manager": BotConfig(bot_id="manager", name="manager",
                                                         expertise=["x"])})
    other_destination = SimpleNamespace(paths=other_paths, fleet=other_fleet)
    release_id = "r-" + "a" * 64
    release = SimpleNamespace(release_id=release_id, native_path=paths.lib)
    plan = SimpleNamespace(
        effects={"fleet_sources": {"f": {"fleet": {"path": str(manifest)}},
                                   "g": {"fleet": {"path": str(other_manifest)}}}},
        inputs={str(path): {"state": path_state(path, source=True)}
                for path in (manifest, other_manifest)},
        frozen_input=lambda source, required: (
            Path(source["path"]), Path(source["path"]).read_bytes()))

    @contextmanager
    def admitted(root, expected_release=None):
        yield release

    monkeypatch.setattr(check, "mutation_admission", admitted)
    monkeypatch.setattr(check, "read_selection", lambda root: {
        "release_id": release_id, "plan_id": "p"})
    monkeypatch.setattr(check, "read_plan", lambda root, plan_id: plan)
    monkeypatch.setattr(check, "native_environment", lambda paths: {
        "CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(paths.fleet_config_dir),
        "CLAUDLOBBY_NATIVE_DIR": str(paths.lib),
        "CLAUDLOBBY_CLI": str(tmp_path / "selected-cli"),
        "CLAUDLOBBY_RELEASE_ID": release_id})
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "tick-complete\n", "")

    monkeypatch.setattr(check.subprocess, "run", run)
    monkeypatch.setattr(check, "resolve_operation_scope", lambda **kwargs: (
        destination, SimpleNamespace(bot_id="worker")))
    with pytest.raises(check.CredentialCheckError, match="only the selected fleet manager"):
        check.check_credentials(root=root, fleet="f")
    assert not calls

    monkeypatch.setattr(check, "resolve_operation_scope", lambda **kwargs: (
        destination, SimpleNamespace(bot_id="manager")))
    result = check.check_credentials(root=root, fleet="f")
    assert result.fleet == "f" and result.release_id == release_id
    argv, kwargs = calls[0]
    assert argv == [str(paths.lib / "creds-check.sh"), "--selected-release", release_id,
                    "--fleet", "f", "--fleet-root", str(fleet_root),
                    "--bots-dir", str(paths.runtime_bots),
                    "--bot", "manager", "--bot", "worker"]
    assert kwargs["env"]["FLEET_ROOT"] == str(fleet_root)
    assert result.state_path == root / "state/creds-check/f.json"
    assert kwargs["env"]["CLAUDLOBBY_CREDS_STATE"] == str(result.state_path)
    monkeypatch.setattr(check, "resolve_operation_scope", lambda **kwargs: (
        other_destination, SimpleNamespace(bot_id="manager")) if kwargs["fleet"] == "g" else (
        destination, SimpleNamespace(bot_id="manager")))
    other_result = check.check_credentials(root=root, fleet="g")
    assert other_result.state_path == root / "state/creds-check/g.json"
    assert calls[1][1]["env"]["CLAUDLOBBY_CREDS_STATE"] == str(other_result.state_path)
    assert calls[1][1]["env"]["FLEET_ROOT"] == str(other_root)
    assert other_result.state_path != result.state_path
    manifest.write_text("fleet: {name: other}\n")
    with pytest.raises(check.CredentialCheckError, match="source changed"):
        check.check_credentials(root=root, fleet="f")
    assert len(calls) == 2, "a changed source must be refused before another probe"


def test_public_credential_check_reports_tick_not_health(estate, monkeypatch, capsys):
    from claudlobby import context
    from claudlobby.__main__ import main
    from claudlobby import credential_check

    root, _fleet_dir, _fleet, paths = estate
    monkeypatch.setattr(context, "get_resources", lambda: paths.package)
    monkeypatch.setattr(credential_check, "check_credentials", lambda **kwargs:
                        SimpleNamespace(fleet="t", release_id="r-selected",
                                        checks="tick_completed", health="unobserved",
                                        state_path=root / "state/creds-check/t.json"))
    assert main(["--root", str(root), "--fleet", "t", "host", "credentials",
                 "check", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "host.credentials.check"
    assert result["data"]["credential_health"] == "unobserved"
    assert result["data"]["state_path"] == str(root / "state/creds-check/t.json")
    assert main(["--root", str(root), "--fleet", "t", "host", "credentials",
                 "check"]) == 0
    assert str(root / "state/creds-check/t.json") in capsys.readouterr().out


class TestTierResolution:
    def test_resolution_reads_every_tier_the_runtime_reads(self, estate):
        """INVERTS `test_visible_tier_follows_the_shipped_resolver`.

        WHY THE OLD ASSERTION WAS WRONG — the claim a reviewer should check,
        rather than whether the new one looks right. It asserted that the label
        is "root" when only root holds a value and "fleet:t" once the fleet does
        — i.e. that exactly ONE tier is consulted and which one flips. The
        runtime consults FOUR and merges them, so that assertion certified a
        narrower reader than the system has. It was not a weak test; it was an
        accurate description of the defect, which is why the suite stayed green
        while `~/.env` credentials were reported "absent from every tier".

        The name carried the premise too: "the shipped resolver" meant
        `Paths.env_file` when it was written, and `Paths.env_file` is the WRITE
        target. Leaving the name would hand the next reader the old framing as
        intent.
        """
        root, fleet_dir, _fleet, paths = estate
        _write_env(Path(os.environ["HOME"]) / ".env", HOST_ONLY="h")
        _write_env(root / ".env", ROOT_ONLY="r")
        _write_env(fleet_dir / ".env", FLEET_ONLY="f")
        _label, values, resolutions = creds.resolved_view(paths)
        assert values["HOST_ONLY"] == "h"
        assert values["ROOT_ONLY"] == "r"
        assert values["FLEET_ONLY"] == "f"
        assert resolutions["HOST_ONLY"].tier == "host"
        assert resolutions["FLEET_ONLY"].tier == "fleet"

    def test_the_most_specific_assignment_decides(self, estate):
        """The half the old one-tier reader could not express at all."""
        root, fleet_dir, _fleet, paths = estate
        _write_env(Path(os.environ["HOME"]) / ".env", A="host")
        _write_env(root / ".env", A="root")
        _write_env(fleet_dir / ".env", A="fleet")
        _label, values, resolutions = creds.resolved_view(paths)
        assert values["A"] == "fleet"
        assert resolutions["A"].shadowed == ("host", "root")

    def test_all_four_tiers_enumerated_not_two(self, estate):
        """INVERTS `test_both_tiers_enumerated_even_though_one_is_read`.

        WHY THE OLD ASSERTION WAS WRONG: it pinned the enumeration to exactly
        `["root", "fleet:t"]`, so a host-tier credential could not appear in the
        inventory even in principle. The equality made it a ceiling, not a
        floor — adding the tier the runtime reads first would have failed it.
        """
        root, fleet_dir, _fleet, paths = estate
        _write_env(Path(os.environ["HOME"]) / ".env", H="0")
        _write_env(root / ".env", A="1")
        _write_env(fleet_dir / ".env", B="2")
        assert [t[0] for t in creds.env_tiers(paths)] == ["host", "root", "fleet:t"]
