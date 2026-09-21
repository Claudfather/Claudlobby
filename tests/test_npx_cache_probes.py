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
import shutil
import subprocess
from pathlib import Path


from claudlobby.commands.core import cmd_warm_cache
from tests.conftest import (
    SubprocessRecorder,
    constructed_env,
    equip_bot_with_mcp,
    warm_cache_args,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "lib" / "check-npx-cache.sh"

PKG = "demo-mcp@1.2.3"


NPX_FRAGMENT = {"npxdemo": {"command": "npx", "args": ["-y", PKG]}}


class TestWarmCacheReadsTheChildStatus:
    """#851. A warm whose child exits non-zero must not be reported as warmed."""

    def test_nonzero_child_is_not_reported_as_a_successful_warm(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """The bug: exit 1 raises nothing, so the old code fell through to
        'cache warm complete'."""
        equip_bot_with_mcp(fleet_dir, NPX_FRAGMENT)
        monkeypatch.setattr(subprocess, "run", SubprocessRecorder(1, "ERR_INVALID_URL"))
        with caplog.at_level(logging.INFO):
            cmd_warm_cache(warm_cache_args(fleet_dir))
        assert "cache warm complete" not in caplog.text

    def test_nonzero_child_surfaces_its_status_and_output(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """capture_output=True swallowed the diagnostic; it must be reported."""
        equip_bot_with_mcp(fleet_dir, NPX_FRAGMENT)
        monkeypatch.setattr(subprocess, "run", SubprocessRecorder(1, "ERR_INVALID_URL"))
        with caplog.at_level(logging.INFO):
            cmd_warm_cache(warm_cache_args(fleet_dir))
        assert PKG in caplog.text
        assert "ERR_INVALID_URL" in caplog.text, "the captured diagnostic was discarded"

    def test_a_failed_warm_exits_nonzero(self, fleet_dir: Path, monkeypatch, caplog):
        """`return 0` regardless is what let reload-fleet log a clean warm."""
        equip_bot_with_mcp(fleet_dir, NPX_FRAGMENT)
        monkeypatch.setattr(subprocess, "run", SubprocessRecorder(1, "boom"))
        with caplog.at_level(logging.INFO):
            assert cmd_warm_cache(warm_cache_args(fleet_dir)) != 0

    def test_successful_warm_still_reports_complete(
        self, fleet_dir: Path, monkeypatch, caplog
    ):
        """Guard: the happy path must keep working."""
        equip_bot_with_mcp(fleet_dir, NPX_FRAGMENT)
        monkeypatch.setattr(subprocess, "run", SubprocessRecorder(0))
        with caplog.at_level(logging.INFO):
            assert cmd_warm_cache(warm_cache_args(fleet_dir)) == 0
        assert "cache warm complete" in caplog.text

    def test_dry_run_never_reports_failure(self, fleet_dir: Path, monkeypatch, caplog):
        """Guard: --dry-run runs no child, so it cannot have failures."""
        equip_bot_with_mcp(fleet_dir, NPX_FRAGMENT)
        monkeypatch.setattr(subprocess, "run", SubprocessRecorder(1, "boom"))
        with caplog.at_level(logging.INFO):
            assert cmd_warm_cache(warm_cache_args(fleet_dir, dry_run=True)) == 0
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


class TestCheckNpxCacheRefusesRatherThanGuessing:
    """#1577's most novel property — exit 2 for "cannot tell", never 0 — had no
    test at all: reverting the whole refusal block to the old fail-open
    (`TARGETS=""` and carry on) failed nothing in the suite.

    0 is the dangerous answer, not 1. `reload-fleet.sh` runs `warm-cache` ONLY
    when this probe fails, so a 0 it did not earn clears the debounce and
    leaves every package cold — #1577's original defect, rebuilt one layer up
    in the seam built to close it.
    """

    def _partial_install(self, tmp_path: Path) -> Path:
        """A real `lib/` with exactly one file missing.

        The script resolves the grammar from its OWN dirname, so the condition
        cannot be made by pointing an env var somewhere else, and the repo's
        own grammar must not be deleted to make it. Copying the tree and
        removing one file is also the shape the real failure has: a partial or
        half-updated install, not a hand-built directory holding two scripts.
        """
        root = tmp_path / "clroot"
        (root / "library" / "mcp").mkdir(parents=True)
        (root / "library" / "mcp" / "demo.json").write_text(
            json.dumps({"demo": {"command": "npx", "args": ["-y", PKG]}})
        )
        shutil.copytree(REPO_ROOT / "lib", root / "lib")
        (root / "lib" / "mcp-package-grammar.py").unlink()
        return root

    def test_an_unreachable_grammar_exits_2_rather_than_reporting_all_clear(
        self, tmp_path: Path
    ):
        root = self._partial_install(tmp_path)
        r = subprocess.run(
            ["bash", str(root / "lib" / "check-npx-cache.sh")],
            capture_output=True,
            text=True,
            env=constructed_env(
                CLAUDLOBBY_ROOT=root, NPX_CACHE_DIR=tmp_path / "_npx"
            ),
        )
        # Assert the REASON, not just the code: this fixture dies at exit 1 if
        # the sourcing chain ever grows a file the copy does not carry, and a
        # bare `!= 0` would read that fixture rot as the refusal under test.
        assert "cannot reach the package grammar" in r.stderr, (
            f"rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}"
        )
        assert r.returncode == 2, (
            f"rc={r.returncode} — 0 would clear reload-fleet's debounce on a "
            f"probe that could not answer; stderr={r.stderr!r}"
        )
