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

from claudlobby.composer import compose_fleet_timers, compose_systemd_unit
from claudlobby.config import load_fleet
from claudlobby.context import native_environment
from claudlobby.paths import Paths
from tests.package_fixtures import source_package

PREFIX = "com.test"
TEMPLATE = f"{PREFIX}.job-failed@.service"
ON_FAILURE = f"{PREFIX}.job-failed@%n.service"

# All three families of fleet job units: the system.yaml jobs, the opt-in
# code-audit sweep and a briefing slot.
_FLEET = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      manager: kev
      bots:
        kev:
          expertise: [eng]
          manages: [mason]
          briefing:
            slots:
              morning: "*-*-* 08:30:00"
        mason:
          expertise: [eng]
          scope:
            repos: [acme/alpha]
      sweep:
        owner_bot: mason
        repos: [acme/alpha]
        schedule: "*-*-* 02:30:00"
"""

# The same fleet with every source of job units switched off.
_FLEET_NO_JOBS = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      manager: kev
      system_defaults: false
      bots:
        kev:
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


def _job_services(timers: Path) -> list[Path]:
    return sorted(path for path in timers.glob(f"{PREFIX}.*.service") if "@" not in path.name)


def test_every_fleet_job_service_names_the_failure_template(tmp_path):
    _, _, timers = _compose(tmp_path / "f")
    services = _job_services(timers)
    assert {f"{PREFIX}.fleet-pulse.service", f"{PREFIX}.code-audit-sweep.service",
            f"{PREFIX}.briefing-kev-morning.service"} <= {path.name for path in services}
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
