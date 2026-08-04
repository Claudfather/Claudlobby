"""Every script a composed unit ExecStarts must be executable in git.

`lib/host-health-check.sh` shipped mode 100644. systemd's ExecStart requires the
executable bit, so its composed unit died `203/EXEC` on every deployment from the
day it was added, and nothing caught it: the unit enrolls fine, the timer arms
fine, and the failure only exists at fire time on the host.

Deriving the set — and why not from `system.yaml`
-------------------------------------------------
The first version of this gate read `claudlobby/system.yaml`'s `script:` keys.
That is a **proxy** for "what a unit ExecStarts", and two paths fall through it:

- `composer.py:2990` hardcodes `$CLAUDLOBBY_ROOT/lib/briefing-trigger.sh` for the
  per-(bot,slot) briefing units — it never appears in `system.yaml`.
- `composer.py:1075` emits `ExecStart={paths.lib}/start-bot.sh` for **every**
  per-bot service — likewise absent from `system.yaml`.

Both are 100755 today, so the proxy never produced a wrong answer; it produced an
answer about a smaller set than the docstring claimed. This version removes the
proxy by **composing for real** and parsing `ExecStart=` out of the units the
composer actually writes. A future hardcoded path is then covered automatically,
which an explicit list would not be — a list is just a second proxy that drifts
the same way.

Composing is also why this cannot be done by parsing composed units on disk:
those live under gitignored `runtime/` and do not exist on a clean CI clone, so
such a gate would pass by finding nothing. It composes into `tmp_path` instead.

Scope stays narrow, deliberately: a script is in scope **because a unit
ExecStarts it**, not because it lives in `lib/`. Files that are sourced
(`. file`) or invoked as `bash file` do not need the bit, and demanding it of
them would be cleanup wearing a correctness gate's clothes.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby.composer import (
    compose_fleet_timers,
    compose_host_timers,
    compose_systemd_unit,
)
from claudlobby.config import load_fleet
from claudlobby.paths import Paths

REPO_DIR = Path(__file__).resolve().parent.parent

# system_defaults: true so the fleet default jobs compose too — with it false the
# derived set silently loses seven of them, which the non-vacuity guard catches.
# The briefing slot exercises composer.py:2990; the bot itself, composer.py:1075.
_FLEET = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      system_defaults: true
      bots:
        kev:
          expertise: [eng]
          briefing:
            slots:
              morning: "*-*-* 08:30:00"
"""

_EXEC_START = re.compile(r"^ExecStart=(.+)$", re.M)
# Any lib/ script named on an ExecStart line, however it was interpolated
# ($CLAUDLOBBY_ROOT/lib/x.sh, {paths.lib}/x.sh, or an absolute rendered path).
_LIB_SCRIPT = re.compile(r"[^\s=]*/lib/([A-Za-z0-9._-]+\.(?:sh|py))")


def _scripts_from_units(text: str) -> set[str]:
    out = set()
    for line in _EXEC_START.findall(text):
        for name in _LIB_SCRIPT.findall(line):
            out.add(f"lib/{name}")
    return out


@pytest.fixture(scope="module")
def execstarted_scripts(tmp_path_factory) -> set[str]:
    """The real set: compose every unit family and read their ExecStart lines."""
    tmp = tmp_path_factory.mktemp("execbit")
    fleet_dir = tmp / "f"
    fleet_dir.mkdir(parents=True, exist_ok=True)
    (fleet_dir / "fleet.yaml").write_text(dedent(_FLEET))
    fleet, merged = load_fleet(fleet_dir / "fleet.yaml")
    paths = Paths(root=fleet_dir, fleet_dir=fleet_dir)

    found: set[str] = set()

    # 1. Host singletons (system.yaml host.jobs) — where host-health-check lives.
    host_dir = compose_host_timers(paths, output_dir=tmp / "host")
    # 2. Fleet jobs + the hardcoded briefing family (composer.py:2990).
    fleet_timers = compose_fleet_timers(fleet, paths, merged, output_dir=tmp / "fleet")
    for d in (host_dir, fleet_timers):
        for unit in Path(d).rglob("*.service"):
            found |= _scripts_from_units(unit.read_text())

    # 3. The per-bot service (composer.py:1075) — start-bot.sh.
    for bot in fleet.bots.values():
        found |= _scripts_from_units(compose_systemd_unit(bot, fleet, paths))

    return found


def git_mode(rel_path: str) -> str | None:
    """The mode git records for a tracked path, or None if untracked."""
    out = subprocess.run(
        ["git", "ls-files", "-s", "--", rel_path],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    ).stdout.split()
    return out[0] if out else None


def test_composition_yields_scripts(execstarted_scripts):
    """Non-vacuous: a gate that finds nothing to check passes by accident.

    Named members, not just a count — the two that the previous system.yaml-based
    derivation could not see are asserted explicitly, so if composition regresses
    and stops emitting them this fails loud instead of silently checking less.
    """
    assert len(execstarted_scripts) >= 10, (
        f"composed only {len(execstarted_scripts)} ExecStart scripts "
        f"({sorted(execstarted_scripts)}) — composition or the parser drifted"
    )
    for required in (
        "lib/host-health-check.sh",  # host job, the original defect
        "lib/briefing-trigger.sh",  # hardcoded at composer.py:2990
        "lib/start-bot.sh",  # per-bot service, composer.py:1075
    ):
        assert required in execstarted_scripts, (
            f"{required} is ExecStarted by a composed unit but did not appear in "
            f"the derived set — coverage regressed: {sorted(execstarted_scripts)}"
        )


def test_every_execstarted_script_is_tracked(execstarted_scripts):
    missing = sorted(p for p in execstarted_scripts if git_mode(p) is None)
    assert not missing, (
        "a composed unit ExecStarts a script that is not tracked in git: "
        + ", ".join(missing)
    )


def test_every_execstarted_script_is_executable_in_git(execstarted_scripts):
    """The gate. A 100644 script ExecStarted by a unit dies 203/EXEC on the host."""
    bad = sorted(
        (p, m)
        for p in execstarted_scripts
        if (m := git_mode(p)) not in (None, "100755")
    )
    assert not bad, (
        "these scripts are ExecStarted by a composed unit but are not executable "
        "in git — their units will fail 203/EXEC on every host:\n"
        + "\n".join(f"  {p}  mode {m}" for p, m in bad)
        + "\n\nFix: git update-index --chmod=+x <path>"
    )
