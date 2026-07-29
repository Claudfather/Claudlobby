"""#851 + #852: two npx-cache probes that could not fail.

Both keyed a verdict off an *absence* and attributed that absence to exactly one
cause:

- `warm-cache` (#851) ran `subprocess.run` without `check=True` and never read
  `.returncode`, so "no exception was raised" was taken to mean "the warm
  worked". A child that simply exits non-zero was reported as warmed, and
  `capture_output=True` then discarded the diagnostic that would have explained
  it.
- `check-npx-cache.sh` (#852) looked only in `~/.npm/_npx`, so "not in the npx
  cache" was taken to mean "not installed". A globally-installed package is
  present, resolvable, and works — and read as MISSING forever, with no amount
  of warming able to clear it.

Each defect gets a test that fails against the pre-fix code, plus guards for the
verdicts that were already correct and must stay that way.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from pathlib import Path


from claudlobby.commands.core import cmd_warm_cache
from tests.conftest import constructed_env

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "lib" / "check-npx-cache.sh"

PKG = "demo-mcp@1.2.3"


def _fleet_with_npx_pkg(fleet_dir: Path) -> Path:
    """Equip the fleet's lead bot with an npx-based MCP fragment."""
    (fleet_dir / "library" / "mcp" / "npxdemo.json").write_text(
        json.dumps({"npxdemo": {"command": "npx", "args": ["-y", PKG]}})
    )
    fy = fleet_dir / "fleet.yaml"
    before = fy.read_text()
    after = before.replace(
        "    lead:\n      expertise: [orchestration]\n",
        "    lead:\n      expertise: [orchestration]\n      mcp: [npxdemo]\n",
    )
    # Assert the edit landed. A silent no-op here leaves the fleet with no npx
    # packages at all, so warm-cache returns early and every assertion below
    # reads the empty path instead of the code under test.
    assert after != before, "fleet.yaml indentation changed — fixture no longer equips the bot"
    fy.write_text(after)
    return fleet_dir


