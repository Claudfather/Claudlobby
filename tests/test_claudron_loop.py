"""Tests for the Claudron session loop (boundary phase L2).

Covers, per the plan's six steps:
  - config coercion of the tri-state ``claudron_session_loop`` (step 1);
  - the composer's per-vault-wired-bot hook merge + narrow verb grants
    (steps 2 + 4), including the F1-structural "no claim env" property (step 3);
  - the validator's loop-without-vault error (step 5);
  - the snippet-parity drift gate against the pinned engine (R3 / the L4 gate,
    runnable here) — skipped when the ``[vault]`` extra is not installed;
  - the N-bot SessionEnd contention behavior — skipped without claudron + git.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claudlobby.composer import (
    CLAUDRON_LOOP_GRANTS,
    _claudron_hook_entries,
    _merge_claudron_hooks,
    _resolve_claudron_executable,
    _session_loop_enabled,
    compose_bot_conf,
    compose_settings_local,
)
from claudlobby.config import BotConfig, FleetConfig, _coerce_bot
from claudlobby.paths import Paths
from claudlobby.validator import validate

# The literal wildcard the phase exists to keep out of every composed file.
WILDCARD_GRANT = "Bash(claudron *)"
# The human-gated curation verbs the wildcard would grant (boundary spec §8).
CURATION_VERBS = ["promote", "plug", "unplug", "config", "migrate", "review", "init"]


@pytest.fixture(autouse=True)
def _clear_claudron_exe_cache():
    """`_resolve_claudron_executable` is `@functools.cache`d (the PATH location
    is host-invariant, so the per-bot compose loop resolves it once). Clear it
    between tests so a prior test's cached result never shadows another test's
    monkeypatched PATH."""
    _resolve_claudron_executable.cache_clear()
    yield


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _paths(tmp_path: Path) -> Paths:
    root = tmp_path / "claudlobby"
    (root / "runtime" / "bots").mkdir(parents=True, exist_ok=True)
    (root / "lib").mkdir(exist_ok=True)
    return Paths(root=root, fleet_dir=root)


def _fleet(bot: BotConfig) -> FleetConfig:
    return FleetConfig(name="t", service_prefix="p", bots={bot.bot_id: bot})


def _compose(tmp_path: Path, **bot_kw) -> tuple[dict, BotConfig]:
    bot = BotConfig(bot_id="b1", name="b1", expertise=["eng"], **bot_kw)
    return compose_settings_local(bot, _fleet(bot), _paths(tmp_path)), bot


def _hook_commands(settings: dict) -> list[str]:
    return [
        h["command"]
        for groups in settings.get("hooks", {}).values()
        for g in groups
        for h in g.get("hooks", [])
    ]


def _allow(settings: dict) -> list[str]:
    return settings.get("permissions", {}).get("allow", [])


# ---------------------------------------------------------------------------
# Step 1 — the tri-state fleet.yaml surface
# ---------------------------------------------------------------------------


class TestSessionLoopConfig:
    def test_unset_is_none(self):
        bot = _coerce_bot("b", {"expertise": ["eng"]}, {})
        assert bot.claudron_session_loop is None

    def test_explicit_true_on_bot(self):
        bot = _coerce_bot("b", {"expertise": ["eng"], "claudron_session_loop": True}, {})
        assert bot.claudron_session_loop is True

    def test_explicit_false_on_bot(self):
        bot = _coerce_bot(
            "b", {"expertise": ["eng"], "claudron_session_loop": False}, {}
        )
        assert bot.claudron_session_loop is False

    def test_from_fleet_defaults(self):
        bot = _coerce_bot("b", {"expertise": ["eng"]}, {"claudron_session_loop": True})
        assert bot.claudron_session_loop is True

    def test_bot_overrides_fleet_default(self):
        bot = _coerce_bot(
            "b",
            {"expertise": ["eng"], "claudron_session_loop": False},
            {"claudron_session_loop": True},
        )
        assert bot.claudron_session_loop is False


class TestSessionLoopEnabled:
    """The default rule: unset ⇒ on iff vault-wired; explicit wins."""

    def test_unset_defaults_on_when_vault_wired(self):
        bot = BotConfig(
            bot_id="b", name="b", expertise=["eng"], claudron_vault_path="/srv/v"
        )
        assert _session_loop_enabled(bot) is True

    def test_unset_off_without_vault(self):
        bot = BotConfig(bot_id="b", name="b", expertise=["eng"])
        assert _session_loop_enabled(bot) is False

    def test_explicit_false_wins_over_vault(self):
        bot = BotConfig(
            bot_id="b",
            name="b",
            expertise=["eng"],
            claudron_vault_path="/srv/v",
            claudron_session_loop=False,
        )
        assert _session_loop_enabled(bot) is False

    def test_explicit_true_without_vault_still_enables(self):
        # The composer resolver enables it; the validator (below) is what errors
        # on this misconfiguration — the layers are independent by design.
        bot = BotConfig(
            bot_id="b", name="b", expertise=["eng"], claudron_session_loop=True
        )
        assert _session_loop_enabled(bot) is True


# ---------------------------------------------------------------------------
# Step 2 — composer installs the three engine hook entries
# ---------------------------------------------------------------------------


class TestHookComposition:
    def test_vault_wired_installs_three_hooks(self, tmp_path):
        settings, _ = _compose(tmp_path, claudron_vault_path="/srv/v")
        assert set(settings["hooks"]) >= {"SessionStart", "PreCompact", "SessionEnd"}
        for event in ("SessionStart", "PreCompact", "SessionEnd"):
            groups = settings["hooks"][event]
            claudron = [g for g in groups if any("hook " in h["command"] for h in g["hooks"])]
            assert len(claudron) == 1, event
            assert claudron[0]["matcher"] == ""

    def test_command_form_is_hook_event(self, tmp_path):
        settings, _ = _compose(tmp_path, claudron_vault_path="/srv/v")
        cmds = _hook_commands(settings)
        assert any(c.endswith("hook session-start") for c in cmds)
        assert any(c.endswith("hook pre-compact") for c in cmds)
        assert any(c.endswith("hook session-end") for c in cmds)

    def test_loop_off_installs_no_hooks(self, tmp_path):
        settings, _ = _compose(
            tmp_path, claudron_vault_path="/srv/v", claudron_session_loop=False
        )
        assert not any(
            k in settings.get("hooks", {})
            for k in ("SessionStart", "PreCompact", "SessionEnd")
        )

    def test_no_vault_no_hooks(self, tmp_path):
        settings, _ = _compose(tmp_path)
        assert "hooks" not in settings

    def test_foreign_hook_preserved(self, tmp_path):
        # A fleet's own SessionStart hook must survive the merge beside claudron's.
        settings, _ = _compose(
            tmp_path,
            claudron_vault_path="/srv/v",
            hooks={"SessionStart": [{"command": "/opt/fleet/vitals.sh"}]},
        )
        cmds = [h["command"] for g in settings["hooks"]["SessionStart"] for h in g["hooks"]]
        assert "/opt/fleet/vitals.sh" in cmds
        assert any(c.endswith("hook session-start") for c in cmds)

    def test_recompose_is_idempotent(self, tmp_path):
        bot = BotConfig(
            bot_id="b1", name="b1", expertise=["eng"], claudron_vault_path="/srv/v"
        )
        fleet, paths = _fleet(bot), _paths(tmp_path)
        first = compose_settings_local(bot, fleet, paths)
        second = compose_settings_local(bot, fleet, paths)
        assert first["hooks"] == second["hooks"]
        # exactly one claudron entry per event, never a duplicate
        for event in ("SessionStart", "PreCompact", "SessionEnd"):
            claudron = [
                g
                for g in second["hooks"][event]
                if any("hook " in h["command"] for h in g["hooks"])
            ]
            assert len(claudron) == 1

    def test_merge_self_replaces_stale_executable(self):
        # A moved venv/pipx path must REPLACE, not duplicate (suffix identity).
        old = _merge_claudron_hooks({}, "/old/venv/bin/claudron")
        new = _merge_claudron_hooks(old, "/new/venv/bin/claudron")
        for event in ("SessionStart", "PreCompact", "SessionEnd"):
            assert len(new[event]) == 1
            assert new[event][0]["hooks"][0]["command"].startswith("/new/venv/bin/claudron")

    def test_executable_absolute_when_on_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: "/abs/bin/claudron")
        exe, warning = _resolve_claudron_executable()
        assert exe == "/abs/bin/claudron"
        assert warning is None
        settings, _ = _compose(tmp_path, claudron_vault_path="/srv/v")
        assert all(
            c.startswith("/abs/bin/claudron") for c in _hook_commands(settings)
        )

    def test_executable_fallback_warns(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda name: None)
        exe, warning = _resolve_claudron_executable()
        assert exe == "claudron"
        assert warning is not None and "PATH" in warning


# ---------------------------------------------------------------------------
# Step 3 — F1 is STRUCTURAL: no claim env is composed anywhere
# ---------------------------------------------------------------------------


class TestNoClaimEnv:
    def test_no_claim_env_in_settings(self, tmp_path):
        settings, _ = _compose(tmp_path, claudron_vault_path="/srv/v")
        # Scan only what the loop composes (hooks + grants), not the tmp memory
        # path — F1 is structural, so no capture-claim token is emitted.
        added = json.dumps(
            {"hooks": settings.get("hooks"), "allow": _allow(settings)}
        ).lower()
        assert "capture_owner" not in added
        assert "capture-owner" not in added
        assert "claim" not in added

    def test_no_claim_env_in_bot_conf(self, tmp_path):
        bot = BotConfig(
            bot_id="b1", name="b1", expertise=["eng"], claudron_vault_path="/srv/v"
        )
        conf = compose_bot_conf(bot, _fleet(bot), _paths(tmp_path))
        # The vault address is composed; a capture-claim env is NOT (F1 structural).
        assert "CLAUDRON_VAULT_PATH=" in conf
        assert "CLAUDRON_CAPTURE_OWNER" not in conf
        assert "CAPTURE_OWNER" not in conf


# ---------------------------------------------------------------------------
# Step 4 — narrow verb grants, never the wildcard
# ---------------------------------------------------------------------------


class TestVerbGrants:
    def test_narrow_grants_present_when_vault_wired(self, tmp_path):
        settings, _ = _compose(tmp_path, claudron_vault_path="/srv/v")
        for grant in CLAUDRON_LOOP_GRANTS:
            assert grant in _allow(settings)

    def test_grants_are_exactly_the_four_read_write_verbs(self):
        assert CLAUDRON_LOOP_GRANTS == [
            "Bash(claudron lookup *)",
            "Bash(claudron recall *)",
            "Bash(claudron capture *)",
            "Bash(claudron status *)",
        ]

    def test_no_wildcard_grant(self, tmp_path):
        settings, _ = _compose(tmp_path, claudron_vault_path="/srv/v")
        assert WILDCARD_GRANT not in _allow(settings)

    def test_no_curation_verb_grantable(self, tmp_path):
        settings, _ = _compose(tmp_path, claudron_vault_path="/srv/v")
        allow = _allow(settings)
        assert WILDCARD_GRANT not in allow
        for verb in CURATION_VERBS:
            assert f"Bash(claudron {verb} *)" not in allow
            # and nothing broader that would cover the verb
            assert not any(p == "Bash(claudron*)" or p == "Bash" for p in allow)

    def test_grants_absent_when_loop_off(self, tmp_path):
        settings, _ = _compose(
            tmp_path, claudron_vault_path="/srv/v", claudron_session_loop=False
        )
        assert not any("claudron" in p for p in _allow(settings))

    def test_wildcard_absent_from_serialized_file(self, tmp_path):
        # The plan's literal assertion: Bash(claudron *) absent from the composed
        # settings.local.json, across loop-on / loop-off / no-vault.
        for kw in (
            {"claudron_vault_path": "/srv/v"},
            {"claudron_vault_path": "/srv/v", "claudron_session_loop": False},
            {},
        ):
            settings, _ = _compose(tmp_path, **kw)
            assert WILDCARD_GRANT not in json.dumps(settings)


# ---------------------------------------------------------------------------
# Step 5 — validator: loop enabled + no vault ⇒ error
# ---------------------------------------------------------------------------


class TestValidator:
    def _loop_errors(self, tmp_path: Path, bot: BotConfig) -> list[str]:
        report = validate(_fleet(bot), _paths(tmp_path))
        return [e for e in report.errors if "session loop has no vault" in e]

    def test_explicit_true_without_vault_errors(self, tmp_path):
        bot = BotConfig(
            bot_id="b1", name="b1", expertise=["eng"], claudron_session_loop=True
        )
        assert self._loop_errors(tmp_path, bot)

    def test_default_unset_without_vault_no_error(self, tmp_path):
        bot = BotConfig(bot_id="b1", name="b1", expertise=["eng"])
        assert not self._loop_errors(tmp_path, bot)

    def test_explicit_false_without_vault_no_error(self, tmp_path):
        bot = BotConfig(
            bot_id="b1", name="b1", expertise=["eng"], claudron_session_loop=False
        )
        assert not self._loop_errors(tmp_path, bot)

    def test_true_with_vault_no_loop_error(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "_shared").mkdir(parents=True)
        bot = BotConfig(
            bot_id="b1",
            name="b1",
            expertise=["eng"],
            claudron_vault_path=str(vault),
            claudron_session_loop=True,
        )
        assert not self._loop_errors(tmp_path, bot)


# ---------------------------------------------------------------------------
# The R3 drift gate — composed entries == the pinned engine's snippet.
# (Skipped when the [vault] extra is not installed; runs in the vault-mode CI.)
# ---------------------------------------------------------------------------


class TestSnippetParity:
    EXE = "/opt/claudron/bin/claudron"

    def test_entries_match_engine_settings_snippet(self):
        hooks = pytest.importorskip("claudron.hooks")
        assert _claudron_hook_entries(self.EXE) == hooks.settings_snippet(self.EXE)["hooks"]

    def test_merge_matches_engine_merge_settings(self):
        hooks = pytest.importorskip("claudron.hooks")
        composed = _merge_claudron_hooks({}, self.EXE)
        engine = hooks.merge_settings({}, hooks.settings_snippet(self.EXE))["hooks"]
        assert composed == engine

    def test_merge_self_replace_matches_engine(self):
        """The self-replacing branch — a stale claudron entry for an event dropped
        and re-installed while a foreign entry survives — is where the rendered
        copy is most likely to drift from the engine, and is exactly what R3
        guards. The empty-base case above cannot exercise it."""
        hooks = pytest.importorskip("claudron.hooks")
        old_exe, new_exe = "/old/venv/bin/claudron", "/new/venv/bin/claudron"
        foreign = {"matcher": "", "hooks": [{"type": "command", "command": "fleet-own.sh"}]}

        def base():  # a hooks block: stale claudron entries (old exe) + a foreign hook
            b = _merge_claudron_hooks({}, old_exe)
            event = next(iter(b))
            b[event] = b[event] + [foreign]
            return b, event

        b_composer, event = base()
        b_engine, _ = base()
        composed = _merge_claudron_hooks(b_composer, new_exe)
        engine = hooks.merge_settings(
            {"hooks": b_engine}, hooks.settings_snippet(new_exe)
        )["hooks"]
        assert composed == engine
        # sanity: foreign hook survived; the stale executable is fully gone
        assert foreign in composed[event]
        assert not any(old_exe in json.dumps(v) for v in composed.values())


# ---------------------------------------------------------------------------
# N-bot SessionEnd contention (≥8 bots, one host, one vault).
# Skipped without claudron + git; runs in the vault-mode CI / a local vault venv.
# ---------------------------------------------------------------------------

N_BOTS = 8
_HAS_CLAUDRON = importlib.util.find_spec("claudron") is not None
_HAS_GIT = shutil.which("git") is not None


def _claudron_hook_argv(event: str) -> list[str]:
    """The composed hook's dispatch, resolved robustly for the test host: the
    console script when present, else ``python -m claudron.cli`` (same engine
    entrypoint the composed ``<exe> hook <event>`` command invokes)."""
    exe = shutil.which("claudron")
    if exe:
        return [exe, "hook", event]
    return [sys.executable, "-m", "claudron.cli", "hook", event]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _engine_version() -> str:
    try:
        import claudron

        return getattr(claudron, "__version__", "?")
    except Exception:
        return "?"


def _engine_has_writelock() -> bool:
    # The vault flock (locking.py) is a post-0.2.0 property; its presence is what
    # turns contention into clean serialization. Recorded, never asserted.
    return importlib.util.find_spec("claudron.locking") is not None


@pytest.mark.skipif(
    not (_HAS_CLAUDRON and _HAS_GIT),
    reason="N-bot contention needs the [vault] extra (claudron) + git",
)
class TestSessionEndContention:
    def _make_vault(self, tmp_path: Path) -> tuple[Path, Path]:
        """A git-backed vault (clone of a bare remote) with N staged notes —
        one per bot, simulating N sessions' captures awaiting a SessionEnd push."""
        remote = tmp_path / "remote.git"
        _git(tmp_path, "init", "--bare", str(remote))
        vault = tmp_path / "vault"
        _git(tmp_path, "clone", str(remote), str(vault))
        _git(vault, "config", "user.email", "fleet@test")
        _git(vault, "config", "user.name", "fleet")
        (vault / "_shared").mkdir()
        (vault / "_shared" / "CONVENTIONS.md").write_text("# conv\n")
        _git(vault, "add", "-A")
        _git(vault, "commit", "-m", "init vault")
        _git(vault, "push", "origin", "HEAD")
        notes = vault / "_shared" / "knowledge"
        notes.mkdir()
        for i in range(N_BOTS):
            (notes / f"note-{i}.md").write_text(
                f"---\ntype: knowledge\ntitle: note {i}\n---\nbody {i}\n"
            )
        return vault, remote

    def _notes_on_remote(self, remote: Path) -> int:
        out = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=remote, capture_output=True, text=True, check=True,
        ).stdout
        return sum(1 for i in range(N_BOTS) if f"note-{i}.md" in out)

    def test_concurrent_session_end_fail_open_and_eventually_consistent(self, tmp_path):
        vault, remote = self._make_vault(tmp_path)
        end_argv = _claudron_hook_argv("session-end")
        start_argv = _claudron_hook_argv("session-start")
        # Resolve the vault via CWD walk-up (contract row 3), which every engine
        # version honors — deliberately NOT via CLAUDRON_VAULT_PATH: the pinned
        # v0.2.0 reads the old CLAUDRON_VAULT spelling, and this test validates
        # concurrent-sync fail-open, not the env-address contract (which has its
        # own tests). run() below sets cwd=vault for the same reason.
        env = dict(os.environ)

        # Fire N SessionEnd hooks at once (started back-to-back for max overlap).
        t0 = time.monotonic()
        procs = [
            subprocess.Popen(
                end_argv, env=env, cwd=str(vault), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for _ in range(N_BOTS)
        ]
        rcs, durations = [], []
        for p in procs:
            start = time.monotonic()
            try:
                p.communicate(input="{}", timeout=60)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
            rcs.append(p.returncode)
            durations.append(time.monotonic() - start)
        burst_s = time.monotonic() - t0

        # (1) THE load-bearing invariant, holds at EVERY engine version: fail-open
        #     under contention — no SessionEnd hook exits nonzero. A concurrent
        #     push that loses the race degrades to a no-op, never a broken session.
        assert all(rc == 0 for rc in rcs), f"a hook exited nonzero: {rcs}"

        # How much of the burst actually landed on the remote (0..N). On an engine
        # with the vault write-lock this tends to N; on the lock-less pinned v0.2.0
        # it varies with the race — the push-loss the risk table names.
        burst_landed = self._notes_on_remote(remote)

        # (2) No work is ever DESTROYED — every note file survives on disk (a raced
        #     push abandons the *push*, not the capture).
        for i in range(N_BOTS):
            assert (vault / "_shared" / "knowledge" / f"note-{i}.md").is_file()

        # (3) Eventual consistency — "unpushed work travels the next session": a
        #     bounded reconcile (SessionStart pull+rebase, then SessionEnd push)
        #     converges the remote to all N notes. This is the recovery the loss
        #     accounting is measured against; it is deterministic (adds never
        #     conflict) and version-independent.
        cycles = 0
        landed = burst_landed
        for _ in range(4):
            if landed == N_BOTS:
                break
            subprocess.run(start_argv, env=env, cwd=str(vault), input="{}", capture_output=True, text=True, timeout=60)
            subprocess.run(end_argv, env=env, cwd=str(vault), input="{}", capture_output=True, text=True, timeout=60)
            landed = self._notes_on_remote(remote)
            cycles += 1
        assert landed == N_BOTS, f"only {landed}/{N_BOTS} notes reached the remote after reconcile"

        # (4) No object corruption from the concurrent writers.
        _git(vault, "fsck")

        within = sum(1 for d in durations if d <= 10.0)
        print(
            f"\n[N-bot contention] engine={_engine_version()} lock={_engine_has_writelock()} "
            f"bots={N_BOTS} burst={burst_s:.2f}s "
            f"fail_open={sum(1 for r in rcs if r == 0)}/{N_BOTS} "
            f"within_10s_budget={within}/{N_BOTS} "
            f"burst_landed={burst_landed}/{N_BOTS} reconcile_cycles={cycles} "
            f"final={landed}/{N_BOTS}"
        )
