"""A failed fleet job unit raises one alert (#2522).

Every service the composer generates for a fleet job names the fleet's failure
template in OnFailure=. systemd starts one instance of the template per failed
unit (``%n`` names it), and that instance runs the selected release's
``fleet notify`` for the unit. Bot services stay out: fleet-pulse watches them.
launchd has no OnFailure=, so the plists carry nothing (#2532 covers them).
"""

from __future__ import annotations

import shlex
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby import config_staging, config_units
from claudlobby.composer import compose_fleet_timers, compose_systemd_unit
from claudlobby.config import load_fleet
from claudlobby.config_plan import PlanError
from claudlobby.config_units import planned_units
from claudlobby.context import native_environment
from claudlobby.paths import Paths
from claudlobby.runtime_admission import unit_systemd_environment
from claudlobby.supervision_inventory import InventoryError, UnitDeclaration
from tests.package_fixtures import source_package
from tests.test_config_staging import staging_case  # noqa: F401
from tests.test_supervision_inventory import Observations

PREFIX = "com.test"
TEMPLATE = f"{PREFIX}.job-failed@.service"
ON_FAILURE = f"{PREFIX}.job-failed@%n.service"

# All three families of fleet job units: the system.yaml jobs, the opt-in
# code-audit sweep and a briefing slot.
_FLEET = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      manager: lead
      bots:
        lead:
          expertise: [eng]
          manages: [builder]
          briefing:
            slots:
              morning: "*-*-* 08:30:00"
        builder:
          expertise: [eng]
          scope:
            repos: [acme/alpha]
      sweep:
        owner_bot: builder
        repos: [acme/alpha]
        schedule: "*-*-* 02:30:00"
"""

# The same fleet with every source of job units switched off.
_FLEET_NO_JOBS = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      manager: lead
      system_defaults: false
      bots:
        lead:
          expertise: [eng]
"""


def _compose(root: Path, body: str = _FLEET):
    root.mkdir(parents=True, exist_ok=True)
    (root / "fleet.yaml").write_text(dedent(body))
    fleet, merged = load_fleet(root / "fleet.yaml")
    paths = Paths(root=root, fleet_dir=root, package=source_package())
    return fleet, paths, compose_fleet_timers(fleet, paths, merged)


