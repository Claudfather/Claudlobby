"""seed_account_onboarding: a Claude profile kept for the bots skips the first-run screens.

Website setup gives the bots their own Claude profile (CLAUDE_CONFIG_DIR, the
browser installation's provider-profile). Read on a real computer after setup,
its .claude.json holds only what the sign-in wrote: firstStartTime,
firstStartVersion, machineID, migrationVersion, userID and a few migration
flags, with no hasCompletedOnboarding and no lastOnboardingVersion. A claude
started there opens its first-run screens (a colour theme, then a notice about
mistakes and prompt injection) and waits for a key. Headless, nobody presses
one, so the bot never reaches its prompt and its start times out.

start-bot.sh already trusts the bot's own folder (seed_workspace_trust); this
marks first-run onboarding done the same way, merging two keys and nothing
else. Neither screen is a consent: the theme is a display preference and the
notice is information. Only a browser installation is touched: a terminal
installation's named profile may never have been signed in to, and the wizard
is where that happens. The harness helper seed_claude_auth_and_trust writes the
same two keys, but writes the whole file, so it is for throwaway profiles only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.conftest import _write_exec, constructed_env

LIB = Path(__file__).resolve().parent.parent / "claudlobby/_runtime_scripts"

# The keys a real provider profile held after website sign-in (values invented).
PROFILE = {
    "firstStartTime": "2026-11-20T10:00:00.000Z",
    "firstStartVersion": "2.1.325",
    "hasResetAutoModeOptInForDefaultOffer": True,
    "machineID": "0" * 64,
    "migrationVersion": 3,
    "opusProMigrationComplete": True,
    "seenNotifications": {},
    "userID": "f" * 64,
}


def _claude(tmp_path, body='echo "2.1.325 (Claude Code)"'):
    marker = tmp_path / "claude-ran"
    stub = tmp_path / "bin" / "claude"
    stub.parent.mkdir(exist_ok=True)
    _write_exec(stub, f'#!/bin/bash\ntouch "{marker}"\n{body}\n')
    return stub, marker


def _seed(tmp_path, claude, *args, config_dir=None, browser=True):
    """``browser`` gives the run a browser installation's exact context; the
    seed acts only there (fleet_provider_claude)."""
    home = tmp_path / "home"
    env = constructed_env(HOME=home, CLAUDE_BIN=claude)
    if browser:
        family = home / ".local/share/claudlobby-browser"
        env["CLAUDLOBBY_ROOT"] = str(family / "data")
        env["CLAUDLOBBY_INSTALLATION"] = str(family / "install")
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    quoted = "".join(f' "{a}"' for a in args)
    return subprocess.run(
        [
            "bash",
            "-c",
            f'. "{LIB}/lib-common.sh"; set +e; seed_account_onboarding{quoted}; echo "rc=$?"',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _profile(tmp_path, content=None):
    cfg = tmp_path / "provider-profile"
    cfg.mkdir()
    (tmp_path / "home").mkdir(exist_ok=True)
    (cfg / ".claude.json").write_text(
        json.dumps(PROFILE if content is None else content)
    )
    return cfg


def test_a_bot_profile_is_marked_onboarded_for_the_version_the_fleet_launches(tmp_path):
    cfg = _profile(tmp_path)
    claude, _ = _claude(tmp_path)
    r = _seed(tmp_path, claude, config_dir=cfg)
    assert r.stdout.strip() == "rc=0", r.stderr
    data = json.loads((cfg / ".claude.json").read_text())
    assert data.pop("hasCompletedOnboarding") is True
    assert data.pop("lastOnboardingVersion") == "2.1.325"
    assert data == PROFILE  # every other key exactly as the sign-in left it


def test_an_onboarded_profile_is_left_byte_for_byte_and_claude_is_not_run(tmp_path):
    cfg = _profile(
        tmp_path,
        {
            **PROFILE,
            "hasCompletedOnboarding": True,
            "lastOnboardingVersion": "2.1.300",
            "theme": "light",
        },
    )
    before = (cfg / ".claude.json").read_bytes()
    claude, marker = _claude(tmp_path)
    r = _seed(tmp_path, claude, config_dir=cfg)
    assert r.stdout.strip() == "rc=0", r.stderr
    assert (cfg / ".claude.json").read_bytes() == before
    assert not marker.exists()


def test_the_operators_default_account_is_left_alone(tmp_path):
    # No CLAUDE_CONFIG_DIR: the bots share the operator's own ~/.claude, whose
    # onboarding is the operator's. Nothing is written anywhere in HOME.
    home = tmp_path / "home"
    home.mkdir()
    claude, marker = _claude(tmp_path)
    r = _seed(tmp_path, claude)
    assert r.stdout.strip() == "rc=0", r.stderr
    assert list(home.rglob("*")) == []
    assert not marker.exists()


def test_a_terminal_installation_with_an_explicit_profile_is_left_alone(tmp_path):
    # bot.conf sets CLAUDE_CONFIG_DIR for every named or non-default account. On
    # a computer set up from a terminal such a profile may never have been signed
    # in to, and the wizard (its login step included) is how that happens.
    cfg = _profile(tmp_path)
    before = (cfg / ".claude.json").read_bytes()
    claude, marker = _claude(tmp_path)
    r = _seed(tmp_path, claude, config_dir=cfg, browser=False)
    assert r.stdout.strip() == "rc=0", r.stderr
    assert (cfg / ".claude.json").read_bytes() == before
    assert not marker.exists()


def test_an_empty_profile_file_is_treated_as_no_file(tmp_path):
    cfg = _profile(tmp_path)
    (cfg / ".claude.json").write_bytes(b"")
    claude, _ = _claude(tmp_path)
    r = _seed(tmp_path, claude, config_dir=cfg)
    assert r.stdout.strip() == "rc=0", r.stderr
    assert json.loads((cfg / ".claude.json").read_text()) == {
        "hasCompletedOnboarding": True,
        "lastOnboardingVersion": "2.1.325",
    }


def test_a_claude_that_cannot_report_its_version_seeds_nothing(tmp_path):
    cfg = _profile(tmp_path)
    before = (cfg / ".claude.json").read_bytes()
    claude, _ = _claude(tmp_path, body='echo "Error: not installed" >&2; exit 1')
    r = _seed(tmp_path, claude, config_dir=cfg)
    assert r.stdout.strip() == "rc=0", r.stderr
    assert (cfg / ".claude.json").read_bytes() == before


def test_an_unreadable_profile_is_not_repaired(tmp_path):
    cfg = _profile(tmp_path)
    (cfg / ".claude.json").write_text("{not json")
    claude, _ = _claude(tmp_path)
    r = _seed(tmp_path, claude, config_dir=cfg)
    assert r.stdout.strip() == "rc=0", r.stderr
    assert (cfg / ".claude.json").read_text() == "{not json"


def test_a_profile_with_no_file_yet_gets_only_the_two_keys(tmp_path):
    cfg = tmp_path / "provider-profile"
    cfg.mkdir()
    (tmp_path / "home").mkdir()
    claude, _ = _claude(tmp_path)
    r = _seed(tmp_path, claude, config_dir=cfg)
    assert r.stdout.strip() == "rc=0", r.stderr
    assert json.loads((cfg / ".claude.json").read_text()) == {
        "hasCompletedOnboarding": True,
        "lastOnboardingVersion": "2.1.325",
    }


def test_start_bot_seeds_onboarding_after_folder_trust_and_before_the_session():
    """Pin the call site: start-bot.sh is the one place a bot's profile is
    prepared before its claude starts. One call, no arguments, so the profile
    is the bot's own CLAUDE_CONFIG_DIR from bot.conf."""
    lines = [
        line.strip()
        for line in (LIB / "start-bot.sh").read_text().splitlines()
        if not line.strip().startswith("#")
    ]
    calls = [i for i, line in enumerate(lines) if "seed_account_onboarding" in line]
    assert len(calls) == 1, calls
    assert lines[calls[0]] == "seed_account_onboarding || true"
    trust = next(
        i for i, line in enumerate(lines) if line.startswith("seed_workspace_trust ")
    )
    spawn = next(
        i for i, line in enumerate(lines) if line.startswith("bot_session_spawn ")
    )
    assert trust < calls[0] < spawn
