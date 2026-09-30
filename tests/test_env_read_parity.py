"""Exercise real tier discovery with synthetic values and closed probe stubs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from claudlobby.commands._helpers import _load_env
from claudlobby.config import load_fleet
from claudlobby.doctor import DoctorReport, check_env_vars
from claudlobby.paths import Paths

REPO = Path(__file__).resolve().parents[1]
KEY = "GITHUB_PAT"


@pytest.fixture
def estate(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "root"
    home.mkdir()
    lib = root / "lib"
    lib.mkdir(parents=True)
    for name in ("lib-common.sh", "supervisor.sh", "env-tiers.sh", "creds-check.sh"):
        shutil.copyfile(REPO / "lib" / name, lib / name)
    fleet_dir = root / "local" / "fixture"
    fleet_dir.mkdir(parents=True)
    (fleet_dir / "fleet.yaml").write_text(
        "fleet:\n  name: fixture\n  system_defaults: false\n  bots:\n"
        "    probe:\n      expertise: [fixture]\n      channels: []\n      mcp: [fixture]\n"
    )
    mcp = root / "library" / "mcp"
    mcp.mkdir(parents=True)
    (mcp / "fixture.json").write_text(json.dumps({
        "fixture": {"command": "fixture"}, "_env_contract": {
            KEY: {"description": "synthetic", "default_tier": "fleet", "secret": True},
        },
    }))
    for d in (home, root, fleet_dir):
        (d / ".env").write_text("")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv(KEY, "fixture-placeholder")
    monkeypatch.delenv(KEY)
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    return home, root, fleet_dir, Paths(root=root, fleet_dir=fleet_dir)


def _doctor(paths):
    fleet, _ = load_fleet(paths.fleet_yaml)
    report = DoctorReport()
    check_env_vars(fleet, paths, report)
    return report.checks[0]


@pytest.mark.parametrize("tier", ["host", "root", "fleet"])
def test_doctor_sees_each_applicable_tier(estate, tier):
    home, root, fleet, paths = estate
    {"host": home, "root": root, "fleet": fleet}[tier].joinpath(".env").write_text(f"{KEY}=synthetic\n")
    assert _doctor(paths).status == "pass"


@pytest.mark.parametrize("ambient", ["", "different-synthetic-value"])
def test_doctor_cascade_wins_over_callers_environment(estate, monkeypatch, ambient):
    _, _, fleet, paths = estate
    (fleet / ".env").write_text(f"{KEY}=synthetic\n")
    expected = _doctor(paths)
    monkeypatch.setenv(KEY, ambient)
    assert _doctor(paths) == expected


def test_doctor_explicit_empty_wins_over_upstream_and_ambient(estate, monkeypatch):
    home, _, fleet, paths = estate
    (home / ".env").write_text(f"{KEY}=synthetic-host\n")
    (fleet / ".env").write_text(f"{KEY}=\n")
    monkeypatch.setenv(KEY, "synthetic-ambient")
    check = _doctor(paths)
    assert check.status == "warn" and "empty" in check.detail
    assert "synthetic" not in check.detail


def test_doctor_retains_ambient_fallback_when_no_tier_assigns(estate, monkeypatch):
    monkeypatch.setenv(KEY, "synthetic-ambient")
    assert _doctor(estate[-1]).status == "pass"


def test_unreachable_resolver_is_disclosed_without_single_file_fallback(estate, caplog):
    _, root, fleet, paths = estate
    (fleet / ".env").write_text(f"{KEY}=must-not-be-loaded\n")
    (root / "lib" / "env-tiers.sh").unlink()
    _load_env(paths)
    assert KEY not in os.environ
    assert "resolver" in caplog.text and "not loaded" in caplog.text
    check = _doctor(paths)
    assert check.status == "warn" and "resolver" in check.detail
    assert "must-not-be-loaded" not in check.detail + caplog.text


@pytest.mark.parametrize("tier", ["host", "root", "fleet"])
def test_command_loader_sees_each_applicable_tier(estate, tier):
    home, root, fleet, paths = estate
    {"host": home, "root": root, "fleet": fleet}[tier].joinpath(".env").write_text(f"{KEY}=synthetic-{tier}\n")
    _load_env(paths)
    assert os.environ[KEY] == f"synthetic-{tier}"


def test_command_loader_preserves_explicit_ambient_override(estate, monkeypatch):
    home, _, _, paths = estate
    (home / ".env").write_text(f"{KEY}=synthetic-host\n")
    monkeypatch.setenv(KEY, "explicit-command-override")
    _load_env(paths)
    assert os.environ[KEY] == "explicit-command-override"


def test_command_loader_keeps_empty_assignment(estate):
    home, _, fleet, paths = estate
    (home / ".env").write_text(f"{KEY}=synthetic-host\n")
    (fleet / ".env").write_text(f"{KEY}=\n")
    _load_env(paths)
    assert os.environ[KEY] == ""


def _check(estate, tmp_path, **extra):
    home, root, _, _ = estate
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text('''#!/bin/bash
cfg=""; out=""; prev=""
for arg in "$@"; do
  [ "$prev" = "--config" ] && cfg="$arg"
  [ "$prev" = "-o" ] && out="$arg"
  prev="$arg"
done
[ -n "$cfg" ] || exit 91
sed -n 's/^header = "Authorization: Bearer \\(.*\\)"$/\\1/p' "$cfg" >> "$PROBE_LOG"
[ -z "$out" ] || printf '{"data":{}}' > "$out"
printf '200'
''')
    curl.chmod(0o755)
    # The executable selection has no real network fallback: curl is first in
    # PATH and this checker uses it for every probe. Alert delivery is a stub.
    tg = root / "lib" / "tg-post.sh"
    tg.write_text('#!/bin/bash\nexit 92\n')
    tg.chmod(0o755)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(home),
        "CLAUDLOBBY_ROOT": str(root), "PLANE_EMIT_DISABLED": "1",
        "PLANE_SOCKET": str(tmp_path / "absent.sock"), "PLANE_EMIT_CLI": "/usr/bin/false",
        "TELEGRAM_STATE_DIR": str(home / "channel"), "TMPDIR": str(tmp_path),
        "PROBE_LOG": str(tmp_path / "probes"), **extra,
    }
    proc = subprocess.run(["/bin/bash", str(root / "lib" / "creds-check.sh"), "fixture"],
                          env=env, cwd=root, capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    assert not (root / "state" / "plane").exists()
    assert not (home / "channel").exists()
    probes = tmp_path / "probes"
    return json.loads((root / "state" / "creds-check-state.json").read_text()), probes.read_text().splitlines() if probes.exists() else []


@pytest.mark.parametrize("tier", ["host", "root", "fleet"])
def test_checker_probes_host_root_and_fleet_tokens(estate, tmp_path, tier):
    home, root, fleet, _ = estate
    {"host": home, "root": root, "fleet": fleet}[tier].joinpath(".env").write_text(
        "RAILWAY_PERSONAL_TOKEN=synthetic-personal\nRAILWAY_PERSONAL_PROJECT_TOKEN=synthetic-project\n"
    )
    state, probes = _check(estate, tmp_path)
    assert state["railway_personal_token"]["status"] == "ok"
    assert state["railway_personal_project_token"]["status"] == "ok"
    assert probes == ["synthetic-personal", "synthetic-project"]


def test_checker_empty_fleet_assignment_cancels_host_and_ambient(estate, tmp_path):
    home, _, fleet, _ = estate
    (home / ".env").write_text("RAILWAY_PERSONAL_TOKEN=synthetic-host\n")
    (fleet / ".env").write_text("RAILWAY_PERSONAL_TOKEN=\n")
    state, probes = _check(estate, tmp_path, RAILWAY_PERSONAL_TOKEN="synthetic-ambient")
    assert probes == []
    # #1213 owns whether empty/missing becomes fail; retain existing status.
    assert state["railway_personal_token"]["status"] == "skip"


def test_fleet_checker_does_not_read_ambient_bot_tier(estate, tmp_path):
    home, root, _, _ = estate
    bot = root / "other-bot"
    bot.mkdir()
    (bot / ".env").write_text("RAILWAY_PERSONAL_TOKEN=wrong-bot-value\n")
    (home / ".env").write_text("RAILWAY_PERSONAL_TOKEN=synthetic-host\n")
    _, probes = _check(estate, tmp_path, BOT_DIR=str(bot), FLEET_NAME="unrelated")
    assert probes == ["synthetic-host"]


def test_explicit_single_file_override_remains_exclusive(estate, tmp_path):
    home, _, fleet, _ = estate
    (home / ".env").write_text("RAILWAY_PERSONAL_TOKEN=wrong-host-value\n")
    (fleet / ".env").write_text("RAILWAY_PERSONAL_TOKEN=wrong-fleet-value\n")
    override = tmp_path / "override.env"
    override.write_text("RAILWAY_PERSONAL_PROJECT_TOKEN=synthetic-override\n")
    state, probes = _check(estate, tmp_path, CLAUDLOBBY_ENV=str(override))
    assert probes == ["synthetic-override"]
    assert state["railway_personal_token"]["status"] == "skip"
