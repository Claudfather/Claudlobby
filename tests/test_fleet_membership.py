"""Fleet-membership gating for supervision scripts (#419, extends #418).

``parse_fleet_bots`` + ``bot_in_fleet`` (lib-common.sh) are the single shared
filter every fleet supervision sweep uses to skip stale/cross-fleet runtime-dir
residue. #418 wired ``keepalive-all`` + ``fleet-pulse``; #419 wires
``weekly-worker-restart``, ``reload-fleet``, and ``git-pull-all`` so a departed
bot's leftover runtime dir is never bounced, marked, or resurrected.

These tests pin:
- the shared predicate (declared-bot parsing + membership, incl. the empty-list →
  "declared" root-mode fallback every script relies on), and
- ``git-pull-all``'s path-derived guard end-to-end — the trickiest of the three
  because it receives an explicit projects path (from a stale per-bot cron entry)
  rather than globbing, and its log-dir ``mkdir`` is what resurrected the orphan.

CI runs pytest only, so the bash logic is exercised via subprocess — without this
wrapper it would be untested in CI. ``weekly-worker-restart`` / ``reload-fleet``
drive spin-up / tmux / claudlobby and are validated empirically (see PR); their
loop gate is the same one-line predicate proven here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_COMMON = REPO_ROOT / "lib" / "lib-common.sh"
GIT_PULL_ALL = REPO_ROOT / "lib" / "git-pull-all.sh"


def _run(snippet: str, *args: str) -> tuple[str, int]:
    """Source lib-common.sh, run a snippet with positional args $2.., return (stdout, rc)."""
    proc = subprocess.run(
        ["bash", "-c", f'. "$1"; {snippet}', "_", str(LIB_COMMON), *args],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip(), proc.returncode


def _make_fleet(
    root: Path, fleet: str, declared: list[str], present: list[str]
) -> Path:
    """Build <root>/local/<fleet>/{fleet.yaml, runtime/bots/<present>/}.

    ``declared`` lands in fleet.yaml (``bots:`` at 2-space, bot keys at 4-space,
    matching the documented schema parse_fleet_bots keys off). ``present`` are the
    runtime-dir residue dirs on disk — which may differ from declared (a departed
    bot leaves a dir behind but is dropped from fleet.yaml). Only the directory
    needs to exist: no code path here sources bot.conf (parse_fleet_bots reads
    fleet.yaml; git-pull-all's guard checks the path + fleet.yaml).
    """
    fdir = root / "local" / fleet
    bots = fdir / "runtime" / "bots"
    bots.mkdir(parents=True)
    lines = ["fleet:", f"  name: {fleet}", "  bots:"]
    for b in declared:
        lines += [f"    {b}:", "      expertise: [x]"]
    (fdir / "fleet.yaml").write_text("\n".join(lines) + "\n")
    for b in present:
        (bots / b).mkdir(parents=True)
    return fdir


class TestParseFleetBots:
    """fleet.yaml is the authoritative roster; missing file → empty (root-mode)."""

    def test_lists_declared_bots(self, tmp_path):
        fdir = _make_fleet(tmp_path, "demo", ["alex", "ari"], present=[])
        out, rc = _run('parse_fleet_bots "$2"', str(fdir / "fleet.yaml"))
        assert rc == 0
        assert out.split() == ["alex", "ari"]

    def test_missing_file_is_empty(self, tmp_path):
        out, rc = _run('parse_fleet_bots "$2"', str(tmp_path / "nope.yaml"))
        assert rc == 0
        assert out == ""

    def test_ignores_non_bot_keys(self, tmp_path):
        fdir = _make_fleet(tmp_path, "demo", ["alex"], present=[])
        out, _ = _run('parse_fleet_bots "$2"', str(fdir / "fleet.yaml"))
        names = out.split()
        assert names == ["alex"]  # not 'name', 'fleet', 'bots', or 'expertise'


class TestBotInFleet:
    """Membership predicate; empty list → declared (preserves pre-fleet.yaml behavior)."""

    def test_member(self):
        _, rc = _run('bot_in_fleet "$2" "$3"', "alex", "alex\nari")
        assert rc == 0

    def test_non_member(self):
        _, rc = _run('bot_in_fleet "$2" "$3"', "craig", "alex\nari")
        assert rc == 1

    def test_empty_list_is_declared(self):
        _, rc = _run('bot_in_fleet "$2" "$3"', "craig", "")
        assert rc == 0

    def test_exact_line_match_not_substring(self):
        # grep -qx: 'al' must not match 'alex'
        _, rc = _run('bot_in_fleet "$2" "$3"', "al", "alex\nari")
        assert rc == 1


class TestGitPullAllFleetGuard:
    """A stale per-bot cron entry for a departed bot must not resurrect its dir."""

    def _run_script(self, root: Path, projects: Path) -> subprocess.CompletedProcess:
        env = {**os.environ, "CLAUDLOBBY_ROOT": str(root)}
        return subprocess.run(
            [str(GIT_PULL_ALL), str(projects)],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_departed_bot_dir_not_resurrected(self, tmp_path):
        # fleet declares 'alex'; 'craig' departed (no runtime dir) but its git-pull
        # cron entry persists. The guard must exit before the log-dir mkdir.
        _make_fleet(tmp_path, "demo", declared=["alex"], present=[])
        craig_projects = (
            tmp_path / "local" / "demo" / "runtime" / "bots" / "craig" / "projects"
        )
        assert not craig_projects.parent.exists()
        proc = self._run_script(tmp_path, craig_projects)
        assert proc.returncode == 0
        assert not craig_projects.parent.exists(), (
            "git-pull-all resurrected a departed bot's runtime dir"
        )

    def test_declared_bot_proceeds(self, tmp_path):
        _make_fleet(tmp_path, "demo", declared=["alex"], present=["alex"])
        alex_projects = (
            tmp_path / "local" / "demo" / "runtime" / "bots" / "alex" / "projects"
        )
        alex_projects.mkdir()
        proc = self._run_script(tmp_path, alex_projects)
        assert proc.returncode == 0
        # proceeded past the guard → log dir created next to projects/
        assert (alex_projects.parent / "git-pull.log").exists()

    def test_generic_non_fleet_path_proceeds(self, tmp_path):
        # A plain repo dir (not .../runtime/bots/<bot>/projects) bypasses the guard.
        repos = tmp_path / "myrepos"
        repos.mkdir()
        proc = self._run_script(tmp_path, repos)
        assert proc.returncode == 0
        assert (tmp_path / "git-pull.log").exists()  # LOG = dirname(repos)/git-pull.log
