"""#1721 — the scheduled vault door: does it run, record, and page ONCE?

Claudron's sync fires only at session boundaries, under a 2s hook budget,
reporting to a log inside the vault it is failing to sync. When a live host
wedged, every sync refused for twelve days printing success-shaped output and
nothing scheduled ever looked. This job is the thing that notices.

Every test drives the REAL `lib/vault-sync.sh` with a stubbed `claudron` whose
envelope the test controls — the script's contract is that it parses that
envelope and never the text, so the stub is the whole seam.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "lib" / "vault-sync.sh"


def _claudron_stub(bindir: Path, *, sync_json: str, sync_rc: int = 0,
                   check_json: str | None = None, check_rc: int | None = None) -> Path:
    """A `claudron` on PATH whose two subcommands answer what the test says.

    Default: no `--check` flag at all (rc 2, argparse's usage error) — the CLI
    contract's signal for an engine that predates the health door, which is
    every engine shipping today. A test wanting a verdict passes `check_json`.

    The payloads are shell-quoted, NOT json.dumps'd: the first version double-
    encoded them, so the stub printed an escaped JSON *string* where the script
    expected an object — and every envelope test failed against code that was
    correct. The tests caught a defect in the tests.
    """
    if check_rc is None:
        check_rc = 0 if check_json is not None else 2
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "claudron"
    stub.write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "--check" ]; then\n'
        f"    printf '%s' {shlex.quote(check_json or '')}\n"
        f"    exit {check_rc}\n"
        "  fi\n"
        "done\n"
        f"printf '%s' {shlex.quote(sync_json)}\n"
        f"exit {sync_rc}\n"
    )
    stub.chmod(0o755)
    return stub


def _root(tmp_path: Path, vaults: dict[str, str]) -> Path:
    """An install root whose bots declare the given {bot: vault_path}."""
    root = tmp_path / "install"
    (root / "state").mkdir(parents=True)
    (root / "lib").mkdir(parents=True, exist_ok=True)
    bots = root / "local" / "demo" / "runtime" / "bots"
    for bot, vault in vaults.items():
        d = bots / bot
        d.mkdir(parents=True)
        Path(vault).mkdir(parents=True, exist_ok=True)
        (d / "bot.conf").write_text(f'export CLAUDRON_VAULT_PATH="{vault}"\n')
    return root


# The identity vars a BOT SESSION exports, which this job must never inherit
# (review). vault-sync runs as a HOST job: it has no fleet, so `resolve_bots_dir`
# is supposed to land on the fleet-less root path and the recipient is supposed to
# come from the declaration. But that resolver's own fallback chain is
# `${CLAUDLOBBY_FLEET:-${FLEET_NAME:-}}`, so a run launched from inside a bot
# session silently substitutes the CALLING bot's fleet for the shape under test --
# measured by the reviewer, whose contaminated run anchored the event on their own
# fleet while the scrubbed run anchored it on the host. A test that inherits them
# passes by contamination. `lib/rehearse-vault-sync.sh::run_job` scrubs the same
# five; this is the Python half of one rule.
_BOT_IDENTITY_VARS = (
    "FLEET_NAME",
    "CLAUDLOBBY_FLEET",
    "BOT_DIR",
    "BOT_ID",
    "CLAUDLOBBY_ALERT_MANAGER",
)


def _scrubbed_env(root: Path, bindir: Path, **env) -> dict:
    """The subprocess env: ambient bot identity REMOVED, then the test's own on top.

    Order is load-bearing — the scrub runs BEFORE `**env`, so a test that sets one
    of these deliberately (the routing tests set CLAUDLOBBY_ALERT_MANAGER) still
    wins, while an ambient value from the session running pytest never does.
    """
    e = dict(os.environ)
    for var in _BOT_IDENTITY_VARS:
        e.pop(var, None)
    e.update({
        "CLAUDLOBBY_ROOT": str(root),
        "PATH": f"{bindir}:{e['PATH']}",
        "PLANE_EMIT_DISABLED": "1",      # the ruled harness exemption
        **env,
    })
    return e


def _run(root: Path, bindir: Path, **env):
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                          env=_scrubbed_env(root, bindir, **env), timeout=120)


OK = json.dumps({"ok": True, "data": {"detail": "up to date", "ahead": 0,
                                      "behind": 0, "uncommitted": 0}})
REFUSED = json.dumps({"ok": False, "error":
                      "refusing to sync: a rebase is stopped part-way"})


class TestDiscoveryIsARead:
    def test_it_finds_the_vault_from_bot_conf_not_a_declaration(self, tmp_path):
        """The vault is ALREADY composed into every bot.conf. A host-level
        field would be a second copy of that fact, and a second copy is how
        the two drift."""
        vault = tmp_path / "vault-a"
        root = _root(tmp_path, {"worker": str(vault)})
        _claudron_stub(tmp_path / "bin", sync_json=OK)
        r = _run(root, tmp_path / "bin")
        assert r.returncode == 0, r.stderr
        log = (root / "state" / "vault-sync.log").read_text()
        assert "vault:vault-a" in log and "ok=1" in log

    def test_two_bots_on_ONE_vault_are_one_subject(self, tmp_path):
        """Deduped by real path: two fleets pointing at one vault must be one
        subject on the plane, not two half-populated ones."""
        vault = tmp_path / "shared-vault"
        root = _root(tmp_path, {"a": str(vault), "b": str(vault)})
        _claudron_stub(tmp_path / "bin", sync_json=OK)
        r = _run(root, tmp_path / "bin")
        assert r.returncode == 0, r.stderr
        log = (root / "state" / "vault-sync.log").read_text()
        assert log.count("vault:shared-vault: ok=") == 1, log

    def test_no_vault_anywhere_says_so_rather_than_passing_silently(self, tmp_path):
        """A silent zero reads exactly like a broken walk."""
        root = _root(tmp_path, {})
        _claudron_stub(tmp_path / "bin", sync_json=OK)
        r = _run(root, tmp_path / "bin")
        assert r.returncode == 0, r.stderr
        assert "no vault discovered" in (root / "state" / "vault-sync.log").read_text()

    def test_no_claudron_on_PATH_is_a_noop_not_a_failure(self, tmp_path):
        root = _root(tmp_path, {"worker": str(tmp_path / "v")})
        empty = tmp_path / "emptybin"
        empty.mkdir()
        e = dict(os.environ)
        # /usr/bin:/bin so `bash` itself still resolves — emptying PATH
        # entirely tests nothing but the harness. claudron is not in either.
        e.update({"CLAUDLOBBY_ROOT": str(root),
                  "PATH": f"{empty}:/usr/bin:/bin",
                  "PLANE_EMIT_DISABLED": "1"})
        r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                           env=e, timeout=120)
        assert r.returncode == 0
        assert "not on PATH" in (root / "state" / "vault-sync.log").read_text()


class TestTheEnvelopeIsParsedNeverGrepped:
    def test_a_refusal_is_recorded_as_not_ok(self, tmp_path):
        root = _root(tmp_path, {"w": str(tmp_path / "v")})
        _claudron_stub(tmp_path / "bin", sync_json=REFUSED, sync_rc=1)
        r = _run(root, tmp_path / "bin")
        assert r.returncode == 0, r.stderr
        log = (root / "state" / "vault-sync.log").read_text()
        assert "ok=0" in log and "rebase is stopped part-way" in log

    def test_an_engine_without_check_records_unknown_and_STILL_SYNCS(self, tmp_path):
        """rc 2 is argparse's usage error — an older engine, not a failing
        vault. Recording a state this job invented would be the very
        success-shaped output the program exists to end."""
        root = _root(tmp_path, {"w": str(tmp_path / "v")})
        _claudron_stub(tmp_path / "bin", sync_json=OK, check_rc=2)
        r = _run(root, tmp_path / "bin")
        assert r.returncode == 0, r.stderr
        log = (root / "state" / "vault-sync.log").read_text()
        assert "no 'sync --check'" in log
        assert "state=unknown" in log
        assert "ok=1" in log, "the sync must still run — the verdict is a report, not a gate"

    def test_a_check_verdict_is_recorded_when_the_engine_has_one(self, tmp_path):
        root = _root(tmp_path, {"w": str(tmp_path / "v")})
        _claudron_stub(tmp_path / "bin", sync_json=OK,
                       check_json=json.dumps({"ok": True, "data": {"state": "clean"}}))
        r = _run(root, tmp_path / "bin")
        assert "state=clean" in (root / "state" / "vault-sync.log").read_text()

    def test_no_envelope_at_all_is_not_ok(self, tmp_path):
        """Absence of output is not success — the #142 shape."""
        root = _root(tmp_path, {"w": str(tmp_path / "v")})
        _claudron_stub(tmp_path / "bin", sync_json="", sync_rc=1)
        r = _run(root, tmp_path / "bin")
        log = (root / "state" / "vault-sync.log").read_text()
        assert "ok=0" in log and "no envelope" in log


class TestPagingIsDebouncedOnSTATECHANGE:
    """A wedged vault is wedged until somebody fixes it — it is not a burst.
    Paging every 15 minutes about a still-true condition is how an operator
    learns to mute the channel, and then the one that matters is muted too."""

    def _marker(self, root):
        d = root / "state" / "vault-sync"
        return sorted(d.glob("*.last")) if d.is_dir() else []

    def test_first_failure_arms_and_a_second_identical_one_does_not(self, tmp_path):
        root = _root(tmp_path, {"w": str(tmp_path / "v")})
        _claudron_stub(tmp_path / "bin", sync_json=REFUSED, sync_rc=1)
        _run(root, tmp_path / "bin")
        marks = self._marker(root)
        assert marks, "the first failure must record a state"
        first = marks[0].read_text()
        assert first.startswith("bad:")
        _run(root, tmp_path / "bin")
        assert marks[0].read_text() == first, "an unchanged state must not re-arm"

    def test_recovery_flips_the_marker_back(self, tmp_path):
        root = _root(tmp_path, {"w": str(tmp_path / "v")})
        _claudron_stub(tmp_path / "bin", sync_json=REFUSED, sync_rc=1)
        _run(root, tmp_path / "bin")
        assert self._marker(root)[0].read_text().startswith("bad:")
        _claudron_stub(tmp_path / "bin", sync_json=OK)
        _run(root, tmp_path / "bin")
        assert self._marker(root)[0].read_text() == "ok"

    def test_a_clean_run_from_scratch_records_ok_and_pages_nothing(self, tmp_path):
        """The positive control for the debounce: a healthy vault must not
        produce a recovery notice it never needed."""
        root = _root(tmp_path, {"w": str(tmp_path / "v")})
        _claudron_stub(tmp_path / "bin", sync_json=OK)
        r = _run(root, tmp_path / "bin")
        assert r.returncode == 0
        assert self._marker(root)[0].read_text() == "ok"


class TestItShipsDormant:
    def test_the_host_job_is_enroll_false(self):
        import yaml

        d = yaml.safe_load((REPO / "claudlobby" / "system.yaml").read_text())
        job = d["host"]["jobs"]["vault-sync"]
        assert job["enroll"] is False, (
            "armed, this job commits and pushes on every host it runs on")
        assert job["script"].endswith("lib/vault-sync.sh")

    def test_the_switch_row_exists_so_doctor_can_SEE_the_off_door(self):
        """Without the row the switches rung cannot list it, and a door nobody
        can see is a door nobody has."""
        from claudlobby import switches as sw

        row = next(s for s in sw.SWITCHES if s.key == "vault-sync")
        assert row.polarity == sw.OPT_IN and row.job == "vault-sync"
        assert row.why_opt_in, "an opt-in must NAME what keeps it off"


# --- the env scrub is a BEHAVIOUR, so it gets a pin that can fail -------------
# The contaminants are set BY THE TEST, never read from the ambient environment.
# A test asserting "FLEET_NAME is absent" against a host that never exported it
# passes with the scrub deleted -- it would be green on every machine a developer
# used and blind in CI, which is #1169's exact shape. Setting them here is what
# makes the assertion capable of failing.
def test_the_subprocess_env_drops_ambient_bot_identity(monkeypatch, tmp_path):
    for var in _BOT_IDENTITY_VARS:
        monkeypatch.setenv(var, f"contaminant-{var.lower()}")

    e = _scrubbed_env(tmp_path / "root", tmp_path / "bin")

    leaked = {v: e[v] for v in _BOT_IDENTITY_VARS if v in e}
    assert not leaked, (
        f"a host job inherited the calling session's identity: {leaked}. "
        "resolve_bots_dir falls back through CLAUDLOBBY_FLEET/FLEET_NAME, so this "
        "run would exercise the caller's fleet instead of the fleet-less shape."
    )


def test_a_deliberate_override_still_wins_over_the_scrub(monkeypatch, tmp_path):
    # The scrub must remove AMBIENT identity without disarming a test that sets one
    # on purpose -- the routing tests declare CLAUDLOBBY_ALERT_MANAGER, and a scrub
    # applied after **env would silently drop it and quietly stop testing routing.
    monkeypatch.setenv("CLAUDLOBBY_ALERT_MANAGER", "ambient-should-lose")

    e = _scrubbed_env(tmp_path / "root", tmp_path / "bin",
                      CLAUDLOBBY_ALERT_MANAGER="declared-should-win")

    assert e["CLAUDLOBBY_ALERT_MANAGER"] == "declared-should-win"
