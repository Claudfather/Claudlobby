"""Tests for `claudlobby bootstrap` — zero-to-running cold-start command."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby.__main__ import main


def _make_bootable(tmp_path: Path) -> Path:
    """Create a minimal claudlobby repo structure for bootstrap testing."""
    root = tmp_path / "claudlobby"
    root.mkdir()

    # fleet.yaml.example (what bootstrap copies if fleet.yaml is missing)
    (root / "fleet.yaml.example").write_text(dedent("""\
        fleet:
          name: test-fleet
          service_prefix: com.test
          bots:
            worker:
              expertise: [eng]
              telegram:
                handle: w_bot
                token_env: TG_TOKEN_W
    """))

    # Library dirs (needed for validate/generate)
    (root / "library" / "expertise").mkdir(parents=True)
    (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
    (root / "library" / "mcp").mkdir(parents=True)

    # Templates dir (needed for compose_claude_md)
    (root / "templates").mkdir()
    (root / "templates" / "claude.md.j2").write_text("# {{ bot_name }}\n")

    # lib/ (needed for spin-up check)
    (root / "lib").mkdir()
    (root / "lib" / "spin-up-bot.sh").write_text("#!/bin/bash\necho ok\n")
    (root / "lib" / "spin-up-bot.sh").chmod(0o755)

    return root


class TestBootstrapEnvCheck:
    """Step 1: environment detection."""

    def test_shows_python_version(self, tmp_path, capsys):
        root = _make_bootable(tmp_path)
        # No fleet.yaml yet — bootstrap should copy the example and stop
        ret = main(["--root", str(root), "bootstrap", "--skip-install"])
        captured = capsys.readouterr()
        assert "Python" in captured.out


class TestBootstrapScaffold:
    """Step 3: fleet.yaml scaffolding from example."""

    def test_copies_example_when_missing(self, tmp_path, capsys):
        root = _make_bootable(tmp_path)
        ret = main(["--root", str(root), "bootstrap", "--skip-install"])
        captured = capsys.readouterr()
        # Should have copied fleet.yaml.example
        assert (root / "fleet.yaml").is_file()
        assert "copied fleet.yaml.example" in captured.out
        # Returns 0 (pauses for user to edit)
        assert ret == 0

    def test_skips_copy_when_fleet_yaml_exists(self, tmp_path, capsys):
        root = _make_bootable(tmp_path)
        # Pre-create fleet.yaml
        shutil.copy2(root / "fleet.yaml.example", root / "fleet.yaml")
        ret = main(["--root", str(root), "bootstrap", "--skip-install", "--skip-spinup"])
        captured = capsys.readouterr()
        assert "fleet.yaml exists" in captured.out

    def test_overlay_mode_scaffolds_to_local(self, tmp_path, capsys):
        root = _make_bootable(tmp_path)
        ret = main(["--root", str(root), "--fleet", "myfleet", "bootstrap", "--skip-install"])
        captured = capsys.readouterr()
        fleet_yaml = root / "local" / "myfleet" / "fleet.yaml"
        assert fleet_yaml.is_file()


class TestBootstrapGenerate:
    """Steps 5-6: validate + generate."""

    def test_full_pipeline_without_spinup(self, tmp_path, capsys):
        root = _make_bootable(tmp_path)
        shutil.copy2(root / "fleet.yaml.example", root / "fleet.yaml")
        ret = main(["--root", str(root), "bootstrap", "--skip-install", "--skip-spinup"])
        captured = capsys.readouterr()

        # Should have validated and generated
        assert "Validate" in captured.out
        assert "Generate" in captured.out

        # Bot directory should exist
        bot_dir = root / "runtime" / "bots" / "worker"
        assert bot_dir.is_dir()
        assert (bot_dir / "CLAUDE.md").is_file()
        assert (bot_dir / "bot.conf").is_file()
        assert ret == 0


class TestBootstrapIdempotent:
    """Re-running bootstrap skips completed steps."""

    def test_second_run_is_noop(self, tmp_path, capsys):
        root = _make_bootable(tmp_path)
        shutil.copy2(root / "fleet.yaml.example", root / "fleet.yaml")

        # First run
        main(["--root", str(root), "bootstrap", "--skip-install", "--skip-spinup"])

        # Second run
        ret = main(["--root", str(root), "bootstrap", "--skip-install", "--skip-spinup"])
        captured = capsys.readouterr()
        assert "fleet.yaml exists" in captured.out
        assert ret == 0
