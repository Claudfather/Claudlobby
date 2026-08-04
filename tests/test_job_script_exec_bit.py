"""Every script a composed unit ExecStarts must be executable in git.

`lib/host-health-check.sh` shipped mode 100644. systemd's ExecStart requires the
executable bit, so its composed unit died `203/EXEC` on every deployment from the
day it was added, and nothing caught it: the unit enrolls fine, the timer arms
fine, and the failure only exists at fire time on the host.

The gate reads `claudlobby/system.yaml` rather than composed unit files. That is
the point — composed units live under gitignored `runtime/` and do not exist on a
clean CI clone, so a gate that parsed them would pass by finding nothing, which is
the same manufactured all-clear the bug itself had. The manifest is in-repo,
declarative, and is the SSOT the composer renders ExecStart from.

Scope is deliberately narrow: a script is in scope **because a unit ExecStarts
it**, not because it lives in `lib/`. Files that are sourced (`. file`) or invoked
as `bash file` do not need the bit, and demanding it of them would be cleanup
wearing a correctness gate's clothes.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SYSTEM_YAML = REPO_DIR / "claudlobby" / "system.yaml"

# `script:` values look like "$CLAUDLOBBY_ROOT/lib/foo.sh" and may carry
# arguments ("… /lib/data-sweep.sh --purge"), which are not part of the path.
_SCRIPT_LINE = re.compile(r'^\s*script:\s*["\']?(\S+)')


def job_script_paths() -> list[str]:
    """Repo-relative paths of every script a composed unit ExecStarts."""
    paths = []
    for line in SYSTEM_YAML.read_text().splitlines():
        m = _SCRIPT_LINE.match(line)
        if not m:
            continue
        raw = m.group(1).split()[0].strip("\"'")
        if "$CLAUDLOBBY_ROOT/" not in raw:
            continue
        paths.append(raw.split("$CLAUDLOBBY_ROOT/", 1)[1])
    return sorted(set(paths))


def git_mode(rel_path: str) -> str | None:
    """The mode git records for a tracked path, or None if untracked."""
    out = subprocess.run(
        ["git", "ls-files", "-s", "--", rel_path],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    ).stdout.split()
    return out[0] if out else None


def test_manifest_yields_scripts():
    """Non-vacuous: a gate that finds nothing to check passes by accident."""
    paths = job_script_paths()
    assert len(paths) >= 10, f"parsed only {len(paths)} script paths — parser drifted"
    assert "lib/host-health-check.sh" in paths


def test_every_job_script_is_tracked():
    missing = [p for p in job_script_paths() if git_mode(p) is None]
    assert not missing, (
        "system.yaml ExecStarts a script that is not tracked in git: "
        + ", ".join(missing)
    )


def test_every_job_script_is_executable_in_git():
    """The gate. A 100644 script ExecStarted by a unit dies 203/EXEC on the host."""
    bad = [(p, m) for p in job_script_paths() if (m := git_mode(p)) != "100755"]
    assert not bad, (
        "these scripts are ExecStarted by a composed unit but are not executable "
        "in git — their units will fail 203/EXEC on every host:\n"
        + "\n".join(f"  {p}  mode {m}" for p, m in bad)
        + "\n\nFix: git update-index --chmod=+x <path>"
    )