def _args(root: Path, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(root=str(root), fleet=None, seed=False, dry_run=dry_run)


class TestWarmCacheReadsTheChildStatus:
    """#851. A warm whose child exits non-zero must not be reported as warmed."""

    @staticmethod
    def _fake_run(returncode: int, stderr: str = ""):
        def run(*a, **kw):
            return subprocess.CompletedProcess(
                args=a[0] if a else [], returncode=returncode, stdout="", stderr=stderr
            )

        return run

    def test_nonzero_child_is_not_reported_as_a_successful_warm(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """The bug: exit 1 raises nothing, so the old code fell through to
        'cache warm complete'."""
        _fleet_with_npx_pkg(fleet_dir)
        monkeypatch.setattr(subprocess, "run", self._fake_run(1, "ERR_INVALID_URL"))
        with caplog.at_level(logging.INFO):
            cmd_warm_cache(_args(fleet_dir))
        assert "cache warm complete" not in caplog.text

    def test_nonzero_child_surfaces_its_status_and_output(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """capture_output=True swallowed the diagnostic; it must be reported."""
        _fleet_with_npx_pkg(fleet_dir)
        monkeypatch.setattr(subprocess, "run", self._fake_run(1, "ERR_INVALID_URL"))
        with caplog.at_level(logging.INFO):
            cmd_warm_cache(_args(fleet_dir))
        assert PKG in caplog.text
        assert "ERR_INVALID_URL" in caplog.text, "the captured diagnostic was discarded"

    def test_a_failed_warm_exits_nonzero(self, fleet_dir: Path, monkeypatch, caplog):
        """`return 0` regardless is what let reload-fleet log a clean warm."""
        _fleet_with_npx_pkg(fleet_dir)
        monkeypatch.setattr(subprocess, "run", self._fake_run(1, "boom"))
        with caplog.at_level(logging.INFO):
            assert cmd_warm_cache(_args(fleet_dir)) != 0

    def test_successful_warm_still_reports_complete(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """Guard: the happy path must keep working."""
        _fleet_with_npx_pkg(fleet_dir)
        monkeypatch.setattr(subprocess, "run", self._fake_run(0))
        with caplog.at_level(logging.INFO):
            assert cmd_warm_cache(_args(fleet_dir)) == 0
        assert "cache warm complete" in caplog.text

    def test_dry_run_never_reports_failure(self, fleet_dir: Path, monkeypatch, caplog):
        """Guard: --dry-run runs no child, so it cannot have failures."""
        _fleet_with_npx_pkg(fleet_dir)
        monkeypatch.setattr(subprocess, "run", self._fake_run(1, "boom"))
        with caplog.at_level(logging.INFO):
            assert cmd_warm_cache(_args(fleet_dir, dry_run=True)) == 0
        assert "dry run" in caplog.text


class TestCheckNpxCacheSeesGlobalInstalls:
    """#852 cause 1. "Not in _npx" is not "not installed"."""

    def _root(self, tmp_path: Path) -> Path:
        root = tmp_path / "clroot"
        (root / "library" / "mcp").mkdir(parents=True)
        (root / "library" / "mcp" / "demo.json").write_text(
            json.dumps({"demo": {"command": "npx", "args": ["-y", PKG]}})
        )
        return root

    def _run(self, root: Path, npx_cache: Path, global_root: Path | None):
        env = constructed_env(CLAUDLOBBY_ROOT=root, NPX_CACHE_DIR=npx_cache)
        if global_root is not None:
            env["NPM_GLOBAL_ROOT"] = str(global_root)
        return subprocess.run(
            ["bash", str(CHECKER)], capture_output=True, text=True, env=env
        )

    @staticmethod
    def _install(where: Path, pkg_bare: str):
        d = where / pkg_bare
        d.mkdir(parents=True)
        (d / "package.json").write_text('{"name":"%s"}' % pkg_bare)

    def test_globally_installed_package_is_not_reported_missing(self, tmp_path: Path):
        """The bug: present globally, absent from _npx, reported MISSING forever
        — and no amount of warm-cache can ever create the entry it waits for."""
        root = self._root(tmp_path)
        cache = tmp_path / "_npx"
        cache.mkdir()
        groot = tmp_path / "global" / "lib" / "node_modules"
        self._install(groot, "demo-mcp")
        r = self._run(root, cache, groot)
        assert "MISSING" not in r.stdout, r.stdout
        assert r.returncode == 0, r.stdout

    def test_a_global_install_is_reported_distinctly_from_a_cache_hit(
        self, tmp_path: Path
    ):
        """Resolvable, but not via the npx cache — the operator should be able to
        tell those apart rather than have them collapsed into one verdict."""
        root = self._root(tmp_path)
        cache = tmp_path / "_npx"
        cache.mkdir()
        groot = tmp_path / "global" / "lib" / "node_modules"
        self._install(groot, "demo-mcp")
        r = self._run(root, cache, groot)
        # Assert the PACKAGE is named as a global install, not merely that the
        # word appears: a summary count like "(0 cached, 1 global)" satisfies a
        # bare substring check while telling the operator nothing about which
        # package it is. Mutation-checked — removing the per-package line must
        # fail this.
        named = [ln for ln in r.stdout.splitlines() if PKG in ln and "global" in ln.lower()]
        assert named, r.stdout

    def test_genuinely_absent_package_is_still_missing(self, tmp_path: Path):
        """Guard: the check must still be able to fail."""
        root = self._root(tmp_path)
        cache = tmp_path / "_npx"
        cache.mkdir()
        groot = tmp_path / "global" / "lib" / "node_modules"
        groot.mkdir(parents=True)
        r = self._run(root, cache, groot)
        assert "MISSING" in r.stdout, r.stdout
        assert r.returncode == 1

    def test_cache_hit_still_passes(self, tmp_path: Path):
        """Guard: the original happy path (present in _npx) is unchanged."""
        root = self._root(tmp_path)
        cache = tmp_path / "_npx"
        self._install(cache / "abc123" / "node_modules", "demo-mcp")
        groot = tmp_path / "global" / "lib" / "node_modules"
        groot.mkdir(parents=True)
        r = self._run(root, cache, groot)
        assert "MISSING" not in r.stdout, r.stdout
        assert r.returncode == 0