def _sections(text: str) -> dict[str, list[tuple[str, str]]]:
    """A unit file's directives by section, comments and blank lines skipped."""
    sections: dict[str, list[tuple[str, str]]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
            continue
        key, _, value = line.partition("=")
        current.append((key, value))
    return sections


def _values(text: str, section: str, key: str) -> list[str]:
    return [value for name, value in _sections(text).get(section, []) if name == key]


def _environment(text: str) -> dict[str, str]:
    return dict(item.split("=", 1) for value in _values(text, "Service", "Environment")
                for item in shlex.split(value))


def _job_services(timers: Path) -> list[Path]:
    return sorted(path for path in timers.glob(f"{PREFIX}.*.service") if "@" not in path.name)


def test_every_fleet_job_service_names_the_failure_template(tmp_path):
    _, _, timers = _compose(tmp_path / "f")
    services = _job_services(timers)
    assert {f"{PREFIX}.fleet-pulse.service", f"{PREFIX}.code-audit-sweep.service",
            f"{PREFIX}.briefing-lead-morning.service"} <= {path.name for path in services}
    for service in services:
        assert _values(service.read_text(), "Unit", "OnFailure") == [ON_FAILURE], service.name


def test_timers_plists_and_bot_services_carry_no_failure_hook(tmp_path):
    fleet, paths, timers = _compose(tmp_path / "f")
    for path in (*timers.glob("*.timer"), *timers.glob("*.plist")):
        assert "OnFailure" not in path.read_text(), path.name
    for bot in fleet.bots.values():
        assert "OnFailure" not in compose_systemd_unit(bot, fleet, paths, boot_delay_s=0), bot.bot_id


def test_the_failure_template_runs_the_selected_cli_fleet_notify_for_the_failed_unit(tmp_path):
    fleet, paths, timers = _compose(tmp_path / "f")
    text = (timers / TEMPLATE).read_text()
    [exec_start] = _values(text, "Service", "ExecStart")
    assert shlex.split(exec_start) == [
        native_environment(paths)["CLAUDLOBBY_CLI"], "--root", str(paths.root), "--fleet", fleet.name,
        "fleet", "notify", "--level", "alert", "--event", "job_failed",
        "--message", "%i failed: journalctl --user -u %i"]
    assert _values(text, "Service", "Type") == ["oneshot"]
    assert _values(text, "Service", "WorkingDirectory") == [str(paths.root)]
    # The fleet job carriers `fleet notify` binds from a timer, with no bot origin
    # (test_operation_context::test_generated_fleet_timer_binds_scope_without_inventing_bot_origin):
    # every assignment the template makes is the one a job service makes.
    template_env = _environment(text)
    job_env = _environment((timers / f"{PREFIX}.fleet-pulse.service").read_text())
    assert {"CLAUDLOBBY_ROOT", "FLEET_ROOT", "CLAUDLOBBY_FLEET", "CLAUDLOBBY_CLI", "PATH"} <= template_env.keys()
    assert template_env["CLAUDLOBBY_FLEET"] == fleet.name
    assert {key: job_env.get(key) for key in template_env} == template_env


@pytest.mark.parametrize("tier", [{"PLANE_EMIT_ENABLED": "1", "PLANE_EMIT_DISABLED": "0"},
                                  {"PLANE_EMIT_ENABLED": "0"}], ids=["armed", "unarmed"])
def test_the_failure_template_carries_the_plane_switch_lines_of_the_job_units(tmp_path, monkeypatch, tier):
    """A named fleet job unit carries the plane switches its tier resolves
    (test_plane_readers_unassigned), and the template carries the same lines."""
    import claudlobby.env_tiers as env_tiers_mod
    resolved = {key: env_tiers_mod.Resolution(name=key, value=value, tier="fleet", path=None)
                for key, value in tier.items()}
    monkeypatch.setattr(env_tiers_mod, "read_tiers", lambda paths, fleet_name=None, bot_name=None: [])
    monkeypatch.setattr(env_tiers_mod, "cascade", lambda tiers: resolved)
    _, _, timers = _compose(tmp_path / "f")

    def plane(path: Path) -> dict[str, str]:
        return {key: value for key, value in _environment(path.read_text()).items()
                if key.startswith("PLANE_")}

    expected = {"PLANE_EMIT_ENABLED": "1", "PLANE_EMIT_DISABLED": "0"} if tier["PLANE_EMIT_ENABLED"] == "1" else {}
    assert plane(timers / f"{PREFIX}.fleet-pulse.service") == expected
    assert plane(timers / TEMPLATE) == expected


def test_the_failure_template_has_no_install_section_and_no_failure_hook_of_its_own(tmp_path):
    _, _, timers = _compose(tmp_path / "f")
    sections = _sections((timers / TEMPLATE).read_text())
    assert set(sections) == {"Unit", "Service"}
    assert "OnFailure" not in dict(sections["Unit"])


def test_a_fleet_that_stops_composing_job_units_loses_its_failure_template(tmp_path):
    root = tmp_path / "f"
    _, _, timers = _compose(root)
    assert (timers / TEMPLATE).is_file()
    _compose(root, _FLEET_NO_JOBS)
    assert not list(timers.glob(f"{PREFIX}.*"))


# ---------------------------------------------------------------------------
# The plan manifest: the template is a Linux unit with no plist and no admission
# ---------------------------------------------------------------------------
def test_the_staged_plan_declares_the_template_for_linux_only(staging_case):
    plan = config_staging.stage_configuration([staging_case.paths], staging_case.release)
    name = "com.primary.job-failed@.service"
    linux = [(d, item) for d, item in planned_units(plan, "Linux") if d.source.name == name]
    assert len(linux) == 1
    assert not [d for d, _ in planned_units(plan, "Darwin") if "job-failed" in d.source.name]
    declaration, item = linux[0]
    assert (item["enroll"], item["phase"], item["admission"], item["service"], item["scope"],
            item["fleet"]) == (True, "producers", None, None, "fleet", "primary")
    jobs = [item for d, item in planned_units(plan, "Linux")
            if d.scope == "fleet" and d.source.suffix == ".service" and "@" not in d.source.name]
    assert jobs and all((job["environment"], job["working_directory"])
                        == (item["environment"], item["working_directory"]) for job in jobs)
    assert item["environment"]["CLAUDLOBBY_RELEASE_ID"] == staging_case.release.release_id
    assert declaration.service is None and declaration.fleet == "primary"


def _template_bytes(environment: dict, working: str, cli: str | None = None) -> bytes:
    return ("[Unit]\nDescription=claudlobby job-failed alert for %i (alpha)\n\n[Service]\nType=oneshot\n"
            f"WorkingDirectory={working}\n"
            + "".join(unit_systemd_environment(key, value) + "\n" for key, value in environment.items())
            + f'ExecStart="{cli or environment["CLAUDLOBBY_CLI"]}" "--root" "{working}" "--fleet" "alpha" '
              '"fleet" "notify" "--level" "alert" "--event" "job_failed" '
              '"--message" "%i failed: journalctl --user -u %i"\n').encode()


def test_template_units_claims_only_the_identity_its_own_bytes_spell(tmp_path):
    environment = {"CLAUDLOBBY_ROOT": "/data", "CLAUDLOBBY_CLI": "/data/state/releases/r-1/venv/bin/claudlobby"}
    family = {"working_directory": "/data", "environment": environment}
    content = _template_bytes(environment, "/data")
    # By module attribute, so that on a tree without it only this test fails.
    template_units = config_units.template_units
    [item] = template_units({TEMPLATE: (content, 0o644)}, [family, dict(family)], destination=tmp_path,
                            scope="fleet", release_id="r-1", fleet="alpha")
    assert item["source"] == str(tmp_path / TEMPLATE) and item["platform"] == "Linux"
    assert (item["environment"], item["working_directory"], item["admission"]) == (environment, "/data", None)
    other = {"working_directory": "/data", "environment": {**environment, "CLAUDLOBBY_CLI": "/elsewhere"}}
    for families, scope in (([], "fleet"), ([family, other], "fleet"), ([family], "host")):
        with pytest.raises(PlanError, match="one fleet identity"):
            template_units({TEMPLATE: (content, 0o644)}, families, destination=tmp_path,
                           scope=scope, release_id="r-1", fleet="alpha")
    with pytest.raises(PlanError, match="differs from its fleet identity"):
        template_units({TEMPLATE: (content, 0o644)}, [other], destination=tmp_path,
                       scope="fleet", release_id="r-1", fleet="alpha")


# ---------------------------------------------------------------------------
# The activation inventory owns the declared template and its instances
# ---------------------------------------------------------------------------
def _declare_template(obs: Observations) -> Path:
    content = _template_bytes(obs.env, str(obs.root))
    source = obs.root / "generated" / TEMPLATE
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(content)
    installed = obs.installed / TEMPLATE
    installed.write_bytes(content)
    obs.rows.append(f"installed\t{TEMPLATE}")
    obs.declarations.append(UnitDeclaration(source, "fleet", obs.root, "reviewed-release",
                                            tuple(obs.env.items()), fleet="alpha"))
    return installed


def _load_instance(obs: Observations, unit: str, template: Path) -> str:
    """The instance systemd loads for a unit whose OnFailure= names the template."""
    name = f"{PREFIX}.job-failed@{unit}.service"
    obs.rows.append(f"loaded\t{name}")
    obs.extra[name] = {
        "Id": name, "LoadState": "loaded", "ActiveState": "inactive", "UnitFileState": "static",
        "FragmentPath": str(template), "WorkingDirectory": str(obs.root),
        "Environment": " ".join(shlex.quote(f"{key}={value}") for key, value in obs.env.items()),
        "ExecStart": f"{{ path={obs.env['CLAUDLOBBY_CLI']} ; }}", "DropInPaths": "",
        "NeedDaemonReload": "no", "Triggers": "", "TriggeredBy": ""}
    return name


def test_the_inventory_owns_the_declared_template_and_refuses_one_installed_by_hand(tmp_path):
    obs = Observations(tmp_path)
    obs.add(f"{PREFIX}.creds-check.service", scope="fleet")
    template = _declare_template(obs)
    instance = _load_instance(obs, f"{PREFIX}.creds-check.service", template)
    inventory = obs.collect().require_complete()
    assert TEMPLATE in {unit.target for unit in inventory.units}
    assert str(template) not in inventory.foreign
    assert ("svc_inventory_properties", [TEMPLATE]) not in obs.calls  # systemctl refuses a template name
    assert ("svc_inventory_properties", [instance]) in obs.calls
    # The control: a template installed by hand whose command runs from under
    # the data root is still an owned unit that no plan declares.
    (obs.installed / "fleet-unit-failed@.service").write_text(
        "[Unit]\nDescription=raise a fleet alert that %i failed\n\n[Service]\nType=oneshot\n"
        f"ExecStart={obs.root}/local/ops/runtime/bots/b/.cli/bin/claudlobby --json --root {obs.root} "
        '--fleet ops fleet notify --level alert --event host_unit_failed --message "%i failed"\n')
    with pytest.raises(InventoryError, match="fleet-unit-failed@.service: owned consumer is absent"):
        obs.collect().require_complete()


def test_an_operator_unit_attached_by_the_documented_drop_in_does_not_trip_the_inventory(tmp_path):
    obs = Observations(tmp_path)
    obs.add(f"{PREFIX}.creds-check.service", scope="fleet")
    template = _declare_template(obs)
    operator = obs.installed / "monitor.service"
    operator.write_text("[Unit]\nDescription=an operator's monitor\n\n[Service]\nType=oneshot\n"
                        "ExecStart=/usr/bin/true\n")
    dropin = obs.installed / "monitor.service.d/20-on-failure.conf"
    dropin.parent.mkdir()
    dropin.write_text(f"[Unit]\nOnFailure={PREFIX}.job-failed@%n.service\n")
    obs.rows += ["installed\tmonitor.service", "loaded\tmonitor.service"]
    obs.extra["monitor.service"] = {
        "Id": "monitor.service", "LoadState": "loaded", "ActiveState": "inactive",
        "UnitFileState": "static", "FragmentPath": str(operator), "WorkingDirectory": "",
        "Environment": "", "ExecStart": "{ path=/usr/bin/true ; }", "DropInPaths": str(dropin),
        "NeedDaemonReload": "no", "Triggers": "", "TriggeredBy": ""}
    instance = _load_instance(obs, "monitor.service", template)
    inventory = obs.collect().require_complete()
    assert str(operator) in inventory.foreign
    assert instance not in {unit.target for unit in inventory.units}
    # Fail closed: the instance must be the declared template's exact definition.
    for key, value in (("DropInPaths", str(obs.installed / f"{PREFIX}.job-failed@.service.d/x.conf")),
                       ("FragmentPath", str(tmp_path / "elsewhere.service")),
                       ("NeedDaemonReload", "yes")):
        saved = obs.extra[instance][key]
        obs.extra[instance][key] = value
        with pytest.raises(InventoryError, match=f"{instance}: instance of a declared template"):
            obs.collect().require_complete()
        obs.extra[instance][key] = saved
    (obs.installed / instance).write_bytes(template.read_bytes())  # a file of its own
    with pytest.raises(InventoryError, match=f"{instance}: instance of a declared template"):
        obs.collect().require_complete()
    (obs.installed / instance).unlink()
    # While activation has the template parked, the instance reads not-found.
    template.unlink()
    obs.extra[instance].update(LoadState="not-found", FragmentPath="", UnitFileState="")
    assert obs.collect().require_complete()
