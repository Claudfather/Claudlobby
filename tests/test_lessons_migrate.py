"""L3 — lessons-migrate: classification drift gate, capture plan, apply branching."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby.commands import lessons_migrate as lm

REPO_LESSONS = Path(__file__).resolve().parent.parent / "library" / "lessons"


def _args(root: Path, **over):
    base = dict(
        root=str(root), fleet=None, seed=False,
        apply=False, vault=None, fleet_scope=None, claudron_bin=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── classification is the freeze's drift gate ────────────────────────────


def test_every_lesson_is_classified():
    # No note escapes a verdict — the freeze depends on this.
    assert lm._classify(REPO_LESSONS) == []


def test_referential_and_behavior_partition_all_files():
    referential = set(lm.REFERENTIAL_LESSONS)
    behavior = set(lm.BEHAVIOR_LESSONS)
    assert referential.isdisjoint(behavior)
    assert referential | behavior == set(lm._all_lesson_files(REPO_LESSONS))


def test_verdict_counts():
    assert len(lm.REFERENTIAL_LESSONS) == 22
    assert len(lm.BEHAVIOR_LESSONS) == 3


def test_canonical_behavior_lessons_not_migrated():
    # The plan's named behavior example, plus the two already-homed rules.
    for src in (
        "messaging-channel-discipline.md",
        "tmux-dispatch-shell-expansion.md",
        "orchestration/consensus-before-escalation.md",
    ):
        assert src in lm.BEHAVIOR_LESSONS
        assert src not in lm.REFERENTIAL_LESSONS


# ── the capture plan ─────────────────────────────────────────────────────


def test_plan_is_one_strict_valid_mapping_per_referential_note():
    plan = lm.build_capture_plan(REPO_LESSONS)
    assert len(plan) == 22
    sources = {m.source for m in plan}
    assert sources == set(lm.REFERENTIAL_LESSONS)
    for m in plan:
        assert m.strict_valid, (m.source, m.problems)
        assert m.title and m.body
        assert "lesson" in m.tags
        assert m.stdin_payload()["type"] == "knowledge"


def test_behavior_lessons_absent_from_plan():
    sources = {m.source for m in lm.build_capture_plan(REPO_LESSONS)}
    assert sources.isdisjoint(lm.BEHAVIOR_LESSONS)


def test_tags_carry_topic_and_curated_extras():
    by_src = {m.source: m for m in lm.build_capture_plan(REPO_LESSONS)}
    assert by_src["dbt/parse-vs-execute-time.md"].tags[:2] == ["lesson", "dbt"]
    assert set(by_src["migration/tmux-server-env-inheritance.md"].tags) >= {
        "lesson", "migration", "tmux", "env",
    }
    assert set(by_src["private-repo-screenshots.md"].tags) >= {
        "lesson", "github", "screenshots",
    }


def test_default_tier_is_shared_and_fleet_scoping_opt_in():
    assert all(m.fleet is None for m in lm.build_capture_plan(REPO_LESSONS))
    scoped = lm.build_capture_plan(REPO_LESSONS, fleet="acme")
    assert all(m.fleet == "acme" for m in scoped)


# ── capture invocation safety (the contract) ─────────────────────────────


def test_capture_argv_shape_and_never_force():
    m = lm.build_capture_plan(REPO_LESSONS)[0]
    argv = m.capture_argv("claudron")
    assert argv[:3] == ["claudron", "capture", "--type"]
    assert "knowledge" in argv and "--stdin" in argv and "--json" in argv
    assert "--force" not in argv  # dedup routes; a human resolves suggest_*
    assert "--fleet" not in argv  # default _shared/


def test_capture_argv_fleet_flag_when_scoped():
    m = lm.build_capture_plan(REPO_LESSONS, fleet="acme")[0]
    assert lm.CaptureMapping.capture_argv(m, "claudron")[-2:] == ["--fleet", "acme"]


# ── dry-run needs no claudron ────────────────────────────────────────────


def test_dry_run_returns_zero(tmp_path):
    assert lm.cmd_lessons_migrate(_args(_repo_root())) == 0


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


# ── the freeze catches an unclassified new note ──────────────────────────


def test_unclassified_note_is_rejected(tmp_path):
    fake_root = tmp_path / "claudlobby"
    (fake_root / "library").mkdir(parents=True)
    shutil.copytree(REPO_LESSONS, fake_root / "library" / "lessons")
    (fake_root / "library" / "lessons" / "brand-new-lesson.md").write_text(
        "---\ntitle: Brand new\n---\n\nbody\n"
    )
    assert lm._classify(fake_root / "library" / "lessons") == ["brand-new-lesson.md"]
    assert lm.cmd_lessons_migrate(_args(fake_root)) == 1


# ── --apply branches on data.action (never the exit code) ────────────────


class _FakeProc:
    def __init__(self, stdout, stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _fake_run_factory(action, captured):
    import json

    def _run(argv, input=None, capture_output=None, text=None, env=None):
        captured.append(SimpleNamespace(argv=argv, input=input, env=env))
        return _FakeProc(json.dumps(
            {"ok": action != "rejected",
             "data": {"action": action, "path": "_shared/x.md",
                      "reason": "r", "written": action in ("created", "updated")}}
        ))
    return _run


def test_apply_created_returns_zero_and_passes_vault_env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    captured: list = []
    monkeypatch.setattr(lm.subprocess, "run", _fake_run_factory("created", captured))
    rc = lm.cmd_lessons_migrate(_args(_repo_root(), apply=True, vault=str(vault)))
    assert rc == 0
    assert len(captured) == 22
    # Every invocation is the write door with the vault addressed, never --force.
    for c in captured:
        assert c.env["CLAUDRON_VAULT_PATH"] == str(vault)
        assert "--force" not in c.argv


def test_apply_suggest_is_idempotent_success(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    captured: list = []
    monkeypatch.setattr(
        lm.subprocess, "run", _fake_run_factory("suggest_update", captured)
    )
    # A re-run dedup-routes every note to suggest_update — still exit 0.
    assert lm.cmd_lessons_migrate(_args(_repo_root(), apply=True, vault=str(vault))) == 0


def test_apply_rejected_returns_nonzero(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    captured: list = []
    monkeypatch.setattr(lm.subprocess, "run", _fake_run_factory("rejected", captured))
    assert lm.cmd_lessons_migrate(_args(_repo_root(), apply=True, vault=str(vault))) == 1


def test_apply_without_vault_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDRON_VAULT_PATH", raising=False)
    assert lm.cmd_lessons_migrate(_args(_repo_root(), apply=True)) == 2
